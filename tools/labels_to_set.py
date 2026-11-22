#!/usr/bin/env python3
"""Build a labeled set directory (commits/ + answers.json) from labeler output, so
tools/soundness.py and tools/measure.py can run on it.

    python3 tools/labels_to_set.py LABELS.json PATCH_DIR OUT_SET_DIR [KINDS.json]

Only commits with a clean label (no error) are included.
"""
import json
import shutil
import sys
from pathlib import Path

labels = json.loads(Path(sys.argv[1]).read_text())
patches = Path(sys.argv[2])
out = Path(sys.argv[3])
rest = [a for a in sys.argv[4:] if not a.startswith("--")]
kinds = json.loads(Path(rest[0]).read_text()) if rest else {}
(out / "commits").mkdir(parents=True, exist_ok=True)
answers = {}
for cid, v in labels.items():
    src = patches / f"{cid}.patch"
    # Focused labels (pytest on the importers only) are complete when the importers include
    # every test the patch can break, as for class-level edits; pass --focused-ok for those.
    if v.get("error") or (v.get("focused") and "--focused-ok" not in sys.argv) or not src.exists():
        continue
    shutil.copy(src, out / "commits" / src.name)
    answers[cid] = {"failing": v["failing"], "kind": kinds.get(cid, {}).get("kind", "corpus")}
(out / "answers.json").write_text(json.dumps({"commits": answers}, indent=1))
print(f"{len(answers)} labeled commits in {out}")
