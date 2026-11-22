"""Fingerprints of import-time state, to see exactly what a class-level edit changed.

`fingerprint_modules(prefix)` walks every loaded module whose name starts with
`prefix` and returns {"module:Class.Nested": {attribute: digest}} for every class
defined there, after Python has run the class bodies and every class-creation hook
(metaclasses, __init_subclass__). Comparing the pinned import with a patched import
names the attributes that differ, including attributes those hooks derive from others
(a keyword trie built from KEYWORDS, required_args built from arg_types), without
having to know how they are derived.

Digests are structural: two separately imported but equal values get the same digest.
Functions are compared by their code (ignoring line numbers), defaults and closure
contents, not by identity. Anything we cannot fingerprint gets a digest that never
matches, which can only widen the candidates.
"""

import enum
import hashlib
import sys
import types

_CODE_CACHE: dict[int, tuple] = {}  # id(code) -> (code, digest); the code is kept alive


def _code_hex(code: types.CodeType) -> str:
    """Digest of a code object and its nested code objects, without line numbers."""
    hit = _CODE_CACHE.get(id(code))
    if hit is not None and hit[0] is code:
        return hit[1]
    from br.analyze import _own_signature

    h = hashlib.sha256(repr(_own_signature(code)).encode())
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            h.update(_code_hex(c).encode())
    digest = h.hexdigest()
    _CODE_CACHE[id(code)] = (code, digest)
    return digest


_PLAIN_KEYS = (str, int, bool, float, bytes, type(None))
# Attributes that are caches of the import machinery, not program state: the abc module's
# per-class registry cache (a C object that cannot be compared across processes).
_SKIPPED_NAMES = {"__dict__", "__weakref__", "__doc__", "__module__", "__firstlineno__",
                  "__static_attributes__", "_abc_impl"}


def instrumented(name: str, value) -> bool:
    """Whether the attribute-read recorder (tools/covmap/recorder.py) wraps this attribute,
    so that the maps can name its readers. Must mirror `_instrument` there."""
    if name.startswith("__") and name.endswith("__"):
        return False
    if hasattr(type(value), "__set__") or isinstance(value, (types.MemberDescriptorType,
                                                            types.GetSetDescriptorType)):
        return False
    return True


class _Digest:
    def __init__(self, limit: int = 200_000):
        self.limit = limit  # nodes visited per attribute; beyond it the value is "unknown"
        # Within one pass nothing changes, so a shared object (a function reused by many
        # subclass tables, a trie) is digested once. `keep` holds the objects so their
        # ids cannot be reused by new objects during the pass.
        self.memo: dict[int, str] = {}
        self.keep: list = []
        self.cycles = 0  # cycle markers emitted so far; a value that met one is not memoised

    def __call__(self, value) -> str:
        h = hashlib.sha256()
        self.count = 0
        self.stack: set[int] = set()
        try:
            self._walk(value, h)
        except (_TooBig, RecursionError):
            return "unknown:" + str(id(value))  # never equal across imports
        return h.hexdigest()

    def _sub(self, v) -> str:
        """Digest of a nested value (a set element or dict key), sharing this pass's memo."""
        h = hashlib.sha256()
        self._walk(v, h)
        return h.hexdigest()

    def _walk(self, v, h) -> None:
        self.count += 1
        if self.count > self.limit:
            raise _TooBig
        t = type(v)
        if v is None or t in (bool, int, float, complex, str, bytes):
            h.update(f"{t.__name__}:{v!r};".encode())
            return
        if isinstance(v, enum.Enum):
            h.update(f"enum:{type(v).__module__}.{type(v).__qualname__}.{v.name};".encode())
            return
        if isinstance(v, type):
            h.update(f"class:{v.__module__}.{v.__qualname__};".encode())
            return
        if isinstance(v, types.ModuleType):
            h.update(f"module:{v.__name__};".encode())
            return
        key = id(v)
        if key in self.stack:
            self.cycles += 1
            h.update(b"cycle;")
            return
        memo = self.memo.get(key)
        if memo is not None:
            h.update(memo.encode())
            return
        outer, h = h, hashlib.sha256()
        cycles_before = self.cycles
        self.stack.add(key)
        try:
            if t in (list, tuple):
                h.update(f"{t.__name__}[{len(v)}](".encode())
                for x in v:
                    self._walk(x, h)
                h.update(b")")
            elif t in (set, frozenset):
                parts = sorted(self._sub(x) for x in v)
                h.update(f"{t.__name__}{{{','.join(parts)}}};".encode())
            elif isinstance(v, (dict, types.MappingProxyType)):
                if all(type(k) in _PLAIN_KEYS or isinstance(k, enum.Enum) for k in v):
                    # Keys whose order does not depend on memory addresses: insertion order
                    # counts (sqlglot iterates some tables in order). Both sides are forked
                    # from one process, so they share a hash seed.
                    h.update(f"odict{{".encode())
                    for k, x in list(v.items()):
                        self._walk(k, h)
                        h.update(b":")
                        self._walk(x, h)
                else:
                    # Keyed by classes or other objects: such tables are often built by
                    # iterating sets of classes, in an order that follows memory addresses
                    # and differs between two imports; compare their content only.
                    items = sorted(((self._sub(k), x) for k, x in v.items()), key=lambda p: p[0])
                    h.update(b"udict{")
                    for dk, x in items:
                        h.update(dk.encode() + b":")
                        self._walk(x, h)
                h.update(b"}")
            elif isinstance(v, types.FunctionType):
                h.update(b"function(" + _code_hex(v.__code__).encode())
                self._walk(v.__defaults__, h)
                self._walk(v.__kwdefaults__, h)
                for cell in v.__closure__ or ():
                    try:
                        self._walk(cell.cell_contents, h)
                    except ValueError:
                        h.update(b"emptycell;")
                h.update(b")")
            elif isinstance(v, (types.MethodType,)):
                h.update(b"method(")
                self._walk(v.__self__, h)  # exp.A.from_arg_list and exp.B.from_arg_list differ
                self._walk(v.__func__, h)
                h.update(b")")
            elif isinstance(v, (staticmethod, classmethod)):
                h.update(f"{t.__name__}(".encode())
                self._walk(v.__func__, h)
                h.update(b")")
            elif isinstance(v, property):
                h.update(b"property(")
                for f in (v.fget, v.fset, v.fdel):
                    self._walk(f, h)
                h.update(b")")
            elif isinstance(v, (types.BuiltinFunctionType, types.WrapperDescriptorType,
                                types.MethodDescriptorType, types.MemberDescriptorType,
                                types.GetSetDescriptorType)):
                h.update(f"builtin:{getattr(v, '__qualname__', repr(v))};".encode())
            elif t.__module__ in ("typing", "typing_extensions") or isinstance(v, (types.GenericAlias, types.UnionType)):
                h.update(f"typing:{v!r};".encode())  # annotations: their printed form is stable
            elif t.__name__ == "_tuplegetter":
                h.update(f"tuplegetter:{v.__reduce__()[1]!r};".encode())
            elif t.__name__ == "Pattern" and hasattr(v, "pattern"):
                h.update(f"re:{v.pattern!r}:{v.flags};".encode())
            elif hasattr(v, "__dict__") or hasattr(type(v), "__slots__"):
                # an instance (e.g. functools.partial, a trie node, an Expression): its type
                # and its attributes
                h.update(f"obj:{t.__module__}.{t.__qualname__}(".encode())
                if isinstance(v, __import__("functools").partial):
                    self._walk(v.func, h)
                    self._walk(v.args, h)
                    self._walk(v.keywords, h)
                state = getattr(v, "__dict__", None)
                if state is not None:
                    self._walk(dict(state), h)
                for slot in getattr(type(v), "__slots__", ()) or ():
                    if isinstance(slot, str) and hasattr(v, slot):
                        h.update(slot.encode())
                        self._walk(getattr(v, slot), h)
                h.update(b")")
            else:
                raise _TooBig  # unknown kind of value: never equal
        finally:
            self.stack.discard(key)
        digest = "m" + h.hexdigest()
        if self.cycles == cycles_before:  # its digest does not depend on where we came from
            self.memo[key] = digest
            self.keep.append(v)
        outer.update(digest.encode())


class _TooBig(Exception):
    pass


def _classes(module) -> list[tuple[str, type]]:
    out = []
    todo = [(name, v) for name, v in vars(module).items() if isinstance(v, type)
            and v.__module__ == module.__name__]
    seen = set()
    while todo:
        name, cls = todo.pop()
        if id(cls) in seen:
            continue
        seen.add(id(cls))
        out.append((cls.__qualname__, cls))
        for k, v in list(vars(cls).items()):
            if isinstance(v, type) and v.__module__ == module.__name__ and v.__qualname__.startswith(cls.__qualname__ + "."):
                todo.append((v.__qualname__, v))
    return out


def _kind(cls: type) -> str:
    """Classes whose members are built by machinery we do not model are never narrowed."""
    if isinstance(cls, enum.EnumMeta):
        return "enum"
    if "__dataclass_fields__" in vars(cls):
        return "dataclass"
    if issubclass(cls, tuple) and hasattr(cls, "_fields"):
        return "namedtuple"
    return "class"


def fingerprint_modules(prefix: str = "sqlglot", only: set[str] | None = None) -> dict[str, dict[str, str]]:
    """Fingerprints of the classes in loaded modules under `prefix` (or only `only`)."""
    digest = _Digest()
    out: dict[str, dict[str, str]] = {}
    for mod_name, module in sorted(list(sys.modules.items())):
        if module is None or not (mod_name == prefix or mod_name.startswith(prefix + ".")):
            continue
        if only is not None and mod_name not in only:
            continue
        for qual, cls in _classes(module):
            attrs = {"__kind__": _kind(cls)}
            for name, value in list(vars(cls).items()):
                if name in _SKIPPED_NAMES:
                    continue
                # "N:" marks attributes the read recorder never wraps: a change there
                # cannot be followed to its readers.
                attrs[name] = ("" if instrumented(name, value) and _kind(cls) == "class" else "N:") + digest(value)
            out[f"{mod_name}:{qual}"] = attrs
    return out


def diff(old: dict[str, dict[str, str]], new: dict[str, dict[str, str]]) -> dict[str, set[str]]:
    """{"module:Class": {changed attribute names}} for classes present in both; a class
    present in only one is reported with the attribute "*"."""
    out: dict[str, set[str]] = {}
    for cls in set(old) | set(new):
        a, b = old.get(cls), new.get(cls)
        if a is None or b is None:
            out[cls] = {"*"}
            continue
        changed = {n for n in set(a) | set(b) if a.get(n) != b.get(n)}
        if changed:
            out[cls] = changed
    return out


def _probe_child(tree: str, modules: list[str], write_fd: int) -> None:
    """Forked child: import sqlglot and `modules` from `tree`, send fingerprints of every
    loaded sqlglot class down the pipe. Never returns."""
    import json
    import os

    try:
        os.setpgid(0, 0)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.chdir(tree)
        sys.path.insert(0, tree)
        import importlib

        import pytest  # noqa: F401 - sqlglot reads sys.modules for unittest/pytest at import
        importlib.import_module("sqlglot")
        for name in modules:
            importlib.import_module(name)
        data = json.dumps({"ok": True, "fp": fingerprint_modules("sqlglot")}).encode()
    except BaseException as exc:  # noqa: BLE001 - the caller falls back to importers
        data = json.dumps({"ok": False, "why": repr(exc)[-300:]}).encode()
    view = memoryview(data)
    while view:
        try:
            n = os.write(write_fd, view)
        except OSError:
            break
        view = view[n:]
    os._exit(0)


def _run_probe(tree: str, modules: list[str], timeout: float) -> dict | None:
    import json
    import os
    import select
    import signal
    import time

    read_fd, write_fd = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        _probe_child(tree, modules, write_fd)
    os.close(write_fd)
    chunks = []
    end = time.monotonic() + timeout
    ok = True
    while True:
        left = end - time.monotonic()
        if left <= 0 or not select.select([read_fd], [], [], left)[0]:
            ok = False
            break
        chunk = os.read(read_fd, 1 << 20)
        if not chunk:
            break
        chunks.append(chunk)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    os.waitpid(pid, 0)
    os.close(read_fd)
    if not ok:
        return None
    try:
        result = json.loads(b"".join(chunks))
    except ValueError:
        return None
    return result if result.get("ok") else None


_PINNED_CACHE: dict[tuple, dict] = {}


def changed_attributes(pinned_tree: str, patched_tree: str, modules: list[str], timeout: float):
    """Import the same modules from the pinned tree and from the patched view, in two fresh
    forked processes, and return {("module", "Class.Qual"): {attribute names}} that differ,
    or None when either side fails (the caller then falls back to every importer).

    A class that exists on one side only, or an attribute that exists on one side only,
    is reported as ("*") so the caller can fall back. An attribute whose value cannot be
    fingerprinted ("unknown:") on either side counts as changed when its class is defined
    in one of the patched modules; elsewhere it is noise from the import machinery
    (abc caches, dataclass fields) and is ignored.
    """
    key = (pinned_tree, tuple(modules))
    old = _PINNED_CACHE.get(key)  # the pinned side depends only on the module list
    if old is None:
        old = _run_probe(pinned_tree, modules, timeout)
        if old is None:
            return None
        _PINNED_CACHE[key] = old
    new = _run_probe(patched_tree, modules, timeout)
    if new is None:
        return None
    a, b = old["fp"], new["fp"]
    out = {}
    patched = set(modules)
    for key in set(a) | set(b):
        module, qual = key.split(":", 1)
        x, y = a.get(key), b.get(key)
        if x is None or y is None:
            out[(module, qual)] = {"*"}
            continue
        changed = set()
        for name in set(x) | set(y):
            u, v = x.get(name), y.get(name)
            if u is None or v is None:
                changed.add("*")
            elif u != v or "unknown:" in u or "unknown:" in v:
                # a value we cannot fingerprint counts as changed: harmless when no test
                # reads it, sound when one does
                changed.add("*" if u.startswith("N:") or v.startswith("N:") else name)
        if changed and (x.get("__kind__") != "class" or y.get("__kind__") != "class"):
            changed.add("*")  # enum, dataclass or named-tuple members: never narrowed
        if changed:
            out[(module, qual)] = changed
    return out
