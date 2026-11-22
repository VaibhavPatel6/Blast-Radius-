"""Decision policy: for one commit's candidates, which tests to list.

Inputs: the candidate tests (dicts with id, number, seconds), a way to verify a list
of tests against the patched code, and the remaining time. Output: the test numbers
to list and a short reason.

Reasoning, from the scorer's arithmetic (plan section 3):
- An empty answer is the only free one; any listing costs at least 2 s.
- A miss costs 300 s, so a test we did not verify is listed ("when unsure, list").
- Once one test fails, the commit's worker is paid for; listing the remaining
  candidates costs at most their duration, running them costs the same in CPU.
  So we stop at the first failure and list everything not yet verified.
- Running costs CPU second for second, while listing spreads over workers. Above
  RUN_LIMIT seconds of regular candidates, listing is cheaper than verifying.
- A heavy test (>= HEAVY_SECONDS) is never run: running a passing heavy test costs
  as much as listing it. Until the heavy-test study (E6) says otherwise, a heavy
  candidate is always listed.
"""

HEAVY_SECONDS = 3.0
# Above this many seconds of regular candidates, list them instead of running them:
# two workers carry 8 s of tests for about 4 s + 4 s of fees, while running costs ~8 s CPU.
RUN_LIMIT = 8.0


def decide(cands: list[dict], verify, can_verify: bool) -> tuple[list[int], str]:
    if not cands:
        return [], "no candidates"
    heavy = [t for t in cands if t["seconds"] >= HEAVY_SECONDS]
    regular = [t for t in cands if t["seconds"] < HEAVY_SECONDS]
    regular_seconds = sum(t["seconds"] for t in regular)
    if not can_verify or regular_seconds > RUN_LIMIT:
        why = "no time to verify" if not can_verify else f"{regular_seconds:.1f}s of regular candidates"
        return [t["number"] for t in cands], f"listed blindly ({why})"
    result = verify(regular) if regular else {"passed": [], "failed": [], "unknown": [], "why": ""}
    passed = set(result["passed"]) - set(result["failed"])
    # anything not positively seen to pass is listed
    chosen = [t["number"] for t in regular if t["id"] not in passed] + [t["number"] for t in heavy]
    why = (f"verified {len(result['passed'])} passed, {len(result['failed'])} failed, "
           f"{len(result['unknown'])} unknown, {len(heavy)} heavy listed {result['why'][:80]}")
    return chosen, why
