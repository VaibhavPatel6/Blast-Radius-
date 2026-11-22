#!/usr/bin/env python3
"""Turn per-test records from collect.py into the committed maps under maps/.

    python3 tools/covmap/build_maps.py OUT_DIR SEED_DIR [SEED_DIR...]

Several seed directories (runs under different PYTHONHASHSEED) are merged by union.

Layout written:
  OUT_DIR/index.json      {"ids": [test ids], "sets": [[test indexes]...],
                            "listed": {dir: set}, "tests": {id: {"spawned":.., "cpu":..}}}
  OUT_DIR/files/<path>.json  per repository file:
        {"lines": {line: set}, "code": {"qualname@firstline": set}, "opened": set}
Every set is an index into "sets", so identical test sets are stored once.
Paths are relative to the repository root.
"""

import json
import os
import sys
from pathlib import Path

PREFIX = "/src/"


def main() -> int:
    args = sys.argv[1:]
    extras = []
    while "--extra" in args:  # JSON {test id: [[file, qualname, firstlineno], ...]} to add
        i = args.index("--extra")
        extras.append(Path(args[i + 1]))
        del args[i:i + 2]
    out = Path(args[0])
    seeds = [Path(p) for p in args[1:]]
    records: dict[str, dict] = {}
    for seed in seeds:
        for p in seed.glob("*.json"):
            d = json.loads(p.read_text())
            tid = d["id"]
            if not d["ok"]:
                print(f"warning: {tid} did not pass in our runner: {str(d.get('error'))[:200]}", file=sys.stderr)
            r = records.setdefault(tid, {"lines": {}, "code": set(), "opened": set(), "listed": set(),
                                         "spawned": set(), "cpu": 0.0, "ok": True, "children": 0,
                                         "touched": set()})
            r["touched"].update(tuple(c) for c in d.get("touched", []))
            r.setdefault("wrapped", set()).update(tuple(c) for c in d.get("wrapped", []))
            for f, ls in d["lines"].items():
                r["lines"].setdefault(f, set()).update(ls)
            r["code"].update(tuple(c) for c in d["code"])
            r["opened"].update(d["opened"])
            r["listed"].update(d["listed"])
            r["spawned"].update(d["spawned"])
            r["cpu"] = max(r["cpu"], d["cpu"] + d["cpu_children"])
            r["ok"] = r["ok"] and d["ok"]
            r["children"] = max(r["children"], d.get("children", 0))
    for extra in extras:
        for tid, codes in json.loads(extra.read_text()).items():
            records[tid]["code"].update(tuple(c) for c in codes)
    ids = sorted(records)
    index = {tid: i for i, tid in enumerate(ids)}
    set_ids: dict[tuple, int] = {}
    sets: list[list[int]] = []

    def intern(members) -> int:
        key = tuple(sorted(members))
        if key not in set_ids:
            set_ids[key] = len(sets)
            sets.append(list(key))
        return set_ids[key]

    def rel(path: str) -> str:
        assert path.startswith(PREFIX), path
        return path[len(PREFIX):]

    per_file: dict[str, dict] = {}

    def entry(path: str) -> dict:
        return per_file.setdefault(rel(path), {"lines": {}, "code": {}, "opened": set(), "touched": {},
                                               "wrapped": {}})

    listed: dict[str, set] = {}
    for tid, r in records.items():
        i = index[tid]
        for f, ls in r["lines"].items():
            e = entry(f)
            for line in ls:
                e["lines"].setdefault(line, set()).add(i)
        for f, qual, first in r["code"]:
            entry(f)["code"].setdefault(f"{qual}@{first}", set()).add(i)
        for f in r["opened"]:
            entry(f)["opened"].add(i)
        for f, qual, first, name in r.get("wrapped", ()):
            entry(f)["wrapped"].setdefault(f"{qual}@{first}", set()).add(name)
        for f, qual, first, name in r["touched"]:
            entry(f)["touched"].setdefault(f"{qual}@{first}", {}).setdefault(name, set()).add(i)
        for d in r["listed"]:
            listed.setdefault(d[len(PREFIX):] if d.startswith(PREFIX) else "", set()).add(i)

    files_dir = out / "files"
    for rel_path, e in sorted(per_file.items()):
        target = files_dir / (rel_path + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "lines": {str(k): intern(v) for k, v in sorted(e["lines"].items())},
            "code": {k: intern(v) for k, v in sorted(e["code"].items())},
            "opened": intern(e["opened"]),
            "touched": {k: {n: intern(v) for n, v in sorted(names.items())}
                        for k, names in sorted(e["touched"].items())},
            "wrapped": {k: sorted(v) for k, v in sorted(e["wrapped"].items())},
        }, separators=(",", ":")))
    (out / "index.json").write_text(json.dumps({
        "ids": ids,
        "sets": sets,
        "listed": {d: intern(v) for d, v in sorted(listed.items())},
        "tests": {tid: {"spawned": sorted(r["spawned"]), "children": r["children"], "cpu": round(r["cpu"], 3),
                        "ok": r["ok"]} for tid, r in records.items()},
    }, separators=(",", ":")))
    print(f"{len(ids)} tests, {len(per_file)} files, {len(sets)} distinct sets", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
