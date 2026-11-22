"""Verification: run candidate tests against the patched code, inside the container.

For one commit:

1. `build_tree` makes a patched view of the pinned tree under /tmp without copying it:
   directories on the path to a changed file are real, everything else is a symbolic
   link into /opt/sqlglot. A changed module gets no link to its old bytecode, so Python
   compiles the new source. /tmp holds 64 MB; a view takes a few kilobytes.
2. `run` forks a child process. The child sends its stdout and stderr to /dev/null
   (a test that prints must never reach our answer stream), changes into the view and
   runs the candidate tests with pytest itself, in that process, reporting each verdict
   on a pipe as soon as it is known. It stops at the first failure (`-x`). Using real
   pytest keeps our verdicts identical to the organizers' (pytest's subtests, for
   example, format parameters that plain unittest never touches).
3. The parent reads verdicts with a watchdog: a test that runs far past its recorded
   time is treated as failed, and its process group is killed.

`fork` copies the running process: the child starts with everything the parent had
imported (never sqlglot), so it skips Python's start-up and those imports.
"""

import json
import os
import select
import shutil
import signal
import sys
import time
from pathlib import Path

PINNED = Path("/opt/sqlglot")

# A test is declared failed when it runs longer than this many times its recorded
# duration plus a floor (seconds). A mutation can turn a 3 ms test into an endless
# loop; the floor absorbs import time and slow first calls.
WATCHDOG_FACTOR = 20
WATCHDOG_FLOOR = 3.0
# The first test of each class also pays for importing its module and setUpClass
# (sqlglot's optimizer tests build duckdb tables and a process pool there).
WATCHDOG_CLASS_SETUP = 30.0


def build_tree(root: Path, changes) -> Path:
    """A patched view of the pinned tree at `root` (which must not exist)."""
    root.mkdir(parents=True)
    new_files: dict[str, str | None] = {}
    for ch in changes:
        if ch.old_path is not None:
            new_files.setdefault(ch.old_path, None)
        if ch.new_path is not None:
            new_files[ch.new_path] = ch.new_text
    real_dirs = {""}
    for path in new_files:
        parts = path.split("/")
        for i in range(1, len(parts)):
            real_dirs.add("/".join(parts[:i]))
    # modules whose bytecode must not be reused
    stale_pyc = set()
    for path in new_files:
        if path.endswith(".py"):
            d, _, name = path.rpartition("/")
            stale_pyc.add((d, name[:-3]))
            real_dirs.add((d + "/" if d else "") + "__pycache__")
    for d in sorted(real_dirs, key=len):
        target = root / d if d else root
        target.mkdir(exist_ok=True)
        source = PINNED / d if d else PINNED
        if not source.is_dir():
            continue
        for entry in os.listdir(source):
            rel = f"{d}/{entry}" if d else entry
            if rel in real_dirs or rel in new_files:
                continue
            if d.endswith("__pycache__"):
                parent = d[: -len("__pycache__")].rstrip("/")
                if (parent, entry.split(".", 1)[0]) in stale_pyc:
                    continue
            os.symlink(source / entry, target / entry)
    for path, text in new_files.items():
        if text is not None:
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(text.encode("utf-8"))
    return root


def remove_tree(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


def _child_pytest(tree: Path, test_ids: list[str], write_fd: int) -> None:
    """Runs in the forked child: real pytest, in this process, on the candidate ids, with
    the same notion of failure as the labels (any failed report for a test, including
    setup, teardown and subtests; a file that fails to collect fails all its tests).
    Never returns."""
    try:
        os.setpgid(0, 0)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.chdir(tree)
        sys.path.insert(0, str(tree))
        out = os.fdopen(write_fd, "w", buffering=1)
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"  # xdist/timeout are not needed here
        import pytest

        class Reporter:
            def __init__(self):
                self.bad = set()

            def pytest_runtest_logreport(self, report):
                if report.failed:
                    self.bad.add(report.nodeid)

            def pytest_runtest_logfinish(self, nodeid, location):
                out.write(json.dumps({"id": nodeid, "ok": nodeid not in self.bad, "why": ""}) + "\n")

            def pytest_collectreport(self, report):
                if report.failed:
                    out.write(json.dumps({"id": "*" + report.nodeid, "ok": False, "why": "collection error"}) + "\n")

        pytest.main(["-q", "-x", "-p", "no:cacheprovider", "--assert=plain", "-o", "addopts=", *test_ids],
                     plugins=[Reporter()])
        out.flush()
    except BaseException as exc:  # noqa: BLE001
        try:
            os.write(write_fd, (json.dumps({"id": "*", "ok": False, "why": repr(exc)[-300:]}) + "\n").encode())
        except OSError:
            pass
    os._exit(0)


def run(tree: Path, tests: list[dict], deadline: float) -> dict:
    """Run `tests` (dicts with id and seconds), cheapest first. Returns
    {"passed": [ids], "failed": [ids], "unknown": [ids], "why": str}."""
    order = sorted(tests, key=lambda t: (t["seconds"], t["id"]))
    seconds = {t["id"]: t["seconds"] for t in order}
    # Group by class so each setUpClass runs once, classes ordered by their cheapest test.
    order.sort(key=lambda t: t["id"].rsplit("::", 1)[0])
    first = {}
    for t in order:
        first.setdefault(t["id"].rsplit("::", 1)[0], t["seconds"])
    order.sort(key=lambda t: (first[t["id"].rsplit("::", 1)[0]], t["id"].rsplit("::", 1)[0], t["seconds"]))
    ids = [t["id"] for t in order]
    read_fd, write_fd = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        _child_pytest(tree, ids, write_fd)
    try:
        os.setpgid(pid, pid)  # also done by the child; whichever runs first wins
    except OSError:
        pass
    os.close(write_fd)
    passed, failed = [], []
    why = ""
    pending = list(ids)
    buffer = b""
    started = time.monotonic()
    killed = False
    first_of_class = set()
    seen_classes = set()
    for tid in ids:
        cls = tid.rsplit("::", 1)[0]
        if cls not in seen_classes:
            seen_classes.add(cls)
            first_of_class.add(tid)
    while pending:
        budget = WATCHDOG_FLOOR + WATCHDOG_FACTOR * seconds[pending[0]]
        if pending[0] in first_of_class:
            budget += WATCHDOG_CLASS_SETUP
        wait_until = min(started + budget, deadline)
        timeout = wait_until - time.monotonic()
        ready = select.select([read_fd], [], [], max(0.0, timeout))[0] if timeout > 0 else []
        if not ready:
            if time.monotonic() < deadline:
                why = "watchdog"
                failed.append(pending.pop(0))
            else:
                why = "deadline"
            killed = True
            break
        chunk = os.read(read_fd, 65536)
        if not chunk:  # child exited
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            msg = json.loads(line)
            tid = msg["id"]
            if tid.startswith("*"):
                why = msg["why"]
                scope = tid[1:]
                hit = [p for p in pending if not scope or p.startswith(scope + "::")] or list(pending)
                for p in hit:
                    pending.remove(p)
                    failed.append(p)
                continue
            if tid in pending:
                pending.remove(tid)
                (passed if msg["ok"] else failed).append(tid)
                if not msg["ok"]:
                    why = why or msg["why"]
            started = time.monotonic()
    if killed or pending:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        os.killpg(pid, signal.SIGKILL)  # pool workers left behind
    except (ProcessLookupError, PermissionError):
        pass
    os.waitpid(pid, 0)
    os.close(read_fd)
    return {"passed": passed, "failed": failed, "unknown": pending, "why": why}
