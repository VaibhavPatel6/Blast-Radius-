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

    def listed(self, directory: str) -> set[str]:
        return self._ids(self._listed.get(directory))
