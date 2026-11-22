"""pytest plugin: record, per test, the code objects entered and files opened while the
whole suite runs in shared pytest processes (as the organizers labeled), to compare with
the one-test-per-cold-process maps.

    COVSUITE_OUT=/out PYTHONPATH=/tools/covmap:/src python -m pytest -p suite_plugin tests

At the start of each test, sys.monitoring.restart_events() re-arms every location that
reported earlier, so each test reports every line and code object it runs, even ones an
earlier test already ran. Events before the first test (collection: importing every test
module) go to "<collection>". Each process writes one JSON file at the end.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recorder  # noqa: E402

_rec = recorder.Recorder("/src/")
_rec.start()
_buckets: dict[str, dict] = {}
_current = "<collection>"
_table: dict[tuple, int] = {}


def _flush(name: str) -> None:
    result = _rec.result()
    codes = []
    for c in result["code"]:
        key = tuple(c)
        if key not in _table:
            _table[key] = len(_table)
        codes.append(_table[key])
    old = _buckets.get(name, {"code": [], "opened": []})
    _buckets[name] = {"code": sorted(set(old["code"]) | set(codes)),
                      "opened": sorted(set(old["opened"]) | set(result["opened"]))}
    _rec.lines, _rec.code, _rec.opened, _rec.listed = {}, set(), set(), set()


def pytest_runtest_logstart(nodeid, location):
    global _current
    _flush(_current)
    _current = nodeid
    recorder.M.restart_events()


def pytest_runtest_logfinish(nodeid, location):
    global _current
    _flush(nodeid)
    _current = "<between>"


def pytest_sessionfinish(session):
    _flush(_current)
    out = os.environ.get("COVSUITE_OUT")
    if not out:
        return
    os.makedirs(out, exist_ok=True)
    table = [list(k) for k, _ in sorted(_table.items(), key=lambda kv: kv[1])]
    with open(os.path.join(out, f"suite-{os.getpid()}.json"), "w") as fh:
        json.dump({"table": table, "tests": _buckets}, fh)
