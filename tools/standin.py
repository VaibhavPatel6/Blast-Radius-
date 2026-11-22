#!/usr/bin/env python3
"""A local stand-in for the practice score.

    python3 tools/standin.py collect IMAGE SET_DIR...      # per-commit cost of a build, cached
    python3 tools/standin.py fit [--counts ALL,MAXN,TPCDS]  # choose 40 patches matching known totals
    python3 tools/standin.py predict IMAGE                  # a build's predicted practice score

Builds with known practice totals (simulated seconds, all 40 commits):
  br-clean3 = fb265ab (S2) 267.897; br-clean5 = 8b26385 (S3) 199.269; br-clean7 = a1541d3 (S4') 199.269.
Every labeled patch is scored by each build (batches of 8 so the time guard never fires),
misses counted at 300 s. `fit` searches for a 40-patch subset whose totals under the three
builds match those numbers (and, when given, the diagnostic's counts of answers that list
all tests, test_max_nodes, test_execute_tpcds under S4'). It is a proxy, not the hidden set.
"""

import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from selector import duration_ms  # noqa: E402

CACHE = ROOT / "notes" / "standin"
KNOWN = {"br-clean3": 267.897, "br-clean5": 199.269, "br-clean7": 199.269}
FLAGS = ["--rm", "--platform", "linux/amd64", "--network", "none", "--cpus", "2", "--memory", "4096m",
         "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--user", "65534:65534", "--env", "HOME=/tmp"]
TESTS = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
DUR = {t["number"]: duration_ms(t["seconds"]) for t in TESTS}
NUM = {t["id"]: t["number"] for t in TESTS}
MAXN = NUM["tests/test_parser.py::TestParser::test_max_nodes"]
TPCDS = NUM["tests/test_executor.py::TestExecutor::test_execute_tpcds"]


def cost(workers) -> int:
    ws = [w for w in workers if w]
    return (max(sum(DUR[n] for n in w) for w in ws) + 2000 * len(ws)) if ws else 0


def category(workers) -> str:
    listed = {n for w in workers for n in w}
    if len(listed) >= len(DUR) - 20:
        return "ALL"
    if MAXN in listed:
        return "MAXN"
    if TPCDS in listed:
        return "TPCDS"
    return "-"


def collect(image: str, sets: list[Path]) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    out_path = CACHE / f"{image}.json"
    data = json.loads(out_path.read_text()) if out_path.exists() else {}
    for set_dir in sets:
        answers = json.loads((set_dir / "answers.json").read_text())["commits"]
        todo = [c for c in sorted(answers) if f"{set_dir.name}/{c}" not in data]
        for i in range(0, len(todo), 8):
            batch = ROOT / "notes" / "standin" / "batch"
            shutil.rmtree(batch, ignore_errors=True)
            (batch / "commits").mkdir(parents=True)
            for c in todo[i:i + 8]:
                shutil.copy(set_dir / "commits" / f"{c}.patch", batch / "commits")
            run = subprocess.run(["docker", "run", *FLAGS, "-v", f"{batch / 'commits'}:/data/commits:ro",
                                  "-v", f"{ROOT / 'sample' / 'tests.json'}:/data/tests.json:ro", image],
                                 capture_output=True, text=True)
            for line in run.stdout.splitlines():
                row = json.loads(line)
                c = row["commit"]
                listed = {n for w in row["workers"] for n in w}
                miss = any(n not in listed for n in answers[c]["failing"])
                data[f"{set_dir.name}/{c}"] = {"cost": cost(row["workers"]) + (300000 if miss else 0),
                                               "category": category(row["workers"]), "miss": miss,
                                               "kind": answers[c].get("kind", "?")}
            out_path.write_text(json.dumps(data, indent=0))
            print(f"{image} {set_dir.name}: {min(i + 8, len(todo))}/{len(todo)}", file=sys.stderr, flush=True)


def load() -> dict:
    return {img: json.loads((CACHE / f"{img}.json").read_text()) for img in KNOWN if (CACHE / f"{img}.json").exists()}


def fit(counts=None, seed: int = 1, rounds: int = 200000) -> None:
    data = load()
    keys = sorted(set.intersection(*(set(d) for d in data.values())))
    keys = [k for k in keys if not any(d[k]["miss"] for d in data.values())]
    rng = random.Random(seed)

    def error(subset):
        e = sum((sum(data[img][k]["cost"] for k in subset) / 1000 - KNOWN[img]) ** 2 for img in data)
        if counts:
            cats = [data["br-clean7"][k]["category"] for k in subset]
            e += sum(25 * (cats.count(c) - n) ** 2 for c, n in zip(("ALL", "MAXN", "TPCDS"), counts))
        return e

    best = rng.sample(keys, 40)
    best_e = error(best)
    for _ in range(rounds):
        cand = list(best)
        cand[rng.randrange(40)] = rng.choice([k for k in keys if k not in cand])
        e = error(cand)
        if e <= best_e:
            best, best_e = cand, e
    (CACHE / "subset.json").write_text(json.dumps(sorted(best), indent=0))
    for img in data:
        print(f"{img}: stand-in {sum(data[img][k]['cost'] for k in best) / 1000:.3f} s vs practice {KNOWN[img]:.3f}")
    cats = [data["br-clean7"][k]["category"] for k in best]
    print("categories under S4':", {c: cats.count(c) for c in ("ALL", "MAXN", "TPCDS", "-")}, "target", counts)
    print(f"{len(keys)} labeled patches available; subset saved to notes/standin/subset.json")


def predict(image: str) -> None:
    subset = json.loads((CACHE / "subset.json").read_text())
    data = json.loads((CACHE / f"{image}.json").read_text())
    missing = [k for k in subset if k not in data]
    if missing:
        print(f"{image}: {len(missing)} stand-in patches not collected yet")
        return
    print(f"{image}: predicted practice simulated {sum(data[k]['cost'] for k in subset) / 1000:.3f} s "
          f"(misses {sum(data[k]['miss'] for k in subset)})")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "collect":
        collect(sys.argv[2], [Path(p).resolve() for p in sys.argv[3:]])
    elif cmd == "fit":
        counts = None
        if "--counts" in sys.argv:
            counts = [int(x) for x in sys.argv[sys.argv.index("--counts") + 1].split(",")]
        fit(counts)
    elif cmd == "predict":
        predict(sys.argv[2])
