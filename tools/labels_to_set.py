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
kinds = json.loads(Path(sys.argv[4]).read_text()) if len(sys.argv) > 4 else {}
(out / "commits").mkdir(parents=True, exist_ok=True)
answers = {}
for cid, v in labels.items():
    src = patches / f"{cid}.patch"
    if v.get("error") or v.get("focused") or not src.exists():
        continue
    shutil.copy(src, out / "commits" / src.name)
    answers[cid] = {"failing": v["failing"], "kind": kinds.get(cid, {}).get("kind", "corpus")}
(out / "answers.json").write_text(json.dumps({"commits": answers}, indent=1))
print(f"{len(answers)} labeled commits in {out}")
