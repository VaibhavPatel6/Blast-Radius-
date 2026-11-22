#!/usr/bin/env python3
"""Record which code objects run during the EXECUTION parts of the TPC executor tests,
as opposed to the parts that only prepare each query (parse, pickle, optimize, transpile).

Run in the development image with the pinned tree at /src (pytest is imported first,
as in every test process):

    python /tools/covmap/tpcds_phases.py /out/tpc_exec.json

Execution parts, per test_executor.py: TestExecutor.setUpClass table loading, the
worker's `mp_execute` (find_tables, ensure_tables, schema handling, Plan,
PythonExecutor.execute), and the parent's comparison of result rows. The selector may
judge test_execute_tpcds / test_execute_tpch by comparing prepared artifacts only when
the patch changes none of the code objects listed here.
"""

import json
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/src")
os.chdir("/src")
import pytest  # noqa: E402,F401 - sqlglot checks at import whether a test framework is loaded

import recorder  # noqa: E402

rec = recorder.Recorder("/src/")
rec.start()
execution: set = set()


def phase_done(is_execution: bool) -> None:
    if is_execution:
        execution.update(tuple(c) for c in rec.result()["code"])
    rec.lines, rec.code, rec.opened, rec.listed = {}, set(), set(), set()
    recorder.M.restart_events()


import tests.test_executor as te  # noqa: E402
from sqlglot import find_tables, parse_one, transpile  # noqa: E402
from sqlglot.executor import execute  # noqa: E402,F401
from sqlglot.executor.python import PythonExecutor  # noqa: E402
from sqlglot.executor.table import ensure_tables  # noqa: E402
from sqlglot.optimizer import optimize  # noqa: E402
from sqlglot.planner import Plan  # noqa: E402
from sqlglot.schema import ensure_schema  # noqa: E402

phase_done(False)  # imports: not execution (class/module edits are refused anyway)
te.TestExecutor.setUpClass()
phase_done(True)  # table loading, duckdb views
for schema, tables, sqls in ((te.TPCH_SCHEMA, te.TestExecutor.tpch_tables, te.TestExecutor.tpch_sqls),
                             (te.TPCDS_SCHEMA, te.TestExecutor.tpcds_tables, te.TestExecutor.tpcds_sqls)):
    te.initializer(schema, tables)
    for meta, sql, _ in sqls:
        expression = pickle.loads(pickle.dumps(parse_one(sql)))
        transpile(sql, write="duckdb")
        phase_done(False)  # preparation
        if meta.get("execute"):
            # the steps of sqlglot.executor.execute, split at the optimized tree
            found = {t.name: te._tables[t.name] for t in find_tables(expression)}
            tables_ = ensure_tables(found)
            phase_done(True)
            optimized = optimize(expression, ensure_schema(te._schema), leave_tables_isolated=True)
            phase_done(False)
            PythonExecutor(tables=tables_).execute(Plan(optimized))
            phase_done(True)
json.dump(sorted(execution), open(sys.argv[1], "w"))
print(f"{len(execution)} code objects run during execution", file=sys.stderr)
