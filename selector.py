"""Blast Radius selector.

For each patch in /data/commits, print one JSON line naming the tests to run and
how to split them across workers.

Per commit: apply the patch to the pinned tree in memory, find the candidate tests
(br/analyze.py), run the cheap ones against the patched code (br/verify.py) and list
whatever was not seen to pass (br/policy.py). When anything about a patch cannot be
bounded or an internal error happens, that commit lists every test instead.

Only answer lines go to stdout, and every line is checked before anything is printed.
"""

import json
import resource
import sys
import time
from pathlib import Path

DATA = Path("/data")
APP = Path(__file__).resolve().parent
PINNED = Path("/opt/sqlglot")  # the pinned sqlglot tree, unpacked at build time
MAX_WORKERS = 8  # the scorer rejects a line with more than 8 non-empty workers
WORKER_COST_MS = 2000  # the scorer charges 2 s for every non-empty worker
# The run gets 3 minutes (4.5 in the final, for 1.5 times as many commits). No new
# verification starts after VERIFY_UNTIL seconds, and a running one is cut off at
# VERIFY_HARD_STOP; the remaining commits list their candidates without running them.
VERIFY_UNTIL = 100
# Seconds allowed for each side of a class-state comparison (import plus fingerprint).
PROBE_TIMEOUT = 25
# Patches larger than this are not read; the commit lists every test. Real patches
# are a few kilobytes, and parsing many megabytes could exhaust memory or time.
MAX_PATCH_BYTES = 2_000_000
# After this many seconds, remaining commits list every test without analysis, so
# the whole batch always finishes inside the 3-minute limit.
ANALYSIS_DEADLINE = 140
VERIFY_HARD_STOP = 120


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


def free_fill(workers: list[list[int]], extras: list[int], durations: dict[int, int]) -> list[list[int]]:
    """Add tests that cost nothing: the commit is charged its busiest worker's load plus
    2 s per worker, so a test that fits under that load on a less busy worker is free.
    `extras` come in order of preference (tests a wider selection would have listed first).
    Insurance only: it never changes the cost."""
    if not workers:
        return workers
    loads = [sum(durations[n] for n in w) for w in workers]
    ceiling = max(loads)
    taken = {n for w in workers for n in w}
    for n in extras:
        if n in taken:
            continue
        i = loads.index(min(loads))
        if loads[i] + durations[n] <= ceiling:
            workers[i].append(n)
            loads[i] += durations[n]
            taken.add(n)
    return workers


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


class Verifier:
    """Runs candidate tests against a patched view of the tree, within a time budget."""

    def __init__(self, started: float):
        self.started = started
        self.preimported = False

    def has_time(self) -> bool:
        return time.monotonic() - self.started < VERIFY_UNTIL

    def __call__(self, commit: str, changes, tests: list[dict]) -> dict:
        from br.verify import build_tree, remove_tree, run

        empty = {"passed": [], "failed": [], "unknown": [t["id"] for t in tests], "why": ""}
        tree = Path("/tmp/br") / commit
        try:
            self._preimport(tests)
            remove_tree(tree)
            build_tree(tree, changes)
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            result = run(tree, tests, self.started + VERIFY_HARD_STOP)
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            used = after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
            result["why"] = f"cpu {used:.2f}s {result['why']}"
            return result
        except Exception as error:  # verification is an optimization; on error, list
            log(f"{commit}: verification error {error!r}")
            return dict(empty, why=repr(error))
        finally:
            remove_tree(tree)

    def _preimport(self, tests: list[dict]) -> None:
        """Import pytest, and heavy third-party packages if candidates need them, once,
        before forking. Children are forked from this process, so they inherit them."""
        import pytest  # noqa: F401

        if self.preimported:
            return
        files = {t["file"] for t in tests}
        for f in files:
            source = read_pinned(f) or ""
            if "import duckdb" in source or "import pandas" in source or "import numpy" in source:
                import duckdb  # noqa: F401
                import numpy  # noqa: F401
                import pandas  # noqa: F401
                import pandas.testing  # noqa: F401

                self.preimported = True
                return


def module_file(module: str) -> str | None:
    """Repository path of a sqlglot module name, from the pinned tree."""
    base = module.replace(".", "/")
    for candidate in (base + ".py", base + "/__init__.py"):
        if (PINNED / candidate).is_file():
            return candidate
    return None


def narrow_class_edits(commit: str, changes, class_edits: list[dict], maps, verifier) -> tuple[set[str], str]:
    """Replace "every importer" for changed class bodies by the tests that read an attribute
    whose final value differs (br/state.py compares a pinned and a patched import, after
    every class-creation hook has run). Any doubt returns every importer."""
    from br.state import changed_attributes
    from br.verify import build_tree, remove_tree

    importers = set().union(*(e["importers"] for e in class_edits))
    if not verifier.has_time():
        return importers, "class edits: importers (no time to compare)"
    if any(maps.readers(e["path"], e["qualname"], "") is None for e in class_edits):
        return importers, "class edits: importers (no read records for the edited file)"
    modules = sorted({e["path"][:-3].replace("/", ".").removesuffix(".__init__") for e in class_edits})
    tree = Path("/tmp/br") / (commit + "-state")
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        remove_tree(tree)
        build_tree(tree, changes)
        changed = changed_attributes(str(PINNED), str(tree), modules, PROBE_TIMEOUT)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        log(f"{commit}: class-state probe cpu {after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime:.2f}s")
    except Exception as error:
        return importers, f"class edits: importers (probe error {error!r})"
    finally:
        remove_tree(tree)
    if changed is None:
        return importers, "class edits: importers (an import failed or timed out)"
    readers: set[str] = set()
    for (module, qual), names in sorted(changed.items()):
        if "*" in names or "__kind__" in names:
            return importers, f"class edits: importers ({module}:{qual} gained or lost a member)"
        path = module_file(module)
        if path is None:
            return importers, f"class edits: importers (no file for {module})"
        for name in sorted(names):
            got = maps.readers(path, qual, name)
            if got is None:
                return importers, f"class edits: importers (no read records for {path})"
            readers |= got
    return readers, f"class edits: {sum(len(v) for v in changed.values())} attributes changed in {len(changed)} classes -> {len(readers)} readers"


def select(commit: str, tests: list[dict], known: set[str], maps, verifier) -> tuple[list[int], str, list[int]]:
    """Test numbers to list for one patch, a one-line reason for the log, and the tests a
    wider (function- or importer-level) selection would have added, for free fill."""
    from br.analyze import Unbounded, candidates, lines_no_test_ran_alone
    from br.patch import PatchError, apply
    from br.policy import decide

    everything = [t["number"] for t in tests]
    patch_path = DATA / "commits" / f"{commit}.patch"
    try:
        size = patch_path.stat().st_size
        if size > MAX_PATCH_BYTES:
            return everything, f"all tests: patch is {size} bytes", []
        text = patch_path.read_bytes().decode("utf-8")
        changes = apply(text, read_pinned)
        class_edits: list[dict] = []
        wide: set[str] = set()
        ids, reasons = candidates(changes, maps, lambda d: (PINNED / d).is_dir(), class_edits, wide)
    except (PatchError, Unbounded, UnicodeDecodeError) as error:
        return everything, f"all tests: {error!r}"[:300], []
    if class_edits:
        wide |= set().union(*(e["importers"] for e in class_edits))
        narrowed, how_classes = narrow_class_edits(commit, changes, class_edits, maps, verifier)
        ids = set(ids) | narrowed
        reasons.append(how_classes)
    # a test our maps never saw could depend on anything: always list it
    unmapped = [t["number"] for t in tests if t["id"] not in known]
    cands = [t for t in tests if t["id"] in ids]
    can_verify = verifier.has_time()
    if can_verify:
        unexplained = lines_no_test_ran_alone(changes, maps)
        if unexplained:
            can_verify = False
            reasons.append(f"no clearing: {len(unexplained)} touched lines no test ran alone, e.g. {unexplained[0]}")
    data_only = all(not (ch.old_path or ch.new_path or "").endswith(".py") for ch in changes)
    chosen, how = decide(cands, lambda regular: verifier(commit, changes, regular), can_verify, data_only)
    by_id = {t["id"]: t for t in tests}
    extras = sorted((by_id[i] for i in wide if i in by_id), key=lambda t: (t["seconds"], t["number"]))
    return sorted(set(chosen) | set(unmapped)), (how + " | " + "; ".join(reasons))[:400], [t["number"] for t in extras]


def main() -> int:
    started = time.monotonic()
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
        mapped = set(maps.ids)
    except Exception as error:
        log(f"cannot load maps ({error!r}); every commit lists every test")
        maps = None
    verifier = Verifier(started)

    lines = []
    for commit in commits:
        workers = everything
        if maps is not None and time.monotonic() - started > ANALYSIS_DEADLINE:
            log(f"{commit}: out of time; listing every test")
        elif maps is not None:
            try:
                chosen, why, extras = select(commit, tests, mapped, maps, verifier)
                workers = pack(chosen, durations)
                zero = [n for n in sorted(durations) if durations[n] == 0]
                workers = free_fill(workers, extras + zero, durations)
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
