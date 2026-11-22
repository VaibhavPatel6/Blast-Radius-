"""Parse a unified diff and apply it to the pinned sqlglot tree, with the standard library only.

`apply(patch_text, read_file)` returns a list of FileChange, one per file the patch
touches, each with the file's text before and after. Anything we cannot read with
certainty raises PatchError, and the caller then falls back to listing every test.
Being strict here is the point: a patch we half-understand must never be judged harmless.
"""

import re
from dataclasses import dataclass


class PatchError(Exception):
    pass


@dataclass
class FileChange:
    old_path: str | None  # None for an added file
    new_path: str | None  # None for a deleted file
    old_text: str | None
    new_text: str | None
    mode_change: bool = False


@dataclass
class _Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list  # (tag, text) with tag in " -+", text including its line ending


@dataclass
class _FilePatch:
    old_path: str | None = None
    new_path: str | None = None
    hunks: list = None
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    mode_change: bool = False
    rename: bool = False


def _lines(text: str) -> list[str]:
    """Split on "\n" only, keeping it. str.splitlines would also split on \r, \f and others."""
    parts = text.split("\n")
    out = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_prefix(path: str) -> str | None:
    path = path.split("\t", 1)[0].rstrip("\r\n")
    if path.startswith('"'):
        raise PatchError(f"quoted path {path!r}")
    if path == "/dev/null":
        return None
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return _safe_path(path)


def _safe_path(path: str) -> str:
    parts = path.split("/")
    if not path or path.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise PatchError(f"unsafe path {path!r}")
    return path


def parse(text: str) -> list[_FilePatch]:
    if text and not text.endswith("\n"):
        text += "\n"  # a patch cut just before its final newline: GNU patch accepts it too
    lines = _lines(text)
    files: list[_FilePatch] = []
    current: _FilePatch | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            current = _FilePatch(hunks=[])
            files.append(current)
            m = re.match(r"^diff --git a/(\S+) b/(\S+)\s*$", line)
            if not m:
                raise PatchError("unreadable diff --git line")
            current.old_path, current.new_path = _safe_path(m.group(1)), _safe_path(m.group(2))
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if current is None or current.hunks:
                current = _FilePatch(hunks=[])
                files.append(current)
            old, new = _strip_prefix(line[4:]), _strip_prefix(lines[i + 1][4:])
            current.old_path, current.new_path = old, new
            current.is_new = current.is_new or old is None
            current.is_deleted = current.is_deleted or new is None
            i += 2
            continue
        if line.startswith("@@"):
            if current is None:
                raise PatchError("hunk before any file header")
            m = _HUNK.match(line)
            if not m:
                raise PatchError("unreadable hunk header")
            old_start, old_len = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            new_start, new_len = int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1)
            hunk = _Hunk(old_start, old_len, new_start, new_len, [])
            i += 1
            seen_old = seen_new = 0
            while i < len(lines) and (seen_old < old_len or seen_new < new_len or
                                      (i < len(lines) and lines[i].startswith("\\"))):
                body = lines[i]
                if body.startswith("\\"):
                    # "\ No newline at end of file": the previous line has no line ending.
                    if not hunk.lines:
                        raise PatchError("no-newline marker with no line")
                    tag, prev = hunk.lines[-1]
                    if not prev.endswith("\n"):
                        raise PatchError("two no-newline markers for one line")
                    hunk.lines[-1] = (tag, prev[:-1])
                    i += 1
                    continue
                tag = body[:1]
                if body in ("\n", "\r\n"):  # some tools drop the space on empty context lines
                    tag, body = " ", " " + body
                if tag not in (" ", "-", "+"):
                    raise PatchError("unexpected line inside hunk")
                hunk.lines.append((tag, body[1:]))
                if tag in " -":
                    seen_old += 1
                if tag in " +":
                    seen_new += 1
                i += 1
            if seen_old != old_len or seen_new != new_len:
                raise PatchError("hunk length does not match its header")
            current.hunks.append(hunk)
            continue
        if current is not None:
            if line.startswith("new file mode"):
                current.is_new = True
            elif line.startswith("deleted file mode"):
                current.is_deleted = True
            elif line.startswith(("old mode", "new mode")):
                current.mode_change = True
            elif line.startswith(("copy from ", "copy to ")):
                raise PatchError("copied file")
            elif line.startswith("rename from "):
                current.rename = True
                current.old_path = _safe_path(line.split(" ", 2)[2].rstrip("\r\n"))
            elif line.startswith("rename to "):
                current.rename = True
                current.new_path = _safe_path(line.split(" ", 2)[2].rstrip("\r\n"))
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                current.is_binary = True
        i += 1
    if not files:
        raise PatchError("no file changes found")
    return files


def _apply_hunks(old_text: str, hunks: list[_Hunk]) -> str:
    old_lines = _lines(old_text)
    out: list[str] = []
    pos = 0  # index into old_lines of the next line not yet copied
    for hunk in hunks:
        expected = [text for tag, text in hunk.lines if tag in " -"]
        start = hunk.old_start - 1 if hunk.old_len > 0 else hunk.old_start
        if start < pos or old_lines[start:start + len(expected)] != expected:
            raise PatchError("hunk does not match the pinned source")
        out.extend(old_lines[pos:start])
        out.extend(text for tag, text in hunk.lines if tag in " +")
        pos = start + len(expected)
    out.extend(old_lines[pos:])
    return "".join(out)


def apply(text: str, read_file) -> list[FileChange]:
    """read_file(path) returns the pinned file's text, or None if it does not exist."""
    changes = []
    touched = set()
    for fp in parse(text):
        if fp.is_binary:
            raise PatchError("binary patch")
        old_path = None if fp.is_new else fp.old_path
        new_path = None if fp.is_deleted else fp.new_path
        for p in {old_path, new_path}:
            if p is not None:
                if p in touched:
                    raise PatchError(f"{p} appears twice")
                touched.add(p)
        old_text = None
        if old_path is not None:
            old_text = read_file(old_path)
            if old_text is None:
                raise PatchError(f"{old_path} is not in the pinned tree")
        elif new_path is not None and read_file(new_path) is not None:
            raise PatchError(f"{new_path} is added but already exists")
        if fp.is_deleted:
            new_text = None
            if fp.hunks and _apply_hunks(old_text, fp.hunks) != "":
                raise PatchError("deletion does not remove the whole file")
        elif old_text is None:
            new_text = _apply_hunks("", fp.hunks)
        else:
            new_text = _apply_hunks(old_text, fp.hunks) if fp.hunks else old_text
        changes.append(FileChange(old_path, new_path, old_text, new_text, fp.mode_change))
    return changes
