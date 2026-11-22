#!/usr/bin/env python3
"""Run br.verify on one patch and explicit test ids, inside the dev image with the
pinned tree mounted at /opt/sqlglot and the repo at /app:

  python /app/tools/verify_demo.py /app/sample/commits/535z.patch ID [ID...]
"""
import json, resource, sys, time
from pathlib import Path
sys.path.insert(0, "/app")
from br.patch import apply
from br.verify import build_tree, run, remove_tree

def read(p):
    f = Path("/opt/sqlglot") / p
    return f.read_bytes().decode() if f.is_file() else None

patch = Path(sys.argv[1])
tests = {t["id"]: t for t in json.load(open("/app/sample/tests.json"))["tests"]}
t0 = time.time(); r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
tree = build_tree(Path("/tmp/v") / patch.stem, apply(patch.read_text(), read))
res = run(tree, [tests[i] for i in sys.argv[2:]], time.monotonic() + 120)
r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
remove_tree(tree)
print(json.dumps(res, indent=1))
print("wall", round(time.time() - t0, 2), "child cpu", round(r1.ru_utime + r1.ru_stime - r0.ru_utime - r0.ru_stime, 2))
