"""Blast Radius selector.

For each patch in /data/commits, print one JSON line naming the tests to run and
how to split them across workers.

Per commit: apply the patch to the pinned tree in memory, find the candidate tests
(br/analyze.py), and list them. When anything about a patch cannot be bounded or
an internal error happens, that commit lists every test instead.

Only answer lines go to stdout, and every line is checked before anything is printed.
"""

import json
import sys
from pathlib import Path

DATA = Path("/data")
APP = Path(__file__).resolve().parent
PINNED = Path("/opt/sqlglot")  # the pinned sqlglot tree, unpacked at build time
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


def read_pinned(path: str) -> str | None:
    f = PINNED / path
    if not f.is_file() or f.is_symlink():
        return None
    return f.read_bytes().decode("utf-8")


def select(patch_path: Path, tests: list[dict], maps) -> tuple[list[int], str]:
    """Test numbers to list for one patch, and a one-line reason for the log."""
    from br.analyze import Unbounded, candidates
    from br.patch import PatchError, apply

    everything = [t["number"] for t in tests]
    try:
        text = patch_path.read_bytes().decode("utf-8")
        ids, reasons = candidates(apply(text, read_pinned), maps)
    except (PatchError, Unbounded, UnicodeDecodeError) as error:
        return everything, f"all tests: {error!r}"[:300]
    known = set(maps.ids)
    # a test our maps never saw could depend on anything: always list it
    chosen = [t["number"] for t in tests if t["id"] in ids or t["id"] not in known]
    return chosen, "; ".join(reasons)[:300]


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
    try:
        sys.path.insert(0, str(APP))
        from br.maps import Maps

        maps = Maps(APP / "maps")
    except Exception as error:
        log(f"cannot load maps ({error!r}); every commit lists every test")
        maps = None

    lines = []
    for commit in commits:
        workers = everything
        if maps is not None:
            try:
                chosen, why = select(DATA / "commits" / f"{commit}.patch", tests, maps)
                workers = pack(chosen, durations)
                log(f"{commit}: {len(chosen)} tests | {why}")
            except Exception as error:  # one bad commit must not cost the run
                log(f"{commit}: internal error {error!r}; listing every test")
                workers = everything
        if not valid_line(commit, workers, known):
            log(f"{commit}: invalid answer, listing every test")
            workers = everything if valid_line(commit, everything, known) else []
        lines.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("".join(line + "\n" for line in lines))
    sys.stdout.flush()
    log(f"answered {len(commits)} commits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
