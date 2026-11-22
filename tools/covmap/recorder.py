"""Records which lines and code objects run, and which files are opened, in this process.

Uses sys.monitoring (Python 3.12+), the interpreter's built-in event API: we ask for a
callback on the first execution of each line (LINE) and each code object (PY_START),
and return DISABLE from the callback so each location reports once and then runs at
full speed. An audit hook (sys.addaudithook) sees file opens, directory listings and
process starts. Audit hooks cannot be removed, which is fine in a throwaway process.
"""

import os
import sys

M = sys.monitoring
TOOL = M.COVERAGE_ID
_FILE_EVENTS = {"open"}
_LIST_EVENTS = {"os.listdir", "os.scandir", "glob.glob", "os.walk", "pathlib.Path.glob", "pathlib.Path.rglob"}
_SPAWN_EVENTS = {"subprocess.Popen", "os.posix_spawn", "os.posix_spawnp", "os.exec", "os.system", "os.fork",
                 "os.forkpty", "os.spawn", "_posixsubprocess.fork_exec"}


class Recorder:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.lines: dict[str, set] = {}
        self.code: set = set()
        self.opened: set = set()
        self.listed: set = set()
        self.spawned: set = set()
        self.active = False

    def _line(self, code, line):
        f = code.co_filename
        if f.startswith(self.prefix):
            s = self.lines.get(f)
            if s is None:
                s = self.lines[f] = set()
            s.add(line)
        return M.DISABLE

    def _start(self, code, offset):
        f = code.co_filename
        if f.startswith(self.prefix):
            self.code.add((f, code.co_qualname, code.co_firstlineno))
        return M.DISABLE

    def _audit(self, event, args):
        if not self.active:
            return
        if event in _FILE_EVENTS or event in _LIST_EVENTS:
            p = args[0] if args else None
            if isinstance(p, bytes):
                p = os.fsdecode(p)
            if not isinstance(p, str):
                return
            if not os.path.isabs(p):
                p = os.path.join(os.getcwd(), p)
            p = os.path.normpath(p)
            if not p.startswith(self.prefix) and p + "/" != self.prefix:
                return
            if event in _FILE_EVENTS:
                if not p.endswith(".pyc"):
                    self.opened.add(p)
            else:
                self.listed.add(p)
        elif event in _SPAWN_EVENTS:
            self.spawned.add(event)

    def start(self):
        M.use_tool_id(TOOL, "covmap")
        M.register_callback(TOOL, M.events.LINE, self._line)
        M.register_callback(TOOL, M.events.PY_START, self._start)
        M.set_events(TOOL, M.events.LINE | M.events.PY_START)
        sys.addaudithook(self._audit)
        self.active = True

    def stop(self):
        self.active = False
        M.set_events(TOOL, 0)

    def result(self) -> dict:
        return {
            "lines": {f: sorted(s) for f, s in self.lines.items()},
            "code": sorted(self.code),
            "opened": sorted(self.opened),
            "listed": sorted(self.listed),
            "spawned": sorted(self.spawned),
        }


def merge(into: dict, other: dict) -> None:
    for f, ls in other.get("lines", {}).items():
        into["lines"][f] = sorted(set(into["lines"].get(f, [])) | set(ls))
    into["code"] = sorted({tuple(c) for c in into["code"]} | {tuple(c) for c in other.get("code", [])})
    for key in ("opened", "listed", "spawned"):
        into[key] = sorted(set(into[key]) | set(other.get(key, [])))
