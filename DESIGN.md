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

Current version (submission S1): every commit lists every test. This can never miss a
failing test. Its only purpose is to prove the build-and-submit path end to end.

**Packing.** The scorer charges a commit its busiest worker's load plus 2 s per worker.
`pack()` tries every worker count from 1 to 8; for each, it places tests longest first
onto the currently least-loaded worker (a standard heuristic called LPT, "longest
processing time first"), computes the scorer's cost in whole milliseconds, and keeps
the cheapest. For all 1,620 tests the best is 4 workers at 36.817 s: the 28.8 s
`test_max_nodes` sits alone on one worker and the rest fit beside it.

**Output safety.** Every line is checked against the scorer's own rules (known test
numbers, no repeats, at most 8 workers) before anything is printed, and all lines are
written in one go at the end. Logs go to stderr only. A wrong line on stdout would void
the whole run, so the checks are not optional.

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
