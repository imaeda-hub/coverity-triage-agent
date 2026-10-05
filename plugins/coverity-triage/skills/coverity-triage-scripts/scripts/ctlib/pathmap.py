"""Map file paths reported by Coverity to repository-relative paths.

Coverity may report absolute paths of the build environment. A path is mapped by
(1) stripping a prefix from ``coverity.path_strip_prefixes``, or, failing that,
(2) the longest trailing part of the path that exists in the repository.
Automatic mappings are noted so the report can say so.
"""

from __future__ import annotations

import os
from pathlib import Path

from .models import Event, Issue

SKIP_DIRS = {".git", ".svn"}


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip()


class PathMapper:
    def __init__(self, root: str | Path, strip_prefixes: list[str]):
        self.root = Path(root)
        self.prefixes = sorted((_norm(p).rstrip("/") + "/" for p in strip_prefixes if p.strip()),
                               key=len, reverse=True)
        self._files: dict[str, list[str]] | None = None

    def _index(self) -> dict[str, list[str]]:
        """Repository files by base name."""
        if self._files is None:
            self._files = {}
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for name in filenames:
                    rel = (Path(dirpath) / name).relative_to(self.root).as_posix()
                    self._files.setdefault(name.lower(), []).append(rel)
        return self._files

    def map(self, path: str) -> tuple[str, str]:
        """Return (repository-relative path, how it was mapped).

        The method is "as_is", "prefix", "suffix" or "unmapped" (the original path is kept).
        """
        norm = _norm(path)
        is_absolute = norm.startswith("/") or (len(norm) > 1 and norm[1] == ":")
        if not is_absolute and (self.root / norm).is_file():
            return norm, "as_is"
        lowered = norm.lower()
        for prefix in self.prefixes:
            if lowered.startswith(prefix.lower()):
                return norm[len(prefix):], "prefix"
        parts = [p for p in norm.split("/") if p and not p.endswith(":")]
        if not parts:
            return norm, "unmapped"
        best: list[str] = []
        best_len = 0
        for candidate in self._index().get(parts[-1].lower(), []):
            cparts = candidate.lower().split("/")
            n = 0
            while n < min(len(cparts), len(parts)) and cparts[-1 - n] == parts[-1 - n].lower():
                n += 1
            if n > best_len:
                best, best_len = [candidate], n
            elif n == best_len:
                best.append(candidate)
        if len(best) == 1:
            return best[0], "suffix"
        return norm, "unmapped"

    def map_issue(self, issue: Issue, notes: list[str]) -> Issue:
        return issue.model_copy(update={"file": self._mapped(issue.file, notes)})

    def map_event(self, event: Event, notes: list[str]) -> Event:
        return event.model_copy(update={"file": self._mapped(event.file, notes)})

    def _mapped(self, path: str, notes: list[str]) -> str:
        rel, method = self.map(path)
        if method in ("suffix", "unmapped"):
            note = _note(path, rel, method)
            if note not in notes:
                notes.append(note)
        return rel


def _note(original: str, mapped: str, method: str) -> str:
    if method == "suffix":
        return f"パスを自動で対応づけました: {original} → {mapped}"
    return f"パスをリポジトリ内に対応づけられませんでした: {original}（設定 coverity.path_strip_prefixes を確認）"
