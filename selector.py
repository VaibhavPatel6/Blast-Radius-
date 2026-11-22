"""Blast Radius selector.

For each patch in /data/commits, print one JSON line naming the tests to run and
how to split them across workers. This version lists every test on every commit
(the safest possible answer) and packs them onto the cheapest number of workers.

Only answer lines go to stdout, and every line is checked before anything is printed.
"""

import json
import sys
from pathlib import Path

DATA = Path("/data")
MAX_WORKERS = 8  # the scorer rejects a line with more than 8 non-empty workers
WORKER_COST_MS = 2000  # the scorer charges 2 s for every non-empty worker


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def duration_ms(seconds: float) -> int:
    """The scorer's rounding: whole milliseconds, halves up."""
    return int(seconds * 1000 + 0.5)


def pack(numbers: list[int], durations: dict[int, int]) -> list[list[int]]:
    """Split tests over 1 to 8 workers and keep the cheapest split.

    Each split is longest-test-first onto the least-loaded worker. The cost is
    the busiest worker's load plus 2 s per worker, as the scorer counts it.
    """
    if not numbers:
        return []
    ordered = sorted(numbers, key=lambda n: (-durations[n], n))
    best_cost, best = None, None
    for count in range(1, min(MAX_WORKERS, len(ordered)) + 1):
        workers = [[] for _ in range(count)]
        loads = [0] * count
        for n in ordered:
            i = loads.index(min(loads))
            workers[i].append(n)
            loads[i] += durations[n]
        cost = max(loads) + WORKER_COST_MS * count
        if best_cost is None or cost < best_cost:
            best_cost, best = cost, workers
    return best


def valid_line(commit: str, workers, known: set[int]) -> bool:
    """The scorer's per-line rules, checked before printing."""
    if not isinstance(commit, str) or not isinstance(workers, list):
        return False
    seen = set()
    occupied = 0
    for worker in workers:
        if not isinstance(worker, list):
            return False
        if worker:
            occupied += 1
        for n in worker:
            if type(n) is not int or n not in known or n in seen:
                return False
            seen.add(n)
    return occupied <= MAX_WORKERS


def main() -> int:
    commits = sorted(p.stem for p in (DATA / "commits").glob("*.patch"))
    try:
        tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
        durations = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    except Exception as error:  # without the test list no test number is safe to print
        log(f"cannot read tests.json ({error!r}); answering [] for every commit")
        tests, durations = [], {}
    known = set(durations)
    everything = pack(sorted(known), durations)

    lines = []
    for commit in commits:
        workers = everything
        if not valid_line(commit, workers, known):
            log(f"{commit}: invalid answer, falling back to []")
            workers = []
        lines.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("".join(line + "\n" for line in lines))
    sys.stdout.flush()
    log(f"listed all {len(known)} tests on {len(everything)} workers for {len(commits)} commits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
