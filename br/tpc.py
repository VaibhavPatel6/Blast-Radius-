"""Decide the TPC executor tests (test_execute_tpcds, 21.5 s; test_execute_tpch, 3.9 s)
without executing them. Entry point: `decide`.

What the real test does (tests/test_executor.py `_mp_execute`): its process imports sqlglot,
the executor and the planner, parses all queries in order and pickles each tree to a
spawned worker. A worker (a fresh import of the same modules) unpickles the tree and, for
queries marked `# execute: true`, optimizes it, renders it in pretty mode (a debug-log
argument, always evaluated), plans and executes it. The parent then transpiles each
executed query to DuckDB, in order, and compares DuckDB's rows with the worker's.

When a patch changes only function code, none of it run during the execution part
(maps/tpc_exec.json, recorded on the pinned commit by tools/covmap/tpcds_phases.py), the
outcome is decided by three things, all compared here between the pinned tree and the
patched tree in two processes forked from the selector:

1. The state the imports leave behind: every attribute of every class and every module
   global in every loaded sqlglot module (every sqlglot module is imported), digested
   structurally as in br/state.py, but with functions digested by file, name, defaults
   and closure and their code compared apart, so that exactly the changed functions' code
   is left to 2 and 3. Taken after import, which is the state the spawned workers start
   from (they import afresh, then only unpickle and run). Equal state means the changed function left nothing different behind at import, whatever
   ran it there (metaclasses, templates the DuckDB generator parses while its class is
   created).
2. What every query's preparation produces: the pickle bytes the worker receives, a
   structural digest of the unpickled tree, and for executed queries the optimized tree
   (structure with types, plain and pretty SQL) and the DuckDB SQL. The digests read the
   trees' slots directly, so they do not depend on sqlglot methods the patch may change.
3. Which queries reach the changed code while being prepared (maps/tpc_queries.json,
   each query recorded in its own process). On the patched side every query is parsed
   and pickled in order and every executed query is transpiled in order, as in the real
   test; only the optimize, pretty and type steps are skipped for executed queries that
   never reach the changed code. If no query reaches it and no map says it runs at
   import, nothing explains why the test is a candidate, and it stays listed.

If 1 and 2 are equal the workers receive identical inputs, read identical state and run
unchanged code, so the test behaves as on the pinned commit, where it passes.

The pinned side is computed once per run per benchmark, after pytest is imported (sqlglot
checks for a test framework at import). It cannot be made at image build: the parse of
TPC-DS query 48 depends on what earlier parses left in the process and on memory-address
order, so only two processes forked from one parent are guaranteed to agree. Any error,
timeout or difference keeps the test listed.
"""

import enum
import hashlib
import json
import os
import pickle
import resource
import sys
import types
from pathlib import Path

TPC_TESTS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "tpc-ds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch": "tpc-h",
}
# Files whose change rules the comparison out: the tests and their helpers and fixtures,
# the executor and the planner (they run during execution).
REFUSE_PREFIXES = ("tests/", "sqlglot/executor/", "sqlglot/planner.py")
# Module globals that name the tree on disk or the import machinery, not program state.
_SKIPPED_GLOBALS = {"__file__", "__cached__", "__spec__", "__loader__", "__path__", "__builtins__"}

_APP = Path(__file__).resolve().parent.parent
_PINNED: dict[str, dict] = {}
_MAPS: dict[str, object] = {}


def _map(name: str):
    if name not in _MAPS:
        data = json.loads((_APP / "maps" / name).read_text())
        if name == "tpc_exec.json":
            data = {(f[len("/src/"):], q, n) for f, q, n in data}
        elif name == "import_time.json":
            data = {tuple(x) for x in data}
        _MAPS[name] = data
    return _MAPS[name]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


# ---------------------------------------------------------------- child side


def _is_changed(key: str, changed: frozenset) -> bool:
    """Whether the code of function `key` ("path|qualname") may differ only because the
    patch changed it: it is a changed code object, nested in one, or encloses one (a
    function's digest covers its nested code; br.analyze lists every code object whose
    own code differs, so an enclosing function not listed has its own code unchanged)."""
    path, _, qual = key.partition("|")
    return any(path == p and (qual == q or qual.startswith(q + ".<locals>.") or q.startswith(qual + ".<locals>."))
               for p, q in changed)


def _state() -> tuple[dict[str, str], dict[str, list[str]]]:
    """Digests of every class and every module's globals in every loaded sqlglot module,
    as in br/state.py except that functions are digested by file, name, defaults and
    closure; their code is returned apart, {"path|qualname": [code digests]}, so that the
    parent can ignore exactly the changed functions' code."""
    from br import state

    root = os.getcwd().rstrip("/") + "/"
    codes: dict[str, set] = {}

    class StateDigest(state._Digest):
        def _walk(self, v, h) -> None:
            if isinstance(v, types.FunctionType):
                code = v.__code__
                path = code.co_filename[len(root):] if code.co_filename.startswith(root) else code.co_filename
                key = f"{path}|{code.co_qualname}"
                codes.setdefault(key, set()).add(state._code_hex(code)[:16])
                self.count += 1
                if id(v) in self.stack:
                    self.cycles += 1
                    h.update(b"cycle;")
                    return
                self.stack.add(id(v))
                try:
                    h.update(f"function:{key}(".encode())
                    self._walk(v.__defaults__, h)
                    self._walk(v.__kwdefaults__, h)
                    for cell in v.__closure__ or ():
                        try:
                            self._walk(cell.cell_contents, h)
                        except ValueError:
                            h.update(b"emptycell;")
                    h.update(b")")
                finally:
                    self.stack.discard(id(v))
                return
            super()._walk(v, h)

    digest = StateDigest()
    out: dict[str, str] = {}
    for mod_name, module in sorted(list(sys.modules.items())):
        if module is None or not (mod_name == "sqlglot" or mod_name.startswith("sqlglot.")):
            continue
        h = hashlib.sha256()
        for name, value in sorted(list(vars(module).items()), key=lambda p: p[0]):
            if name in _SKIPPED_GLOBALS:
                continue
            XX = h.hexdigest()[:24]
        for qual, cls in state._classes(module):
            h = hashlib.sha256(state._kind(cls).encode())
            for name, value in sorted(list(vars(cls).items()), key=lambda p: p[0]):
                if name in state._SKIPPED_NAMES:
                    continue
                h.update(name.encode() + b"=" + digest(value).encode() + b";")
            out[f"{mod_name}:{qual}"] = h.hexdigest()[:24]
    return out, {k: sorted(v) for k, v in codes.items()}


def _state_equal(row: dict, pinned: dict, changed: frozenset) -> bool:
    """Same digests everywhere, the same functions held, and the same code for every one
    of them that is not a changed function."""
    if row.get("modules") != pinned.get("modules") or row.get("state") != pinned.get("state"):
        return False
    a, b = row.get("codes"), pinned.get("codes")
    if a is None or b is None or set(a) != set(b):
        return False
    return all(a[k] == b[k] or _is_changed(k, changed) for k in a)


def _tree(root, expression_type) -> str:
    """Digest of an expression tree read from its slots (type, args, comments, meta, type
    annotation, position under the parent), never through sqlglot's own methods."""
    from br import state

    other = state._Digest(limit=1_000_000)
    out: list[str] = []
    stack = [root]
    while stack:
        v = stack.pop()
        t = type(v)
        if t is _Mark:
            out.append(v.text)
        elif v is None or t in (bool, int, float, complex, str, bytes):
            out.append(f"{t.__name__}:{v!r}")
        elif isinstance(v, enum.Enum):
            out.append(f"enum:{t.__module__}.{t.__qualname__}.{v.name}")
        elif isinstance(v, expression_type):
            get = object.__getattribute__
            out.append(f"E({t.__module__}.{t.__qualname__}")
            stack.append(_Mark(")"))
            for slot in ("_type", "_meta", "comments", "index", "arg_key"):
                stack.append(get(v, slot))
                stack.append(_Mark(slot + "="))
            args = get(v, "args")
            if type(args) is not dict:
                out.append("args:" + other(args))
                continue
            for k, x in reversed(list(args.items())):
                stack.append(x)
                stack.append(_Mark(f"{k!r}="))
        elif t in (list, tuple):
            out.append(f"{t.__name__}[{len(v)}](")
            stack.append(_Mark(")"))
            stack.extend(reversed(v))
        elif t is dict and all(type(k) is str for k in v):
            out.append("{")
            stack.append(_Mark("}"))
            for k, x in reversed(list(v.items())):
                stack.append(x)
                stack.append(_Mark(f"{k!r}:"))
        else:
            out.append("other:" + other(v))  # "unknown:<id>" never matches across processes
    return _sha("\n".join(out))


class _Mark:
    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


def _artifacts(benchmark: str, only, changed: frozenset, expected=None) -> list:
    """Runs in a forked child, inside the tree to judge: rows in the order computed. With
    `expected` (the pinned rows, inherited through fork), stops at the first difference.
    `only`: the executed queries to optimize (all when None)."""
    import importlib
    import pkgutil

    import pytest  # noqa: F401 - sqlglot checks at import whether a test framework is loaded

    import sqlglot
    import sqlglot.executor  # noqa: F401 - imported by the real test and its workers
    import sqlglot.executor.python  # noqa: F401
    import sqlglot.planner  # noqa: F401
    from sqlglot import parse_one, transpile
    from sqlglot.expressions.core import Expression
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    # Every sqlglot module, so that what a changed import-time function leaves on shared
    # classes while some other test in the same process loads a dialect is seen too.
    failed = []
    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if info.name.rsplit(".", 1)[1] != "__main__":
            try:
                importlib.import_module(info.name)
            except Exception as error:  # noqa: BLE001 - compared between the two sides
                failed.append(f"{info.name}:{type(error).__name__}")

    out: list[dict] = []

    def emit(row: dict) -> bool:
        out.append(row)
        if expected is None:
            return True
        return len(expected) >= len(out) and _matches(row, expected[len(out) - 1], changed)

    # The import state is digested in a grandchild forked now (its memory is this process's
    # state at import) that waits until the queries have matched: a patch whose queries
    # differ, the usual case, never pays for the digest.
    state = _Snapshot()
    schema = TPCDS_SCHEMA if benchmark == "tpc-ds" else TPCH_SCHEMA
    pairs = list(load_sql_fixture_pairs(f"optimizer/{benchmark}/{benchmark}.sql"))
    blobs: dict[int, bytes] = {}
    for i, (meta, sql, _) in enumerate(pairs):  # the parent: parse and pickle every query
        row = {"step": "parse", "query": i}
        try:
            blobs[i] = pickle.dumps(parse_one(sql))
            row["pickle"] = hashlib.sha256(blobs[i]).hexdigest()[:24]
        except Exception as error:  # noqa: BLE001 - an error is an outcome to compare too
            row["error"] = type(error).__name__ + ":" + str(error)[:200]
        if not emit(row):
            state.discard()
            return out
    for i, (meta, sql, _) in enumerate(pairs):  # the workers, then the parent's comparisons
        if i not in blobs:
            continue
        row = {"step": "run", "query": i}
        try:
            loaded = pickle.loads(blobs[i])
            row["loaded"] = _tree(loaded, Expression)
            if meta.get("execute"):
                if only is None or i in only:
                    optimized = optimize(loaded, ensure_schema(schema), leave_tables_isolated=True)
                    row["optimized"] = _tree(optimized, Expression)
                    row["sql"] = _sha(optimized.sql() + "\n" + optimized.sql(pretty=True))
                row["duckdb"] = _sha(transpile(sql, write="duckdb")[0])
        except Exception as error:  # noqa: BLE001
            row["error"] = type(error).__name__ + ":" + str(error)[:200]
        if not emit(row):
            state.discard()
            return out
    row = state.collect()
    if row is None:
        raise RuntimeError("state digest failed")
    row["modules"] = _sha(repr((row["modules"], failed)))
    emit(row)
    return out


class _Snapshot:
    """A forked copy of the current process that digests its import state on request."""

    def __init__(self):
        self.go_r, self.go_w = os.pipe()
        self.out_r, self.out_w = os.pipe()
        self.pid = os.fork()
        if self.pid == 0:
            os.close(self.go_w)
            os.close(self.out_r)
            try:
                if os.read(self.go_r, 1) != b"g":
                    os._exit(0)
                state, codes = _state()
                loaded = sorted(m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot."))
                data = json.dumps({"step": "import", "modules": loaded, "state": state, "codes": codes}).encode()
                view = memoryview(data)
                while view:
                    view = view[os.write(self.out_w, view):]
            except BaseException:  # noqa: BLE001 - the parent sees no data and keeps the test
                pass
            os._exit(0)
        os.close(self.go_r)
        os.close(self.out_w)

    def discard(self) -> None:
        os.close(self.go_w)
        os.close(self.out_r)
        os.waitpid(self.pid, 0)

    def collect(self):
        os.write(self.go_w, b"g")
        os.close(self.go_w)
        chunks = []
        while chunk := os.read(self.out_r, 1 << 20):
            chunks.append(chunk)
        os.close(self.out_r)
        os.waitpid(self.pid, 0)
        try:
            return json.loads(b"".join(chunks))
        except ValueError:
            return None


def _matches(row: dict, pinned: dict, changed: frozenset) -> bool:
    """A patched row equals the pinned row. The patched side may leave out the optimized
    artifacts of an executed query outside `only`; nothing else may be missing, and an
    error on either side is never a match."""
    if row.get("step") != pinned.get("step") or row.get("query") != pinned.get("query"):
        return False
    if "error" in row or "error" in pinned:
        return False
    if row["step"] == "import":
        return _state_equal(row, pinned, changed)
    if not set(row) <= set(pinned):
        return False
    skipped = {"optimized", "sql"} if "optimized" not in row else set()
    return all(row.get(k) == pinned.get(k) for k in set(pinned) - skipped)


def _child(tree: str, benchmark: str, write_fd: int, only, changed, expected) -> None:
    try:
        os.setpgid(0, 0)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.chdir(tree)
        sys.path.insert(0, tree)
        data = json.dumps({"ok": True, "rows": _artifacts(benchmark, only, changed, expected)}).encode()
    except BaseException as exc:  # noqa: BLE001
        data = json.dumps({"ok": False, "why": repr(exc)[-300:]}).encode()
    view = memoryview(data)
    while view:
        try:
            view = view[os.write(write_fd, view):]
        except OSError:
            break
    os._exit(0)


def _run(tree: str, benchmark: str, timeout: float, only=None, changed=frozenset(), expected=None):
    import select
    import signal
    import time

    read_fd, write_fd = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        _child(tree, benchmark, write_fd, only, changed, expected)
    os.close(write_fd)
    chunks, end, ok = [], time.monotonic() + timeout, True
    while True:
        left = end - time.monotonic()
        if left <= 0 or not select.select([read_fd], [], [], left)[0]:
            ok = False
            break
        chunk = os.read(read_fd, 1 << 20)
        if not chunk:
            break
        chunks.append(chunk)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    os.waitpid(pid, 0)
    os.close(read_fd)
    if not ok:
        return None
    try:
        result = json.loads(b"".join(chunks))
    except ValueError:
        return None
    return result["rows"] if result.get("ok") else None


# ---------------------------------------------------------------- parent side


def compare(pinned_tree: str, patched_tree: str, benchmark: str, timeout: float,
            only, changed: frozenset) -> tuple[bool, str]:
    """(same, why): whether the patched tree leaves the same state at import and after the
    queries, and prepares every query to the same artifacts, as the pinned tree."""
    if benchmark not in _PINNED:
        rows = _run(pinned_tree, benchmark, timeout)
        _PINNED[benchmark] = None if rows is None or any("error" in r for r in rows) else rows
    expected = _PINNED[benchmark]
    if expected is None:  # not retried: every later commit would pay for it again
        return False, "pinned side failed"
    rows = _run(patched_tree, benchmark, timeout, only, changed, expected)
    if rows is None:
        return False, "patched side failed or timed out"
    for row, pin in zip(rows, expected):
        if not _matches(row, pin, changed):
            if row["step"] == "import":
                keys = sorted(k for k in set(row["state"]) | set(pin["state"])
                              if row["state"].get(k) != pin["state"].get(k))
                funcs = sorted(k for k in set(row["codes"]) | set(pin["codes"])
                               if row["codes"].get(k) != pin["codes"].get(k) and not _is_changed(k, changed))
                return False, f"{row['step']} state differs: {','.join((keys + funcs)[:3]) or 'modules'}"
            return False, f"{row['step']} differs at query {row.get('query')}"
    if len(rows) != len(expected):
        return False, "fewer rows"
    return True, "same"


def decide(commit: str, changes, chosen: list[int], tests: list[dict], changed_objects: list[tuple],
           class_edits: list, pinned_tree: str, timeout: float, log=lambda message: None,
           work_root: str = "/tmp/br") -> tuple[list[int], str]:
    """The whole TPC decision for one patch: (tests still listed, a note for the log).

    `chosen` are the test numbers the selector would list; `tests` is tests.json's list;
    `changed_objects` and `class_edits` come from br.analyze.candidates. Drops a TPC test
    only on the evidence described in this module's docstring; any doubt keeps it."""
    from br.verify import build_tree, remove_tree

    by_number = {t["number"]: t for t in tests}
    present = {n: TPC_TESTS[by_number[n]["id"]] for n in chosen if by_number[n]["id"] in TPC_TESTS}
    if not present:
        return chosen, ""
    paths = {p for ch in changes for p in (ch.old_path, ch.new_path) if p}
    if class_edits or not changed_objects or any(
            not p.endswith(".py") or not p.startswith("sqlglot/") or p.startswith(REFUSE_PREFIXES) for p in paths):
        return chosen, " | tpc: kept (patch shape)"
    if any(kind != "function" for _, _, _, kind in changed_objects):
        return chosen, " | tpc: kept (class or module code changed)"
    if any(ch.old_path != ch.new_path for ch in changes):
        return chosen, " | tpc: kept (file added, removed or renamed)"
    execution = _map("tpc_exec.json")
    if any((p, q, n) in execution for p, q, n, _ in changed_objects):
        return chosen, " | tpc: kept (changed code runs during execution)"
    import_time = _map("import_time.json")
    queries = _map("tpc_queries.json")
    keys = [f"{p}|{q}|{n}" for p, q, n, _ in changed_objects]
    at_import = any((p, q, n) in import_time for p, q, n, _ in changed_objects)
    affected = {b: frozenset(i for k in keys for i in queries.get(b, {}).get(k, [])) for b in set(present.values())}
    notes = []
    for n, b in sorted(present.items()):
        if not affected[b] and not at_import:
            # the test is a candidate, yet no map says where it runs the change
            notes.append(f"{b} kept: no map explains it")
            del present[n]
    if not present:
        return chosen, f" | tpc: kept ({'; '.join(notes)})"
    changed = frozenset((p, q) for p, q, _, _ in changed_objects)
    tree = Path(work_root) / (commit + "-tpc")
    dropped = []
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        remove_tree(tree)
        build_tree(tree, changes)
        for n, benchmark in sorted(present.items()):
            same, why = compare(pinned_tree, str(tree), benchmark, timeout, affected[benchmark], changed)
            notes.append(f"{benchmark} {why}")
            if same:
                dropped.append(n)
    except Exception as error:  # noqa: BLE001
        return chosen, f" | tpc: kept (error {error!r})"
    finally:
        remove_tree(tree)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        log(f"{commit}: tpc comparison cpu {after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime:.2f}s")
    if not dropped:
        return chosen, f" | tpc: kept ({'; '.join(notes)})"
    names = ",".join(by_number[n]["id"].rsplit("_", 1)[1] for n in dropped)
    return [n for n in chosen if n not in dropped], f" | tpc: dropped [{names}] ({'; '.join(notes)})"


def same_as_pinned(pinned_tree: str, patched_tree: str, benchmark: str, timeout: float,
                   only: frozenset | None = None) -> bool:
    """The selector's earlier call. No changed function is named, so any changed function
    held in class or module state reads as a difference, which keeps the test listed."""
    return compare(pinned_tree, patched_tree, benchmark, timeout, only, frozenset())[0]
