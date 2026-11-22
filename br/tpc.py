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

1. The state the imports leave behind, when a changed function runs at import
   (maps/import_time.json; otherwise the imports run the same code on both trees): every
   global and every class attribute of every loaded sqlglot module (both sides import
   every sqlglot module), digested structurally as in br/state.py. A function is digested
   by its file, name, code, defaults and closure; the patched code of each changed code
   object, and of each one enclosing it, is first renamed to its pinned code digest
   (`code_renames`, pairing code objects by position), so every other function, a nested
   one included, is compared by its real code in place. Taken in a process forked right
   after import, the state the spawned workers start from (they import afresh, then only
   unpickle and run). Equal state means the changed function left nothing different
   behind at import, whatever ran it there (metaclasses, templates the DuckDB generator
   parses while its class is created).
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

# Kinds br/state.py's digest cannot read, all found among sqlglot's module globals.
_DICT_VIEWS = (type({}.keys()), type({}.values()), type({}.items()))
_VALUE_TYPES = (__import__("datetime").date, __import__("datetime").datetime, __import__("datetime").time,
                __import__("datetime").timedelta, __import__("decimal").Decimal)
_LOCK_TYPES = (type(__import__("threading").RLock()), type(__import__("threading").Lock()))

# Tuples sqlglot builds from a set, so their order follows memory addresses even on the
# pinned tree (no test can depend on it): compared as sets.
_UNORDERED_GLOBALS = {("sqlglot.optimizer.canonicalize", "_CANONICALIZE_TYPES")}

# Class-body and module-level edits are judged too (attribute by attribute, against the
# execution-read record maps/tpc_reads.json). False refuses them as before.
CLASS_LEVEL = True

_APP = Path(__file__).resolve().parent.parent
_PINNED: dict[str, dict] = {}
_MAPS: dict[str, object] = {}


def _stage_map():
    """maps/tpc_stages.json when the image has it, else None (then every query that reaches
    the change at any stage is optimized)."""
    if "tpc_stages.json" not in _MAPS:
        try:
            _MAPS["tpc_stages.json"] = json.loads((_APP / "maps" / "tpc_stages.json").read_text())
        except (OSError, ValueError):
            _MAPS["tpc_stages.json"] = None
    return _MAPS["tpc_stages.json"]


def _execution_record():
    """maps/tpc_execution.json (tools/covmap/tpc_execution.py): code run and class
    attributes read during the execution parts along the real worker path
    (test_executor.mp_execute -> sqlglot.executor.execute), or None."""
    if "tpc_execution.json" not in _MAPS:
        try:
            _MAPS["tpc_execution.json"] = json.loads((_APP / "maps" / "tpc_execution.json").read_text())
        except (OSError, ValueError):
            _MAPS["tpc_execution.json"] = None
    return _MAPS["tpc_execution.json"]


def _map(name: str):
    if name not in _MAPS:
        data = json.loads((_APP / "maps" / name).read_text())
        if name == "tpc_exec.json":
            data = {(f[len("/src/"):], q, n) for f, q, n in data}
            record = _execution_record()
            if record is not None:  # the real worker path also runs execute()'s own code
                data |= {tuple(c) for c in record["code"]}
        elif name == "import_time.json":
            data = {tuple(x) for x in data}
        _MAPS[name] = data
    return _MAPS[name]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


# ---------------------------------------------------------------- child side


def code_renames(changes) -> dict[str, str] | None:
    """{patched code digest: pinned code digest} for every code object that differs between
    the pinned and the patched text of the changed files: the changed code objects and the
    ones enclosing them (a function's code digest covers its nested code). Code objects are
    paired by position, as br.analyze pairs them. Every other function keeps its own digest,
    so a function nested in a changed one, a different inner function returned by a changed
    helper, or two same-named lambdas swapping places all show as a difference. None when
    the pairing is ambiguous (two pinned digests for one patched digest) or a file does not
    compile; the caller then keeps the test."""
    from br import analyze, state

    renames: dict[str, str] = {}

    def walk(old, new) -> bool:
        o, n = state._code_hex(old)[:16], state._code_hex(new)[:16]
        if o != n:
            if renames.get(n, o) != o:
                return False
            renames[n] = o
        old_kids, new_kids = analyze._keyed_children(old), analyze._keyed_children(new)
        return all(walk(old_kids[k], new_kids[k]) for k in old_kids if k in new_kids)

    for ch in changes:
        if ch.old_path is None or ch.new_path != ch.old_path or not ch.old_path.endswith(".py"):
            return None
        try:
            old = analyze._compile(ch.old_text, ch.old_path)
            new = analyze._compile(ch.new_text, ch.new_path)
        except Exception:  # noqa: BLE001
            return None
        if old is None or new is None or not walk(old, new):
            return None
    return renames


def _state(renames: dict[str, str]) -> dict:
    """{"module" or "module:Class": digest} for every loaded sqlglot module: its globals and
    each of its classes' attributes, digested structurally as in br/state.py. A function is
    digested by its file, qualified name, code, defaults and closure, its code digest passed
    through `renames` (patched code of a changed function reads as its pinned code)."""
    from br import state

    root = os.getcwd().rstrip("/") + "/"

    class StateDigest(state._Digest):
        def _walk(self, v, h) -> None:
            if isinstance(v, types.FunctionType):
                code = v.__code__
                path = code.co_filename[len(root):] if code.co_filename.startswith(root) else code.co_filename
                own = state._code_hex(code)[:16]
                self.count += 1
                if id(v) in self.stack:
                    self.cycles += 1
                    h.update(b"cycle;")
                    return
                self.stack.add(id(v))
                try:
                    h.update(f"function:{path}|{code.co_qualname}|{renames.get(own, own)}(".encode())
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
            t = type(v)
            if t in _DICT_VIEWS:
                self.count += 1
                h.update(f"{t.__name__}(".encode())
                self._walk(list(v), h)
                h.update(b")")
                return
            if t in _VALUE_TYPES:
                self.count += 1
                h.update(f"{t.__module__}.{t.__qualname__}:{v!r};".encode())
                return
            if t in _LOCK_TYPES:  # import locks: no program state
                self.count += 1
                h.update(f"lock:{t.__name__};".encode())
                return
            super()._walk(v, h)

    def function_of(value):
        """"path|qualname|firstlineno" when the value is a function (also wrapped in
        staticmethod or classmethod), else None."""
        if isinstance(value, (staticmethod, classmethod)):
            value = value.__func__
        if not isinstance(value, types.FunctionType):
            return None
        code = value.__code__
        path = code.co_filename[len(root):] if code.co_filename.startswith(root) else code.co_filename
        return f"{path}|{code.co_qualname}|{code.co_firstlineno}"

    digest = StateDigest()
    out: dict[str, str] = {}
    detail: dict[str, dict[str, str]] = {}
    functions: dict[str, dict[str, str]] = {}
    for mod_name, module in sorted(list(sys.modules.items())):
        if module is None or not (mod_name == "sqlglot" or mod_name.startswith("sqlglot.")):
            continue
        h = hashlib.sha256()
        names = detail[mod_name] = {}
        for name, value in sorted(list(vars(module).items()), key=lambda p: p[0]):
            if name in _SKIPPED_GLOBALS:
                continue
            if (mod_name, name) in _UNORDERED_GLOBALS and type(value) is tuple:
                value = frozenset(value)
            d = names[name] = digest(value)[:16]
            h.update(name.encode() + b"=" + d.encode() + b";")
            if (f := function_of(value)) is not None:
                functions.setdefault(mod_name, {})[name] = f
        out[mod_name] = h.hexdigest()[:24]
        for qual, cls in state._classes(module):
            kind = state._kind(cls)
            h = hashlib.sha256(kind.encode())
            names = detail[f"{mod_name}:{qual}"] = {"__kind__": kind}
            for name, value in sorted(list(vars(cls).items()), key=lambda p: p[0]):
                if name in state._SKIPPED_NAMES:
                    continue
                d = names[name] = digest(value)[:16]
                h.update(name.encode() + b"=" + d.encode() + b";")
                if (f := function_of(value)) is not None:
                    functions.setdefault(f"{mod_name}:{qual}", {})[name] = f
            out[f"{mod_name}:{qual}"] = h.hexdigest()[:24]
    return {"summary": out, "detail": detail, "functions": functions}


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


def _artifacts(benchmark: str, only, renames: dict, with_state: bool, expected=None) -> list:
    """Runs in a forked child, inside the tree to judge: rows in the order computed. With
    `expected` (the pinned rows), stops at the first difference. `only`: the executed
    queries to optimize (all when None). `with_state`: digest the import state (always on
    the pinned side; on the patched side only when a changed function runs at import,
    since otherwise the import runs the same code as on the pinned tree)."""
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

    # Every sqlglot module, on both sides alike: what a changed import-time function leaves
    # on shared classes while another test in the same process loads a dialect is seen too,
    # and the queries are prepared after the same imports whatever the patch.
    failed = []
    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if info.name.rsplit(".", 1)[1] != "__main__":
            try:
                importlib.import_module(info.name)
            except Exception as error:  # noqa: BLE001 - compared between the two sides
                failed.append(f"{info.name}:{type(error).__name__}")
    if _HOOK_IN_CHILD:
        import importlib as _il
        module, _, function = _HOOK_IN_CHILD.partition(":")
        getattr(_il.import_module(module), function)()
    loaded = sorted(m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot."))
    modules = _sha(repr((loaded, failed)))

    out: list[dict] = []

    def emit(row: dict) -> bool:
        out.append(row)
        if expected is None:
            return True
        return len(expected) >= len(out) and _matches(row, expected[len(out) - 1])

    # The import state is digested in a grandchild forked now (its memory is this process's
    # state at import) that waits until the queries have matched: a patch whose queries
    # differ, the usual case, never pays for the digest.
    snapshot = _Snapshot(renames) if with_state else None
    schema = TPCDS_SCHEMA if benchmark == "tpc-ds" else TPCH_SCHEMA
    pairs = list(load_sql_fixture_pairs(f"optimizer/{benchmark}/{benchmark}.sql"))
    blobs: dict[int, bytes] = {}
    try:
        for i, (meta, sql, _) in enumerate(pairs):  # the parent: parse and pickle every query
            row = {"step": "parse", "query": i}
            try:
                blobs[i] = pickle.dumps(parse_one(sql))
                row["pickle"] = hashlib.sha256(blobs[i]).hexdigest()[:24]
            except Exception as error:  # noqa: BLE001 - an error is an outcome to compare too
                row["error"] = type(error).__name__ + ":" + str(error)[:200]
            if not emit(row):
                return out
        for i, (meta, sql, _) in enumerate(pairs):  # the workers, then the parent's comparisons
            if i not in blobs:
                continue
            row = {"step": "run", "query": i}
            try:
                loaded_tree = pickle.loads(blobs[i])
                row["loaded"] = _tree(loaded_tree, Expression)
                if meta.get("execute"):
                    if only is None or i in only:
                        optimized = optimize(loaded_tree, ensure_schema(schema), leave_tables_isolated=True)
                        row["optimized"] = _tree(optimized, Expression)
                        row["sql"] = _sha(optimized.sql() + "\n" + optimized.sql(pretty=True))
                    row["duckdb"] = _sha(transpile(sql, write="duckdb")[0])
            except Exception as error:  # noqa: BLE001
                row["error"] = type(error).__name__ + ":" + str(error)[:200]
            if not emit(row):
                return out
        row = {"step": "import", "modules": modules}
        if snapshot is not None:
            state = snapshot.collect()
            snapshot = None
            if state is None:
                raise RuntimeError("state digest failed")
            row["state"], row["detail"], row["functions"] = state["summary"], state["detail"], state["functions"]
        emit(row)
        return out
    finally:
        if snapshot is not None:
            snapshot.discard()


# Self-tests only (tools/tpc_gate.py): "module:function" the child calls right after the
# imports, to change one value and check that the comparison sees it.
_TEST_HOOK = None
_HOOK_IN_CHILD = None


class _Snapshot:
    """A forked copy of the current process that digests its import state on request."""

    def __init__(self, renames: dict):
        self.go_r, self.go_w = os.pipe()
        self.out_r, self.out_w = os.pipe()
        self.pid = os.fork()
        if self.pid == 0:
            os.close(self.go_w)
            os.close(self.out_r)
            try:
                if os.read(self.go_r, 1) == b"g":
                    data = json.dumps(_state(renames)).encode()
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


def _matches(row: dict, pinned: dict) -> bool:
    """A patched row equals the pinned row. The patched side may leave out the optimized
    artifacts of an executed query outside `only`, and the import state when no changed
    function runs at import; nothing else may be missing, and an error on either side is
    never a match."""
    if row.get("step") != pinned.get("step") or row.get("query") != pinned.get("query"):
        return False
    if "error" in row or "error" in pinned or not set(row) <= set(pinned):
        return False
    skipped = set()
    if row["step"] == "run" and "optimized" not in row:
        skipped = {"optimized", "sql"}
    if row["step"] == "import":
        skipped = {"state", "detail", "functions"} if "state" not in row else {"detail", "functions"}
    return all(row.get(k) == pinned.get(k) for k in set(pinned) - skipped)


def _main_child() -> None:
    """Entry of a child interpreter (see `_run`): arguments as JSON on stdin, rows as JSON
    on stdout."""
    global _HOOK_IN_CHILD
    args = json.loads(sys.stdin.buffer.read())
    _HOOK_IN_CHILD = args.get("hook")
    out = os.fdopen(os.dup(1), "wb")
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    try:
        os.chdir(args["tree"])
        sys.path.insert(0, args["tree"])
        only = None if args["only"] is None else frozenset(args["only"])
        rows = _artifacts(args["benchmark"], only, args["renames"], args["with_state"], args["expected"])
        data = json.dumps({"ok": True, "rows": rows}).encode()
    except BaseException as exc:  # noqa: BLE001
        data = json.dumps({"ok": False, "why": repr(exc)[-300:]}).encode()
    out.write(data)
    out.flush()
    os._exit(0)


# Both sides run in a fresh interpreter with this environment, never in a fork of the
# selector: with a fixed hash seed every artifact, the import state and TPC-DS query 48's
# parse included, came out identical across unrelated processes (different environment
# sizes, working directories, import histories; tools/tpc_baseline.py diff), so the pinned
# side can be computed at image build. With differing hash seeds the import state and
# query 48 differ.
CHILD_ENV = {"PYTHONHASHSEED": "0", "SKIP_INTEGRATION": "0", "PYTHON_CPU_COUNT": "1",
             "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8"}


def _run(tree: str, benchmark: str, timeout: float, only=None, renames=None, with_state=True, expected=None):
    import signal
    import subprocess

    env = dict(CHILD_ENV, PATH=os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
    payload = json.dumps({"tree": tree, "benchmark": benchmark, "only": None if only is None else sorted(only),
                          "renames": renames or {}, "with_state": with_state, "expected": expected,
                          "hook": _TEST_HOOK}).encode()
    code = f"import sys; sys.path.insert(0, {str(_APP)!r}); from br.tpc import _main_child; _main_child()"
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, cwd="/", start_new_session=True)
    except OSError:
        return None
    try:
        data, _ = proc.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        data = None
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
    if not data:
        return None
    try:
        result = json.loads(data)
    except ValueError:
        return None
    return result["rows"] if result.get("ok") else None


# ---------------------------------------------------------------- parent side

# Pinned rows computed at image build (tools/tpc_baseline.py), if the image has them.
BASELINE = _APP / "maps" / "tpc_pinned.json"


def pinned_rows(pinned_tree: str, benchmark: str, timeout: float):
    """The pinned rows: from the build-time baseline when present and made by this Python,
    else computed once per run in a child forked from this process."""
    if benchmark not in _PINNED:
        rows = None
        try:
            data = json.loads(BASELINE.read_text())
            if data.get("python") == sys.version and data.get("tree") == pinned_tree:
                rows = data["rows"][benchmark]
        except (OSError, ValueError, KeyError):
            rows = None
        if rows is None:
            rows = _run(pinned_tree, benchmark, timeout)
        _PINNED[benchmark] = None if rows is None or any("error" in r for r in rows) else rows
    return _PINNED[benchmark]


def _reads():
    """maps/tpc_reads.json (tools/covmap/tpc_reads.py): the class attributes the pinned
    commit's TPC tests read during their execution parts, and those the recorder could see
    at all. None without the map."""
    if "tpc_reads.json" not in _MAPS:
        try:
            data = json.loads((_APP / "maps" / "tpc_reads.json").read_text())
            instrumented = {tuple(x) for x in data["instrumented"]}
            read = {tuple(x) for x in data["execution_reads"]}
            record = _execution_record()
            if record is None or record.get("bypass"):
                raise ValueError("no real-path record, or executed code that can bypass it")
            # attributes both recorders instrumented; reads seen by either
            instrumented &= {tuple(x) for x in record["instrumented"]}
            read |= {tuple(x) for x in record["execution_reads"]}
            _MAPS["tpc_reads.json"] = (instrumented, read)
        except (OSError, ValueError, KeyError):
            _MAPS["tpc_reads.json"] = None
    return _MAPS["tpc_reads.json"]


def unread_differences(row: dict, pin: dict) -> tuple[bool, str]:
    """For a class or module edit: whether every attribute whose import-time value differs
    is a class attribute the recorder instruments and the execution parts never read. A
    module value, a class or attribute present on one side only, an enum, dataclass or
    named-tuple member, or an attribute the recorder cannot see keeps the test."""
    reads = _reads()
    if reads is None:
        return False, "no execution read record"
    instrumented, read = reads
    a, b = row.get("detail"), pin.get("detail")
    if a is None or b is None:
        return False, "no attribute digests"
    if set(a) != set(b):
        return False, f"{sorted(set(a) ^ set(b))[0]} exists on one side only"
    fa, fb = row.get("functions") or {}, pin.get("functions") or {}
    execution = _map("tpc_exec.json")
    changed = []
    for key in sorted(a):
        x, y = a[key], b[key]
        if x == y:
            continue
        for name in sorted(n for n in set(x) | set(y) if x.get(n) != y.get(n)):
            if name not in x or name not in y:
                return False, f"{key}.{name} exists on one side only"
            # A function value (its defaults or closure changed, or its code under a
            # rename): it matters only when called, and the execution record says
            # whether its code runs during execution at all.
            f_new, f_old = fa.get(key, {}).get(name), fb.get(key, {}).get(name)
            if f_new and f_old and f_new.rsplit("|", 1)[0] == f_old.rsplit("|", 1)[0]:
                path, qual_f, line = f_old.split("|")
                if (path, qual_f, int(line)) in execution:
                    return False, f"{key}.{name} changed and runs during execution"
                changed.append(f"{key.split(':')[-1]}.{name}()")
                continue
            if ":" not in key:
                return False, f"module value {key}.{name} changed"
            if x.get("__kind__") != "class" or y.get("__kind__") != "class":
                return False, f"{key} is an enum, dataclass or named tuple"
            module, qual = key.split(":", 1)
            if (module, qual, name) not in instrumented:
                return False, f"{key}.{name} changed and its reads are not recorded"
            if (module, qual, name) in read:
                return False, f"{key}.{name} changed and is read during execution"
            changed.append(f"{qual}.{name}")
    return True, f"changed only {', '.join(changed[:4])}{' ...' if len(changed) > 4 else ''}, never read during execution"


def compare(pinned_tree: str, patched_tree: str, benchmark: str, timeout: float,
            only, renames: dict, with_state: bool, allow_unread: bool = False) -> tuple[bool, str]:
    """(same, why): whether the patched tree prepares every query to the same artifacts,
    loads the same modules and (with_state) leaves the same import state as the pinned one.
    With `allow_unread` (class and module edits), the import state may differ in class
    attributes the execution parts never read (`unread_differences`)."""
    expected = pinned_rows(pinned_tree, benchmark, timeout)
    if expected is None:  # not retried: every later commit would pay for it again
        return False, "pinned side failed"
    rows = _run(patched_tree, benchmark, timeout, only, renames, with_state, expected)
    if rows is None:
        return False, "patched side failed or timed out"
    for row, pin in zip(rows, expected):
        if not _matches(row, pin):
            if row["step"] == "import":
                if row.get("modules") != pin.get("modules"):
                    return False, "loaded modules differ"
                if allow_unread and len(rows) == len(expected):
                    ok, why = unread_differences(row, pin)
                    return ok, (f"same queries; import state {why}" if ok else f"import state differs: {why}")
                keys = sorted(k for k in set(row["state"]) | set(pin["state"])
                              if row["state"].get(k) != pin["state"].get(k))
                return False, f"import state differs: {','.join(keys[:3])}"
            return False, f"{row['step']} differs at query {row.get('query')}"
    if len(rows) != len(expected):
        return False, "fewer rows"
    return True, "same"


# Expected CPU of the check on the scorer (seconds, patched side; the pinned side comes
# from the build-time baseline). Scorer measurement (practice diagnostic, 2026-11-22): one
# full TPC-DS preparation pass (all 57 executed queries optimized) cost 4.71 s of CPU,
# about 0.083 s per executed query. The other parts are the local amd64 measurements
# (tools/tpc_cost.py, load 17-20) scaled by the same ratio (4.71 / 24.2 for the full pass
# measured locally): early exit 0.4, base 0.85, state 0.55 for tpc-ds.
PER_OPTIMIZED = 0.083
EST = {"tpc-ds": {"early": 0.4, "base": 0.85, "per_optimized": PER_OPTIMIZED, "state": 0.55},
       "tpc-h": {"early": 0.5, "base": 0.5, "per_optimized": 0.0, "state": 0.6}}
# Without a baseline the pinned side is computed once per run: about base + all optimized
# + state for tpc-ds, base + state for tpc-h.
EST_PINNED = {"tpc-ds": 0.85 + PER_OPTIMIZED * 57 + 0.55, "tpc-h": 0.5 + 0.6}
# Share of comparisons that ended in a drop on the labeled sets (tools/tpc_gate.py:
# 15 drops in 31 comparisons on e6-tpcds and the sample).
P_DROP = 0.45


def _executed() -> dict[str, frozenset] | None:
    """Executed query indexes per benchmark, from the pinned rows when loaded."""
    out = {}
    for b, rows in _PINNED.items():
        if rows is None:
            return None
        out[b] = frozenset(r["query"] for r in rows if r["step"] == "run" and "optimized" in r)
    return out


def worth_it(present: dict, affected: dict, at_import: bool, saving_ms) -> tuple[dict, str]:
    """The subset of `present` ({test number: benchmark}) whose comparison is expected to
    save more than it costs: P_DROP * saving against P_DROP * (cost of a full comparison)
    + (1 - P_DROP) * (cost of an early exit), with the pinned side added once when it is
    not loaded yet. Returns ({}, why) when no subset pays."""
    executed = _executed() or {}

    def cpu(b: str) -> float:
        e = EST[b]
        reach = len(affected[b] & executed[b]) if b in executed else len(affected[b])
        full = e["base"] + e["per_optimized"] * reach + (e["state"] if at_import else 0.0)
        return P_DROP * full + (1 - P_DROP) * e["early"] + (0.0 if b in _PINNED else EST_PINNED[b])

    best, best_net, note = {}, 0.0, ""
    items = sorted(present.items())
    for mask in range(1, 1 << len(items)):
        subset = dict(x for i, x in enumerate(items) if mask >> i & 1)
        gain = P_DROP * saving_ms(sorted(subset)) / 1000
        cost = sum(cpu(b) for b in subset.values())
        note += f"{'+'.join(sorted(subset.values()))} gain {gain:.1f}s cpu {cost:.1f}s; "
        if gain - cost > best_net:
            best, best_net = subset, gain - cost
    return best, note.rstrip("; ")


FIXTURES = {"tests/fixtures/optimizer/tpc-ds/tpc-ds.sql": "tpc-ds",
            "tests/fixtures/optimizer/tpc-h/tpc-h.sql": "tpc-h"}


def _fixture_pairs(pinned_tree: str, relative: str, text: str) -> list | None:
    """The (settings, query, expected) pairs the tests' own loader
    (tests/helpers.py load_sql_fixture_pairs, from the pinned tree; it imports only os)
    reads from `text`, or None when it raises."""
    import importlib.util
    import tempfile

    spec = importlib.util.spec_from_file_location("_br_tpc_helpers", os.path.join(pinned_tree, "tests", "helpers.py"))
    helpers = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(helpers)
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            target = Path(tmp) / relative
            target.parent.mkdir(parents=True)
            target.write_text(text, encoding="utf-8")
            helpers.FIXTURES_DIR = tmp
            return [list(p) for p in helpers.load_sql_fixture_pairs(relative)]
    except Exception:  # noqa: BLE001 - a fixture the loader cannot read breaks setUpClass
        return None


def decide_fixture(changes, chosen: list[int], tests: list[dict], pinned_tree: str):
    """For a patch that edits only the TPC query fixtures in place: the executor tests use
    each pair's settings and query and ignore the expected output (test_execute_tpch and
    test_execute_tpcds; test_optimized_tpch reads the expected output and is not decided
    here). When the loader reads every changed fixture to the same settings and queries,
    in the same order, those two tests run exactly as on the pinned commit. None when the
    patch is not such an edit."""
    if not changes or any(ch.old_path not in FIXTURES or ch.new_path != ch.old_path
                          or ch.old_text is None or ch.new_text is None for ch in changes):
        return None
    by_number = {t["number"]: t for t in tests}
    present = [n for n in chosen if by_number[n]["id"] in TPC_TESTS]
    if not present:
        return chosen, ""
    for ch in changes:
        relative = ch.old_path[len("tests/fixtures/"):]
        old = _fixture_pairs(pinned_tree, relative, ch.old_text)
        new = _fixture_pairs(pinned_tree, relative, ch.new_text)
        if old is None or new is None:
            return chosen, f" | tpc: kept (fixture {ch.old_path} does not load)"
        if [p[:2] for p in old] != [p[:2] for p in new]:
            return chosen, f" | tpc: kept (fixture {ch.old_path}: settings or queries changed)"
    names = ",".join(by_number[n]["id"].rsplit("_", 1)[1] for n in present)
    return [n for n in chosen if n not in present], f" | tpc: dropped [{names}] (fixture: only expected outputs or comments changed)"


def decide(commit: str, changes, chosen: list[int], tests: list[dict], changed_objects: list[tuple],
           class_edits: list, pinned_tree: str, timeout: float, log=lambda message: None,
           work_root: str = "/tmp/br", saving_ms=None) -> tuple[list[int], str]:
    """The whole TPC decision for one patch: (tests still listed, a note for the log).

    `chosen` are the test numbers the selector would list; `tests` is tests.json's list;
    `changed_objects` and `class_edits` come from br.analyze.candidates. `saving_ms`, when
    given, maps a list of test numbers to the answer-cost saving (ms) of leaving them out;
    the check then runs only when its expected saving beats its expected CPU. Drops a TPC
    test only on the evidence described in this module's docstring; any doubt keeps it."""
    from br.verify import build_tree, remove_tree

    by_number = {t["number"]: t for t in tests}
    present = {n: TPC_TESTS[by_number[n]["id"]] for n in chosen if by_number[n]["id"] in TPC_TESTS}
    if not present:
        return chosen, ""
    fixture = decide_fixture(changes, chosen, tests, pinned_tree)
    if fixture is not None:
        return fixture
    paths = {p for ch in changes for p in (ch.old_path, ch.new_path) if p}
    if (not changed_objects and not class_edits) or any(
            not p.endswith(".py") or not p.startswith("sqlglot/") or p.startswith(REFUSE_PREFIXES) for p in paths):
        return chosen, " | tpc: kept (patch shape)"
    # A class-body or module-level edit runs at import: its effect is the import state,
    # compared attribute by attribute, and every query is prepared in full.
    class_level = bool(class_edits) or any(kind != "function" for _, _, _, kind in changed_objects)
    if class_level and (not CLASS_LEVEL or _reads() is None):
        return chosen, " | tpc: kept (class or module code changed)"
    if any(ch.old_path != ch.new_path for ch in changes):
        return chosen, " | tpc: kept (file added, removed or renamed)"
    execution = _map("tpc_exec.json")
    if any((p, q, n) in execution for p, q, n, _ in changed_objects):
        return chosen, " | tpc: kept (changed code runs during execution)"
    import_time = _map("import_time.json")
    queries = _map("tpc_queries.json")
    keys = [f"{p}|{q}|{n}" for p, q, n, _ in changed_objects]
    at_import = class_level or any((p, q, n) in import_time for p, q, n, _ in changed_objects)
    affected = {b: frozenset(i for k in keys for i in queries.get(b, {}).get(k, [])) for b in set(present.values())}
    reached = dict(affected)
    stages = _stage_map()
    if stages is not None:
        # Per stage (tools/covmap/tpc_stages.py): only queries whose WORKER stages (unpickle,
        # optimize, optimized SQL) reach the change need optimizing. A change the main
        # process alone runs (parse, pickle, DuckDB text) reaches a worker only through the
        # pickle bytes, which are compared for every query.
        for b in reached:
            main = frozenset(i for k in keys for i in stages[b]["main"].get(k, []))
            worker = frozenset(i for k in keys for i in stages[b]["worker"].get(k, []))
            reached[b] = affected[b] | main | worker
            affected[b] = worker
    notes = []
    for n, b in sorted(present.items()):
        if not reached[b] and not at_import:
            # the test is a candidate, yet no map says where it runs the change
            notes.append(f"{b} kept: no map explains it")
            del present[n]
    if not present:
        return chosen, f" | tpc: kept ({'; '.join(notes)})"
    if class_level:  # which queries read a changed attribute is not recorded: prepare all
        affected = {b: frozenset(range(99 if b == "tpc-ds" else 22)) for b in affected}  # every query
    if saving_ms is not None:
        for b in set(present.values()):  # loads the baseline, if the image has one
            pinned_rows(pinned_tree, b, timeout) if BASELINE.exists() else None
        worth, estimate_note = worth_it(present, affected, at_import, saving_ms)
        if not worth:
            return chosen, f" | tpc: kept (not worth it: {estimate_note})"
        present = worth
    renames = code_renames(changes)
    if renames is None:
        return chosen, " | tpc: kept (changed code cannot be paired)"
    tree = Path(work_root) / (commit + "-tpc")
    dropped = []
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        remove_tree(tree)
        build_tree(tree, changes)
        for n, benchmark in sorted(present.items()):
            same, why = compare(pinned_tree, str(tree), benchmark, timeout, affected[benchmark], renames, at_import,
                                allow_unread=class_level)
            notes.append(f"{benchmark} {why} ({len(reached[benchmark])} queries reach the change, "
                         f"{len(affected[benchmark])} in worker stages)")
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
    """The selector's earlier call. No renames are known, so any changed function held in
    class or module state reads as a difference, which keeps the test listed."""
    return compare(pinned_tree, patched_tree, benchmark, timeout, only, {}, True)[0]
