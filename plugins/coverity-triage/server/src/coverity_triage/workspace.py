"""Workspaces scoped to one work item (spec D-57; design 3 source tools).

Investigation code is a read-only snapshot. Fix and annotation edits go to an overlay
directory on top of the latest-revision snapshot, so parallel work items never touch
each other or the user's working copy.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
from pathlib import Path, PurePosixPath

from .encoding import read_source, replace_once, write_source

FORBIDDEN_PARTS = {".git", ".svn"}
MAX_SEARCH_HITS = 200
MAX_SEARCH_FILE_BYTES = 2_000_000


class WorkspaceError(Exception):
    """Raised for invalid paths or operations in a workspace."""


def safe_relpath(path: str) -> str:
    """Normalize a repository-relative path and reject anything leaving the workspace."""
    text = path.replace("\\", "/").strip()
    if not text:
        raise WorkspaceError("パスが空です")
    pure = PurePosixPath(text)
    if pure.is_absolute() or re.match(r"^[A-Za-z]:", text):
        raise WorkspaceError(f"リポジトリからの相対パスを指定してください: {path}")
    parts = [p for p in pure.parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise WorkspaceError(f"作業領域の外は参照できません: {path}")
    if any(p in FORBIDDEN_PARTS for p in parts):
        raise WorkspaceError(f"VCS の管理領域は参照できません: {path}")
    return "/".join(parts)


class ReadOnlyTree:
    """A directory tree that can be read and searched."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def locate(self, rel: str) -> Path:
        return self.root / safe_relpath(rel)

    def read_lines(self, rel: str, start: int = 1, end: int | None = None) -> dict:
        path = self.locate(rel)
        if not path.is_file():
            raise WorkspaceError(f"ファイルが見つかりません: {rel}")
        source = read_source(path)
        lines = source.lines()
        start = max(1, start)
        end = len(lines) if end is None else min(end, len(lines))
        numbered = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))
        return {"path": safe_relpath(rel), "total_lines": len(lines), "start": start,
                "end": end, "encoding": source.encoding, "content": numbered}

    def iter_files(self, glob: str | None = None):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in FORBIDDEN_PARTS]
            for name in filenames:
                full = Path(dirpath) / name
                rel = full.relative_to(self.root).as_posix()
                if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(name, glob)):
                    continue
                yield rel, full

    def search(self, pattern: str, glob: str | None = None,
               max_hits: int = MAX_SEARCH_HITS) -> dict:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise WorkspaceError(f"正規表現が不正です: {exc}") from exc
        hits, truncated = [], False
        for rel, full in self.iter_files(glob):
            if full.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            try:
                lines = read_source(full).lines()
            except Exception:
                continue  # binary or undecodable file
            for n, line in enumerate(lines, 1):
                if regex.search(line):
                    if len(hits) >= max_hits:
                        truncated = True
                        break
                    hits.append({"path": rel, "line": n, "text": line.strip()[:300]})
            if truncated:
                break
        return {"hits": hits, "truncated": truncated}


class OverlayTree(ReadOnlyTree):
    """Latest-revision snapshot with this work item's edits layered on top."""

    def __init__(self, base: str | Path, overlay: str | Path):
        super().__init__(base)
        self.base = Path(base)
        self.overlay = Path(overlay)

    def locate(self, rel: str) -> Path:
        rel = safe_relpath(rel)
        edited = self.overlay / rel
        return edited if edited.is_file() else self.base / rel

    def iter_files(self, glob: str | None = None):
        seen = set()
        for tree in (ReadOnlyTree(self.overlay), ReadOnlyTree(self.base)):
            if not tree.root.is_dir():
                continue
            for rel, full in tree.iter_files(glob):
                if rel not in seen:
                    seen.add(rel)
                    yield rel, full

    def edit(self, rel: str, old: str, new: str, ascii_file_encoding: str = "utf-8") -> dict:
        rel = safe_relpath(rel)
        current = self.locate(rel)
        if not current.is_file():
            raise WorkspaceError(f"ファイルが見つかりません: {rel}")
        updated = replace_once(read_source(current), old, new, ascii_file_encoding)
        target = self.overlay / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        write_source(target, updated)
        return {"path": rel, "encoding": updated.encoding, "newline": repr(updated.newline)}

    def changed_files(self) -> list[str]:
        if not self.overlay.is_dir():
            return []
        changed = []
        for rel, full in ReadOnlyTree(self.overlay).iter_files():
            base = self.base / rel
            if not base.is_file() or base.read_bytes() != full.read_bytes():
                changed.append(rel)
        return sorted(changed)

    def mirror_to(self, dest: str | Path) -> list[str]:
        """Copy changed files keeping the repository layout (spec D-4)."""
        files = self.changed_files()
        for rel in files:
            target = Path(dest) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.overlay / rel, target)
        return files

    def materialize(self, dest: str | Path) -> Path:
        """Full tree with the edits applied, for build and re-analysis."""
        dest = Path(dest)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.base, dest, ignore=shutil.ignore_patterns(*FORBIDDEN_PARTS))
        for rel in self.changed_files():
            shutil.copy2(self.overlay / rel, dest / rel)
        return dest
