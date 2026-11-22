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
# Slow tests that cost less to run than to list, with their estimated scorer CPU (ms):
# measured 1.0–1.44 s on quiet local runs in our child, times 1.5 for the scorer, plus margin.
RUN_WHEN_CANDIDATE = {"tests/test_generator.py::TestGenerator::test_generator_memory": 2500}
# Seconds allowed for computing one side of the TPC preparation comparison.
TPC_TIMEOUT = 40
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
        self.warm = None

    def warm_run(self, commit: str, changes, tests: list[dict]) -> dict:
        """Run tests in a worker forked from the warm zygote, with patched function bodies
        swapped in (br/warm.py). The caller has checked the patch is eligible."""
        from br.verify import WATCHDOG_CLASS_SETUP, WATCHDOG_FACTOR, WATCHDOG_FLOOR
        from br.warm import Warm

        if self.warm is None:
            self.warm = Warm()
        payload = [{"path": ch.old_path, "old": ch.old_text, "new": ch.new_text} for ch in changes]
        seconds = {t["id"]: t["seconds"] for t in tests}
        order = sorted(tests, key=lambda t: (t["id"].rsplit("::", 1)[0], t["seconds"]))
        seen = set()

        def budget(test_id: str, first: bool) -> float:
            cls = test_id.rsplit("::", 1)[0]
            extra = WATCHDOG_CLASS_SETUP if cls not in seen else 0.0
            seen.add(cls)
            return WATCHDOG_FLOOR + WATCHDOG_FACTOR * seconds[test_id] + extra

        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        result = self.warm.run(payload, order, budget, self.started + VERIFY_HARD_STOP)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        result["why"] = f"warm cpu {after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime:.2f}s {result['why']}"
        return result

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
    readers, problem = _readers_of(changed, maps, run_only=False)
    if problem:
        return importers, problem
    note = f"class edits: {sum(len(v) for v in changed.values())} attributes changed in {len(changed)} classes -> {len(readers)} readers"
    # Reads made at import (a metaclass or class body consuming the value) leave their
    # results in class and module state. If every sqlglot module, imported on both sides,
    # shows no difference beyond attributes whose test-time readers we know, only those
    # test-time readers can observe the change.
    run_readers, problem = _readers_of(changed, maps, run_only=True)
    if problem or run_readers == readers or not verifier.has_time():
        return readers, note
    tree = Path("/tmp/br") / (commit + "-state-all")
    try:
        remove_tree(tree)
        build_tree(tree, changes)
        everything = changed_attributes(str(PINNED), str(tree), ["*"], PROBE_TIMEOUT)
    except Exception:
        everything = None
    finally:
        remove_tree(tree)
    if everything is None:
        return readers, note
    run_all, problem = _readers_of(everything, maps, run_only=True)
    if problem:
        return readers, note + " (full import: " + problem + ")"
    return run_all, note + f"; full-import comparison: {len(run_all)} test-time readers"


def _readers_of(changed: dict, maps, run_only: bool) -> tuple[set[str], str]:
    readers: set[str] = set()
    for (module, qual), names in sorted(changed.items()):
        if "*" in names or "__kind__" in names:
            return set(), f"class edits: importers ({module}:{qual} gained or lost a member)"
        path = module_file(module)
        if path is None:
            return set(), f"class edits: importers (no file for {module})"
        wrapped = maps.wrapped(path, qual)
        for name in sorted(names):
            if wrapped is None or name not in wrapped:
                # the recorder never instrumented this name, so its readers are unknown
                return set(), f"class edits: importers ({module}:{qual}.{name} has no read records)"
            got = maps.readers_run(path, qual, name) if run_only else maps.readers(path, qual, name)
            if got is None:
                return set(), f"class edits: importers (no read records for {path})"
            readers |= got
    return readers, ""


def answer_cost(numbers: list[int], durations: dict[int, int]) -> int:
    workers = pack(numbers, durations)
    return (max(sum(durations[n] for n in w) for w in workers) + WORKER_COST_MS * len(workers)) if workers else 0


_TPC_EXECUTION: set | None = None
_IMPORT_TIME: set | None = None
_TPC_QUERIES: dict | None = None
# A function entered by nearly every test run alone is almost certainly run at import.
IMPORT_LIKE_ENTRANTS = 1500


def decide_tpc(commit, changes, chosen, tests, changed_objects, class_edits, verifier, allowed, maps=None):
    """Drop the TPC executor tests when what they depend on provably did not change
    (br/tpc.py: prepared queries, import state, fixtures), when that is expected to pay."""
    if not allowed or not verifier.has_time():
        return chosen, ""
    from br.tpc import decide

    durations = {t["number"]: duration_ms(t["seconds"]) for t in tests}

    def saving_ms(drop):
        return answer_cost(chosen, durations) - answer_cost([n for n in chosen if n not in drop], durations)

    return decide(commit, changes, chosen, tests, changed_objects, class_edits, str(PINNED),
                  TPC_TIMEOUT, log, saving_ms=saving_ms)


def run_cheap_heavy(commit: str, changes, chosen: list[int], tests: list[dict], verifier, allowed: bool):
    """A slow test that is cheap to run (test_generator_memory: 6.7 s recorded, about 1.5 s
    of scorer CPU in our child) is run when it is a candidate and leaving it out would
    save more than running it costs; a pass drops it. Anything else stays listed."""
    if not allowed or not verifier.has_time():
        return chosen, ""
    by_number = {t["number"]: t for t in tests}
    durations = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    for test_id, run_cpu_ms in RUN_WHEN_CANDIDATE.items():
        hit = [n for n in chosen if by_number[n]["id"] == test_id]
        if not hit:
            continue
        rest = [n for n in chosen if n != hit[0]]
        saving = answer_cost(chosen, durations) - answer_cost(rest, durations)
        if saving <= run_cpu_ms:
            continue
        result = verifier(commit, changes, [by_number[hit[0]]])
        if by_number[hit[0]]["id"] in result["passed"] and not result["failed"]:
            return rest, f" | ran {test_id.rsplit('::', 1)[1]}: passed ({result['why'][:20]}), saved {saving / 1000:.1f}s"
        return chosen, f" | ran {test_id.rsplit('::', 1)[1]}: not passed, listed"
    return chosen, ""


def cands_ids_for_warm(ids, tests: list[dict]) -> list[str]:
    from br.policy import HEAVY_SECONDS

    return [t["id"] for t in tests if t["id"] in ids and t["seconds"] < HEAVY_SECONDS]


def warm_eligible(changes, changed_objects: list[tuple], test_ids: list[str], maps) -> tuple[bool, str]:
    """Whether swapping patched function bodies into a warm process gives exactly the
    patched behaviour: only edits inside existing Python files, only function bodies that
    are not nested in other functions and never run at import, and no candidate that
    starts child processes (they import sqlglot afresh from disk)."""
    global _IMPORT_TIME
    for ch in changes:
        if ch.old_path is None or ch.new_path != ch.old_path or not ch.old_path.endswith(".py"):
            return False, "not an edit inside an existing Python file"
        if not ch.old_path.startswith(("sqlglot/", "tests/")):
            return False, "file outside sqlglot/ and tests/"
    if not changed_objects:
        return False, "no changed code"
    if _IMPORT_TIME is None:
        _IMPORT_TIME = {tuple(x) for x in json.loads((APP / "maps" / "import_time.json").read_text())}
    for p, q, n, kind in changed_objects:
        if kind != "function" or "<locals>" in q:
            return False, f"{q} is not a top-level function body"
        if (p, q, n) in _IMPORT_TIME:
            return False, f"{q} runs at import"
    for test_id in test_ids:
        if maps.tests.get(test_id, {}).get("children", 1):
            return False, f"{test_id} starts child processes"
    return True, ""


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
        # A patch that only swaps `is`/`==` (or `is not`/`!=`) at comparison sites where
        # every operand pair the pinned suite produced compares by identity cannot change
        # any test (br/swap.py); only tests that read the file as data stay listed.
        from br.swap import prove

        proven, why_swap, keep = prove(changes, maps)
        swap_answer = None
        if proven:
            # DIAGNOSTIC 4: S6 would answer this; the diagnostic answers as f8e7357 does
            # (the analysis below) and only records S6's verdict on test_execute_tpcds.
            keep_numbers = [t["number"] for t in tests if t["id"] in keep]
            unmapped = [t["number"] for t in tests if t["id"] not in known]
            swap_answer = sorted(set(keep_numbers) | set(unmapped))
            log(f"{commit}: DIAGNOSTIC swap proof would answer {len(swap_answer)} tests: {why_swap}"[:400])
        class_edits: list[dict] = []
        wide: set[str] = set()
        changed_objects: list[tuple] = []
        ids, reasons = candidates(changes, maps, lambda d: (PINNED / d).is_dir(), class_edits, wide, changed_objects)
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
    warm_ok, warm_why = warm_eligible(changes, changed_objects, cands_ids_for_warm(ids, tests), maps)
    if not warm_ok:
        reasons.append(f"no warm run: {warm_why}")
    data_only = all(not (ch.old_path or ch.new_path or "").endswith(".py") for ch in changes)
    chosen, how = decide(cands, lambda regular: verifier.warm_run(commit, changes, regular),
                         can_verify and warm_ok, data_only)
    by_id = {t["id"]: t for t in tests}
    chosen, how_run = run_cheap_heavy(commit, changes, chosen, tests, verifier, can_verify and not data_only)
    how += how_run
    base_chosen = list(chosen)  # DIAGNOSTIC 4: f8e7357's answer (TPC check off, no swap proof)
    if swap_answer is None:
        chosen, how_tpc = decide_tpc(commit, changes, chosen, tests, changed_objects, class_edits, verifier,
                                     can_verify, maps)
    else:
        chosen, how_tpc = swap_answer, " | swap proof"
    how += how_tpc
    tpcds_number = next((t["number"] for t in tests if t["id"] == DIAG_TPCDS_ID), None)
    if tpcds_number in base_chosen:
        if tpcds_number not in chosen:
            DIAG4[commit] = ("drop", 0)
        elif True:
            category = diag5_category(commit, how_tpc)
            DIAG4[commit] = ("keep", DIAG5_CODES[category])
            log(f"{commit}: DIAGNOSTIC5 category {category} diff={DIAG5_DIFF.get(commit)}")
        else:
            paths = {p for ch in changes for p in (ch.old_path, ch.new_path) if p}
            parts = (25 if any(p.startswith("tests/") and p.endswith(".py") for p in paths) else 0) \
                + (125 if any(not p.endswith(".py") and p not in DIAG_TPC_FIXTURES for p in paths) else 0) \
                + (625 if any(p in DIAG_TPC_FIXTURES for p in paths) else 0) \
                + (3125 if any(p.startswith("sqlglot/executor/") for p in paths) else 0) \
                + (15625 if "sqlglot/planner.py" in paths else 0)
            shape = "tpc: kept (patch shape)" in how_tpc or "tpc: kept (fixture" in how_tpc
            DIAG4[commit] = ("keep", 5 + parts if (shape and parts) else 0)
        log(f"{commit}: DIAGNOSTIC tpcds {DIAG4[commit]}{how_tpc}"[:400])
    chosen = base_chosen
    extras = sorted((by_id[i] for i in wide if i in by_id), key=lambda t: (t["seconds"], t["number"]))
    return sorted(set(chosen) | set(unmapped)), (how + " | " + "; ".join(reasons))[:400], [t["number"] for t in extras]


# DIAGNOSTIC BUILD 5 (DIAG-4's frame with the markers below), not an entry. Answers exactly as f8e7357 (practice 199.269 s
# simulated) while running every S6 decider (so its CPU shows S6's cost), plus:
#  - for each commit whose f8e7357 answer lists test_execute_tpcds, a marker in ms:
#    S6 drops it: 1, and the test is really left out, with other unused tests added so
#    the simulated cost is unchanged (a miss then shows S6's drop is wrong);
#    S6 keeps it for the patch shape or the fixture rule: 5 plus the parts present
#    (.py under tests/ 25, a non-Python file other than a TPC fixture 125, a TPC fixture
#    625, sqlglot/executor/ 3125, sqlglot/planner.py 15625); any other reason: 0
#    (inferred: practice has 4 such commits, so other = 4 - digit0 - digit1);
#  - a fixed handicap of 3000 ms on the first other commit, so S6 itself scores better.
# Decode: D = round((simulated - 199.269 - 3.000) * 1000), read in base 5.
DIAG_TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
DIAG_TPC_FIXTURES = {"tests/fixtures/optimizer/tpc-ds/tpc-ds.sql", "tests/fixtures/optimizer/tpc-h/tpc-h.sql"}
DIAG_HANDICAP_MS = 3000
DIAG4: dict = {}


def _dcost(workers, durations) -> int:
    occupied = [w for w in workers if w]
    return (max(sum(durations[n] for n in w) for w in occupied) + WORKER_COST_MS * len(occupied)) if occupied else 0


def _fill(durations, unused, target):
    """Test numbers from `unused` whose durations sum to exactly `target` ms, or None
    (exact subset sum: one bitset of reachable sums per prefix, then a walk back)."""
    if target <= 0:
        return None
    items = sorted(n for n in unused if 0 < durations[n] <= target)
    mask = (1 << (target + 1)) - 1
    prefix = [1]
    for n in items:
        prefix.append((prefix[-1] | (prefix[-1] << durations[n])) & mask)
    if not (prefix[-1] >> target) & 1:
        return None
    picked, left = [], target
    for i in range(len(items), 0, -1):
        if (prefix[i - 1] >> left) & 1:
            continue  # reachable without item i-1
        n = items[i - 1]
        picked.append(n)
        left -= durations[n]
    return picked if left == 0 else None


def _add_exact(workers, durations, exclude, ms):
    """`workers` with unused tests (none in `exclude`) added to the busiest worker so the
    simulated cost rises by exactly `ms`, or None."""
    listed = {n for w in workers for n in w}
    occupied = [list(w) for w in workers if w]
    # with no worker yet, the added tests open one, which itself costs WORKER_COST_MS
    need = ms if occupied else ms - WORKER_COST_MS
    if need <= 0:
        return None
    pick = _fill(durations, [n for n in durations if n not in listed and n not in exclude], need)
    if pick is None:
        return None
    base = _dcost(workers, durations)
    if occupied:
        new = [list(w) for w in workers]
        loads = [sum(durations[n] for n in w) if w else -1 for w in new]
        new[loads.index(max(loads))].extend(pick)
    else:
        new = [pick]
    return new if _dcost(new, durations) - base == ms else None


def diagnostic4(commit, chosen, workers, durations):
    """(workers, exclude-from-free-fill) for one commit."""
    entry = DIAG4.get(commit)
    tpcds = next(iter(DIAG4_TPCDS), None)
    if entry is None:
        if DIAG4_STATE["handicap"]:
            return workers, set()
        new = _add_exact(workers, durations, {tpcds}, DIAG_HANDICAP_MS)
        if new is None:
            return workers, set()
        DIAG4_STATE["handicap"] = True
        log(f"{commit}: DIAGNOSTIC handicap {DIAG_HANDICAP_MS}")
        return new, set()
    kind, ms = entry
    if not ms and kind != "drop":
        return workers, set()
    if kind == "drop":
        target = _dcost(workers, durations) + ms
        without = pack([n for n in chosen if n != tpcds], durations)
        new = _add_exact(without, durations, {tpcds}, target - _dcost(without, durations))
        if new is not None:
            log(f"{commit}: DIAGNOSTIC marker {ms} (tpcds dropped)")
            return new, {tpcds}
        log(f"{commit}: DIAGNOSTIC drop not applied; marker only")
    new = _add_exact(workers, durations, {tpcds}, ms)
    if new is None:
        log(f"{commit}: DIAGNOSTIC unmarked {ms}")
        return workers, set()
    log(f"{commit}: DIAGNOSTIC marker {ms}")
    return new, set()


# DIAGNOSTIC 5: for commits S6 keeps test_execute_tpcds on for a reason other than the
# patch shape or the fixture rule, which reason. Codes (ms) chosen so that any 4 of them
# (0 repeated allowed) have distinct sums. Drops and shape/fixture keeps are 0 (DIAG-4
# counts them).
DIAG5_CODES = {"rest": 0, "differ_1_3": 1, "differ_4plus": 5, "exec_map": 21, "not_worth": 55,
               "import_state": 153, "not_run": 368, "shared_state": 856, "other": 1424}
DIAG5_DIFF: dict = {}
DIAG5_CURRENT = {"commit": None}


def diag5_category(commit, how_tpc):
    h = how_tpc or ""
    if "tpc: kept (patch shape)" in h or "tpc: kept (fixture" in h:
        return "rest"
    if not h.strip() or "tpc:" not in h:
        return "not_run"
    if "could change state other tests share" in h:
        return "shared_state"
    if "changed code runs during execution" in h:
        return "exec_map"
    if "not worth it" in h:
        return "not_worth"
    part = h[h.find("tpc-ds"):] if "tpc-ds" in h else h
    part = part.split(";")[0]
    if " differs at query " in part and "import state" not in part:
        n = DIAG5_DIFF.get(commit)
        if n is None:
            return "other"
        return "differ_1_3" if n <= 3 else "differ_4plus"
    if "import state differs" in part or "loaded modules differ" in part:
        return "import_state"
    return "other"


def _diag5_install():
    """Count how many TPC-DS queries differ when the comparison stops at the first one."""
    import br.tpc as tpc

    original = tpc.compare

    def counting(pinned_tree, patched_tree, benchmark, timeout, only, renames, with_state, allow_unread=False):
        same, why = original(pinned_tree, patched_tree, benchmark, timeout, only, renames, with_state,
                             allow_unread=allow_unread)
        if not same and benchmark == "tpc-ds" and " differs at query " in why:
            try:
                expected = tpc.pinned_rows(pinned_tree, benchmark, timeout)
                rows = tpc._run(patched_tree, benchmark, timeout, only, renames, with_state, None)
                if rows is not None and expected is not None:
                    bad = {r.get("query") for r, p in zip(rows, expected) if not tpc._matches(r, p)}
                    bad |= {p.get("query") for p in expected[len(rows):]}
                    bad.discard(None)
                    DIAG5_DIFF[DIAG5_CURRENT["commit"]] = len(bad)
            except Exception as error:  # noqa: BLE001
                log(f"DIAGNOSTIC5 count error {error!r}")
        return same, why

    tpc.compare = counting


DIAG4_TPCDS: set = set()
DIAG4_STATE = {"handicap": False}


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
    try:
        sys.path.insert(0, str(APP))
        _diag5_install()
    except Exception as error:  # noqa: BLE001
        log(f"DIAGNOSTIC5 install error {error!r}")

    lines = []
    for commit in commits:
        workers = everything
        if maps is not None and time.monotonic() - started > ANALYSIS_DEADLINE:
            log(f"{commit}: out of time; listing every test")
        elif maps is not None:
            try:
                DIAG5_CURRENT["commit"] = commit
                chosen, why, extras = select(commit, tests, mapped, maps, verifier)
                workers = pack(chosen, durations)
                DIAG4_TPCDS.update(t["number"] for t in tests if t["id"] == DIAG_TPCDS_ID)
                try:
                    pre, exclude = diagnostic4(commit, chosen, workers, durations)  # before free fill: exact
                except Exception as error:  # the diagnostic must never change the answer otherwise
                    log(f"{commit}: DIAGNOSTIC error {error!r}")
                    pre, exclude = workers, set()
                if valid_line(commit, pre, known):
                    workers = pre
                else:
                    exclude = set()
                zero = [n for n in sorted(durations) if durations[n] == 0]
                workers = free_fill(workers, [n for n in extras + zero if n not in exclude], durations)
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
