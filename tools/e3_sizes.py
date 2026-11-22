#!/usr/bin/env python3
"""E3: how large are candidate sets, if a one-token edit lands on a random line of sqlglot/?

    python3 tools/e3_sizes.py MAPS_DIR [--tests sample/tests.json]

Each line of sqlglot/*.py that holds instructions is attributed to its innermost code
object; its candidates are the tests that entered that code object. Files weigh
equally. Reports count and regular (non-heavy) seconds of candidates: median, p90, mean,
split by function-level and class/module-level lines, plus how often each heavy test is
a candidate.
"""
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from br.maps import Maps  # noqa: E402

PRISTINE = ROOT / ".cache" / "sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
HEAVY = 3.0


def walk(code, out):
    for _, _, line in code.co_lines():
        if line is not None:
            out[line] = code  # children are visited after, so the innermost wins
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            walk(c, out)


def wquant(pairs, q):
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    acc = 0
    for v, w in pairs:
        acc += w
        if acc >= q * total:
            return v
    return pairs[-1][0] if pairs else 0


def main():
    maps = Maps(Path(sys.argv[1]))
    tests = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
    secs = {t["id"]: t["seconds"] for t in tests}
    heavy = {t["id"] for t in tests if t["seconds"] >= HEAVY}
    rows = {"function": [], "class/module": []}
    heavy_hits = {h: 0.0 for h in heavy}
    total_w = 0.0
    files = sorted((PRISTINE / "sqlglot").rglob("*.py"))
    for f in files:
        rel = f.relative_to(PRISTINE).as_posix()
        code = compile(f.read_text(), "/src/" + rel, "exec")
        lines = {}
        walk(code, lines)
        if not lines:
            continue
        w = 1.0 / len(lines)
        cache = {}
        for line, c in lines.items():
            key = (c.co_qualname, c.co_firstlineno)
            if key not in cache:
                cache[key] = maps.entered(rel, *key)
            cands = cache[key]
            kind = "function" if c.co_flags & 0x01 else "class/module"  # CO_OPTIMIZED: a function
            reg = sum(secs.get(t, 0) for t in cands if t not in heavy)
            rows[kind].append((len(cands), reg, w))
            total_w += w
            for h in heavy & cands:
                heavy_hits[h] += w
    for kind, r in rows.items():
        if not r:
            continue
        n = [(a, w) for a, _, w in r]
        s = [(b, w) for _, b, w in r]
        tw = sum(w for _, _, w in r)
        print(f"{kind:13} weight {tw:6.1f}  count median {wquant(n, .5):5} p90 {wquant(n, .9):5} "
              f"mean {sum(a * w for a, w in n) / tw:7.1f} | regular s median {wquant(s, .5):6.2f} "
              f"p90 {wquant(s, .9):6.2f} mean {sum(a * w for a, w in s) / tw:6.2f} | "
              f"zero-candidate share {sum(w for a, w in n if a == 0) / tw:.2f}")
    for h, w in sorted(heavy_hits.items(), key=lambda x: -x[1]):
        print(f"heavy candidate share {w / total_w:.2f}  {h}")


if __name__ == "__main__":
    main()
