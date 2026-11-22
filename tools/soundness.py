#!/usr/bin/env python3
"""E4/G1: for each labeled patch, are all failing tests among the candidates?

    python3 tools/soundness.py MAPS_DIR SET_DIR [SET_DIR...]

Prints per commit: failing count, candidate count and seconds, misses, and reasons.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from br.analyze import Unbounded, candidates  # noqa: E402
from br.maps import Maps  # noqa: E402
from br.patch import PatchError, apply  # noqa: E402
from selector import duration_ms, pack  # noqa: E402

PRISTINE = ROOT / ".cache" / "sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def read(p):
    f = PRISTINE / p
    return f.read_bytes().decode("utf-8") if f.is_file() else None


def main():
    maps = Maps(Path(sys.argv[1]))
    total_bad = 0
    blind_total = 0
    for set_dir in map(Path, sys.argv[2:]):
        tests_path = set_dir / "tests.json"
        if not tests_path.exists():
            tests_path = ROOT / "sample" / "tests.json"
        tests = json.loads(tests_path.read_text())["tests"]
        by_number = {t["number"]: t for t in tests}
        answers = json.loads((set_dir / "answers.json").read_text())["commits"]
        unmapped = {t["id"] for t in tests} - set(maps.ids)
        for cid, ans in sorted(answers.items()):
            text = (set_dir / "commits" / f"{cid}.patch").read_bytes().decode("utf-8", "replace")
            failing = {by_number[n]["id"] for n in ans["failing"]}
            try:
                cands, reasons = candidates(apply(text, read), maps)
                cands |= unmapped
                rung = "cand"
            except (PatchError, Unbounded) as e:
                cands, reasons, rung = {t["id"] for t in tests}, [repr(e)], "L3"
            missed = failing - cands
            secs = sum(t["seconds"] for t in tests if t["id"] in cands)
            heavy = sorted(t["id"].rsplit("::", 1)[1] for t in tests if t["id"] in cands and t["seconds"] >= 3)
            total_bad += bool(missed)
            durations = {t["number"]: duration_ms(t["seconds"]) for t in tests}
            nums = [t["number"] for t in tests if t["id"] in cands]
            workers = pack(nums, durations)
            blind_total += (max(sum(durations[n] for n in w) for w in workers) + 2000 * len(workers)) if workers else 0
            blind_total += 300000 if missed else 0
            print(f"{cid} {ans.get('kind', '?'):8} fail {len(failing):3} cand {len(cands):4} {secs:7.2f}s {rung} "
                  f"{'MISSED ' + str(sorted(missed)) if missed else 'ok'} heavy={heavy}  | {'; '.join(reasons)[:200]}")
    print(f"commits with misses: {total_bad}   cost if candidates listed blindly: {blind_total / 1000:.3f} s")
    return 1 if total_bad else 0


if __name__ == "__main__":
    sys.exit(main())
