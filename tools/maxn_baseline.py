#!/usr/bin/env python3
"""Pinned-side record of the test_max_nodes stand-in (br/maxn.py), computed at image
build so the run does not pay for it:

    python /app/tools/maxn_baseline.py build /app/maps/maxn_pinned.json

Run after the pinned tree is unpacked and compiled at /opt/sqlglot. At run time br/maxn.py
loads it when its Python version and tree path match.

    python tools/maxn_baseline.py diff A.json B.json ...    (reproducibility check)
"""

import json
import sys

sys.path.insert(0, "/app")


def build(out: str) -> int:
    from br import maxn

    record = maxn.run("/opt/sqlglot", 600, coverage=True)
    if record is None or not maxn._expected_pass(record):
        print("pinned stand-in failed or does not pass; no baseline written", file=sys.stderr)
        return 1
    with open(out, "w") as fh:
        json.dump({"python": sys.version, "tree": "/opt/sqlglot", "record": record}, fh)
    print(f"wrote {out}: {len(record['calls'])} functions, {len(record['coverage'])} lines, peak {record['peak']}",
          file=sys.stderr)
    return 0


def diff(paths: list[str]) -> int:
    records = [json.load(open(p))["record"] for p in paths]
    bad = 0
    for path, r in zip(paths[1:], records[1:]):
        for key in records[0]:
            if r.get(key) != records[0][key]:
                bad += 1
                extra = ""
                if key == "peak":
                    extra = f" {records[0][key]} vs {r[key]}"
                print(f"{path}: {key} differs{extra}")
    print(f"{len(paths)} records, {bad} differences")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(build(sys.argv[2]) if sys.argv[1] == "build" else diff(sys.argv[2:]))
