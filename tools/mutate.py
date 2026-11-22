#!/usr/bin/env python3
"""Generate one-token mutants of the pinned tree, like the sample's code edits, for
labeling (heavy-test study E6, corpus growth). Never used at run time.

    python3 tools/mutate.py OUT_DIR N [--seed S] [--files GLOB ...] [--covered-by TEST_ID --maps maps]

Mutations: and<->or, ==<->!=, is<->==, is not<->!=, <<->=, ><->>=, True<->False,
an integer +1, `not x` -> `x`. Each patch changes one token on one line inside a
function body or a class/module-level statement, chosen at random, and is written in
`git diff` format with 3 lines of context.
"""
import argparse
import difflib
import io
import json
import random
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRISTINE = ROOT / ".cache" / "sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa"

SWAPS = {"and": "or", "or": "and", "==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">",
         "True": "False", "False": "True", "is": "=="}


def candidates_in(text: str):
    """(line, col_start, col_end, replacement) for every mutable token."""
    out = []
    toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    for i, tok in enumerate(toks):
        s = tok.string
        if tok.type == tokenize.NAME and s in ("and", "or", "True", "False"):
            out.append((tok.start[0], tok.start[1], tok.end[1], SWAPS[s]))
        elif tok.type == tokenize.NAME and s == "is":
            nxt = toks[i + 1].string if i + 1 < len(toks) else ""
            if nxt == "not":
                out.append((tok.start[0], tok.start[1], toks[i + 1].end[1], "!="))
            else:
                out.append((tok.start[0], tok.start[1], tok.end[1], "=="))
        elif tok.type == tokenize.NAME and s == "not":
            prev = toks[i - 1].string if i else ""
            if prev not in ("is",) and toks[i + 1].string != "in":
                out.append((tok.start[0], tok.start[1], toks[i + 1].start[1], ""))
        elif tok.type == tokenize.OP and s in ("==", "!=", "<", "<=", ">", ">="):
            out.append((tok.start[0], tok.start[1], tok.end[1], SWAPS[s]))
        elif tok.type == tokenize.NUMBER and s.isdigit() and tok.start[0] == tok.end[0]:
            out.append((tok.start[0], tok.start[1], tok.end[1], str(int(s) + 1)))
    return out


def function_lines(text: str, rel: str) -> set[int]:
    """Lines whose innermost code object is a function (CO_OPTIMIZED)."""
    import types
    owner = {}

    def walk(code):
        for _, _, line in code.co_lines():
            if line is not None:
                owner[line] = code
        for c in code.co_consts:
            if isinstance(c, types.CodeType):
                walk(c)
    walk(compile(text, rel, "exec"))
    return {line for line, code in owner.items() if code.co_flags & 0x01}


def make_patch(rel: str, old: str, new: str) -> str:
    diff = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                f"a/{rel}", f"b/{rel}", n=3)
    return f"diff --git a/{rel} b/{rel}\n" + "".join(diff)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("n", type=int)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--files", nargs="*", default=["sqlglot/**/*.py"])
    ap.add_argument("--covered-by", help="only lines this test executed (needs --maps)")
    ap.add_argument("--maps", type=Path, default=ROOT / "maps")
    ap.add_argument("--prefix", default="m")
    ap.add_argument("--functions-only", action="store_true", help="only lines inside function bodies")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    files = sorted({p for g in args.files for p in PRISTINE.glob(g) if p.is_file()})
    covered = None
    if args.covered_by:
        sys.path.insert(0, str(ROOT))
        from br.maps import Maps
        maps = Maps(args.maps)
        covered = {}
        for f in files:
            rel = f.relative_to(PRISTINE).as_posix()
            e = maps._file(rel)
            if not e:
                continue
            lines = {int(l) for l, si in e["lines"].items() if args.covered_by in maps._ids(si)}
            if lines:
                covered[rel] = lines
        files = [f for f in files if f.relative_to(PRISTINE).as_posix() in covered]
    commits = args.out / "commits"
    commits.mkdir(parents=True, exist_ok=True)
    meta = {}
    made = 0
    attempts = 0
    while made < args.n and attempts < args.n * 50:
        attempts += 1
        f = rng.choice(files)
        rel = f.relative_to(PRISTINE).as_posix()
        text = f.read_text()
        cands = candidates_in(text)
        if covered is not None:
            cands = [c for c in cands if c[0] in covered[rel]]
        if args.functions_only:
            in_function = function_lines(text, rel)
            cands = [c for c in cands if c[0] in in_function]
        if not cands:
            continue
        line, a, b, rep = rng.choice(cands)
        lines = text.splitlines(keepends=True)
        old_line = lines[line - 1]
        new_line = old_line[:a] + rep + old_line[b:]
        if rep == "":  # `not x` -> `x`: drop the space too
            new_line = old_line[:a] + old_line[b:]
        lines[line - 1] = new_line
        new = "".join(lines)
        try:
            compile(new, rel, "exec")
        except SyntaxError:
            continue
        cid = f"{args.prefix}{made:03d}"
        (commits / f"{cid}.patch").write_text(make_patch(rel, text, new))
        meta[cid] = {"file": rel, "line": line, "old": old_line.strip(), "new": new_line.strip()}
        made += 1
    (args.out / "mutants.json").write_text(json.dumps(meta, indent=1))
    print(f"{made} mutants in {commits}")


if __name__ == "__main__":
    main()
