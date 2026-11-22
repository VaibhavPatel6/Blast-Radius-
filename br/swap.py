"""Prove that a patch which only swaps `is` with `==` (or `is not` with `!=`) at some
comparisons changes no test's behaviour. Entry point: `prove`.

Evidence: maps/cmp_types.json, recorded from the pinned commit's own test suite by
tools/cmpmap (every process the suite starts, pool workers included). For every
comparison site in sqlglot ("path:line:col" in the pinned source) it lists the pairs of
operand TYPES that reached it, with two properties of each type: identity equality (its
__eq__/__ne__ are object's, or it is NoneType) and plain enum (an Enum whose enum classes
do not override __eq__/__ne__). It records types only, nothing about values or edits.

For a pair of identity-equal types `a == b` is `a is b` (both __eq__ return NotImplemented
unless a is b, and Python then falls back to identity), and `a != b` is `a is not b`. Two
members of one plain enum are equal exactly when they are the same member (members with
equal values are aliases of one object). So when every pair recorded at a site is of
those kinds, the swapped comparison returns the same result as the original at every
evaluation the suite makes, and every test runs as on the pinned commit. Each test runs
the same code up to the first evaluation of a swapped site, gets the same answer there, and
so on: the pairs that reach the site under the patch are pairs the record has.

Refused (no proof): any change outside a sqlglot/*.py file edited in place, any difference
other than the swapped operators (the files must agree token for token elsewhere, with the
same number of lines), a chained comparison, a site with no record, or a recorded pair of
any other kind. Tests that read the changed file as data are still listed (`keep`),
since its text changed.
"""

import ast
import json
from pathlib import Path

# Vaibhav's decision: ships switched on once its gate passes (see tools/swap_gate.py).
ENABLED = True

_APP = Path(__file__).resolve().parent.parent
_MAP: dict | None = None
_SWAPS = {(ast.Is, ast.Eq), (ast.Eq, ast.Is), (ast.IsNot, ast.NotEq), (ast.NotEq, ast.IsNot)}


def _sites() -> dict:
    global _MAP
    if _MAP is None:
        _MAP = json.loads((_APP / "maps" / "cmp_types.json").read_text())["sites"]
    return _MAP


def _harmless(pair: list) -> bool:
    (type_a, identity_a, enum_a), (type_b, identity_b, enum_b) = pair
    return (identity_a and identity_b) or (enum_a and enum_b and type_a == type_b)


def swapped_sites(old_text: str, new_text: str, path: str) -> list[str] | None:
    """Sites ("path:line:col", pinned positions) whose operator the patch swaps, when that
    is the only difference between the two texts; else None."""
    try:
        old_tree, new_tree = ast.parse(old_text), ast.parse(new_text)
    except SyntaxError:
        return None
    old_cmp = [n for n in ast.walk(old_tree) if isinstance(n, ast.Compare)]
    new_cmp = [n for n in ast.walk(new_tree) if isinstance(n, ast.Compare)]
    if len(old_cmp) != len(new_cmp):
        return None
    sites, lines = [], set()
    for o, n in zip(old_cmp, new_cmp):
        if len(o.ops) != len(n.ops):
            return None
        for i, (a, b) in enumerate(zip(o.ops, n.ops)):
            if type(a) is type(b):
                continue
            if (type(a), type(b)) not in _SWAPS or len(o.ops) != 1:
                return None
            sites.append(f"{path}:{o.lineno}:{o.col_offset}")
            lines.update(range(o.lineno, o.end_lineno + 1))
            n.ops[i] = type(a)()
    if not sites or ast.dump(old_tree) != ast.dump(new_tree):
        return None
    old_lines, new_lines = old_text.split("\n"), new_text.split("\n")
    if len(old_lines) != len(new_lines):
        return None
    for number, (a, b) in enumerate(zip(old_lines, new_lines), start=1):
        if a != b and number not in lines:
            return None  # a comment or whitespace changed elsewhere
    return sites


def prove(changes, maps) -> tuple[bool, str, set[str]]:
    """(proven, why, keep): whether the patch is only is/== swaps proven harmless, and the
    tests to list anyway (they read a changed file as data)."""
    if not ENABLED:
        return False, "switched off", set()
    if not changes:
        return False, "empty patch", set()
    try:
        sites_map = _sites()
    except (OSError, ValueError, KeyError) as error:
        return False, f"no comparison map ({error!r})", set()
    keep: set[str] = set()
    proven: list[str] = []
    for ch in changes:
        path = ch.old_path
        if path is None or ch.new_path != path or not path.startswith("sqlglot/") or not path.endswith(".py") \
                or getattr(ch, "mode_change", False) or ch.old_text is None or ch.new_text is None:
            return False, f"not an in-place edit of a sqlglot Python file ({ch.old_path} -> {ch.new_path})", set()
        sites = swapped_sites(ch.old_text, ch.new_text, path)
        if sites is None:
            return False, f"{path}: more than is/== swaps", set()
        for site in sites:
            pairs = sites_map.get(site)
            if not pairs:
                return False, f"{site}: no recorded operand types", set()
            bad = [p for p in pairs if not _harmless(p)]
            if bad:
                return False, f"{site}: {bad[0][0][0]} vs {bad[0][1][0]} is not identity-equal", set()
            proven.append(site)
        # its text changed: tests that read it as data (a directory listing sees names
        # only, and no file is added or removed)
        keep |= maps.opened(path)
    return True, f"is/== swap proven harmless at {', '.join(proven)}", keep
