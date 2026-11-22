#!/usr/bin/env python3
"""Lines some test runs in a shared pytest process but no test runs alone.

    python3 tools/covmap/suite_only_lines.py SUITE_DIR [SUITE_DIR...] COLD_DIR OUT.json

OUT.json: {repo-relative file: [line numbers]}. A patch touching such a line may
behave differently in the organizers' run than in any single-test check, so its
candidates must be listed, never cleared by verification.
"""
import glob
import json
import sys

*suite_dirs, cold_dir, out_path = sys.argv[1:]
alone: dict[str, set] = {}
for p in glob.glob(f"{cold_dir}/*.json"):
    for f, ls in json.load(open(p))["lines"].items():
        alone.setdefault(f, set()).update(ls)
in_suite: dict[str, set] = {}
for d in suite_dirs:
    for p in glob.glob(f"{d}/suite-*.json"):
        for name, b in json.load(open(p))["tests"].items():
            if name.startswith("<"):
                continue
            for f, ls in b.get("lines", {}).items():
                in_suite.setdefault(f, set()).update(ls)
out = {}
for f, ls in in_suite.items():
    extra = sorted(ls - alone.get(f, set()))
    if extra and f.startswith("/src/"):
        out[f[len("/src/"):]] = extra
json.dump(out, open(out_path, "w"), indent=0)
print(f"{sum(len(v) for v in out.values())} suite-only lines in {len(out)} files")
for f, ls in sorted(out.items(), key=lambda kv: -len(kv[1]))[:15]:
    print(f"  {len(ls):4}  {f}  {ls[:8]}")
