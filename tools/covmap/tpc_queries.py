#!/usr/bin/env python3
"""Which TPC benchmark queries enter each code object while being PREPARED (parse, pickle
round trip, optimize, pretty and plain SQL, DuckDB transpilation), each query prepared in
its own forked process so that cached work is charged to every query that needs it.

    python /tools/covmap/tpc_queries.py /out/tpc_queries.json     (dev image, tree at /src)

Output: {"tpc-ds": {"path|qualname|firstlineno": [query indexes]}, "tpc-h": {...}}
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
from sqlglot.optimizer import optimize  # noqa: E402
from sqlglot.schema import ensure_schema  # noqa: E402
from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs  # noqa: E402


def prepare(meta, sql, schema):
    tree = parse_one(sql)
    loaded = pickle.loads(pickle.dumps(tree))
    if meta.get("execute"):
        optimized = optimize(loaded, ensure_schema(schema), leave_tables_isolated=True)
        optimized.sql()
        optimized.sql(pretty=True)
        repr(optimized)
        [n.type.sql() if n.type else None for n in optimized.walk()]
        transpile(sql, write="duckdb")


out = {}
for benchmark, schema in (("tpc-ds", TPCDS_SCHEMA), ("tpc-h", TPCH_SCHEMA)):
    pairs = list(load_sql_fixture_pairs(f"optimizer/{benchmark}/{benchmark}.sql"))
    index: dict[str, list[int]] = {}
    for i, (meta, sql, _) in enumerate(pairs):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            rec = recorder.Recorder("/src/")
            rec.start()
            try:
                prepare(meta, sql, schema)
            except Exception:
                pass
            rec.stop()
            codes = [f"{f[len('/src/'):]}|{q}|{n}" for f, q, n in rec.result()["code"]]
            with os.fdopen(w, "wb") as fh:
                fh.write(json.dumps(codes).encode())
            os._exit(0)
        os.close(w)
        data = b""
        while chunk := os.read(r, 1 << 20):
            data += chunk
        os.close(r)
        os.waitpid(pid, 0)
        for key in json.loads(data):
            index.setdefault(key, []).append(i)
    out[benchmark] = index
    print(f"{benchmark}: {len(pairs)} queries, {len(index)} code objects", file=sys.stderr)
json.dump(out, open(sys.argv[1], "w"))
