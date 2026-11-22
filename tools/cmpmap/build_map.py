#!/usr/bin/env python3
"""Merge the recorder's per-process files (cmprec.py) into maps/cmp_types.json:

    python3 tools/cmpmap/build_map.py OUT_DIR maps/cmp_types.json

{"sites": {"path:line:col": [[[type, identity, plain_enum], [type, identity, plain_enum]], ...]}}
"""

import json
import sys
from pathlib import Path


def main() -> int:
    sites: dict[str, set] = {}
    files = sorted(Path(sys.argv[1]).glob("cmp-*.jsonl"))
    for f in files:
        for line in f.read_text().splitlines():
            try:
                site, a, b = json.loads(line)
            except ValueError:
                continue  # a line cut short by a killed process
            sites.setdefault(site, set()).add((tuple(a), tuple(b)))
    out = {"sites": {s: sorted([list(a), list(b)] for a, b in pairs) for s, pairs in sorted(sites.items())}}
    Path(sys.argv[2]).write_text(json.dumps(out, separators=(",", ":")))
    print(f"{len(files)} process records, {len(out['sites'])} sites", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
