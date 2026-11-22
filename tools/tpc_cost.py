#!/usr/bin/env python3
"""CPU of each part of the TPC check (br/tpc.py), measured on a no-op patched view of the
pinned tree, for the estimate in br/tpc.py. Run in the entry image (amd64) on a quiet
machine:

    docker run --rm --platform linux/amd64 --entrypoint python IMAGE /app/tools/tpc_cost.py [REPEATS [BASELINE]]

Cases, per benchmark, patched side only (the pinned side comes from the baseline):
  early    a difference at the first row: interpreter, imports, first parse
  base     every query parsed, pickled, unpickled and transpiled, none optimized, no state
  full     as base, every executed query optimized (the per-query optimize cost is
           (full - base) / executed queries)
  state    as full, plus the import-state digest
"""

import resource
import sys
from pathlib import Path

sys.path.insert(0, "/app")


def cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def main() -> int:
    import pytest  # noqa: F401

    from br import tpc
    from br.patch import FileChange
    from br.verify import build_tree, remove_tree

    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    if len(sys.argv) > 2:
        tpc.BASELINE = Path(sys.argv[2])
    pinned = "/opt/sqlglot"
    tree = Path("/tmp/br/cost-tpc")
    text = (Path(pinned) / "sqlglot/time.py").read_text()
    remove_tree(tree)
    build_tree(tree, [FileChange("sqlglot/time.py", "sqlglot/time.py", text, text)])
    for benchmark in ("tpc-h", "tpc-ds"):
        t0 = cpu()
        expected = tpc.pinned_rows(pinned, benchmark, 600)
        print(f"{benchmark} pinned side: {cpu() - t0:.2f}s ({'baseline' if tpc.BASELINE.exists() else 'computed'})")
        executed = sum(1 for r in expected if r["step"] == "run" and "optimized" in r)
        for case, kwargs in (("early", dict(only=frozenset(), with_state=False, expected=[{"step": "none"}])),
                             ("base", dict(only=frozenset(), with_state=False, expected=expected)),
                             ("full", dict(only=None, with_state=False, expected=expected)),
                             ("state", dict(only=None, with_state=True, expected=expected))):
            times = []
            for _ in range(repeats):
                t0 = cpu()
                rows = tpc._run(str(tree), benchmark, 600, renames={}, **kwargs)
                times.append(cpu() - t0)
                assert rows is not None, case
            print(f"{benchmark} {case}: " + " ".join(f"{t:.2f}" for t in times) + f" s  (executed queries {executed})",
                  flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
