#!/usr/bin/env python3
"""Gate for the test_max_nodes stand-in (br/maxn.py): which patches it drops the test on,
and whether the real test fails on any of them. Development image, this checkout at /app,
the pinned tree at /opt/sqlglot:

    python /app/tools/maxn_gate.py [--baseline FILE] [--all] SET_DIR[:LABELS.json] ...

Labels: a set's answers.json (failing test numbers), or a tools/label_one_test.py output
given after a colon. --all decides every patch, not only those where the test is a
candidate (the stand-in's verdict is still compared with the label).
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


def read_pinned(path: str):
    f = PINNED / path
    return f.read_bytes().decode("utf-8") if f.is_file() and not f.is_symlink() else None


def cpu() -> float:
    a, b = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return a.ru_utime + a.ru_stime + b.ru_utime + b.ru_stime


def main() -> int:
    args = sys.argv[1:]
    from br import maxn
    maxn.BASELINE = Path("/nonexistent")
    if "--baseline" in args:
        i = args.index("--baseline")
        maxn.BASELINE = Path(args[i + 1])
        del args[i:i + 2]
    every = "--all" in args
    if every:
        args.remove("--all")
    maps = Maps(Path("/app/maps"))
    tests = json.loads(Path("/app/sample/tests.json").read_text())["tests"]
    by_id = {t["id"]: t for t in tests}
    number = by_id[maxn.TEST_ID]["number"]
    t0 = cpu()
    pinned = maxn.pinned_record(str(PINNED), 600)
    print(f"pinned record: {'ok' if pinned else 'FAILED'} cpu {cpu() - t0:.2f}s "
          f"({'baseline' if maxn.BASELINE.exists() else 'computed'})", flush=True)
    drops = misses = decided = fails_seen = 0
    for spec in args:
        set_dir, _, label_path = spec.partition(":")
        set_dir = Path(set_dir)
        if label_path:
            results = json.loads(Path(label_path).read_text())["results"] if Path(label_path).exists() else {}
            failing = {p.stem: (results[p.stem]["status"] != "passed") if p.stem in results else None
                       for p in (set_dir / "commits").glob("*.patch")}
        else:
            answers = json.loads((set_dir / "answers.json").read_text())["commits"]
            failing = {k: number in v["failing"] for k, v in answers.items()}
        for commit in sorted(failing):
            text = (set_dir / "commits" / f"{commit}.patch").read_bytes().decode("utf-8", "replace")
            fails = failing[commit]
            fails_seen += bool(fails)
            try:
                changes = apply(text, read_pinned)
                class_edits, wide, changed_objects = [], set(), []
                ids, _ = candidates(changes, maps, lambda d: (PINNED / d).is_dir(), class_edits, wide, changed_objects)
            except (PatchError, Unbounded) as error:
                print(f"{set_dir.name}/{commit}: all tests ({error!r:.60}) fails={fails}", flush=True)
                continue
            candidate = maxn.TEST_ID in ids
            if not candidate and not every:
                print(f"{set_dir.name}/{commit}: not a candidate fails={fails}"
                      f"{'  NOT A CANDIDATE BUT FAILS' if fails else ''}", flush=True)
                continue
            decided += 1
            c0, w0 = cpu(), time.monotonic()
            kept, how = maxn.decide(commit, changes, [number], tests, changed_objects, class_edits, maps,
                                    str(PINNED), 120)
            dropped = number not in kept
            drops += dropped
            missed = dropped and fails
            misses += bool(missed)
            print(f"{set_dir.name}/{commit}: candidate={candidate} fails={fails} dropped={dropped} "
                  f"{'MISSED' if missed else 'ok'} cpu {cpu() - c0:.2f}s wall {time.monotonic() - w0:.1f}s"
                  f" {how.strip()[:170]}", flush=True)
    print(f"TOTAL decided {decided}, dropped {drops}, dropped while the real test fails {misses}, "
          f"failing patches seen {fails_seen}")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
