"""pytest plugin for the labeler: records every test id that pytest reported as failed.

A test counts as failed if any report for it failed: setup, call, teardown or any
subtest. Files that fail to collect are recorded separately. Each process (the
xdist controller and every worker) writes its own file, and the labeler takes the union.
"""

import json
import os

_failed = set()
_seen = set()
_collect_errors = set()


def pytest_runtest_logreport(report):
    _seen.add(report.nodeid)
    if report.failed:
        _failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _collect_errors.add(report.nodeid)


def pytest_sessionfinish(session):
    out = os.environ.get("OUTCOME_DIR")
    if not out:
        return
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, f"outcome-{os.getpid()}.json")
    with open(path, "w") as fh:
        json.dump(
            {"failed": sorted(_failed), "seen": sorted(_seen), "collect_errors": sorted(_collect_errors)},
            fh,
        )
