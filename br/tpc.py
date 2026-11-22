"""Decide the TPC executor tests (test_execute_tpcds, 21.5 s; test_execute_tpch, 3.9 s)
without executing them.

Per query the test parses the SQL, pickles the tree to a worker, and there runs sqlglot's
executor: optimize, then plan and execute; the parent compares the rows with DuckDB's
answer for the query transpiled to DuckDB. When a patch changes no code that runs during
the execution part (maps/tpc_exec.json, recorded from the pinned commit by
tools/covmap/tpcds_phases.py) and nothing else the test depends on (its files, fixtures,
the executor package, class or module level code), the outcome is decided by what
sqlglot prepares before execution: the parse tree after a pickle round trip, the
optimized tree (SQL, structure and inferred types) and the DuckDB transpilation. If all
of those equal the pinned commit's, the workers receive identical inputs and run
unchanged code, so the test behaves as on the pinned commit, where it passes.

Both sides are computed in processes forked from the selector, after pytest is imported
(sqlglot checks for a test framework at import), running the same sequence of queries.
The pinned side is computed once per run. It cannot be made at image build: the parse of
TPC-DS query 48 depends on what earlier parses left in the process and on memory-address
order (changing an unrelated environment variable changed it), so only two processes
forked from one parent are guaranteed to agree.
"""

import hashlib
import json
import os
import pickle
import sys

TPC_TESTS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "tpc-ds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch": "tpc-h",
}
# Files whose change rules the comparison out: the tests and their helpers and fixtures,
# the executor and the planner (they run during execution).
REFUSE_PREFIXES = ("tests/", "sqlglot/executor/", "sqlglot/planner.py")

_PINNED: dict[str, list] = {}
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def _artifacts(benchmark: str, expected: list | None = None) -> list:
    """Runs in a forked child, inside the tree to judge. With `expected` (the pinned rows,
    inherited through fork), stops at the first query that differs."""
    import pytest  # noqa: F401

    from sqlglot import parse_one, transpile
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    schema = TPCDS_SCHEMA if benchmark == "tpc-ds" else TPCH_SCHEMA
    out = []
    for meta, sql, _ in load_sql_fixture_pairs(f"optimizer/{benchmark}/{benchmark}.sql"):
        row = {}
        try:
            tree = parse_one(sql)
            blob = pickle.dumps(tree)
            row["pickle"] = hashlib.sha256(blob).hexdigest()[:24]
            loaded = pickle.loads(blob)
            row["loaded"] = _sha(repr(loaded))
            if meta.get("execute"):
                optimized = optimize(loaded, ensure_schema(schema), leave_tables_isolated=True)
                types = [(type(n).__name__, n.type.sql() if n.type else None) for n in optimized.walk()]
                # the executor also renders the optimized tree in pretty mode (a debug log
                # argument, evaluated for every query), so that output is compared too
                pretty = optimized.sql(pretty=True)
                row["optimized"] = _sha(optimized.sql() + "\n" + pretty + "\n" + repr(optimized) + "\n" + repr(types))
                row["duckdb"] = _sha(transpile(sql, write="duckdb")[0])
        except Exception as error:  # noqa: BLE001 - an error is an outcome to compare too
            row["error"] = type(error).__name__ + ":" + str(error)[:200]
        out.append(row)
        if expected is not None and (len(expected) < len(out) or expected[len(out) - 1] != row):
            break  # differs: the answer is already "keep the test"
    return out


def _child(tree: str, benchmark: str, write_fd: int) -> None:
    try:
        os.setpgid(0, 0)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.chdir(tree)
        sys.path.insert(0, tree)
        data = json.dumps({"ok": True, "rows": _artifacts(benchmark, _PINNED.get(benchmark))}).encode()
    except BaseException as exc:  # noqa: BLE001
        data = json.dumps({"ok": False, "why": repr(exc)[-300:]}).encode()
    view = memoryview(data)
    while view:
        try:
            view = view[os.write(write_fd, view):]
        except OSError:
            break
    os._exit(0)


def _run(tree: str, benchmark: str, timeout: float):
    import select
    import signal
    import time

    read_fd, write_fd = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        _child(tree, benchmark, write_fd)
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


def same_as_pinned(pinned_tree: str, patched_tree: str, benchmark: str, timeout: float) -> bool:
    """True only when every query's prepared artifacts equal the pinned commit's."""
    if benchmark not in _PINNED:
        rows = _run(pinned_tree, benchmark, timeout)
        if rows is None:
            return False
        _PINNED[benchmark] = rows
    rows = _run(patched_tree, benchmark, timeout)
    return rows is not None and rows == _PINNED[benchmark] and not any("error" in r for r in rows)

