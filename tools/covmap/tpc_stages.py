#!/usr/bin/env python3
"""Which TPC benchmark queries enter each code object in the WORKER stages (unpickle,
optimize, plain and pretty SQL of the optimized tree) and in the MAIN-process stages
(parse, pickle, DuckDB transpilation) of the executor tests, with the process structure
of the real test: the main stages of a query run in a fork that parses it, the worker
stages in a separate fork of a parent that has parsed nothing (a spawned worker parses
nothing either), from the pickle bytes the main fork produced. Each query is recorded in
its own forks, so cached work is charged to every query that needs it.

    python /tools/covmap/tpc_stages.py /out/tpc_stages.json     (dev image, tree at /src)

Output: {"tpc-ds": {"worker": {"path|qualname|firstlineno": [query indexes]}, "main": {...}},
         "tpc-h": {...}}
"""

import json
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/src")
os.chdir("/src")
import pytest  # noqa: E402,F401

import recorder  # noqa: E402
from sqlglot import parse_one, transpile  # noqa: E402
import sqlglot.executor  # noqa: E402,F401
import sqlglot.planner  # noqa: E402,F401
from sqlglot.optimizer import optimize  # noqa: E402
from sqlglot.schema import ensure_schema  # noqa: E402
from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs  # noqa: E402


def in_fork(work):
    """Run work(rec) in a forked child with a fresh recorder; return (codes, payload)."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        rec = recorder.Recorder("/src/")
        rec.start()
        try:
            payload = work()
        except Exception:  # noqa: BLE001
            payload = None
        rec.stop()
        codes = [f"{f[len('/src/'):]}|{q}|{n}" for f, q, n in rec.result()["code"]]
        with os.fdopen(w, "wb") as fh:
            fh.write(pickle.dumps((codes, payload)))
        os._exit(0)
    os.close(w)
    data = b""
    while chunk := os.read(r, 1 << 20):
        data += chunk
    os.close(r)
    os.waitpid(pid, 0)
    return pickle.loads(data)


out = {}
for benchmark, schema in (("tpc-ds", TPCDS_SCHEMA), ("tpc-h", TPCH_SCHEMA)):
    pairs = list(load_sql_fixture_pairs(f"optimizer/{benchmark}/{benchmark}.sql"))
    index = {"worker": {}, "main": {}}
    for i, (meta, sql, _) in enumerate(pairs):
        def main_stages(meta=meta, sql=sql):
            blob = pickle.dumps(parse_one(sql))
            if meta.get("execute"):
                transpile(sql, write="duckdb")
            return blob

        main_codes, blob = in_fork(main_stages)

        def worker_stages(meta=meta, blob=blob):
            loaded = pickle.loads(blob)
            if meta.get("execute"):
                optimized = optimize(loaded, ensure_schema(schema), leave_tables_isolated=True)
                optimized.sql()
                optimized.sql(pretty=True)
                [n.type.sql() if n.type else None for n in optimized.walk()]
            return None

        worker_codes, _ = in_fork(worker_stages) if blob is not None else ([], None)
        for stage, codes in (("main", main_codes), ("worker", worker_codes)):
            for key in codes:
                index[stage].setdefault(key, []).append(i)
    out[benchmark] = index
    print(f"{benchmark}: {len(pairs)} queries, worker {len(index['worker'])} / main {len(index['main'])} code objects",
          file=sys.stderr)
json.dump(out, open(sys.argv[1], "w"))
