#!/usr/bin/env python3
"""Generate one-site is/== swap patches from the comparison map, for the gate:

    python3 tools/cmpmap/make_swaps.py PINNED_TREE maps/cmp_types.json OUT_DIR N_PROVEN N_UNPROVEN [SEED]

Picks sites at random (fixed seed) among those every recorded pair of which is harmless
(identity-equal types or two members of one plain enum), and among those with some other
pair, swaps `is`<->`==` / `is not`<->`!=` at that site and writes OUT_DIR/commits/*.patch.
"""

import ast
import difflib
import json
import random
import sys
from pathlib import Path

SWAP = {ast.Is: "==", ast.Eq: "is", ast.IsNot: "!=", ast.NotEq: "is not"}
TOKEN = {ast.Is: "is", ast.Eq: "==", ast.IsNot: "is not", ast.NotEq: "!="}


def harmless(pair) -> bool:
    (ta, ia, ea), (tb, ib, eb) = pair
    return (ia and ib) or (ea and eb and ta == tb)


def swap(text: str, line: int, col: int) -> str | None:
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and node.lineno == line and node.col_offset == col and len(node.ops) == 1:
            op = type(node.ops[0])
            left, right = node.left, node.comparators[0]
            if left.end_lineno != right.lineno:
                return None
            raw = text.split("\n")[left.end_lineno - 1].encode()
            between = raw[left.end_col_offset:right.col_offset].decode()
            if between.count(TOKEN[op]) != 1 or (op is ast.Is and "not" in between):
                return None
            new_between = between.replace(TOKEN[op], SWAP[op])
            lines = text.split("\n")
            new_raw = raw[:left.end_col_offset] + new_between.encode() + raw[right.col_offset:]
            lines[left.end_lineno - 1] = new_raw.decode()
            return "\n".join(lines)
    return None


def main() -> int:
    tree, map_path, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    n_proven, n_unproven = int(sys.argv[4]), int(sys.argv[5])
    rng = random.Random(int(sys.argv[6]) if len(sys.argv) > 6 else 7)
    sites = json.loads(map_path.read_text())["sites"]
    proven = sorted(s for s, p in sites.items() if all(harmless(x) for x in p))
    unproven = sorted(s for s, p in sites.items() if not all(harmless(x) for x in p))
    (out / "commits").mkdir(parents=True, exist_ok=True)
    made = []
    for pool, n, tag in ((proven, n_proven, "sp"), (unproven, n_unproven, "su")):
        rng.shuffle(pool)
        count = 0
        for site in pool:
            if count >= n:
                break
            path, line, col = site.rsplit(":", 2)
            if path.startswith("sqlglot/__main__"):
                continue
            old = (tree / path).read_text()
            new = swap(old, int(line), int(col))
            if new is None:
                continue
            name = f"{tag}{count:02d}"
            diff = difflib.unified_diff(old.split("\n"), new.split("\n"), f"a/{path}", f"b/{path}", lineterm="", n=3)
            (out / "commits" / f"{name}.patch").write_text(f"diff --git a/{path} b/{path}\n" + "\n".join(diff) + "\n")
            made.append((name, site))
            count += 1
    (out / "sites.json").write_text(json.dumps(dict(made), indent=1))
    print(f"proven sites {len(proven)}, unproven {len(unproven)}; made {made}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
