"""Decide tests/test_parser.py::TestParser::test_max_nodes (28.8 s) with a scaled-down
stand-in. Entry point: `decide`. A calculated gamble approved by Vaibhav: the guards
matter more than the saving.

The real test runs six statements: parse "SELECT 1" and a 500-column SELECT under a
1,000,000-node limit; parse W=10,000 projections of a depth-100 EQ chain under limits of
1,000,000 (100*W: the input has 100*W+1 counted nodes, so it fails at the last one) and
50,000 (5*W), expecting ParseError messages "Maximum number of AST nodes" and "(50000)";
then a 100-projection SELECT with a limit of 5 must fail and "SELECT 1, 2, 3" must parse.

The stand-in runs the same statements with W=10 and W=40 and the limits scaled with W
(100*W and 5*W), so the boundary stays exactly one node over. In a fresh interpreter
for the patched tree (fixed environment, as br/tpc.py) it records:
- each statement's outcome: the type returned, or the exception type and its full message;
- how many times each sqlglot function is entered during the W=10 run (identical counts
  mean the patched tree does the same work in the same calls: no new recursion, no extra
  passes, nothing that could make the full-size parse slower or deeper);
- the peak traced memory of the W=10 boundary parse (the real test peaks near 1.7 GB in a
  4 GB container: the patched tree may not use more).
The pinned side's record comes from the image build (tools/maxn_baseline.py), else from
a pinned child once per run. The test is dropped only when every outcome and every count
is equal and the memory is not larger.

Refused before running anything (the test stays listed): a patch that touches tests/ or
any non-Python file, adds, removes or renames a file, changes class or module code; a
changed line that adds or changes a number, a loop (for, while, comprehension, break,
continue, range) or a name that looks like a limit or a size (max, limit, size, depth,
chunk, recursion); and a changed line the real test runs (maps) that the stand-in does
not (its coverage on the pinned tree, recorded at build).
"""

import io
import json
import os
import re
import resource
import sys
import tokenize
from pathlib import Path

TEST_ID = "tests/test_parser.py::TestParser::test_max_nodes"
WIDTHS = (10, 40)  # outcomes at both; the width-10 run is the counted one
COUNT_WIDTH = 10
MEMORY_WIDTH = 10
# The patched peak may exceed the pinned one by at most 2%: at width 10 (about 2 MB traced)
# that catches about 100 bytes more per node or token; less is harmless at full size.
MEMORY_SLACK = 1.02

_APP = Path(__file__).resolve().parent.parent
BASELINE = _APP / "maps" / "maxn_pinned.json"
_PINNED: dict = {}

_LIMIT_NAME = re.compile(r"max|limit|size|depth|chunk|recurs|MAX|LIMIT|SIZE|DEPTH|CHUNK", re.I)
_LOOP_WORDS = {"for", "while", "break", "continue", "range", "iter", "next", "yield", "async"}


# ---------------------------------------------------------------- child side


def _statements(width: int):
    """The real test's statements, with W projections and limits scaled with W."""
    from sqlglot import parse_one

    cols = [f"col{i}" for i in range(500)]
    expr = "x" + "=x" * 100
    sql = f"SELECT {','.join([expr] * width)}"
    return [
        lambda: parse_one("SELECT 1", max_nodes=1_000_000),
        lambda: parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000),
        lambda: parse_one(sql, max_nodes=100 * width),
        lambda: parse_one(sql, max_nodes=5 * width),
        lambda: parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5),
        lambda: parse_one("SELECT 1, 2, 3"),
    ]


def _outcome(f) -> list:
    try:
        r = f()
        return ["ok", f"{type(r).__module__}.{type(r).__qualname__}"]
    except BaseException as e:  # noqa: BLE001 - an exception is the outcome
        return [f"{type(e).__module__}.{type(e).__qualname__}", str(e)[:2000]]


def _record(with_coverage: bool) -> dict:
    """Runs in the child, inside the tree to judge."""
    import tracemalloc
    import unittest  # noqa: F401 - sqlglot checks at import whether a test framework is
    # loaded (expressions/core.py:48 accepts unittest or pytest); the real test is a
    # unittest.TestCase, and unittest costs a tenth of pytest to import

    import sqlglot  # noqa: F401

    root = os.getcwd().rstrip("/") + "/"
    out: dict = {"outcomes": {}}
    for width in WIDTHS:
        if width != COUNT_WIDTH:
            out["outcomes"][str(width)] = [_outcome(f) for f in _statements(width)]

    mon = sys.monitoring
    tool = 4
    mon.use_tool_id(tool, "br-maxn")
    calls: dict[str, int] = {}

    def on_start(code, offset):
        name = code.co_filename
        if not name.startswith(root + "sqlglot/"):
            return mon.DISABLE
        key = f"{name[len(root):]}|{code.co_qualname}"
        calls[key] = calls.get(key, 0) + 1

    mon.register_callback(tool, mon.events.PY_START, on_start)
    statements = _statements(COUNT_WIDTH)
    mon.set_events(tool, mon.events.PY_START)
    try:
        counted = [_outcome(f) for f in statements]
    finally:
        mon.set_events(tool, 0)
        mon.register_callback(tool, mon.events.PY_START, None)
        mon.free_tool_id(tool)
    out["outcomes"][str(COUNT_WIDTH)] = counted
    out["calls"] = calls

    boundary = _statements(MEMORY_WIDTH)[2]
    tracemalloc.start()
    try:
        out["memory_outcome"] = _outcome(boundary)
        out["peak"] = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    if with_coverage:  # pinned side at build: which lines the stand-in runs
        lines: set = set()
        mon.use_tool_id(tool, "br-maxn")

        def on_line(code, line):
            name = code.co_filename
            if not name.startswith(root + "sqlglot/"):
                return mon.DISABLE
            lines.add(f"{name[len(root):]}:{line}")

        mon.register_callback(tool, mon.events.LINE, on_line)
        mon.set_events(tool, mon.events.LINE)
        try:
            for width in WIDTHS:
                for f in _statements(width):
                    _outcome(f)
        finally:
            mon.set_events(tool, 0)
            mon.free_tool_id(tool)
        out["coverage"] = sorted(lines)
    return out


def _main_child() -> None:
    args = json.loads(sys.stdin.buffer.read())
    out = os.fdopen(os.dup(1), "wb")
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    try:
        os.chdir(args["tree"])
        sys.path.insert(0, args["tree"])
        data = json.dumps({"ok": True, "record": _record(args["coverage"])}).encode()
    except BaseException as exc:  # noqa: BLE001
        data = json.dumps({"ok": False, "why": repr(exc)[-300:]}).encode()
    out.write(data)
    out.flush()
    os._exit(0)


CHILD_ENV = {"PYTHONHASHSEED": "0", "SKIP_INTEGRATION": "0", "PYTHON_CPU_COUNT": "1",
             "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8"}


def run(tree: str, timeout: float, coverage: bool = False):
    """The stand-in's record for `tree`, from a fresh interpreter, or None."""
    import signal
    import subprocess

    env = dict(CHILD_ENV, PATH=os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
    code = f"import sys; sys.path.insert(0, {str(_APP)!r}); from br.maxn import _main_child; _main_child()"
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, cwd="/", start_new_session=True)
    except OSError:
        return None
    try:
        data, _ = proc.communicate(json.dumps({"tree": tree, "coverage": coverage}).encode(), timeout=timeout)
    except subprocess.TimeoutExpired:
        data = None
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
    if not data:
        return None
    try:
        result = json.loads(data)
    except ValueError:
        return None
    return result["record"] if result.get("ok") else None


# ---------------------------------------------------------------- parent side


def _expected_pass(record: dict) -> bool:
    """The pinned record must itself show the test's expectations at every width."""
    for width, outcomes in record["outcomes"].items():
        w = int(width)
        o = outcomes
        if not (o[0][0] == "ok" and o[0][1].endswith(".Select") and o[1][0] == "ok"
                and o[1][1].endswith(".Select") and o[2][0].endswith("ParseError")
                and "Maximum number of AST nodes" in o[2][1] and o[3][0].endswith("ParseError")
                and f"({5 * w})" in o[3][1] and o[4][0].endswith("ParseError")
                and o[5][0] == "ok" and o[5][1].endswith(".Select")):
            return False
    return True


def pinned_record(pinned_tree: str, timeout: float):
    if "record" not in _PINNED:
        record = None
        try:
            data = json.loads(BASELINE.read_text())
            if data.get("python") == sys.version and data.get("tree") == pinned_tree:
                record = data["record"]
        except (OSError, ValueError, KeyError):
            record = None
        if record is None:
            record = run(pinned_tree, timeout, coverage=True)
        _PINNED["record"] = record if record is not None and _expected_pass(record) else None
    return _PINNED["record"]


def _tokens(text: str) -> list[tuple[int, str]]:
    try:
        return [(t.type, t.string) for t in tokenize.generate_tokens(io.StringIO(text).readline)
                if t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                                  tokenize.COMMENT, tokenize.ENDMARKER)]
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return [(-1, text)]


def _changed_lines(old: str, new: str) -> tuple[list[str], list[str], set[int]]:
    """(removed lines, added lines, old line numbers touched), by a line diff."""
    import difflib

    a, b = old.split("\n"), new.split("\n")
    removed, added, touched = [], [], set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        removed += a[i1:i2]
        added += b[j1:j2]
        touched |= set(range(i1 + 1, i2 + 1)) if i2 > i1 else {i1, i1 + 1}
    return removed, added, touched


def static_refusal(changes, changed_objects, class_edits) -> str | None:
    """Why the stand-in cannot stand for the real test on this patch, or None."""
    if class_edits or not changed_objects:
        return "patch shape"
    for ch in changes:
        path = ch.old_path or ch.new_path or ""
        if ch.old_path is None or ch.new_path != ch.old_path:
            return "file added, removed or renamed"
        if not path.endswith(".py") or not path.startswith("sqlglot/"):
            return f"touches {path}"
    if any(kind != "function" for _, _, _, kind in changed_objects):
        return "class or module code changed"
    for ch in changes:
        removed, added, _ = _changed_lines(ch.old_text, ch.new_text)
        old_tokens, new_tokens = _tokens("\n".join(removed)), _tokens("\n".join(added))
        numbers = lambda toks: sorted(s for t, s in toks if t == tokenize.NUMBER)  # noqa: E731
        if numbers(old_tokens) != numbers(new_tokens):
            return f"a number changed in {ch.old_path}"
        for t, s in old_tokens + new_tokens:
            if t == -1:
                return f"changed lines of {ch.old_path} do not tokenize"
            if t == tokenize.NAME and (s in _LOOP_WORDS or _LIMIT_NAME.search(s)):
                return f"a changed line in {ch.old_path} has '{s}' (loop, limit or size)"
    return None


def coverage_refusal(changes, maps, record: dict) -> str | None:
    """A changed line the real test runs (maps) that the stand-in did not run."""
    covered = set(record.get("coverage", ()))
    if not covered:
        return "no stand-in coverage"
    for ch in changes:
        _, _, touched = _changed_lines(ch.old_text, ch.new_text)
        for line in sorted(touched):
            if TEST_ID in maps.lines(ch.old_path, line) and f"{ch.old_path}:{line}" not in covered:
                return f"{ch.old_path}:{line} runs in the real test, not in the stand-in"
    return None


def same(pinned: dict, patched: dict) -> tuple[bool, str]:
    if patched["outcomes"] != pinned["outcomes"]:
        bad = [w for w in pinned["outcomes"] if patched["outcomes"].get(w) != pinned["outcomes"][w]]
        return False, f"outcomes differ at width {','.join(bad)}"
    if patched["memory_outcome"] != pinned["memory_outcome"]:
        return False, "outcome differs under tracing"
    if patched["calls"] != pinned["calls"]:
        keys = sorted(k for k in set(patched["calls"]) | set(pinned["calls"])
                      if patched["calls"].get(k) != pinned["calls"].get(k))
        return False, f"calls differ: {','.join(keys[:3])}"
    if patched["peak"] > pinned["peak"] * MEMORY_SLACK:
        return False, f"peak memory {patched['peak']} > pinned {pinned['peak']}"
    return True, "same"


# Patched-side CPU of one use (seconds), measured with tools/maxn_cost.py; set from a
# quiet measurement or the scorer.
EST_CPU = 1.0


def decide(commit: str, changes, chosen: list[int], tests: list[dict], changed_objects: list[tuple],
           class_edits: list, maps, pinned_tree: str, timeout: float, log=lambda message: None,
           work_root: str = "/tmp/br", saving_ms=None, enabled: bool = True) -> tuple[list[int], str]:
    """(tests still listed, a note for the log) for one patch."""
    from br.verify import build_tree, remove_tree

    by_number = {t["number"]: t for t in tests}
    hit = [n for n in chosen if by_number[n]["id"] == TEST_ID]
    if not hit or not enabled:
        return chosen, ""
    why = static_refusal(changes, changed_objects, class_edits)
    if why:
        return chosen, f" | maxn: kept ({why})"
    if saving_ms is not None and saving_ms(hit) / 1000 <= EST_CPU:
        return chosen, " | maxn: kept (not worth it)"
    pinned = pinned_record(pinned_tree, timeout)
    if pinned is None:
        return chosen, " | maxn: kept (pinned stand-in failed)"
    why = coverage_refusal(changes, maps, pinned)
    if why:
        return chosen, f" | maxn: kept ({why})"
    tree = Path(work_root) / (commit + "-maxn")
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        remove_tree(tree)
        build_tree(tree, changes)
        patched = run(str(tree), timeout)
    except Exception as error:  # noqa: BLE001
        return chosen, f" | maxn: kept (error {error!r})"
    finally:
        remove_tree(tree)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        log(f"{commit}: maxn stand-in cpu {after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime:.2f}s")
    if patched is None:
        return chosen, " | maxn: kept (patched stand-in failed or timed out)"
    ok, why = same(pinned, patched)
    if not ok:
        return chosen, f" | maxn: kept ({why})"
    return [n for n in chosen if n not in hit], " | maxn: dropped (stand-in identical)"
