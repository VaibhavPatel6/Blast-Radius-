#!/usr/bin/env python3
"""Per executed TPC-DS query: the code objects its EXECUTION enters (test_executor.mp_execute
in a cold fork, as a fresh pool worker would run it: initializer, then the query), and
the CPU that execution costs.

    python /tools/covmap/tpc_exec_queries.py OUT.json [--record]     (tree at /src)

With --record, code objects are recorded (sys.monitoring PY_START, sqlglot and tests
code), and the CPU figures include that small overhead; without it only CPU is measured.
Output: {"cpu": {query: seconds}, "setup_cpu": seconds, "code": {"path|qualname|line": [queries]}}
"""

import json
import os
import pickle
import resource
import sys

sys.path.insert(0, "/src")
os.chdir("/src")
import pytest  # noqa: E402,F401

RECORD = "--record" in sys.argv
PREFIX = "/src/"


def cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


t0 = cpu()
import tests.test_executor as te  # noqa: E402
from sqlglot import parse_one  # noqa: E402

te.TestExecutor.setUpClass()
setup_cpu = cpu() - t0
initargs = pickle.dumps((te.TPCDS_SCHEMA, te.TestExecutor.tpcds_tables))
jobs = [(i, pickle.dumps(parse_one(sql)), meta) for i, (meta, sql, _) in enumerate(te.TestExecutor.tpcds_sqls)
        if meta.get("execute")]
out = {"cpu": {}, "setup_cpu": round(setup_cpu, 3), "code": {}}
for i, blob, meta in jobs:
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        te.initializer(*pickle.loads(initargs))
        expression = pickle.loads(blob)
        codes = set()
        if RECORD:
            M = sys.monitoring

            def on_start(code, offset):
                if code.co_filename.startswith(PREFIX):
                    codes.add(f"{code.co_filename[len(PREFIX):]}|{code.co_qualname}|{code.co_firstlineno}")
                return M.DISABLE

            M.use_tool_id(5, "q")
            M.register_callback(5, M.events.PY_START, on_start)
            M.set_events(5, M.events.PY_START)
        start = cpu()
        try:
            te.mp_execute(expression, meta)
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        spent = cpu() - start
        with os.fdopen(w, "wb") as fh:
            pickle.dump((spent, ok, sorted(codes)), fh)
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as fh:
        spent, ok, codes = pickle.load(fh)
    os.waitpid(pid, 0)
    out["cpu"][i] = round(spent, 3)
    for c in codes:
        out["code"].setdefault(c, []).append(i)
json.dump(out, open(sys.argv[1], "w"))
cpus = sorted(out["cpu"].values())
print(f"{len(cpus)} queries, setup {setup_cpu:.2f}s, execution total {sum(cpus):.1f}s, median {cpus[len(cpus)//2]:.2f}s, "
      f"max {cpus[-1]:.2f}s, {len(out['code'])} code objects", file=sys.stderr)
