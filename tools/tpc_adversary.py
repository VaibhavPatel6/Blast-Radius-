#!/usr/bin/env python3
"""Generate patches aimed at the TPC check's drop rule (br/tpc.py), for labeling with real
pytest and gating with tools/tpc_gate.py:

    python3 tools/tpc_adversary.py PINNED_TREE maps/tpc_execution.json OUT_DIR [SEED]

Kinds (prefix of the patch name):
  ar  class attribute the pinned TPC tests read during execution (expected: kept)
  au  class attribute never read during execution, in a class some other attribute of
      which is read then (the near misses the rule could get wrong)
  mv  module-level constant in sqlglot (optimizer, expressions, generator, schema, helper, ...)
  fd  default argument of a module-level function or a method
  fx  TPC fixture edits: one character in a query or a settings line, a pair added or
      removed, the expected output only
Each class or module edit changes one literal value: a bool flipped, an int +1, a string
with one character changed, or the first element of a dict, set, list or tuple removed.
"""

import ast
import difflib
import json
import random
import sys
from pathlib import Path

SKIP_PREFIXES = ("sqlglot/executor/", "sqlglot/planner.py", "sqlglot/__main__")


def _like(node, elts):
    return ast.Set(elts=elts) if isinstance(node, ast.Set) else type(node)(elts=elts, ctx=ast.Load())


def mutate(node: ast.AST) -> str | None:
    """Source of a one-step mutation of a literal value node, or None."""
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            return repr(not v)
        if isinstance(v, int):
            return repr(v + 1)
        if isinstance(v, str) and v:
            return repr(v[:-1] + ("y" if v[-1] != "y" else "z"))
        return None
    if isinstance(node, ast.Dict) and len(node.keys) >= 2 and node.keys[0] is not None:
        return ast.unparse(ast.Dict(keys=node.keys[1:], values=node.values[1:]))
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)) and len(node.elts) >= 2:
        if any(isinstance(e, ast.Starred) for e in node.elts[:1]):
            return None
        return ast.unparse(_like(node, node.elts[1:]))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("frozenset", "set", "tuple") \
            and len(node.args) == 1 and isinstance(node.args[0], (ast.Set, ast.List, ast.Tuple)) \
            and len(node.args[0].elts) >= 2:
        inner = node.args[0]
        return f"{node.func.id}({ast.unparse(_like(inner, inner.elts[1:]))})"
    return None


def replace(text: str, node: ast.AST, new: str) -> str:
    lines = text.split("\n")
    start, end = node.lineno - 1, node.end_lineno - 1
    first = lines[start].encode()[:node.col_offset].decode()
    last = lines[end].encode()[node.end_col_offset:].decode()
    return "\n".join(lines[:start] + [first + new + last] + lines[end + 1:])


def patch(path: str, old: str, new: str) -> str:
    diff = difflib.unified_diff(old.split("\n"), new.split("\n"), f"a/{path}", f"b/{path}", lineterm="", n=3)
    return f"diff --git a/{path} b/{path}\n" + "\n".join(diff) + "\n"


def module_name(path: str) -> str:
    return path[:-3].replace("/", ".").removesuffix(".__init__")


def class_attributes(tree: ast.Module):
    """(qualname, name, value node) for single-name assignments in class bodies."""
    out = []

    def walk(body, prefix):
        for node in body:
            if isinstance(node, ast.ClassDef):
                qual = f"{prefix}{node.name}"
                for item in node.body:
                    target = value = None
                    if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
                        target, value = item.targets[0].id, item.value
                    elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.value is not None:
                        target, value = item.target.id, item.value
                    if target and not (target.startswith("__") and target.endswith("__")):
                        out.append((qual, target, value))
                walk(node.body, qual + ".")

    walk(tree.body, "")
    return out


def main() -> int:
    tree, record_path, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 5)
    record = json.loads(record_path.read_text())
    read = {tuple(x) for x in record["execution_reads"]}
    read_classes = {(m, q) for m, q, _ in read}
    (out / "commits").mkdir(parents=True, exist_ok=True)
    made: dict[str, str] = {}
    pools: dict[str, list] = {"ar": [], "au": [], "mv": [], "fd": []}
    for file in sorted((tree / "sqlglot").rglob("*.py")):
        path = str(file.relative_to(tree))
        if path.startswith(SKIP_PREFIXES):
            continue
        text = file.read_text()
        module = ast.parse(text)
        mod = module_name(path)
        for qual, name, value in class_attributes(module):
            new = mutate(value)
            if new is None:
                continue
            k = (mod, qual, name)
            if k in read:
                pools["ar"].append((path, value, new, f"{mod}:{qual}.{name}"))
            elif (mod, qual) in read_classes:
                pools["au"].append((path, value, new, f"{mod}:{qual}.{name}"))
        for node in module.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                if isinstance(target, ast.Name) and node.value is not None and not target.id.startswith("__"):
                    new = mutate(node.value)
                    if new is not None:
                        pools["mv"].append((path, node.value, new, f"{mod}.{target.id}"))
        for node in ast.walk(module):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                    new = mutate(default)
                    if new is not None:
                        pools["fd"].append((path, default, new, f"{mod}:{node.name} default"))
    quota = {"ar": 40, "au": 30, "mv": 15, "fd": 12}
    for kind, pool in pools.items():
        rng.shuffle(pool)
        for i, (path, node, new, what) in enumerate(pool[:quota[kind]]):
            old = (tree / path).read_text()
            name = f"{kind}{i:02d}"
            (out / "commits" / f"{name}.patch").write_text(patch(path, old, replace(old, node, new)))
            made[name] = what

    # fixture edits
    ds, h = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql", "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
    tds, th = (tree / ds).read_text(), (tree / h).read_text()
    statements = tds.split(";")
    fixture = {}
    # one character in an executed query's text: a digit, an operator, a column name
    for n, (old_s, new_s) in enumerate((("d_year = 2001", "d_year = 2002"), ("> (SELECT Avg(ctr_total_return) * 1.2", "> (SELECT Avg(ctr_total_return) * 1.3"),
                                         ("LIMIT 100;", "LIMIT 10;"), ("s_state = 'TN'", "s_state = 'TX'"),
                                         ("ORDER  BY c_customer_id", "ORDER  BY c_customer_sk"))):
        if old_s in tds:
            fixture[f"fx1{n}"] = (ds, tds, tds.replace(old_s, new_s, 1), f"query text: {old_s} -> {new_s}")
    # settings line: execute switched off, and an unknown value
    fixture["fx20"] = (ds, tds, tds.replace("# execute: true\n", "# execute: false\n", 1), "settings: execute false")
    fixture["fx21"] = (ds, tds, tds.replace("# execute: true\n", "#execute: true\n", 1), "settings: no space")
    fixture["fx22"] = (ds, tds, tds.replace("-- TPC-DS 2\n", "-- TPC-DS 2\n# execute: true\n", 1), "settings: execute added to query 2")
    # a pair removed, a pair duplicated
    second = tds.index("--------------------------------------\n-- TPC-DS 2")
    fixture["fx30"] = (ds, tds, tds[second:], "pair removed (TPC-DS 1)")
    first_pair = tds[:second]
    fixture["fx31"] = (ds, tds, first_pair + tds, "pair added (TPC-DS 1 twice)")
    # expected output only
    fixture["fx40"] = (ds, tds, tds.replace('AS "ctr_total_return"', 'AS "ctr_total_returns"', 1), "expected output only")
    fixture["fx41"] = (h, th, th.replace("sum_qty", "sum_qtx", 2).replace("sum_qtx", "sum_qty", 1), "tpc-h expected output only")
    for name, (path, old, new, what) in fixture.items():
        if old != new:
            (out / "commits" / f"{name}.patch").write_text(patch(path, old, new))
            made[name] = what
    (out / "made.json").write_text(json.dumps(made, indent=1))
    only = {n: ["tests/test_executor.py::TestExecutor::test_execute_tpcds",
                "tests/test_executor.py::TestExecutor::test_execute_tpch"] for n in made}
    (out / "only.json").write_text(json.dumps(only))
    print({k: len(v) for k, v in pools.items()}, f"made {len(made)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
