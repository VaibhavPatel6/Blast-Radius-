#!/usr/bin/env python3
"""Gate for the TPC check (br/tpc.py): on labeled sets, which TPC executor tests it drops
and whether any dropped test really fails. Runs in the development image with this
checkout at /app and the pinned tree at /opt/sqlglot:

    docker run --rm -e PYTHON_CPU_COUNT=1 -v $PWD:/app:ro -v $PINNED:/opt/sqlglot:ro \\
        -v $SET:/set:ro br-dev:arm64 python /app/tools/tpc_gate.py [--baseline FILE] [--estimate] new /set [/set2 ...]

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


def _module_global():
    import datetime
    import sqlglot.parsers.tsql as m
    m.DEFAULT_START_DATE = m.DEFAULT_START_DATE + datetime.timedelta(days=1)
    return "sqlglot.parsers.tsql.DEFAULT_START_DATE"


def _class_attribute():
    from sqlglot.dialects.tsql import TSQL
    name = sorted(k for k, v in vars(TSQL).items() if type(v) is bool)[0]
    setattr(TSQL, name, not getattr(TSQL, name))
    return f"TSQL.{name}"


def _lambda_swap():
    import types
    from br import state
    from sqlglot.dialects.tsql import TSQL
    for owner in (TSQL.Parser, TSQL.Generator, TSQL.Tokenizer):
        for name, table in sorted(vars(owner).items()):
            if type(table) is not dict:
                continue
            seen = {}
            for k, v in table.items():
                if isinstance(v, types.FunctionType):
                    other = seen.get(v.__code__.co_qualname)
                    if other is not None and state._code_hex(table[other].__code__) != state._code_hex(v.__code__):
                        table[other], table[k] = table[k], table[other]
                        return f"{owner.__qualname__}.{name}[{other!r}] <-> [{k!r}] ({v.__code__.co_qualname})"
                    seen.setdefault(v.__code__.co_qualname, k)
    raise RuntimeError("no same-named lambdas to swap")


def selftest() -> int:
    """The import-state comparison must see a changed module global, a changed class
    attribute and two same-named lambdas swapped in a class table, and nothing on a no-op."""
    from br import tpc
    tpc._TEST_HOOK = None
    assert tpc.pinned_rows(str(PINNED), "tpc-h", 300) is not None, "pinned side failed"
    failures = 0
    for name, hook, expect_same in (("no-op", None, True), ("module global", _module_global, False),
                                    ("class attribute", _class_attribute, False),
                                    ("lambda swap", _lambda_swap, False)):
        tpc._TEST_HOOK = f"tools.tpc_gate:{hook.__name__}" if hook else None
        same, why = tpc.compare(str(PINNED), str(PINNED), "tpc-h", 300, frozenset(), {}, True)
        ok = same == expect_same and (expect_same or why.startswith("import state differs"))
        failures += not ok
        print(f"selftest {name}: {'PASS' if ok else 'FAIL'} same={same} ({why})", flush=True)
    tpc._TEST_HOOK = None
    return 1 if failures else 0


def main() -> int:
    args = sys.argv[1:]
    baseline = None
    use_estimate = "--estimate" in args
    if use_estimate:
        args.remove("--estimate")
    if "--baseline" in args:
        i = args.index("--baseline")
        baseline = Path(args[i + 1])
        del args[i:i + 2]
    mode, sets = args[0], [Path(p) for p in args[1:]]
    import pytest  # noqa: F401 - as in the selector
    from br import tpc
    tpc.BASELINE = baseline or Path("/nonexistent")
    if mode == "noop":
        # the pinned side (from the baseline when given) against a patched view of the
        # pinned tree that changes nothing, at the patched trees' path
        from br.patch import FileChange
        from br.verify import build_tree, remove_tree
        tree = Path("/tmp/br/noop-tpc")
        text = read_pinned("sqlglot/time.py")
        remove_tree(tree)
        build_tree(tree, [FileChange("sqlglot/time.py", "sqlglot/time.py", text, text)])
        for benchmark in ("tpc-h", "tpc-ds"):
            t0 = cpu()
            rows = tpc.pinned_rows(str(PINNED), benchmark, 300)
            t1 = cpu()
            same, why = tpc.compare(str(PINNED), str(tree), benchmark, 300, None, {}, True)
            print(f"{benchmark}: same={same} {why}; pinned side cpu {t1 - t0:.2f}s, patched side cpu {cpu() - t1:.2f}s"
                  f" ({'baseline' if baseline else 'computed'})", flush=True)
        return 0
    if mode == "selftest":
        return selftest()
    maps = Maps(Path("/app/maps"))
    if mode == "old":
        import selector
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
                for edit in class_edits:  # as selector.py before narrowing class edits
                    ids = set(ids) | set(edit["importers"])
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
                saving_ms = None
                if use_estimate:
                    from selector import answer_cost, duration_ms
                    durations = {t["number"]: duration_ms(t["seconds"]) for t in tests}
                    everything = sorted(by_id[i]["number"] for i in ids if i in by_id)

                    def saving_ms(drop, everything=everything, durations=durations):
                        rest = [n for n in everything if n not in drop]
                        return answer_cost(everything, durations) - answer_cost(rest, durations)
                kept, how = tpc.decide(commit, changes, chosen, tests, changed_objects, class_edits,
                                       str(PINNED), 120, saving_ms=saving_ms, maps=maps)
            dropped = set(chosen) - set(kept)
            missed = dropped & failing
            drops += len(dropped)
            misses += len(missed)
            fails = sorted(failing & set(chosen))
            paths = sorted({p for ch in changes for p in (ch.old_path, ch.new_path) if p})
            kinds = sorted({k for *_, k in changed_objects} | ({"class-edit"} if class_edits else set()))
            if all(p.startswith("tests/fixtures/") for p in paths):
                kinds = ["fixture"]
            elif not kinds:
                kinds = ["no code"]
            print(f"{set_dir.name}/{commit}: kind {'+'.join(kinds)} paths {','.join(paths)[:80]}", flush=True)
            print(f"{set_dir.name}/{commit}: cand {chosen} failing {fails} dropped {sorted(dropped)} "
                  f"{'MISSED ' + str(sorted(missed)) if missed else 'ok'} cpu {cpu() - t0:.2f}s "
                  f"wall {time.monotonic() - w0:.1f}s {how.strip()[:160]}", flush=True)
    print(f"TOTAL mode={mode} commits with TPC candidates {decided}, tests dropped {drops}, dropped tests that fail {misses}")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
