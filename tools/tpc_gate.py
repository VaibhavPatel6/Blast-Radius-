#!/usr/bin/env python3
"""Gate for the TPC check (br/tpc.py): on labeled sets, which TPC executor tests it drops
and whether any dropped test really fails. Runs in the development image with this
checkout at /app and the pinned tree at /opt/sqlglot:

    docker run --rm -e PYTHON_CPU_COUNT=1 -v $PWD:/app:ro -v $PINNED:/opt/sqlglot:ro \\
        -v $SET:/set:ro br-dev:arm64 python /app/tools/tpc_gate.py new /set [/set2 ...]

Mode `new` calls br.tpc.decide; mode `old` calls selector.decide_tpc (run it with /app at
an older checkout to get the numbers before a change); mode `noop` compares the pinned tree
with itself and prints the CPU of each side. A set holds commits/*.patch and answers.json;
tests.json is the set's own or the sample's.
"""

import json
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")
from br.analyze import Unbounded, candidates  # noqa: E402
from br.maps import Maps  # noqa: E402
from br.patch import PatchError, apply  # noqa: E402

PINNED = Path("/opt/sqlglot")
TPC = {"tests/test_executor.py::TestExecutor::test_execute_tpcds",
       "tests/test_executor.py::TestExecutor::test_execute_tpch"}


def read_pinned(path: str):
    f = PINNED / path
    return f.read_bytes().decode("utf-8") if f.is_file() and not f.is_symlink() else None


def cpu() -> float:
    a, b = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return a.ru_utime + a.ru_stime + b.ru_utime + b.ru_stime


class AlwaysTime:
    def has_time(self) -> bool:
        return True


def main() -> int:
    mode, sets = sys.argv[1], [Path(p) for p in sys.argv[2:]]
    import pytest  # noqa: F401 - as in the selector, before anything forks
    if mode == "noop":
        from br import tpc
        for benchmark in ("tpc-h", "tpc-ds"):
            for side in ("pinned", "pinned again"):
                t0 = cpu()
                if side == "pinned":
                    tpc._PINNED.pop(benchmark, None)
                same, why = tpc.compare(str(PINNED), str(PINNED), benchmark, 120, None, frozenset())
                print(f"{benchmark} {side}: same={same} {why} cpu {cpu() - t0:.2f}s", flush=True)
        return 0
    maps = Maps(Path("/app/maps"))
    if mode == "old":
        import selector
    else:
        from br import tpc
    drops = misses = decided = 0
    for set_dir in sets:
        tests_path = set_dir / "tests.json"
        if not tests_path.exists():
            tests_path = Path("/app/sample/tests.json")
        tests = json.loads(tests_path.read_text())["tests"]
        by_id = {t["id"]: t for t in tests}
        answers = json.loads((set_dir / "answers.json").read_text())["commits"]
        for commit, answer in sorted(answers.items()):
            text = (set_dir / "commits" / f"{commit}.patch").read_bytes().decode("utf-8", "replace")
            failing = set(answer["failing"])
            try:
                changes = apply(text, read_pinned)
                class_edits, wide, changed_objects = [], set(), []
                ids, _ = candidates(changes, maps, lambda d: (PINNED / d).is_dir(), class_edits, wide, changed_objects)
            except (PatchError, Unbounded) as error:
                print(f"{set_dir.name}/{commit}: all tests ({error!r:.60})", flush=True)
                continue
            chosen = sorted(by_id[i]["number"] for i in ids & TPC if i in by_id)
            if not chosen:
                print(f"{set_dir.name}/{commit}: no TPC candidate", flush=True)
                continue
            decided += 1
            t0, w0 = cpu(), time.monotonic()
            if mode == "old":
                kept, how = selector.decide_tpc(commit, changes, chosen, tests, changed_objects, class_edits,
                                                AlwaysTime(), True, maps)
            else:
                kept, how = tpc.decide(commit, changes, chosen, tests, changed_objects, class_edits,
                                       str(PINNED), 120)
            dropped = set(chosen) - set(kept)
            missed = dropped & failing
            drops += len(dropped)
            misses += len(missed)
            fails = sorted(failing & set(chosen))
            print(f"{set_dir.name}/{commit}: cand {chosen} failing {fails} dropped {sorted(dropped)} "
                  f"{'MISSED ' + str(sorted(missed)) if missed else 'ok'} cpu {cpu() - t0:.2f}s "
                  f"wall {time.monotonic() - w0:.1f}s {how.strip()[:160]}", flush=True)
    print(f"TOTAL mode={mode} commits with TPC candidates {decided}, tests dropped {drops}, dropped tests that fail {misses}")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
