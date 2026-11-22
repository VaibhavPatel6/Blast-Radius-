#!/usr/bin/env python3
"""G3: build a set of hostile patches that the selector must survive with valid output.

    python3 tools/make_fuzz_set.py OUT_DIR

Writes OUT_DIR/commits/*.patch and an answers.json with no failing tests (this set
checks validity, time and limits only; tools/measure.py reports rejections).
"""
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    out = Path(sys.argv[1])
    commits = out / "commits"
    commits.mkdir(parents=True, exist_ok=True)
    real = sorted((ROOT / "sample" / "commits").glob("*.patch"))
    base = real[0].read_bytes()
    rng = random.Random(7)
    cases = {
        "empt": b"",
        "nl00": b"\n",
        "binr": bytes(rng.randrange(256) for _ in range(5000)),
        "bom0": b"\xef\xbb\xbf" + base,
        "crlf": base.replace(b"\n", b"\r\n"),
        "trav": base.replace(b"a/sqlglot/", b"a/../../etc/").replace(b"b/sqlglot/", b"b/../../etc/"),
        "nofi": base.replace(b"sqlglot/", b"sqlglot/does_not_exist/"),
        "trnc": base[: len(base) // 2],
        "huge": b"".join(base for _ in range(3000)),
        "gitb": b"diff --git a/x.bin b/x.bin\nnew file mode 100644\nindex 0000000..1111111\nGIT binary patch\nliteral 3\nKcmZ?d00001\n\nliteral 0\nHcmV?d00001\n\n",
        "dele": b"diff --git a/sqlglot/trie.py b/sqlglot/trie.py\ndeleted file mode 100644\n",
        "cnft": b"diff --git a/tests/conftest.py b/tests/conftest.py\nnew file mode 100644\n--- /dev/null\n+++ b/tests/conftest.py\n@@ -0,0 +1 @@\n+import os\n",
        "utf8": base.replace(b"+", "+# é中 x ".encode(), 1),
        "nul0": base.replace(b"\n", b"\x00\n", 3),
        "hunk": base.replace(b"@@ -", b"@@ -99999", 1),
        "dupl": base + base,
    }
    for name, data in cases.items():
        (commits / f"{name}.patch").write_bytes(data)
    # also every real sample patch, so the set exercises the normal path at volume
    for i in range(120):
        (commits / f"r{i:03d}.patch").write_bytes(real[i % len(real)].read_bytes())
    answers = {"commits": {p.stem: {"failing": [], "kind": "fuzz"} for p in commits.glob("*.patch")}}
    (out / "answers.json").write_text(json.dumps(answers))
    print(f"{len(answers['commits'])} patches in {commits}")


if __name__ == "__main__":
    main()
