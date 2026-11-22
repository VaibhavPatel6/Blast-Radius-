# Design

How this entry works, one section per component, in plain language. Each section
says what the component does and why it is safe.

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

A test that the maps do not know (a different `tests.json`) is always listed. If the
maps name a code object that this interpreter does not produce when it compiles the
pinned file, the maps and the image disagree and that commit lists every test (a
lookup must never come back empty just because a name changed).

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
