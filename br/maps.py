"""Read the committed maps (built by tools/covmap/build_maps.py from the pinned commit).

Each repository file has its own map file, so a patch loads only what it touches.
A file with no map file was never touched by any test.
"""

import json
from pathlib import Path


class Maps:
    def __init__(self, root: Path):
        self.root = root
        index = json.loads((root / "index.json").read_text("utf-8"))
        self.ids: list[str] = index["ids"]
        self.sets: list[list[int]] = index["sets"]
        self._listed: dict[str, int] = index["listed"]
        self.tests: dict[str, dict] = index["tests"]
        self._files: dict[str, dict | None] = {}

    def _ids(self, set_index: int | None) -> set[str]:
        if set_index is None:
            return set()
        return {self.ids[i] for i in self.sets[set_index]}

    def _file(self, path: str) -> dict | None:
        if path not in self._files:
            f = self.root / "files" / (path + ".json")
            self._files[path] = json.loads(f.read_text("utf-8")) if f.is_file() else None
        return self._files[path]

    def opened(self, path: str) -> set[str]:
        e = self._file(path)
        return self._ids(e["opened"]) if e else set()

    def entered(self, path: str, qualname: str, firstlineno: int) -> set[str]:
        e = self._file(path)
        return self._ids(e["code"].get(f"{qualname}@{firstlineno}")) if e else set()

    def code_keys(self, path: str) -> set[str]:
        e = self._file(path)
        return set(e["code"]) if e else set()

    def lines(self, path: str, line: int) -> set[str]:
        e = self._file(path)
        return self._ids(e["lines"].get(str(line))) if e else set()

    def readers(self, path: str, qualname: str, attribute: str) -> set[str] | None:
        """Tests that read `attribute` from the dict of class `qualname` (defined in `path`),
        or None when the maps hold no read records for that file at all."""
        e = self._file(path)
        if not e or "touched" not in e or not e["touched"]:
            return None
        out: set[str] = set()
        for key, names in e["touched"].items():
            if key.rsplit("@", 1)[0] == qualname and attribute in names:
                out |= self._ids(names[attribute])
        return out

    def readers_run(self, path: str, qualname: str, attribute: str) -> set[str] | None:
        """Like `readers`, counting only reads made outside imports (by the test itself or
        code it calls), or None when the maps hold no such records for the file."""
        e = self._file(path)
        if not e or not e.get("touched_run"):
            return None
        out: set[str] = set()
        for key, names in e["touched_run"].items():
            if key.rsplit("@", 1)[0] == qualname and attribute in names:
                out |= self._ids(names[attribute])
        return out

    def wrapped(self, path: str, qualname: str) -> set[str] | None:
        """Attribute names the read recorder actually instrumented on that class, or None."""
        e = self._file(path)
        if not e or not e.get("wrapped"):
            return None
        names = [set(v) for k, v in e["wrapped"].items() if k.rsplit("@", 1)[0] == qualname]
        return set().union(*names) if names else None

    def listed(self, directory: str) -> set[str]:
        return self._ids(self._listed.get(directory))
