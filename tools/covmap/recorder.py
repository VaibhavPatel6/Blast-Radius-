"""Records which lines and code objects run, and which files are opened, in this process.

Uses sys.monitoring (Python 3.12+), the interpreter's built-in event API: we ask for a
callback on the first execution of each line (LINE) and each code object (PY_START),
and return DISABLE from the callback so each location reports once and then runs at
full speed. An audit hook (sys.addaudithook) sees file opens, directory listings and
process starts. Audit hooks cannot be removed, which is fine in a throwaway process.
"""

import builtins
import enum
import os
import sys
import types

M = sys.monitoring
TOOL = M.COVERAGE_ID
_FILE_EVENTS = {"open"}
_LIST_EVENTS = {"os.listdir", "os.scandir", "glob.glob", "os.walk", "pathlib.Path.glob", "pathlib.Path.rglob"}
_SPAWN_EVENTS = {"subprocess.Popen", "os.posix_spawn", "os.posix_spawnp", "os.exec", "os.system", "os.fork",
                 "os.forkpty", "os.spawn", "_posixsubprocess.fork_exec"}


def _phase(frame, prefix: str) -> str:
    """"import" when the nearest relevant frame is a sqlglot module body (the read happens
    while a sqlglot module is being imported, even if a test triggered that import), "run"
    when test code is nearer (the test, or code it calls, reads the value)."""
    while frame is not None:
        name = frame.f_code.co_filename
        if name.startswith(prefix + "tests/"):
            return "run"
        if frame.f_code.co_name == "<module>" and name.startswith(prefix + "sqlglot/"):
            return "import"
        frame = frame.f_back
    return "run"


class Recorder:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.lines: dict[str, set] = {}
        self.code: set = set()
        self.opened: set = set()
        self.listed: set = set()
        self.spawned: set = set()
        self.touched: set = set()  # class bodies (file, qualname, firstlineno) whose attributes were read
        self.wrapped: set = set()  # (file, qualname, firstlineno, name) actually instrumented
        self.active = False
        self._build_class = None

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

    def _instrument(self, cls, body):
        """Replace each attribute defined in a sqlglot class with a stand-in that records
        the first read of that attribute (through the class, a subclass or an instance)
        and then puts the original back. Dunder names, data descriptors (properties,
        slots) and enums are left alone."""
        if isinstance(cls, enum.EnumMeta):
            return
        file, qual, first = body.co_filename, body.co_qualname, body.co_firstlineno
        recorder = self

        class Touch:
            __slots__ = ("value", "name")

            def __init__(self, value, name):
                self.value = value
                self.name = name

            def __get__(self, instance, owner=None):
                value = self.value
                if recorder.active:
                    phase = _phase(sys._getframe(1), recorder.prefix)
                    recorder.touched.add((file, qual, first, self.name, phase))
                    # Keep watching through import-time reads, so the first read made by the
                    # test itself is recorded too; stop watching after that.
                    if phase == "run" and cls.__dict__.get(self.name) is self:
                        type.__setattr__(cls, self.name, value)
                getter = getattr(type(value), "__get__", None)
                return getter(value, instance, owner) if getter is not None else value

        for name, value in list(cls.__dict__.items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            if hasattr(type(value), "__set__") or isinstance(value, (types.MemberDescriptorType,
                                                                     types.GetSetDescriptorType)):
                continue
            try:
                type.__setattr__(cls, name, Touch(value, name))
                recorder.wrapped.add((file, qual, first, name))
            except (TypeError, AttributeError):
                pass

    def _install_class_hook(self):
        original = builtins.__build_class__
        self._build_class = original
        prefix = self.prefix + "sqlglot/"
        recorder = self

        def build_class(func, name, *bases, **kwargs):
            cls = original(func, name, *bases, **kwargs)
            try:
                body = func.__code__
                if body.co_filename.startswith(prefix) and isinstance(cls, type):
                    recorder._instrument(cls, body)
            except Exception:  # never let recording change behaviour
                pass
            return cls

        builtins.__build_class__ = build_class

    def start(self):
        if os.environ.get("COVMAP_CLASSES") == "1":
            self._install_class_hook()
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
            "touched": sorted(self.touched),
            "wrapped": sorted(self.wrapped),
        }


def merge(into: dict, other: dict) -> None:
    for f, ls in other.get("lines", {}).items():
        into["lines"][f] = sorted(set(into["lines"].get(f, [])) | set(ls))
    into["code"] = sorted({tuple(c) for c in into["code"]} | {tuple(c) for c in other.get("code", [])})
    for key in ("opened", "listed", "spawned"):
        into[key] = sorted(set(into[key]) | set(other.get(key, [])))
    into["touched"] = sorted({tuple(c) for c in into.get("touched", [])} | {tuple(c) for c in other.get("touched", [])})
    into["wrapped"] = sorted({tuple(c) for c in into.get("wrapped", [])} | {tuple(c) for c in other.get("wrapped", [])})
