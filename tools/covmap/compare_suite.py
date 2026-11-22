#!/usr/bin/env python3
"""Compare in-suite coverage (suite_plugin.py) with one-test-per-process coverage
(collect.py): per test, code objects entered in the suite but not alone.

    python3 tools/covmap/compare_suite.py SUITE_DIR COLD_DIR tests.json [--write OUT.json]
"""
import glob
import json
import sys
from collections import Counter

suite_dir, cold_dir, tests_path = sys.argv[1:4]
tests = json.load(open(tests_path))["tests"]
num = {t["id"]: t["number"] for t in tests}
suite = {}
collection = set()
for p in glob.glob(f"{suite_dir}/suite-*.json"):
    d = json.load(open(p))
    table = [tuple(x) for x in d["table"]]
    for name, b in d["tests"].items():
        codes = {table[i] for i in b["code"]}
        if name in num:
            suite.setdefault(name, set()).update(codes)
        elif name == "<collection>":
            collection |= codes
extra = {}
per_obj = Counter()
for tid, codes in suite.items():
    cold = json.load(open(f"{cold_dir}/{num[tid]}.json"))
    alone = {tuple(c) for c in cold["code"]}
    diff = codes - alone
    if diff:
        extra[tid] = sorted(diff)
        per_obj.update(diff)
print(f"tests seen in suite: {len(suite)} of {len(tests)}; tests entering code they never enter alone: {len(extra)}; "
      f"distinct such code objects: {len(per_obj)}; collection-time code objects: {len(collection)}")
for obj, n in per_obj.most_common(25):
    print(f"  {n:5} tests  {obj[0].replace('/src/', '')}:{obj[1]}@{obj[2]}")
if "--write" in sys.argv:
    json.dump(extra, open(sys.argv[sys.argv.index("--write") + 1], "w"))
