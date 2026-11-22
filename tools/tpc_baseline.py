#!/usr/bin/env python3
"""Pinned-side rows of the TPC check (br/tpc.py), computed at image build so the run does
not pay for them:

    python /app/tools/tpc_baseline.py build /app/maps/tpc_pinned.json

Run it after the pinned tree is unpacked and compiled at /opt/sqlglot and br/ and maps/ are
in /app, with the same environment the selector runs under (SKIP_INTEGRATION=0,
PYTHON_CPU_COUNT=1). At run time br/tpc.py loads the file when its Python version and tree
path match, and otherwise computes the pinned side itself as before.

    python tools/tpc_baseline.py diff A.json B.json [C.json ...]

prints which rows differ between baselines made in unrelated processes (the
reproducibility check: different hash seeds, environment sizes, process histories).
"""

import json
import sys

sys.path.insert(0, "/app")


def build(out: str) -> int:
    import pytest  # noqa: F401 - as in the selector, before anything forks

    from br import tpc

    rows = {}
    for benchmark in ("tpc-h", "tpc-ds"):
        got = tpc._run("/opt/sqlglot", benchmark, 900)
        if got is None or any("error" in r for r in got):
            print(f"{benchmark}: pinned side failed; no baseline written", file=sys.stderr)
            return 1
        rows[benchmark] = got
    with open(out, "w") as fh:
        json.dump({"python": sys.version, "tree": "/opt/sqlglot", "rows": rows}, fh)
    print(f"wrote {out}: {', '.join(f'{b} {len(r)} rows' for b, r in rows.items())}", file=sys.stderr)
    return 0


def diff(paths: list[str]) -> int:
    data = [json.load(open(p)) for p in paths]
    bad = 0
    for benchmark in ("tpc-h", "tpc-ds"):
        first = data[0]["rows"][benchmark]
        for other, path in zip(data[1:], paths[1:]):
            rows = other["rows"][benchmark]
            if len(rows) != len(first):
                print(f"{benchmark} {path}: {len(rows)} rows vs {len(first)}")
                bad += 1
                continue
            for a, b in zip(first, rows):
                if a != b:
                    bad += 1
                    keys = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
                    if a.get("step") == "import" and "state" in keys:
                        keys += sorted(k for k in a["state"] if a["state"][k] != b["state"].get(k))[:5]
                    print(f"{benchmark} {path}: {a.get('step')} query {a.get('query')} differs in {keys}")
    print(f"{len(paths)} baselines, {bad} differing rows")
    return 1 if bad else 0


if __name__ == "__main__":
    if sys.argv[1] == "build":
        sys.exit(build(sys.argv[2]))
    sys.exit(diff(sys.argv[2:]))
