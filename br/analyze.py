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
import tomllib
import types

from br.maps import Maps


class Unbounded(Exception):
    pass


# Files pytest or the interpreter read on their own, wherever they sit in the tree.
ALWAYS_UNBOUNDED = {"conftest.py", "pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "tox.ini",
                    "sitecustomize.py", "usercustomize.py"}
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


def _own_signature(code: types.CodeType) -> tuple:
    """Everything about a code object except line numbers and the bodies of its children."""
    consts = tuple(("<code>", c.co_qualname) if isinstance(c, types.CodeType) else (type(c).__name__, repr(c))
                   for c in code.co_consts)
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


def _compile(text: str, path: str):
    try:
        return compile(text, "/src/" + path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
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


def candidates(changes, maps: Maps) -> tuple[set[str], list[str]]:
    found: set[str] = set()
    reasons: list[str] = []
    for ch in changes:
        for path in {p for p in (ch.old_path, ch.new_path) if p}:
            name = path.rsplit("/", 1)[-1]
            if name in ALWAYS_UNBOUNDED or path.endswith(".pth"):
                raise Unbounded(f"{path} is read by pytest or the interpreter on its own")
            added_or_removed = (path == ch.new_path and ch.old_path != path) or (path == ch.old_path and ch.new_path != path)
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
            got, why = _file_candidates(path, old, new, maps)
            found |= got
            reasons.append(f"{path}: {why} -> {len(got)}")
    return found, reasons


def _file_candidates(path: str, old: str | None, new: str | None, maps: Maps) -> tuple[set[str], str]:
    out: set[str] = set()
    if old is not None and new is not None and old == new:
        return out, "unchanged"
    # readers of the file as data, whatever its type
    out |= maps.opened(path)
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    if old is None:  # added: whoever listed the directory could see it
        out |= maps.listed(directory)
        return out, "added file: directory listers"
    if not path.endswith(".py"):
        return out, "data file: readers"
    old_code = _compile(old, path)
    if old_code is None:
        raise Unbounded(f"{path}: pinned file does not compile")
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
