"""Warm verification: run candidate tests against patched function bodies without paying
for imports on every commit.

A *zygote* process is forked once from the selector. It imports pytest, then sqlglot from
the pinned tree (`/opt/sqlglot`), then any test module a request needs; it never changes
any of them. For each request it forks a *worker*. The worker compiles the patched file,
gives every live function object whose code is a changed code object the new code
(`function.__code__ = new_code`), and runs the candidate tests with real pytest, writing
one JSON line per verdict to a pipe the selector reads.

Only for edits that this makes exact (the caller checks before asking): every changed code
object is a function body, not nested in another function, with the same free variables,
not run while modules are imported (so nothing computed from the old code is left in the
zygote), and no candidate starts child processes (those import sqlglot afresh from disk
and would never see the new code). Everything else uses a fresh import (br/verify.py) or
is listed.
"""

import gc
import json
import os
import select
import signal
import sys
import time
import types

PINNED = "/opt/sqlglot"


def _worker(request: dict, resp_fd: int) -> None:
    """Forked from the zygote. Never returns."""
    rid = request["id"]

    def send(obj) -> None:
        os.write(resp_fd, (json.dumps(dict(obj, id=rid)) + "\n").encode())

    try:
        os.setpgid(0, 0)
        send({"pid": os.getpid()})
        from br.analyze import changed_pairs

        swapped = 0
        for change in request["changes"]:
            path = change["path"]
            filename = os.path.join(PINNED, path)
            old = compile(change["old"].encode(), filename, "exec", dont_inherit=True)
            new = compile(change["new"].encode(), filename, "exec", dont_inherit=True)
            wanted = {}
            for o, n, kids_differ in changed_pairs(old, new):
                if kids_differ or not (o.co_flags & 0x01) or o.co_freevars != n.co_freevars:
                    raise RuntimeError(f"not swappable: {o.co_qualname}")
                wanted[(o.co_qualname, o.co_firstlineno)] = n
            module = path[:-3].replace("/", ".").removesuffix(".__init__")
            __import__(module)
            found = set()
            for obj in gc.get_objects():
                if isinstance(obj, types.FunctionType):
                    code = obj.__code__
                    key = (code.co_qualname, code.co_firstlineno)
                    if code.co_filename == filename and key in wanted:
                        obj.__code__ = wanted[key]
                        found.add(key)
                        swapped += 1
            if found != set(wanted):
                raise RuntimeError(f"live functions not found for {sorted(set(wanted) - found)}")
        send({"swapped": swapped})
        import pytest

        class Reporter:
            def __init__(self):
                self.bad = set()

            def pytest_runtest_logreport(self, report):
                if report.failed:
                    self.bad.add(report.nodeid)

            def pytest_runtest_logfinish(self, nodeid, location):
                send({"test": nodeid, "ok": nodeid not in self.bad})

            def pytest_collectreport(self, report):
                if report.failed:
                    send({"test": "*" + report.nodeid, "ok": False})

        pytest.main(["-q", "-x", "-p", "no:cacheprovider", "--assert=plain", "-o", "addopts=",
                     "--rootdir", PINNED, *request["tests"]], plugins=[Reporter()])
        send({"done": True})
    except BaseException as exc:  # noqa: BLE001
        try:
            send({"error": repr(exc)[-300:]})
        except OSError:
            pass
    os._exit(0)


def _zygote(req_fd: int, resp_fd: int) -> None:
    """Forked once from the selector. Serves requests until its input closes."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.chdir(PINNED)
        sys.path.insert(0, PINNED)
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        import pytest  # noqa: F401 - before sqlglot, which checks for a test framework at import
        import sqlglot  # noqa: F401

        reader = os.fdopen(req_fd, "r")
        for line in reader:
            request = json.loads(line)
            for module in request.get("warm", []):
                try:
                    __import__(module)
                except BaseException:  # noqa: BLE001 - the worker will report it
                    pass
            pid = os.fork()
            if pid == 0:
                _worker(request, resp_fd)
            os.waitpid(pid, 0)
    finally:
        os._exit(0)


class Warm:
    def __init__(self):
        self.req = None
        self.resp = None
        self.buffer = b""
        self.next_id = 0
        self.broken = False

    def _start(self) -> None:
        req_r, req_w = os.pipe()
        resp_r, resp_w = os.pipe()
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:
            os.close(req_w)
            os.close(resp_r)
            _zygote(req_r, resp_w)
        os.close(req_r)
        os.close(resp_w)
        self.zygote = pid
        self.req = os.fdopen(req_w, "w", buffering=1)
        self.resp = resp_r

    def _next(self, until: float):
        """The next JSON line from the workers, or None at `until` or if the pipe closed."""
        while b"\n" not in self.buffer:
            left = until - time.monotonic()
            if left <= 0 or not select.select([self.resp], [], [], left)[0]:
                return None
            chunk = os.read(self.resp, 65536)
            if not chunk:
                return None
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def run(self, changes: list[dict], tests: list[dict], budget_for, deadline: float) -> dict:
        """changes: [{"path", "old", "new"}]; tests: dicts with id and seconds, in order.
        budget_for(test) gives the watchdog seconds for a test. Returns the same shape as
        br.verify.run; on any trouble the tests are reported unknown (so they are listed)."""
        ids = [t["id"] for t in tests]
        result = {"passed": [], "failed": [], "unknown": list(ids), "why": ""}
        if self.broken:
            result["why"] = "warm unavailable"
            return result
        if self.req is None:
            self._start()
        self.next_id += 1
        rid = self.next_id
        modules = sorted({c["path"][:-3].replace("/", ".").removesuffix(".__init__") for c in changes}
                         | {t["id"].split("::")[0][:-3].replace("/", ".") for t in tests})
        try:
            self.req.write(json.dumps({"id": rid, "changes": changes, "tests": ids, "warm": modules}) + "\n")
        except OSError:
            self.broken = True
            result["why"] = "warm zygote gone"
            return result
        pending = list(ids)
        worker = None
        started = time.monotonic()
        first = True
        setup_until = min(deadline, time.monotonic() + 60)  # zygote imports + swap
        swapped = False
        while True:
            if not swapped:
                until = setup_until
            elif pending:
                until = min(deadline, started + budget_for(pending[0], first))
            else:
                until = min(deadline, time.monotonic() + 10)
            msg = self._next(until)
            if msg is None:
                result["why"] = "warm watchdog" if swapped else "warm timeout"
                break
            if msg.get("id") != rid:
                continue
            if "pid" in msg:
                worker = msg["pid"]
            elif "swapped" in msg:
                swapped = True
                started = time.monotonic()
            elif "error" in msg:
                result["why"] = "warm error " + msg["error"]
                break
            elif msg.get("done"):
                break
            else:
                test = msg["test"]
                if test.startswith("*"):
                    scope = test[1:]
                    for p in [p for p in pending if p.startswith(scope + "::") or p == scope]:
                        pending.remove(p)
                        result["failed"].append(p)
                    result["why"] = "collection error"
                elif test in pending:
                    pending.remove(test)
                    (result["passed"] if msg["ok"] else result["failed"]).append(test)
                started = time.monotonic()
                first = False
        if worker is not None:
            try:
                os.killpg(worker, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if worker is None and result["why"]:
            # the zygote itself did not answer: do not trust it again
            self.broken = True
            try:
                os.kill(self.zygote, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        elif result["why"] in ("warm watchdog", "warm timeout"):
            # drain what the killed worker may still have written for this request
            while self._next(time.monotonic() + 0.2) is not None:
                pass
        result["unknown"] = pending
        return result
