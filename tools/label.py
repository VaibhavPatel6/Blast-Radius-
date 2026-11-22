#!/usr/bin/env python3
"""Labeler: apply each patch to a fresh copy of the pinned tree, run the whole
suite with real pytest in the development image, and report failing test numbers.

    python3 tools/label.py --tests sample/tests.json --out labels.json PATCH...

Runs on the host. Patches are applied with `git apply` in a directory outside
any git repository, so git behaves like plain `patch`. A test counts as failing
if any pytest report for it failed (setup, call, teardown or a subtest), if its
file failed to collect, or if pytest never reported it at all.
"""

import argparse
import concurrent.futures as cf
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRISTINE = ROOT / ".cache" / "sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
DEFAULT_WORK = Path(os.environ.get("LABEL_WORK", "/private/tmp/br-label"))


def label_one(patch: Path, args, tests) -> dict:
    cid = patch.stem
    work = args.work / cid
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(PRISTINE, work, symlinks=True)
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=work, capture_output=True, text=True)
    if top.returncode == 0:
        raise SystemExit(f"work dir {work} is inside a git repository ({top.stdout.strip()}); refusing")
    applied = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch.resolve())], cwd=work,
                             capture_output=True, text=True)
    if applied.returncode != 0:
        return {"commit": cid, "error": "apply_failed", "detail": applied.stderr[-2000:]}
    started = time.time()
    cmd = ["docker", "run", "--rm", "--platform", f"linux/{args.arch}",
           "-v", f"{work}:/src", "-v", f"{ROOT / 'tools'}:/tools:ro",
           "-e", "PYTHONPATH=/src:/tools", "-e", "OUTCOME_DIR=/src/.outcome", "-e", "SKIP_INTEGRATION=0",
           # sqlglot's tests size process pools from the CPU count; Docker reports the host's.
           "-e", f"PYTHON_CPU_COUNT={args.cpu_count}", "--cpus", str(args.cpus), "--memory", args.memory,
           f"br-dev:{args.arch}", "python", "-m", "pytest", "-p", "outcome_plugin", "-p", "no:cacheprovider",
           "-n", str(args.n), "-q", "--timeout", str(args.timeout), "tests"]
    run = subprocess.run(cmd, capture_output=True, text=True)
    (work.parent / f"{cid}.log").write_text(run.stdout[-200000:] + "\n--stderr--\n" + run.stderr[-50000:])
    failed, seen, collect_errors = set(), set(), set()
    for p in glob.glob(str(work / ".outcome" / "*.json")):
        d = json.loads(Path(p).read_text())
        failed |= set(d["failed"])
        seen |= set(d["seen"])
        collect_errors |= set(d["collect_errors"])
    if "crashed" in run.stdout or "INTERNALERROR" in run.stdout or run.returncode not in (0, 1):
        return {"commit": cid, "error": "pytest_crashed", "pytest_exit": run.returncode,
                "detail": run.stdout[-1500:]}
    numbers = set()
    unseen = []
    for t in tests:
        tid = t["id"]
        if tid in failed or any(tid == ce or tid.startswith(ce + "::") or t["file"] == ce for ce in collect_errors):
            numbers.add(t["number"])
        elif tid not in seen:
            numbers.add(t["number"])
            unseen.append(tid)
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return {"commit": cid, "failing": sorted(numbers), "collect_errors": sorted(collect_errors),
            "unseen": unseen, "pytest_exit": run.returncode, "seconds": round(time.time() - started, 1),
            "unknown_failed_ids": sorted(failed - {t["id"] for t in tests})}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("patches", nargs="+", type=Path)
    ap.add_argument("--tests", type=Path, default=ROOT / "sample" / "tests.json")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--arch", default="amd64")
    ap.add_argument("-n", type=int, default=3, help="pytest-xdist workers per container")
    ap.add_argument("-j", type=int, default=3, help="containers at once")
    ap.add_argument("--timeout", type=int, default=900, help="pytest-timeout seconds per test (hang guard)")
    ap.add_argument("--cpu-count", type=int, default=2, help="PYTHON_CPU_COUNT inside the container")
    ap.add_argument("--cpus", type=float, default=3)
    ap.add_argument("--memory", default="3g")
    ap.add_argument("--work", type=Path, default=DEFAULT_WORK)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    tests = json.loads(args.tests.read_text())["tests"]
    results = json.loads(args.out.read_text()) if args.out.exists() else {}
    todo = [p for p in args.patches if p.stem not in results]
    with cf.ThreadPoolExecutor(args.j) as pool:
        futures = {pool.submit(label_one, p, args, tests): p for p in todo}
        for fut in cf.as_completed(futures):
            r = fut.result()
            results[r["commit"]] = r
            args.out.write_text(json.dumps(results, indent=1, sort_keys=True))
            print(f"{r['commit']}: {len(r.get('failing', []))} failing, {r.get('seconds')} s, "
                  f"exit {r.get('pytest_exit')} {r.get('error', '')}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
