#!/usr/bin/env python3
"""Write each patch's candidate test ids (or "ALL") as JSON, for focused labeling.
Run under the image's Python (3.13):

  docker run --rm --platform linux/arm64 -v $PWD:/app br-dev:arm64 \
      python /app/tools/candidates_json.py /app/maps SET_DIR OUT.json
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from br.analyze import Unbounded, candidates  # noqa: E402
from br.maps import Maps  # noqa: E402
from br.patch import PatchError, apply  # noqa: E402

PRISTINE = ROOT / ".cache" / "sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def read(p):
    f = PRISTINE / p
    return f.read_bytes().decode("utf-8") if f.is_file() else None


maps = Maps(Path(sys.argv[1]))
out = {}
for patch in sorted((Path(sys.argv[2]) / "commits").glob("*.patch")):
    try:
        ids, _ = candidates(apply(patch.read_bytes().decode("utf-8"), read), maps)
        out[patch.stem] = sorted(ids)
    except (PatchError, Unbounded, UnicodeDecodeError):
        out[patch.stem] = "ALL"
Path(sys.argv[3]).write_text(json.dumps(out))
print(f"{len(out)} patches, {sum(v == 'ALL' for v in out.values())} ALL")
