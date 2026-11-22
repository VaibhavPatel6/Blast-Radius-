"""Operand-type recorder for comparison sites (`is`, `is not`, `==`, `!=`) in sqlglot.

Installed by this directory's sitecustomize.py in every Python process started with
CMPMAP_DIR set: the suite's own process, xdist workers, spawned pool workers and
`python -c` subprocesses all inherit the environment. sqlglot modules under SRC are
compiled from source with each single comparison `a OP b` rewritten to
`__br_cmp__(site, op, a, b)`, which evaluates `a OP b` exactly as written (same operands,
same order, same result, same exceptions) and records, once per site, the TYPES of the two
operands with two properties of each type: whether its equality is the default identity
equality, and whether it is an Enum whose equality is not overridden by the enum classes.
No values are recorded. A site is "path:line:col" of the comparison in the pinned source.
"""

import ast
import builtins
import enum
import importlib.machinery
import json
import os
import sys

SRC = os.environ.get("CMPMAP_SRC", "/src/")
OPS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3}
_seen: set = set()
_types: dict = {}
_out = None


def _type_info(t: type) -> list:
    info = _types.get(t)
    if info is None:
        # identity equality: object's own __eq__/__ne__, or None (NoneType's __eq__ returns
        # NotImplemented for anything but None itself, so it compares by identity)
        default = t is type(None) or (getattr(t, "__eq__", None) is object.__eq__
                                      and getattr(t, "__ne__", None) is object.__ne__)
        plain_enum = issubclass(t, enum.Enum) and not any(
            ("__eq__" in vars(c) or "__ne__" in vars(c)) for c in t.__mro__
            if isinstance(c, enum.EnumMeta) and c is not enum.Enum)
        info = [f"{t.__module__}.{t.__qualname__}", default, plain_enum]
        _types[t] = info
    return info


def _record(site: str, a, b) -> None:
    global _out
    key = (site, type(a), type(b))
    if key in _seen:
        return
    _seen.add(key)
    try:
        if _out is None:
            _out = open(os.path.join(os.environ["CMPMAP_DIR"], f"cmp-{os.getpid()}.jsonl"), "a")
        _out.write(json.dumps([site, _type_info(type(a)), _type_info(type(b))]) + "\n")
        _out.flush()
    except Exception:  # noqa: BLE001 - recording must never change the program
        pass


def __br_cmp__(site, op, a, b):
    if (site, type(a), type(b)) not in _seen:
        _record(site, a, b)
    if op == 0:
        return a is b
    if op == 1:
        return a is not b
    if op == 2:
        return a == b
    return a != b


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel: str):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and type(node.ops[0]) in OPS:
            call = ast.Call(func=ast.Name("__br_cmp__", ast.Load()),
                            args=[ast.Constant(f"{self.rel}:{node.lineno}:{node.col_offset}"),
                                  ast.Constant(OPS[type(node.ops[0])]), node.left, node.comparators[0]],
                            keywords=[])
            return ast.copy_location(call, node)
        return node


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        source = self.get_data(self.path)
        tree = ast.parse(source, self.path)
        tree = ast.fix_missing_locations(_Rewrite(self.path[len(SRC):]).visit(tree))
        return compile(tree, self.path, "exec", dont_inherit=True)


class _Finder:
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or not spec.origin.endswith(".py") or not spec.origin.startswith(SRC):
            return spec
        spec.loader = _Loader(fullname, spec.origin)
        return spec


def install() -> None:
    builtins.__br_cmp__ = __br_cmp__
    sys.meta_path.insert(0, _Finder)
