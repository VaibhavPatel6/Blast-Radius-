#!/usr/bin/env python3
"""Which sqlglot class attributes the TPC executor tests read during their EXECUTION parts
(the same split as tpcds_phases.py: setUpClass table loading, find_tables and
ensure_tables, planning and PythonExecutor.execute), on the pinned commit.

    python /tools/covmap/tpc_reads.py /out/tpc_reads.json     (dev image, tree at /src)

Every attribute defined in a sqlglot class body is replaced, right after the class is
created, by a stand-in descriptor (dunders, data descriptors and enums are left alone,
and listed as not instrumented). At the start of every execution phase each attribute is
armed again; its first read in that phase is recorded and the attribute put back. So a
read during execution is seen even when the same attribute was read at import or while
preparing the query. Reads in preparation phases are not recorded: their effect is
compared by the TPC check itself.

Output: {"instrumented": [["module", "Class.Qual", "name"], ...],
         "execution_reads": [["module", "Class.Qual", "name"], ...]}
"""

import builtins
import enum
import json
import os
import pickle
import sys
import types

sys.path.insert(0, "/src")
os.chdir("/src")
import pytest  # noqa: E402,F401 - sqlglot checks at import whether a test framework is loaded

PREFIX = "/src/sqlglot/"
armed: dict = {}      # (cls, name) -> stand-in currently installed
restored: list = []   # (cls, name) put back since the last arming
instrumented: set = set()
reads: set = set()
phase = {"execution": False}


def key(cls, name):
    return (cls.__module__, cls.__qualname__, name)


class Touch:
    __slots__ = ("cls", "name", "value")

    def __init__(self, cls, name, value):
        self.cls, self.name, self.value = cls, name, value

    def __get__(self, instance, owner=None):
        cls, name, value = self.cls, self.name, self.value
        if phase["execution"]:
            reads.add(key(cls, name))
        if cls.__dict__.get(name) is self:
            type.__setattr__(cls, name, value)
            armed.pop((cls, name), None)
            restored.append((cls, name))
        getter = getattr(type(value), "__get__", None)
        return getter(value, instance, owner) if getter is not None else value


def arm(cls, name) -> None:
    value = cls.__dict__.get(name)
    if isinstance(value, Touch):
        return
    try:
        stand_in = Touch(cls, name, value)
        type.__setattr__(cls, name, stand_in)
        armed[(cls, name)] = stand_in
    except (TypeError, AttributeError):
        pass


def instrument(cls) -> None:
    if isinstance(cls, enum.EnumMeta):
        return
    for name, value in list(cls.__dict__.items()):
        if name.startswith("__") and name.endswith("__"):
            continue
        if hasattr(type(value), "__set__") or isinstance(value, (types.MemberDescriptorType,
                                                                 types.GetSetDescriptorType)):
            continue
        instrumented.add(key(cls, name))
        restored.append((cls, name))  # armed at the first execution phase


original_build_class = builtins.__build_class__


def build_class(func, name, *bases, **kwargs):
    cls = original_build_class(func, name, *bases, **kwargs)
    try:
        if func.__code__.co_filename.startswith(PREFIX) and isinstance(cls, type):
            instrument(cls)
    except Exception:  # noqa: BLE001
        pass
    return cls


builtins.__build_class__ = build_class


def execution(on: bool) -> None:
    if on:
        while restored:
            arm(*restored.pop())
    phase["execution"] = on


# Every sqlglot module, as the TPC check imports them all: so that every class is
# instrumented, and an attribute of a class the executor tests never touch is recorded as
# instrumented and unread rather than unknown.
import importlib  # noqa: E402
import pkgutil  # noqa: E402

import sqlglot  # noqa: E402

for _info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if _info.name.rsplit(".", 1)[1] != "__main__":
        try:
            importlib.import_module(_info.name)
        except Exception:  # noqa: BLE001
            pass

import tests.test_executor as te  # noqa: E402
from sqlglot import find_tables, parse_one, transpile  # noqa: E402
from sqlglot.executor.python import PythonExecutor  # noqa: E402
from sqlglot.executor.table import ensure_tables  # noqa: E402
from sqlglot.optimizer import optimize  # noqa: E402
from sqlglot.planner import Plan  # noqa: E402
from sqlglot.schema import ensure_schema  # noqa: E402

execution(True)
te.TestExecutor.setUpClass()  # table loading, duckdb views
execution(False)
for schema, tables, sqls in ((te.TPCH_SCHEMA, te.TestExecutor.tpch_tables, te.TestExecutor.tpch_sqls),
                             (te.TPCDS_SCHEMA, te.TestExecutor.tpcds_tables, te.TestExecutor.tpcds_sqls)):
    execution(True)
    te.initializer(schema, tables)
    execution(False)
    for meta, sql, _ in sqls:
        expression = pickle.loads(pickle.dumps(parse_one(sql)))
        transpile(sql, write="duckdb")
        if meta.get("execute"):
            execution(True)
            found = {t.name: te._tables[t.name] for t in find_tables(expression)}
            tables_ = ensure_tables(found)
            execution(False)
            optimized = optimize(expression, ensure_schema(te._schema), leave_tables_isolated=True)
            optimized.sql(pretty=True)
            execution(True)
            PythonExecutor(tables=tables_).execute(Plan(optimized))
            execution(False)
builtins.__build_class__ = original_build_class
json.dump({"instrumented": sorted(instrumented), "execution_reads": sorted(reads)}, open(sys.argv[1], "w"))
print(f"{len(instrumented)} instrumented attributes, {len(reads)} read during execution", file=sys.stderr)
