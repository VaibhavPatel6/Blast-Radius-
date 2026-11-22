#!/usr/bin/env python3
"""Per executed TPC-DS query: which sqlglot class attributes it reads. Adapted from the slow-test
session's tools/covmap/tpc_reads.py (same stand-in descriptors, same instrumented set); each query
runs in its own fork of a process that has imported everything, loaded the tables
(setUpClass) and run the pool initializer, so a cache filled by an earlier query cannot hide a
read. Two sets per query: "execution" (find_tables and ensure_tables, Plan, PythonExecutor.execute:
the phases of tpc_reads.py) and "all" (also parse, the pickle round trip, transpile to DuckDB,
optimize and the pretty SQL).

    python tools/tpc_reads_queries.py OUT.json     (dev image, pinned tree at /src)

Output: {"tpc-ds": {"execution": {"module|Class.Qual|name": [queries]}, "all": {...}},
         "setup_reads": [...], "instrumented": [...]}

Original description of the instrumentation:
Which sqlglot class attributes the TPC executor tests read during their EXECUTION parts
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


# reads go to the current bucket instead of one set
bucket = {"name": None}
per_bucket: dict = {}
_orig_add = None


class _Reads(set):
    def add(self, item):  # called by Touch.__get__ when phase["execution"] is on
        b = bucket["name"]
        if b is not None:
            per_bucket.setdefault(b, set()).add(item)
        set.add(self, item)


reads = _Reads()
# Touch.__get__ looks the global "reads" up at call time, so rebinding it here is enough.

bucket["name"] = "setup"
execution(True)
te.TestExecutor.setUpClass()  # table loading, duckdb views
te.initializer(te.TPCDS_SCHEMA, te.TestExecutor.tpcds_tables)
execution(False)
setup_reads = set(per_bucket.get("setup", set()))
out = {"tpc-ds": {"execution": {}, "all": {}}, "setup_reads": sorted("|".join(k) for k in setup_reads),
       "instrumented": sorted("|".join(k) for k in instrumented)}
for i, (meta, sql, _) in enumerate(te.TestExecutor.tpcds_sqls):
    if not meta.get("execute"):
        continue
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        per_bucket.clear()
        try:
            bucket["name"] = "prep"
            execution(True)  # arm every attribute read so far
            expression = pickle.loads(pickle.dumps(parse_one(sql)))
            transpile(sql, write="duckdb")
            bucket["name"] = "exec"
            execution(True)  # arm again: a read in preparation is seen again in execution
            found = {t.name: te._tables[t.name] for t in find_tables(expression)}
            tables_ = ensure_tables(found)
            bucket["name"] = "prep"
            execution(True)
            optimized = optimize(expression, ensure_schema(te._schema), leave_tables_isolated=True)
            optimized.sql(pretty=True)
            bucket["name"] = "exec"
            execution(True)
            PythonExecutor(tables=tables_).execute(Plan(optimized))
            execution(False)
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        with os.fdopen(w, "w") as fh:
            json.dump({"ok": ok, "prep": sorted("|".join(k) for k in per_bucket.get("prep", set())),
                       "exec": sorted("|".join(k) for k in per_bucket.get("exec", set()))}, fh)
        os._exit(0)
    os.close(w)
    with os.fdopen(r) as fh:
        got = json.load(fh)
    os.waitpid(pid, 0)
    if not got["ok"]:
        print(f"query {i} failed", file=sys.stderr)
    for k in got["exec"]:
        out["tpc-ds"]["execution"].setdefault(k, []).append(i)
    for k in set(got["exec"]) | set(got["prep"]):
        out["tpc-ds"]["all"].setdefault(k, []).append(i)
builtins.__build_class__ = original_build_class
json.dump(out, open(sys.argv[1], "w"))
print(f"{len(instrumented)} instrumented; per-query execution reads {len(out['tpc-ds']['execution'])}, "
      f"all reads {len(out['tpc-ds']['all'])}, setup reads {len(setup_reads)}", file=sys.stderr)
