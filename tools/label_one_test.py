#!/usr/bin/env python3
"""Label one test on a set of patches: run it for real (pytest, one process per patch) on
the patched tree and record pass or fail. Development image, this checkout at /app, the
pinned tree at /opt/sqlglot:

    python /app/tools/label_one_test.py TEST_ID OUT.json SET_DIR/commits [JOBS]

A patch that does not apply, a timeout or a crash counts as a failure. The pinned tree is
run first as a control and must pass.
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, "/app")
from br.patch import PatchError, apply  # noqa: E402
from br.verify import build_tree, remove_tree  # noqa: E402

PINNED = Path("/opt/sqlglot")


def read_pinned(path):
    f = PINNED / path
    return f.read_bytes().decode("utf-8") if f.is_file() else None


def run(test_id: str, tree: Path) -> dict:
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="1", PYTHONDONTWRITEBYTECODE="1")
    t0 = time.monotonic()
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test_id],
                              cwd=tree, env=env, capture_output=True, timeout=600)
        out = proc.stdout.decode("utf-8", "replace")
        status = "passed" if proc.returncode == 0 and " passed" in out else "failed"
        tail = out.strip().splitlines()[-1] if out.strip() else ""
    except subprocess.TimeoutExpired:
        status, tail = "failed", "timeout"
    return {"status": status, "seconds": round(time.monotonic() - t0, 1), "tail": tail[-200:]}


def label(test_id: str, patch: Path) -> tuple[str, dict]:
    name = patch.stem
    tree = Path("/tmp/label") / name
    try:
        changes = apply(patch.read_bytes().decode("utf-8"), read_pinned)
        remove_tree(tree)
        build_tree(tree, changes)
    except (PatchError, OSError, UnicodeDecodeError) as error:
        return name, {"status": "failed", "tail": f"patch does not apply: {error!r}"[:200]}
    try:
        return name, run(test_id, tree)
    finally:
        remove_tree(tree)


def main() -> int:
    test_id, out, commits = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    control = run(test_id, PINNED)
    print(f"pinned: {control}", flush=True)
    if control["status"] != "passed":
        return 1
    results = {}
    with ThreadPoolExecutor(jobs) as pool:
        for name, result in pool.map(lambda p: label(test_id, p), sorted(commits.glob("*.patch"))):
            results[name] = result
            print(f"{name}: {result}", flush=True)
            out.write_text(json.dumps({"test": test_id, "control": control, "results": results}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
