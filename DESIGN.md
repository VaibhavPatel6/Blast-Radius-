# Design

How this entry works, one section per component, in plain language. Each section
says what the component does and why it is safe.

**The scored final entry is S10, commit `8715ffb`** (practice 186.0 s: 177.242 s simulated,
8.8 s CPU, 0 missed). This document describes that commit. `origin/main` is one merge
further, at S11 (`fe93df9`), which differs only in how the TPC check prepares its queries
(see the end of the TPC section); S11 was gated but not submitted.

## Image (`Dockerfile`, `build/fetch_sqlglot.py`)

The image starts from the kit's pinned Python image, installs the exact packages in
`requirements.lock` from PyPI, and downloads sqlglot at the pinned commit
(`eb11aec4…`) as a tarball from sqlglot's own GitHub repository. Those are the only
downloads, and both are on the contest's allowed list.

Why it is safe: GitHub generates archive tarballs on demand and does not promise the
bytes stay identical, so instead of hashing the tarball we hash its *contents*: every
file's path and SHA-256, sorted. That manifest hash is committed in
`build/fetch_sqlglot.py`, and the build fails if the download does not match it. So a
rebuild after the deadline either produces exactly the same sqlglot tree or fails
loudly; it can never silently change.

Bytecode is compiled during the build (`compileall`) because at run time the disk is
read-only, and without `.pyc` files Python would recompile every module on every run
and we would pay for it in CPU seconds. Everything is made readable by `nobody`, the
user the scorer runs as.

## Selector (`selector.py`)

Reads `/data/tests.json` and the patch names in `/data/commits`, and prints one JSON
line per commit.

Per commit: apply the patch in memory (`br/patch.py`), find the candidate tests
(`br/analyze.py`), and list them all. Verification (`br/verify.py`, `br/policy.py`)
is built but switched off (`VERIFY_UNTIL = 0`) until we know how fast the scorer's
CPU is, because every second our container computes is added to the score. Any doubt
(a patch we cannot parse or bound, an internal error, an invalid line) makes that
commit list every test, which can never miss.

**Packing.** The scorer charges a commit its busiest worker's load plus 2 s per worker.
`pack()` tries every worker count from 1 to 8; for each, it places tests longest first
onto the currently least-loaded worker (a standard heuristic called LPT, "longest
processing time first"), computes the scorer's cost in whole milliseconds, and keeps
the cheapest. For all 1,620 tests the best is 4 workers at 36.817 s: the 28.8 s
`test_max_nodes` sits alone on one worker and the rest fit beside it.

**Limits.** A patch over 2 MB, or a changed Python file over 2 MB, is not analysed
(compiling a huge file can take gigabytes of memory); that commit lists every test.
After 140 seconds the remaining commits list every test without analysis, so the
batch always finishes inside the 3-minute limit.

**Output safety.** Every line is checked against the scorer's own rules (known test
numbers, no repeats, at most 8 workers) before anything is printed, and all lines are
written in one go at the end. Logs go to stderr only. A wrong line on stdout would void
the whole run, so the checks are not optional.

## Patch reading (`br/patch.py`)

Parses each unified diff and applies it to the pinned files in memory, using only the
standard library (the image has no `git` or `patch`, and the contest forbids
installing system packages from outside the allowed list). Every context line and
removed line must match the pinned file exactly. Added, deleted and renamed files and
"No newline at end of file" are handled. Anything else (binary patches, copies, odd
paths such as `../`, hunks that do not match, a file named twice) raises an error,
and that commit falls back to listing every test. Checked against `git apply` on
every sample patch (`tools/check_patch.py`).

## Coverage maps (`tools/covmap/`, output in `maps/`)

Built once, offline, from the pinned commit, and committed as plain files. Each of the
1,620 tests runs alone in a fresh process (`fork` from a parent that has loaded only
numpy, pandas and duckdb, never sqlglot or the tests). That matters: anything a test
needs that happens at import time, or in a cache filled on first use, is charged to
that test rather than to whichever test happened to run first.

Recording uses `sys.monitoring`, Python's built-in event interface (3.12+). We ask
for a callback the first time each line runs and the first time each *code object*
starts. (Python compiles every function, lambda, class body and module body into a
code object; a module body runs when the module is imported.) The callback returns
`DISABLE`, so each location reports once and then runs at full speed. An *audit hook*
(`sys.addaudithook`, which Python calls on events such as opening a file or starting
a process) records the files a test opens as data and the directories it lists.

Tests that start other Python processes (sqlglot's optimizer and executor tests use
process pools; one dialect test runs `python -c`) are covered too: a
`sitecustomize.py` on the path turns recording on in any Python process started
during mapping, and its record is merged into the test's.

**Shared processes.** The organizers ran many tests per pytest process. To check that
one-test-per-process maps miss nothing that matters, `tools/covmap/suite_plugin.py`
records coverage per test during a normal pytest run of the whole suite (re-arming
`sys.monitoring` at each test's start with `restart_events()`). Sequentially and under
pytest-xdist alike, only 3 tests entered code they never enter alone, all because
pytest formats subtest parameters with `repr()`; those entries are added to the maps.

## Patch analysis (`br/analyze.py`)

For each changed file:

- **Python files**: compile the old and new file *from bytes*, exactly as import does
  (so a `# coding:` comment or a byte-order mark has its real effect), and compare the two trees of code
  objects, ignoring line numbers. A comment or blank line moves code but changes no
  code object, so it affects nothing. For each code object whose own instructions,
  constants or names changed, the candidates are the tests that entered it. A changed
  module-level or class-level statement changes the module or class body, which every
  importer ran, so its candidates are the importers. A new syntax error: the importers.
  Two details keep "only lines moved" from looking like a change: constants are
  compared by type and value (not by their printed form, since two equal sets can
  print in different orders), and the one constant where Python 3.13 stores a class's
  own line number (`__firstlineno__`) is ignored when nothing else uses it.
- **Other files** (fixtures, data): the tests that opened that file.
- **Added files**: the tests that listed that directory (or, for a file in a new
  folder, the nearest folder that exists). **Deleted data files**: their readers and
  the directory's listers.
- **A new file or folder at the repository root**: the root is on Python's import
  path ahead of the installed packages and is scanned for package metadata, so a new
  `something.py` there can shadow any import. Every test is listed.
- **Test files added or removed** (`tests/**/test_*.py`, `*_test.py`, `__init__.py`):
  pytest imports every test file before running any test, so a new one could change
  state that every test sees. Every test is listed.
- **Files pytest or Python read by themselves** (`conftest.py`, pytest and tox ini
  files, a changed pytest section in `setup.cfg` or `pyproject.toml`,
  `sitecustomize.py`, `.pth` files): the change cannot be bounded, so every test is listed.

**Statement-level narrowing.** For a changed function, the candidates are not all tests
that entered it but only those that executed a line of the statement the patch touched
(the whole statement, so the continuation lines of a multi-line condition count). This
is only allowed when the function's shape is unchanged: the same flags (a `yield`
anywhere makes it a generator), the same local, free and cell variable names (a new
assignment anywhere makes a name local for the whole function), and the same nested
functions. If any touched line was executed by no test run alone, it falls back to
every test that entered the function: such a line may run only after another test has
changed shared state (we measured 14 such lines, e.g. the dialect re-registration loop
that runs after `test_dialect_entry_points` clears the registry).

**Test files.** An edit in `tests/` outside an ordinary test method or helper (module or
class level code, `setUp`, `setUpClass`, `tearDown`...) can leave state behind for other
tests in the same pytest process, so every test is listed.

A test that the maps do not know (a different `tests.json`) is always listed. If the
maps name a code object that this interpreter does not produce when it compiles the
pinned file, the maps and the image disagree and that commit lists every test (a
lookup must never come back empty just because a name changed).

## Class-level edits (`br/state.py`, `narrow_class_edits` in `selector.py`)

When a patch changes only values inside a class body (a constant, a dict entry, a flag),
the candidates would be every test that imported the module. Instead, two fresh forked
processes import the same modules, one from the pinned tree and one from the patched
view, after `pytest` (sqlglot checks at import whether a test framework is loaded). Each
computes a structural fingerprint of every attribute of every loaded sqlglot class, after
all class-creation hooks (metaclasses, `__init_subclass__`) have run. The differences
name exactly which attributes changed, including attributes those hooks derive from
others (a keyword trie from `KEYWORDS`, `required_args` from `arg_types`). The candidates
are then the tests that read one of those attributes, from a map recorded by running
every test alone with each class attribute replaced by a stand-in that notes its first
read (`COVMAP_CLASSES=1` in `tools/covmap/recorder.py`).

Each recorded read is also tagged with its phase: "import" when the nearest relevant
frame is a sqlglot module body (a metaclass or class body consuming the value while a
module is imported, even if a test triggered that import), "run" otherwise. Import-time
reads leave their results in class attributes and module-level data. So when the first
comparison finds readers that only read at import, a second comparison imports every
sqlglot module on both sides and fingerprints all classes and all module-level data; if
nothing differs beyond attributes whose test-time readers are recorded, only those
test-time readers can observe the change (for example, a Snowflake tokenizer setting read
by the dialect's metaclass at import: 1,088 importers, 273 test-time readers). Any
difference in module-level data, which has no read map, keeps the wider set.

Falls back to every importer when: a class or attribute exists on one side only; the
class is an enum, dataclass or named tuple; a changed attribute is one the read recorder
does not wrap (special `__names__` such as `__init__`, whose defaults live in the class
body; properties and other data descriptors), so its readers are unknown; an import
fails or times out; the maps hold no read records for the file. A value the fingerprint
cannot read counts as changed.

Fingerprints compare values, not identities: functions by their code (ignoring line
numbers), defaults and closure contents; bound methods also by the object they are bound
to (`exp.A.from_arg_list` differs from `exp.B.from_arg_list`). Tables keyed by strings,
numbers or enum members are compared in order, since sqlglot iterates some of them in
order. Tables keyed by classes, and sequences made only of classes, are compared by content only: they are often built by
iterating sets of classes, whose order follows memory addresses and differs between two
imports. Both sides are forked from the same process, so they share a hash seed; a no-op
comparison of typical modules reports no differences.

## The TPC executor tests (`br/tpc.py`, `decide_tpc` in `selector.py`)

`test_execute_tpcds` (21.5 s) and `test_execute_tpch` (3.9 s) parse each benchmark query in
the pytest process, pickle it to a freshly started worker process, run sqlglot's own
executor there (optimize, plan, execute), and compare the rows with DuckDB's answer for the
query transpiled to DuckDB in the pytest process. `br.tpc.decide` leaves them out only on
evidence that they run exactly as at the pinned commit, where they pass:

- **Code that runs only before execution.** Every changed code object is paired with its
  pinned version; none may run during the execution part (`maps/tpc_execution.json`,
  recorded along the real worker path). Patches touching tests, the executor or the
  planner, renaming files, or changing code no map explains, keep the tests.
- **What the workers receive.** For every query the change can reach (per-query and
  per-stage maps, `maps/tpc_queries.json`, `maps/tpc_stages.json`), the pickle bytes, a
  structural digest of the unpickled tree, and for executed queries the optimized tree
  (structure, inferred types, plain and pretty SQL) and the DuckDB SQL are computed on the
  pinned tree and on the patched view, in processes forked from the selector. Any
  difference, error or timeout keeps the tests. The pinned side comes from a baseline
  built in the image (`tools/tpc_baseline.py`, used only when made by the same Python for
  the same tree) or is computed once per run.
- **The state the imports leave.** When a change runs at import (class bodies, module
  code, functions in `maps/import_time.json`), every global and class attribute of every
  sqlglot module is digested on both sides; only attributes the execution never reads
  (`maps/tpc_reads.json`) may differ.
- **State shared with earlier tests.** The real test runs after a thousand others in one
  process, so a function edit that writes shared state only for inputs other tests give
  would pass the cold comparison and fail in the suite. The tests are therefore kept for
  any function edit whose bytecode, against the pinned version, uses different names
  (`get` to `pop`, a dropped `.copy()`), adds or changes a store, delete or in-place
  operation, changes a constant in a function that passes a copy-like keyword, or gains or
  loses a nested function (`mutation_refusal`).
- **Fixture-only edits.** An edit to the TPC query fixtures that leaves every query and its
  settings unchanged (expected outputs, comments) drops both tests.
- **Worth it.** The comparison runs only when its expected CPU is below the simulated
  seconds leaving the tests out would save.
- **Non-Python files.** The selector passes the maps to the check, so a non-Python
  change the TPC tests never open or list (added or removed files charged to the nearest
  existing directory, as the analyzer does) is set aside and the rest decided as above.
  A TPC fixture edit keeps the tests when a changed query's text equals a query of the
  other benchmark (the test caches DuckDB answers by SQL text).
- **Subset execution** (`RUN_LIMIT = 10`). When at most ten TPC-DS queries prepare
  differently, or a fixture edit changes queries, or a function edit under the executor or
  the planner reaches at most ten queries (`maps/tpc_exec_queries.json`), those queries are
  executed the way the test executes them, and the tests are dropped only if all pass.
  Class-edit subset runs are built but off (`CLASS_RUNS = False`): their TPC-H side
  accepted a difference without running anything.
- **Constants and containers.** The shared-state rule also refuses a changed set of
  constant types or a changed count of container-building instructions in a changed
  function.
- **Module loading** (`lazy_loading_refusal` in `selector.py`). The check prepares queries
  in a child that has imported every sqlglot module, while the real test loads dialects
  lazily. A one-token edit in `_Dialect.get` (`not in` to `in`, or `is`, or
  `_try_load(default)`) broke the real test while both cold sides agreed. So the TPC tests
  are kept for any change to `_Dialect` methods or `get_or_raise`, to
  `sqlglot/dialects/__init__.py`, `sqlglot/optimizer/__init__.py`, `sqlglot/serde.py` or
  `sqlglot/__init__.py`, or to a function that imports modules or names the loading
  machinery (`import_module`, `_try_load`, `_classes`, `entry_points` and others).
  S11 additionally prepares the queries after only the test module's own imports, so
  dialects load lazily as in the test, and digests the import state in a separate child
  that imports everything.
- **Known residue.** The check and the subset runs use a fixed hash seed; the real test
  does not. No executed query was found whose result depends on it.

`test_generator_memory` (6.7 s) is simply run in a child when it is a candidate and
leaving it out would save more than about 2.5 s; a pass drops it.

## The `is`/`==` swap proof (`br/swap.py`, called first in `select()`)

A patch that only swaps `is` with `==` (or `is not` with `!=`) at some comparisons is proven
harmless from `maps/cmp_types.json`: the operand types the pinned suite brought to every
comparison site (every process it starts, pool workers included). When every recorded
pair at each swapped site is of identity-equal types or two members of one plain enum,
the swapped comparison gives the same answer at every evaluation, so every test runs as at
the pinned commit; only tests that read the changed file as text stay listed. Any other
difference in the files, a chained comparison, a site with no record, or any other kind of
pair, and the patch goes through the normal analysis.

## Free fill (`free_fill` in `selector.py`)

The scorer charges the busiest worker plus 2 s per worker, so a test that fits under the
busiest worker's load on another worker costs nothing. After packing, we add such tests:
first the ones a wider selection would have listed (function-level or importer-level),
then the 94 tests recorded at 0 ms. This never changes the cost and may cover a failing
test our narrowing missed.

## The test runner (`br/runner.py`) — built, not in this build

Held out of S6 (branch `runner-s5-held`): running candidates cheapest first missed a test that
fails only after an earlier test in file order changes shared state. To return once it
runs tests in collection order and trusts a pass only after every earlier candidate in
the same worker passed.


Runs a commit's cheap candidate tests against the patched code and drops the ones it sees
pass, when doing so is expected to cost less CPU than listing them costs in simulated
seconds (expected CPU: a fixed overhead plus 1.2 CPU-seconds per recorded test-second plus
known class setups; the budget for a commit is what dropping its tests would save).

- A **base** process (forked once from the selector) imports unittest and pytest, never
  sqlglot. A **warm** process forked from it imports sqlglot from the pinned tree and the
  test modules, recording with `sys.monitoring` which code objects ran while it did.
- **Swap worker** (forked from warm, per commit): when every change is a function body
  that did not run in the warm process, keeps its free variables, parameters and docstring,
  and does not run at import, the worker gives every live function holding the old code the
  new code (`f.__code__ = new`) and runs the tests. Otherwise a **fresh worker** imports
  sqlglot and the tests from the patched view.
- Tests run through unittest's own machinery, reproducing what in pytest can change a
  verdict (subtest messages and parameters are formatted as pytest does, stack depth,
  warnings). It stops at the first failure; a pass counts only if nothing failed before it.
- Undecided, so listed: heavy tests, the 25 tests that start child processes (they import
  sqlglot afresh from disk), tests of patches touching a line no test ran alone, tests not
  in the maps, tests that resolve their own path through the symlinked view, anything after
  a failure or past a CPU or time limit, anything that hangs or crashes.
- Every test, worker and batch has a CPU limit; whatever goes wrong only leaves tests
  listed.

Checked by its author on 210 labeled patches (no labeled-failing test ever reported as
passing) and against pytest on all 1,592 runnable tests of the pinned tree.

## Verification (`br/verify.py`, `br/policy.py`) — built, currently off

A forked child runs candidate tests with real pytest on the patched view and stops at the
first failure; only tests seen to pass are dropped. Measured on the sample with a fresh
sqlglot import per commit, it saved about as many simulated seconds as it cost in CPU, so
it is switched off (`VERIFY_BUDGET = 0`) until children can start from an already-warm
process.

## Development tools (`tools/`, never part of the image)

- `tools/Dockerfile.dev`: the pinned base plus `requirements.lock`, without sqlglot;
  a patched sqlglot tree is mounted into it.
- `tools/label.py`: the labeler. For each patch it copies the pinned tree, applies the
  patch, and runs sqlglot's whole test suite with real pytest (`SKIP_INTEGRATION=0`,
  as the organizers did). It is how we check our understanding of "fails" against the
  sample answers and how we label our own test patches.
- `tools/outcome_plugin.py`: a small pytest plugin. pytest calls a function named
  `pytest_runtest_logreport` for every result it produces, including each subtest
  (sqlglot uses `self.subTest`, and pytest 9 prints a test as `PASSED` even when one of
  its subtests failed). The plugin records any test id with a failed report, plus test
  files that could not be imported, so a failing subtest counts as a failing test.

Labeled data stays out of the repository and out of the image; nothing at run time
reads any answers.
