"""Decision policy: for one commit's candidates, which tests to list.

Inputs: the candidate tests (dicts with id, number, seconds), a way to verify a list of
tests against the patched code, and whether verification is allowed for this commit.
Output: the test numbers to list and a short reason.

Reasoning, from the scorer's arithmetic and measured costs:
- An empty answer is the only free one; any listing costs at least 2 s (one worker).
- A miss costs 300 s, so a test we did not see pass is listed ("when unsure, list").
- On the scorer one CPU-second buys about one recorded second of ordinary tests (other
  teams measured 0.8 to 1.2), plus a fixed cost to fork and import (about 0.25 s) and the
  setUpClass work of a few test classes. Verifying pays when that estimate is below what
  listing would cost, which in practice means small candidate sets (VERIFY_BUDGET).
- We stop at the first failure (pytest -x) and list everything not seen to pass: a pass
  reported after a failure in the same process could be an artifact of the failure.
- A heavy test (>= HEAVY_SECONDS) is never run and is always listed when it is a candidate;
  running a passing heavy test costs as much as listing it, and no rule drops it on weaker
  evidence than a run.
"""

HEAVY_SECONDS = 3.0
# Verify only when the estimated CPU of doing so is at most this many seconds: below the
# 2-second fee of the one worker that listing would need, plus a little of the listed time.
# 0 = off: with a fresh import per commit, verification measured break-even on the sample
# (EXPERIMENTS #7: ~8 s simulated saved for ~8 s of scorer CPU). It returns with a warm parent.
VERIFY_BUDGET = 0.0
# Estimated scorer CPU: per recorded test second, per verification child, and the setUpClass
# work of classes that build expensive fixtures (duckdb tables and a process pool; TPC data).
CPU_PER_RECORDED_SECOND = 1.0
CPU_PER_CHILD = 0.3
CLASS_SETUP_CPU = {
    # measured locally at ~8 s per verification under amd64 emulation (~2.7x the scorer)
    "tests/test_optimizer.py::TestOptimizer": 3.0,
    "tests/test_executor.py::TestExecutor": 3.0,
}


def estimated_cpu(tests: list[dict]) -> float:
    classes = {t["id"].rsplit("::", 1)[0] for t in tests}
    return (CPU_PER_CHILD + CPU_PER_RECORDED_SECOND * sum(t["seconds"] for t in tests)
            + sum(CLASS_SETUP_CPU.get(c, 0.0) for c in classes))


def decide(cands: list[dict], verify, can_verify: bool, data_only: bool = False) -> tuple[list[int], str]:
    """data_only: every candidate comes from a changed data file (a fixture). Such edits
    broke their readers in all 11 sample cases, so running them only adds CPU (rule R3)."""
    if not cands:
        return [], "no candidates"
    if data_only:
        return [t["number"] for t in cands], "listed blindly (fixture readers usually fail)"
    heavy = [t for t in cands if t["seconds"] >= HEAVY_SECONDS]
    regular = [t for t in cands if t["seconds"] < HEAVY_SECONDS]
    cost = estimated_cpu(regular) if regular else 0.0
    if not regular or not can_verify or cost > VERIFY_BUDGET:
        why = ("only heavy candidates" if not regular else
               "verification not allowed here" if not can_verify else f"verifying would cost ~{cost:.1f}s")
        return [t["number"] for t in cands], f"listed blindly ({why})"
    result = verify(regular)
    passed = set(result["passed"]) - set(result["failed"])
    # anything not positively seen to pass is listed
    chosen = [t["number"] for t in regular if t["id"] not in passed] + [t["number"] for t in heavy]
    why = (f"verified ~{cost:.1f}s: {len(result['passed'])} passed, {len(result['failed'])} failed, "
           f"{len(result['unknown'])} unknown, {len(heavy)} heavy listed {result['why'][:80]}")
    return chosen, why
