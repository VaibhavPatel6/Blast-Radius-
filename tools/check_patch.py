#!/usr/bin/env python3
"""Check br/patch.py against `git apply` on every patch given: same resulting files."""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from br.patch import apply, PatchError
PRISTINE = ROOT / ".cache" / "sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa"

def read(p):
    f = PRISTINE / p
    return f.read_bytes().decode("utf-8") if f.is_file() else None

bad = 0
scratch = Path(sys.argv[1]); scratch.mkdir(parents=True, exist_ok=True)
for patch in sys.argv[2:]:
    patch = Path(patch)
    text = patch.read_bytes().decode("utf-8")
    try:
        changes = apply(text, read)
    except PatchError as e:
        ours = f"PatchError {e}"
        changes = None
    work = scratch / patch.stem
    shutil.rmtree(work, ignore_errors=True)
    files = set()
    for c in (changes or []):
        files |= {p for p in (c.old_path, c.new_path) if p}
    # git apply on a minimal copy holding only the touched files
    work.mkdir()
    for f in files:
        if (PRISTINE / f).exists():
            (work / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(PRISTINE / f, work / f)
    g = subprocess.run(["git", "apply", str(patch.resolve())], cwd=work, capture_output=True, text=True)
    if changes is None:
        print(f"{patch.stem}: ours {ours}; git rc {g.returncode}")
        bad += g.returncode == 0
        continue
    for c in changes:
        if c.new_path:
            got = (work / c.new_path).read_bytes().decode("utf-8") if (work / c.new_path).exists() else None
            if got != c.new_text:
                print(f"{patch.stem}: {c.new_path} differs from git apply"); bad += 1
        if c.old_path and c.old_path != c.new_path and (work / c.old_path).exists():
            print(f"{patch.stem}: {c.old_path} still exists after git apply"); bad += 1
print(f"{len(sys.argv) - 2} patches, {bad} disagreements")
sys.exit(1 if bad else 0)
