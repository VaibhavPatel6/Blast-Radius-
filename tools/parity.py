#!/usr/bin/env python3
"""G2: run the real verification path on labeled commits and check that no test it
reports as passing is labeled failing. Run inside the image (amd64, as the scorer):

  docker run --rm --platform linux/amd64 ... -v SET:/set:ro -v $PWD/tools:/tools:ro \
      --entrypoint python blast-radius-entry /tools/parity.py /set [--all]

--all runs every regular candidate (no stop at first failure) to compare every verdict.
"""
import json
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")
import selector  # noqa: E402
from br import verify  # noqa: E402
from br.analyze import Unbounded, candidates  # noqa: E402
from br.maps import Maps  # noqa: E402
from br.patch import PatchError, apply  # noqa: E402



def main():
    # Spawned pool workers re-import the main script; everything must sit under this guard.
    set_dir = Path(sys.argv[1])
    everything = "--all" in sys.argv
    tests = json.loads((set_dir / "tests.json").read_text())["tests"] if (set_dir / "tests.json").exists() else \
        json.loads(Path("/data/tests.json").read_text())["tests"]
    by_id = {t["id"]: t for t in tests}
    by_num = {t["number"]: t for t in tests}
    answers = json.loads((set_dir / "answers.json").read_text())["commits"]
    maps = Maps(Path("/app/maps"))
    import pytest  # noqa: F401  (as the selector does before forking)
    bad = 0
    total_cpu = 0.0
    for cid, ans in sorted(answers.items()):
        failing = {by_num[n]["id"] for n in ans["failing"]}
        try:
            changes = apply((set_dir / "commits" / f"{cid}.patch").read_bytes().decode("utf-8"), selector.read_pinned)
            ids, _ = candidates(changes, maps, lambda d: (selector.PINNED / d).is_dir())
        except (PatchError, Unbounded, UnicodeDecodeError) as e:
            print(f"{cid}: unbounded ({e!r})")
            continue
        regular = [by_id[i] for i in ids if i in by_id and by_id[i]["seconds"] < 3.0]
        if not regular:
            print(f"{cid}: no regular candidates")
            continue
        tree = Path("/tmp/parity") / cid
        verify.remove_tree(tree)
        verify.build_tree(tree, changes)
        r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        if everything:
            verify_args = dict()
            import br.verify as V
            res = {"passed": [], "failed": [], "unknown": [], "why": ""}
            for t in regular:  # one at a time so a failure does not stop the rest
                one = V.run(tree, [t], time.monotonic() + 300)
                for k in ("passed", "failed", "unknown"):
                    res[k] += one[k]
        else:
            res = verify.run(tree, regular, time.monotonic() + 300)
        r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = r1.ru_utime + r1.ru_stime - r0.ru_utime - r0.ru_stime
        total_cpu += cpu
        verify.remove_tree(tree)
        wrong_pass = sorted(set(res["passed"]) & failing)
        over = sorted(set(res["failed"]) - failing)
        bad += bool(wrong_pass)
        print(f"{cid}: {len(regular)} regular ({sum(t['seconds'] for t in regular):.2f}s rec) -> {len(res['passed'])}p "
              f"{len(res['failed'])}f {len(res['unknown'])}u cpu {cpu:.2f}s"
              f"{'  WRONG-PASS ' + str(wrong_pass) if wrong_pass else ''}{'  over-fail ' + str(over) if over else ''} {res['why'][:60]}",
              flush=True)
    print(f"commits where a labeled-failing test was reported passing: {bad}; verification cpu {total_cpu:.1f}s")


if __name__ == "__main__":
    main()
