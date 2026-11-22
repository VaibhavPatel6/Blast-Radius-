"""Patch analysis: which tests could a patch affect?

`candidates(changes, maps)` returns (test ids, reasons), or raises Unbounded when the
change cannot be bounded, in which case the caller lists every test.

The question we answer for each changed file is "which tests could observe this?":

- A Python file is compiled before and after. Python turns each function, lambda,
  class body and module body into a *code object*. We compare the two trees of code
  objects while ignoring line numbers, so an edit that only moves code (a comment, a
  blank line) changes nothing. For each code object whose own code changed, the
  candidates are the tests that entered it when we ran each test alone (maps/).
  A module or class body runs at import, so "entered the module body" means
  "imported the module".
- Any other file: the tests that opened it as data. A file added where none was:
  the tests that listed its directory.
- Files that steer pytest itself (conftest.py, pytest/tox ini files, config sections),
  binary patches, and anything we cannot parse: Unbounded.
"""

import configparser
import dis
import tomllib
import types

from br.maps import Maps


class Unbounded(Exception):
    pass


# Files pytest or the interpreter read on their own, wherever they sit in the tree.
ALWAYS_UNBOUNDED = {"conftest.py", "pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "tox.ini",
                    "sitecustomize.py", "usercustomize.py"}
# Top-level folders of the pinned tree (a new file inside one of these is not at the root).
TOP_LEVEL = {"benchmarks", "pdoc", "posts", "sqlglot", "sqlglot-integration-tests", "sqlglotc", "tests",
             ".github"}
# Files that can hold a pytest section; only that section matters to the test run.
CONFIG_FILES = {"setup.cfg", "pyproject.toml"}

_CODE_FIELDS = ("co_code", "co_names", "co_varnames", "co_freevars", "co_cellvars", "co_flags",
                "co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_nlocals", "co_name",
                "co_qualname", "co_exceptiontable", "co_stacksize")


def _children(code: types.CodeType) -> list[types.CodeType]:
    return [c for c in code.co_consts if isinstance(c, types.CodeType)]


def _keyed_children(code: types.CodeType) -> dict[tuple, types.CodeType]:
    """Children keyed by (qualname, n-th with that qualname): lambdas share a qualname."""
    seen: dict[str, int] = {}
    out = {}
    for c in _children(code):
        n = seen.get(c.co_qualname, 0)
        seen[c.co_qualname] = n + 1
        out[(c.co_qualname, n)] = c
    return out


def _const_key(c):
    """A comparable, hashable form of a constant that keeps types apart (1, 1.0 and True
    are equal in Python but are different code) and ignores set ordering."""
    if isinstance(c, types.CodeType):
        return ("<code>", c.co_qualname)
    if isinstance(c, tuple):
        return ("tuple", tuple(_const_key(x) for x in c))
    if isinstance(c, frozenset):
        return ("frozenset", frozenset(_const_key(x) for x in c))
    if isinstance(c, (float, complex)):
        return (type(c).__name__, repr(c))  # keeps -0.0 and nan distinct
    return (type(c).__name__, c)


def _firstlineno_const(code: types.CodeType) -> int | None:
    """Index of the constant a class body stores as __firstlineno__ (Python 3.13+), if that
    constant is used nowhere else. It is a line number, so a line inserted above the
    class changes it; masking it keeps "only lines moved" from counting as a change."""
    if "__firstlineno__" not in code.co_names:
        return None
    loads: dict[int, int] = {}
    target = None
    previous = None
    for ins in dis.get_instructions(code):
        if ins.opname == "LOAD_CONST":
            loads[ins.arg] = loads.get(ins.arg, 0) + 1
        if ins.opname == "STORE_NAME" and ins.argval == "__firstlineno__" and previous is not None \
                and previous.opname == "LOAD_CONST":
            target = previous.arg
        previous = ins
    if target is None or loads.get(target) != 1 or type(code.co_consts[target]) is not int:
        return None
    return target


def _own_signature(code: types.CodeType) -> tuple:
    """Everything about a code object except line numbers and the bodies of its children."""
    masked = _firstlineno_const(code)
    consts = tuple(("<line>",) if i == masked else _const_key(c) for i, c in enumerate(code.co_consts))
    return tuple(getattr(code, f) for f in _CODE_FIELDS) + (consts,)


def changed_code_objects(old: types.CodeType, new: types.CodeType) -> list[types.CodeType]:
    """Old code objects whose own code differs in the new tree (ignoring line numbers)."""
    changed = []
    if _own_signature(old) != _own_signature(new):
        changed.append(old)
    old_kids, new_kids = _keyed_children(old), _keyed_children(new)
    if set(old_kids) != set(new_kids) and old not in changed:
        changed.append(old)
    for key, child in old_kids.items():
        if key in new_kids:
            changed.extend(changed_code_objects(child, new_kids[key]))
    return changed


# A changed Python file larger than this is not compiled (compiling a huge file can
# take gigabytes); the commit lists every test instead. sqlglot's largest file is ~0.4 MB.
MAX_PY_BYTES = 2_000_000


def _compile(text: str, path: str):
    """Compile the file the way import does: from bytes, so a coding declaration or a
    byte-order mark takes effect exactly as it would for Python's import system."""
    data = text.encode("utf-8")
    if len(data) > MAX_PY_BYTES:
        raise Unbounded(f"{path}: {len(data)} bytes is too large to analyse")
    try:
        return compile(data, "/src/" + path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, UnicodeDecodeError, LookupError, MemoryError, RecursionError):
        return None


def _pytest_collects(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith("tests/") and name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py") or name == "__init__.py")


def _pytest_section(name: str, text: str | None):
    if text is None:
        return None
    if name == "pyproject.toml":
        return tomllib.loads(text).get("tool", {}).get("pytest")
    parser = configparser.RawConfigParser()
    parser.read_string(text)
    return dict(parser.items("tool:pytest")) if parser.has_section("tool:pytest") else None


def _pytest_config_changed(name: str, old: str | None, new: str | None) -> bool:
    try:
        return _pytest_section(name, old) != _pytest_section(name, new)
    except Exception:  # unreadable config: assume it matters
        return True


def _all_keys(code: types.CodeType, out: set) -> set:
    out.add(f"{code.co_qualname}@{code.co_firstlineno}")
    for c in _children(code):
        _all_keys(c, out)
    return out


def _listing_dir(path: str, is_dir) -> str:
    """The directory whose listing would reveal a new file at `path`: its parent if that
    exists in the pinned tree, else the nearest ancestor that does (a new folder is
    noticed by whoever lists the folder it was created in)."""
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    while directory and not is_dir(directory):
        directory = directory.rsplit("/", 1)[0] if "/" in directory else ""
    return directory


def candidates(changes, maps: Maps, is_dir) -> tuple[set[str], list[str]]:
    """is_dir(relative path) says whether a directory exists in the pinned tree."""
    found: set[str] = set()
    reasons: list[str] = []
    for ch in changes:
        for path in {p for p in (ch.old_path, ch.new_path) if p}:
            name = path.rsplit("/", 1)[-1]
            if name in ALWAYS_UNBOUNDED or path.endswith(".pth"):
                raise Unbounded(f"{path} is read by pytest or the interpreter on its own")
            added_or_removed = (path == ch.new_path and ch.old_path != path) or (path == ch.old_path and ch.new_path != path)
            added = path == ch.new_path and ch.old_path != path
            if added and ("/" not in path or path.split("/", 1)[0] not in TOP_LEVEL):
                # The repository root is on sys.path (before site-packages) and is scanned
                # for package metadata: a new file or folder there can shadow any import.
                raise Unbounded(f"{path}: new entry at the repository root")
            if added_or_removed and _pytest_collects(path):
                # pytest imports every collected test file before any test runs, so a new
                # one can change state every test sees; a removed one changes the set.
                raise Unbounded(f"{path}: test module added or removed")
            if path in CONFIG_FILES:
                old = ch.old_text if path == ch.old_path else None
                new = ch.new_text if path == ch.new_path else None
                if _pytest_config_changed(name, old, new):
                    raise Unbounded(f"{path}: pytest section changed")
        if ch.mode_change and ch.old_path == ch.new_path and ch.old_text == ch.new_text:
            continue  # permission bits only
        if ch.old_path is not None and ch.new_path is not None and ch.old_path != ch.new_path:
            parts = [(ch.old_path, ch.old_text, None), (ch.new_path, None, ch.new_text)]
        else:
            parts = [(ch.old_path or ch.new_path, ch.old_text, ch.new_text)]
        for path, old, new in parts:
            got, why = _file_candidates(path, old, new, maps, is_dir)
            found |= got
            reasons.append(f"{path}: {why} -> {len(got)}")
    return found, reasons


def _file_candidates(path: str, old: str | None, new: str | None, maps: Maps, is_dir) -> tuple[set[str], str]:
    out: set[str] = set()
    if old is not None and new is not None and old == new:
        return out, "unchanged"
    # readers of the file as data, whatever its type
    out |= maps.opened(path)
    directory = _listing_dir(path, is_dir)
    if old is None:  # added: whoever listed the directory could see it
        out |= maps.listed(directory)
        return out, "added file: directory listers"
    if not path.endswith(".py"):
        if new is None:  # deleted: its readers, and whoever listed its directory
            out |= maps.listed(directory)
            return out, "deleted data file: readers and directory listers"
        return out, "data file: readers"
    old_code = _compile(old, path)
    if old_code is None:
        raise Unbounded(f"{path}: pinned file does not compile")
    # The maps name code objects as compiled when they were built. If this interpreter
    # compiles the pinned file differently, a lookup could silently find nothing.
    unknown = maps.code_keys(path) - _all_keys(old_code, set())
    if unknown:
        raise Unbounded(f"{path}: maps and this interpreter disagree on {len(unknown)} code objects")
    module_key = (old_code.co_qualname, old_code.co_firstlineno)
    if new is None:
        out |= maps.entered(path, *module_key) | maps.listed(directory)
        return out, "deleted module: importers"
    new_code = _compile(new, path)
    if new_code is None:
        out |= maps.entered(path, *module_key)
        return out, "syntax error: importers"
    changed = changed_code_objects(old_code, new_code)
    if not changed:
        return out, "inert (code objects identical)"
    for c in changed:
        out |= maps.entered(path, c.co_qualname, c.co_firstlineno)
    return out, f"{len(changed)} code objects changed: " + ",".join(c.co_qualname for c in changed[:4])
