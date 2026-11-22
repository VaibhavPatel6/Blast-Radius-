"""Build step: download sqlglot at the pinned commit from its own GitHub repository,
check the contents against a committed manifest hash, and unpack it to a directory.

    python fetch_sqlglot.py /opt/sqlglot          # build: download, verify, unpack
    python fetch_sqlglot.py --manifest TARBALL    # development: print the manifest hash

The hash covers every file's path and contents, not the tarball's bytes, because
GitHub does not promise that its generated archives are byte-for-byte stable.
"""

import hashlib
import io
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"
EXPECTED_MANIFEST = "d1e4ef422f377f2f400aa81b6b1fb7ad457e2e661381649c614ccd0192c26fb0"


def manifest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if not p.is_dir() or p.is_symlink()):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            body = b"link:" + str(path.readlink()).encode()
        else:
            body = hashlib.sha256(path.read_bytes()).hexdigest().encode()
        digest.update(rel.encode() + b"\0" + body + b"\n")
    return digest.hexdigest()


def unpack(data: bytes, into: Path) -> Path:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        archive.extractall(into, filter="data")
    (top,) = list(into.iterdir())
    return top


def main(argv: list[str]) -> int:
    if argv[:1] == ["--manifest"]:
        with tempfile.TemporaryDirectory() as tmp:
            print(manifest(unpack(Path(argv[1]).read_bytes(), Path(tmp))))
        return 0
    dest = Path(argv[0])
    with urllib.request.urlopen(URL, timeout=300) as response:
        data = response.read()
    with tempfile.TemporaryDirectory() as tmp:
        top = unpack(data, Path(tmp))
        found = manifest(top)
        if found != EXPECTED_MANIFEST:
            print(f"sqlglot manifest mismatch: {found} != {EXPECTED_MANIFEST}", file=sys.stderr)
            return 1
        shutil.move(str(top), dest)
    print(f"sqlglot {COMMIT} verified and unpacked to {dest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
