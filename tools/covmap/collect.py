#!/usr/bin/env python3
"""Collect, for every test, what it touched in the pinned tree, each test in its own cold process.

Run inside the development image with the pinned tree at /src (bytecode compiled):

    python /tools/covmap/collect.py --tests /tests.json --out /out/seed0 -j 8

The parent imports third-party packages only (never sqlglot or the tests), then forks
one child per test. The child turns on recording, imports the test's module, runs that
one test through unittest (so setUpClass/setUpModule run too), and writes a JSON file:

- lines:  {file: [line numbers executed]} for files under /src
- code:   [[file, qualname, firstlineno]] code objects entered (functions, lambdas,
          class bodies, module bodies)
- opened: files under /src opened as data (not .pyc), listed: directories listed
- spawned: subprocess / exec / fork / spawn events seen
- ok:     whether the test passed in our runner; cpu: child CPU seconds

Processes the test starts (spawned pool workers, `python -c ...`) record too: the
sitecustomize.py next to this file turns recording on in any Python that starts with
COVMAP_DIR set, and those records are merged into the test's own.
"""

import argparse
import json
import os
import resource
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import recorder  # noqa: E402


def run_test(test_id: str, out_path: str, child_dir: str) -> None:
    os.makedirs(child_dir, exist_ok=True)
    os.environ["COVMAP_DIR"] = child_dir
    rec = recorder.Recorder("/src/")
    rec.start()
    ok = False
    error = None
    try:
        import unittest

        file, cls_name, method = test_id.split("::")
        module_name = file[:-3].replace("/", ".")
        module = __import__(module_name, fromlist=["_"])
        cls = getattr(module, cls_name)
        suite = unittest.TestSuite([cls(method)])
        result = unittest.TestResult()
        suite.run(result)
        ok = result.wasSuccessful() and not result.skipped
        if not ok:
            error = [str(x[1])[-500:] for x in result.errors + result.failures] + [str(s) for s in result.skipped]
    except BaseException as exc:  # noqa: BLE001
        error = repr(exc)[-500:]
    rec.stop()
    data = rec.result()
    # merge records from processes this test started
    for name in os.listdir(child_dir):
        try:
            with open(os.path.join(child_dir, name)) as fh:
                recorder.merge(data, json.load(fh))
        except (OSError, ValueError):
            data.setdefault("child_errors", []).append(name)
    self_usage = resource.getrusage(resource.RUSAGE_SELF)
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    data.update(
        id=test_id,
        ok=ok,
        error=error,
        cpu=round(self_usage.ru_utime + self_usage.ru_stime, 4),
        cpu_children=round(kids.ru_utime + kids.ru_stime, 4),
        children=len(os.listdir(child_dir)),
    )
    tmp = out_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.rename(tmp, out_path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("-j", type=int, default=4)
    ap.add_argument("--only", nargs="*", help="test numbers to run")
    ap.add_argument("--timeout", type=float, default=900)
    args = ap.parse_args()
    tests = json.load(open(args.tests))["tests"]
    if args.only:
        wanted = {int(x) for x in args.only}
        tests = [t for t in tests if t["number"] in wanted]
    os.makedirs(args.out, exist_ok=True)
    os.chdir("/src")
    sys.path.insert(0, "/src")

    # Third-party packages only: their import cost is then shared, but nothing of
    # sqlglot or the tests is loaded before the fork.
    import unittest  # noqa: F401
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    import pandas.testing  # noqa: F401
    import duckdb  # noqa: F401
    assert not any(m == "sqlglot" or m.startswith(("sqlglot.", "tests")) for m in sys.modules)

    # longest first so the heavy tests do not finish last
    todo = sorted(tests, key=lambda t: -t["seconds"])
    todo = [t for t in todo if not os.path.exists(os.path.join(args.out, f"{t['number']}.json"))]
    running = {}
    started_all = time.time()
    done = 0
    while todo or running:
        while todo and len(running) < args.j:
            t = todo.pop(0)
            out_path = os.path.join(args.out, f"{t['number']}.json")
            child_dir = os.path.join(args.out, "children", str(t["number"]))
            pid = os.fork()
            if pid == 0:
                try:
                    os.setsid()
                    run_test(t["id"], out_path, child_dir)
                finally:
                    os._exit(0)
            running[pid] = (t, time.time())
        pid, status = os.waitpid(-1, os.WNOHANG)
        if pid == 0:
            now = time.time()
            for p, (t, started) in list(running.items()):
                if now - started > args.timeout:
                    print(f"timeout {t['id']}", file=sys.stderr, flush=True)
                    try:
                        os.killpg(p, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            time.sleep(0.02)
            continue
        if pid not in running:  # an orphaned grandchild reparented to us (we may be PID 1)
            continue
        t, started = running.pop(pid)
        done += 1
        out_path = os.path.join(args.out, f"{t['number']}.json")
        if not os.path.exists(out_path):
            print(f"NO RESULT {t['id']} status {status}", file=sys.stderr, flush=True)
        if done % 50 == 0:
            print(f"{done} done, {time.time() - started_all:.0f} s", file=sys.stderr, flush=True)
    print(f"all done in {time.time() - started_all:.0f} s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
