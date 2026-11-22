#!/usr/bin/env python3
"""What the TPC executor tests run and read during their EXECUTION parts, recorded on the
pinned commit along the real worker path: setUpClass (table loading), the pool
initializer, and test_executor.mp_execute itself (find_tables, then
sqlglot.executor.execute: ensure_tables, ensure_schema, the table-args check, Plan,
PythonExecutor). Only what execute() does from `optimize` up to `Plan` counts as
preparation: the optimized tree and its pretty SQL, which the TPC check compares.
Everything else execute() does is execution.

    python /tools/covmap/tpc_execution.py /out/tpc_execution.json   (dev image, tree at /src)

Records, during execution phases only:
- code objects entered (sys.monitoring PY_START, re-enabled at every execution phase);
- class attributes read: every attribute defined in a sqlglot class body is replaced by
  a stand-in descriptor right after the class is created and re-armed at the start of
  every execution phase, so a read is seen even when the attribute was read before;
- executed code objects whose names include `__dict__`, `vars`, `getattr_static` or
  `__getattribute__`: reads that could bypass the stand-ins.

Output: {"code": [[path, qualname, firstlineno], ...], "instrumented": [[module, qual, name], ...],
         "execution_reads": [[module, qual, name], ...], "bypass": [[path, qualname, firstlineno, names], ...]}
"""

import builtins
import enum
import importlib
import json
import os
import pickle
import pkgutil
import sys
import types

sys.path.insert(0, "/src")
os.chdir("/src")
import pytest  # noqa: E402,F401 - sqlglot checks at import whether a test framework is loaded

PREFIX = "/src/"
restored: list = []
instrumented: set = set()
reads: set = set()
codes: dict = {}
phase = {"execution": False}
M = sys.monitoring
TOOL = 5


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
            restored.append((cls, name))
        getter = getattr(type(value), "__get__", None)
        return getter(value, instance, owner) if getter is not None else value


def arm(cls, name) -> None:
    value = cls.__dict__.get(name)
    if isinstance(value, Touch):
        return
    try:
        type.__setattr__(cls, name, Touch(cls, name, value))
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
        restored.append((cls, name))


original_build_class = builtins.__build_class__


def build_class(func, name, *bases, **kwargs):
    cls = original_build_class(func, name, *bases, **kwargs)
    try:
        if func.__code__.co_filename.startswith(PREFIX + "sqlglot/") and isinstance(cls, type):
            instrument(cls)
    except Exception:  # noqa: BLE001
        pass
    return cls


builtins.__build_class__ = build_class


def on_start(code, offset):
    if phase["execution"] and code.co_filename.startswith(PREFIX):
        codes[(code.co_filename[len(PREFIX):], code.co_qualname, code.co_firstlineno)] = code
    return M.DISABLE


M.use_tool_id(TOOL, "tpc-execution")
M.register_callback(TOOL, M.events.PY_START, on_start)
M.set_events(TOOL, M.events.PY_START)


def execution(on: bool) -> None:
    if on:
        while restored:
            arm(*restored.pop())
        M.restart_events()
    phase["execution"] = on


import sqlglot  # noqa: E402

for _info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if _info.name.rsplit(".", 1)[1] != "__main__":
        try:
            importlib.import_module(_info.name)
        except Exception:  # noqa: BLE001
            pass

import sqlglot.executor as executor_module  # noqa: E402
import tests.test_executor as te  # noqa: E402
from sqlglot import parse_one, transpile  # noqa: E402

real_optimize = executor_module.optimize
real_plan = executor_module.Plan


def optimize_as_preparation(*args, **kwargs):
    """Preparation lasts from optimize() to Plan(): the optimized tree and its pretty SQL
    (the debug-log argument execute() evaluates in between) are what the TPC check
    compares."""
    phase["execution"] = False
    return real_optimize(*args, **kwargs)


def plan_as_execution(*args, **kwargs):
    execution(True)
    return real_plan(*args, **kwargs)


executor_module.optimize = optimize_as_preparation
executor_module.Plan = plan_as_execution

def cold_workers() -> None:
    """Each executed query in its own fork of this process, which has prepared nothing:
    a spawned worker starts with cold caches, receives the tables and the query as pickle
    bytes, runs the initializer and mp_execute. The main-process work (setUpClass, parse,
    pickle) happens in another fork, so none of its caches reach the workers."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # the test's main process
        os.close(r)
        te.TestExecutor.setUpClass()
        jobs = []
        for schema, tables, sqls in ((te.TPCH_SCHEMA, te.TestExecutor.tpch_tables, te.TestExecutor.tpch_sqls),
                                     (te.TPCDS_SCHEMA, te.TestExecutor.tpcds_tables, te.TestExecutor.tpcds_sqls)):
            initargs = pickle.dumps((schema, tables))
            for meta, sql, _ in sqls:
                if meta.get("execute"):
                    jobs.append((initargs, pickle.dumps(parse_one(sql)), meta))
        with os.fdopen(w, "wb") as fh:
            pickle.dump(jobs, fh)
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as fh:
        jobs = pickle.load(fh)  # bytes only: nothing is unpickled in this process
    os.waitpid(pid, 0)
    for initargs, blob, meta in jobs:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # a fresh worker
            os.close(r)
            execution(True)
            te.initializer(*pickle.loads(initargs))  # the pool's initializer at worker start
            execution(False)
            expression = pickle.loads(blob)  # the task's argument: compared by the TPC check
            execution(True)
            te.mp_execute(expression, meta)
            execution(False)
            scanned = {k: sorted(BYPASS & set(c.co_names)) for k, c in codes.items()}
            with os.fdopen(w, "wb") as fh:
                pickle.dump((sorted(reads), scanned), fh)
            os._exit(0)
        os.close(w)
        with os.fdopen(r, "rb") as fh:
            got_reads, got_codes = pickle.load(fh)
        os.waitpid(pid, 0)
        cold_reads.update(map(tuple, got_reads))
        cold_codes.update(got_codes)


BYPASS = {"__dict__", "vars", "getattr_static", "__getattribute__"}
cold_reads: set = set()
cold_codes: dict = {}
cold_workers()

execution(True)
te.TestExecutor.setUpClass()
execution(False)
for schema, tables, sqls in ((te.TPCH_SCHEMA, te.TestExecutor.tpch_tables, te.TestExecutor.tpch_sqls),
                             (te.TPCDS_SCHEMA, te.TestExecutor.tpcds_tables, te.TestExecutor.tpcds_sqls)):
    execution(True)
    te.initializer(pickle.loads(pickle.dumps(schema)), pickle.loads(pickle.dumps(tables)))
    execution(False)
    for meta, sql, _ in sqls:
        expression = pickle.loads(pickle.dumps(parse_one(sql)))
        transpile(sql, write="duckdb")
        execution(True)
        te.mp_execute(expression, meta)  # the worker's call, None for queries not executed
        execution(False)
builtins.__build_class__ = original_build_class
M.set_events(TOOL, 0)

cold_only_reads = sorted(cold_reads - reads)
cold_only_codes = sorted(set(cold_codes) - set(codes))
reads |= cold_reads
bypass = [[*k, names] for k, names in sorted(cold_codes.items()) if names]
for (path, qual, line), code in sorted(codes.items()):
    names = sorted(BYPASS & set(code.co_names))
    if names and [path, qual, line, names] not in bypass:
        bypass.append([path, qual, line, names])
all_codes = sorted(set(codes) | set(cold_codes))
json.dump({"code": all_codes, "instrumented": sorted(instrumented), "execution_reads": sorted(reads),
           "bypass": bypass}, open(sys.argv[1], "w"))
print(f"{len(all_codes)} code objects ({len(cold_only_codes)} only in cold workers: {cold_only_codes[:5]}), {len(instrumented)} instrumented attributes, {len(reads)} read "
      f"({len(cold_only_reads)} seen only by cold workers: {cold_only_reads[:5]}), bypass candidates {bypass}",
      file=sys.stderr)
