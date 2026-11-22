#!/usr/bin/env python3
"""Gate for the is/== swap proof (br/swap.py): for labeled patches, which ones it proves
harmless and whether any test fails on them outside the tests it still lists.

    python3 tools/swap_gate.py SET_DIR[:LABELS.json] ...

Labels: the set's answers.json ({"commits": {id: {"failing": [numbers]}}}) or a
tools/label.py output ({id: {"failing": [numbers]}}) after a colon. Runs on the host or in
the development image; reads the pinned tree from .cache.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from br import swap  # noqa: E402
from br.maps import Maps  # noqa: E402
from br.patch import PatchError, apply  # noqa: E402

# The pinned tree: /opt/sqlglot in an image, else .cache in this checkout or in the main
# checkout that holds this worktree (.cache is not tracked).
_NAME = "sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
PRISTINE = next((p for p in [Path("/opt/sqlglot")] + [d / ".cache" / _NAME for d in (ROOT, *ROOT.parents)]
                 if (p / "sqlglot").is_dir()), None)


def read(path: str):
    f = PRISTINE / path
    return f.read_bytes().decode("utf-8") if f.is_file() else None


def main() -> int:
    maps = Maps(ROOT / "maps")
    tests = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
    by_number = {t["number"]: t["id"] for t in tests}
    proven = misses = seen = 0
    for spec in sys.argv[1:]:
        set_dir, _, labels = spec.partition(":")
        set_dir = Path(set_dir)
        if labels:
            data = json.loads(Path(labels).read_text())
        else:
            data = json.loads((set_dir / "answers.json").read_text())["commits"]
        for commit, answer in sorted(data.items()):
            if "failing" not in answer:
                print(f"{set_dir.name}/{commit}: no label ({answer.get('error')})")
                continue
            seen += 1
            failing = {by_number[n] for n in answer["failing"]}
            text = (set_dir / "commits" / f"{commit}.patch").read_bytes().decode("utf-8", "replace")
            try:
                changes = apply(text, read)
            except PatchError as error:
                print(f"{set_dir.name}/{commit}: does not apply ({error})")
                continue
            ok, why, keep = swap.prove(changes, maps)
            missed = failing - keep if ok else set()
            proven += ok
            misses += bool(missed)
            print(f"{set_dir.name}/{commit}: failing {len(failing)} proven={ok} keep {len(keep)} "
                  f"{'MISSED ' + str(sorted(missed)[:5]) if missed else 'ok'} | {why[:150]}")
    print(f"TOTAL labeled {seen}, proven harmless {proven}, proven with a failing test not kept {misses}")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
