#!/usr/bin/env python3
"""Measuring command: run the image under the scorer's Docker flags on labeled sets
and report, per commit, our cost against the oracle, plus misses and container CPU.

    python3 tools/measure.py [--image blast-radius-entry] [--runs 1] SET_DIR...

A set directory holds `commits/*.patch`, `tests.json` (or uses the sample's) and
`answers.json`. CPU is the container's own cgroup counter (cpu.stat usage_usec),
the same kind of number the scorer charges.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import score  # noqa: E402
from selector import duration_ms, pack  # noqa: E402

FLAGS = ["--rm", "--platform", "linux/amd64", "--network", "none", "--cpus", "2",
         "--memory", "4096m", "--memory-swap", "4096m", "--pids-limit", "256", "--read-only",
         "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
         "--security-opt", "no-new-privileges", "--user", "65534:65534", "--env", "HOME=/tmp"]


def image_cmd(image: str) -> list[str]:
    out = subprocess.run(["docker", "inspect", "--format", "{{json .Config.Cmd}}", image],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def run_once(image: str, set_dir: Path, tests_path: Path):
    cmd = " ".join(f"'{c}'" for c in image_cmd(image))
    shell = (f"start=$(grep usage_usec /sys/fs/cgroup/cpu.stat | cut -d' ' -f2); {cmd}; rc=$?; "
             "end=$(grep usage_usec /sys/fs/cgroup/cpu.stat | cut -d' ' -f2); "
             "echo \"MEASURE rc=$rc cpu_usec=$end\" >&2")
    run = subprocess.run(
        ["docker", "run", *FLAGS, "-v", f"{set_dir / 'commits'}:/data/commits:ro",
         "-v", f"{tests_path}:/data/tests.json:ro", "--entrypoint", "sh", image, "-c", shell],
        capture_output=True)
    stderr = run.stderr.decode("utf-8", "replace")
    cpu = rc = None
    for line in stderr.splitlines():
        if line.startswith("MEASURE "):
            fields = dict(kv.split("=") for kv in line.split()[1:])
            cpu, rc = int(fields["cpu_usec"]), int(fields["rc"])
    return run.stdout, stderr, cpu, rc


def report(set_dir: Path, stdout: bytes, tests: list[dict], answers: dict, cpu, rc) -> dict:
    durations = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    evaluation = score.evaluate_run(stdout, tests, answers)
    if not evaluation["ok"]:
        print(f"{set_dir.name}: REJECTED {evaluation['code']} line {evaluation['line']} (exit {rc})")
        return {"ok": False}
    rows = []
    lines = {json.loads(l)["commit"]: json.loads(l)["workers"] for l in stdout.decode().splitlines()}
    for commit, ans in sorted(answers.items()):
        workers = [w for w in lines[commit] if w]
        ours = (max(sum(durations[n] for n in w) for w in workers) + 2000 * len(workers)) if workers else 0
        listed = {n for w in workers for n in w}
        miss = any(n not in listed for n in ans["failing"])
        best = pack(ans["failing"], durations)
        oracle = (max(sum(durations[n] for n in w) for w in best) + 2000 * len(best)) if best else 0
        rows.append((ours + (300000 if miss else 0) - oracle, commit, ans.get("kind", "?"), oracle, ours, miss,
                     len(listed), len(ans["failing"])))
    sim = evaluation["simulated_milliseconds"]
    oracle_total = sum(r[3] for r in rows)
    misses = [r[1] for r in rows if r[5]]
    print(f"\n== {set_dir.name}: {len(rows)} commits, exit {rc}")
    print(f"simulated {sim / 1000:.3f} s  oracle {oracle_total / 1000:.3f} s  gap {(sim - oracle_total) / 1000:.3f} s"
          f"  cpu {cpu / 1e6 if cpu is not None else float('nan'):.3f} s  misses {len(misses)} {misses}")
    print("largest excess:  commit kind  oracle  ours  listed/failing  miss")
    for excess, commit, kind, oracle, ours, miss, nlisted, nfail in sorted(rows, reverse=True)[:5]:
        print(f"  {excess / 1000:8.3f}  {commit:6} {kind:8} {oracle / 1000:7.3f} {ours / 1000:7.3f}  "
              f"{nlisted}/{nfail}  {'MISS' if miss else ''}")
    return {"ok": True, "simulated": sim, "oracle": oracle_total, "cpu": cpu, "misses": misses}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sets", nargs="*", type=Path, default=[ROOT / "sample"])
    ap.add_argument("--image", default="blast-radius-entry")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--stderr", action="store_true", help="print the selector's stderr")
    args = ap.parse_args()
    bad = 0
    for set_dir in args.sets:
        set_dir = set_dir.resolve()
        tests_path = set_dir / "tests.json"
        if not tests_path.exists():
            tests_path = ROOT / "sample" / "tests.json"
        tests = json.loads(tests_path.read_text())["tests"]
        answers = json.loads((set_dir / "answers.json").read_text())["commits"]
        for _ in range(args.runs):
            stdout, stderr, cpu, rc = run_once(args.image, set_dir, tests_path)
            if args.stderr:
                sys.stderr.write(stderr)
            r = report(set_dir, stdout, tests, answers, cpu, rc)
            bad += (not r["ok"]) or bool(r.get("misses")) or rc != 0
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
