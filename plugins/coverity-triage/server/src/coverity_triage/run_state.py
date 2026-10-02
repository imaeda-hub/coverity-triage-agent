"""Run folder, progress tracking and resume (spec D-15, D-43, D-51).

Layout of one run folder (``<output_dir>/<YYYYMMDD-HHMMSS>/``)::

    run.json          run metadata and per-item progress
    issues.json       issues found by the filter (cached)
    results/<id>.json results submitted by worker subagents
    cid/<id>.md       per-item reports
    summary.md        summary list with the approval column
    patches/          patch files
    fixed/            fixed files mirrored with the working copy layout
    operations.log    tool operation log
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .grouping import WorkItem
from .models import Issue

ItemStatus = Literal["pending", "in_progress", "done", "error"]

RUN_FILE = "run.json"
ISSUES_FILE = "issues.json"


class RunError(Exception):
    """Raised for invalid operations on a run."""


class ItemState(BaseModel):
    id: str
    cids: list[int]
    status: ItemStatus = "pending"
    error: str = ""
    attempts: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    seconds: float | None = None
    split_from: str | None = None
    applied: bool = False
    apply_result: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_group(self) -> bool:
        return self.id.startswith("G")


class RunMeta(BaseModel):
    run_id: str
    repo_root: str
    created_at: str
    filter_name: str
    filter: dict[str, Any]
    verify_mode: str
    analyzed_revision: str | None = None
    analyzed_revision_source: Literal["snapshot", "filter", "local"] = "local"
    items: list[ItemState] = Field(default_factory=list)


_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(run_dir: Path) -> threading.RLock:
    key = str(run_dir.resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def _write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class RunStore:
    """Thread-safe access to one run folder. Parallel subagents share one MCP server process."""

    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir)
        if not (self.run_dir / RUN_FILE).is_file():
            raise RunError(f"実行フォルダではありません: {self.run_dir}")
        self._lock = _lock_for(self.run_dir)

    # ---- creation -------------------------------------------------------------------

    @classmethod
    def create(cls, output_dir: str | Path, *, repo_root: str, filter_name: str,
               filter_data: dict[str, Any], verify_mode: str, issues: list[Issue],
               items: list[WorkItem], analyzed_revision: str | None,
               analyzed_revision_source: str) -> RunStore:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        base = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_id, n = base, 1
        while (output / run_id).exists():
            n += 1
            run_id = f"{base}-{n}"
        run_dir = output / run_id
        for sub in ("results", "cid", "patches", "fixed"):
            (run_dir / sub).mkdir(parents=True)
        meta = RunMeta(
            run_id=run_id, repo_root=str(Path(repo_root).resolve()),
            created_at=datetime.now().isoformat(timespec="seconds"),
            filter_name=filter_name, filter=filter_data, verify_mode=verify_mode,
            analyzed_revision=analyzed_revision,
            analyzed_revision_source=analyzed_revision_source,  # type: ignore[arg-type]
            items=[ItemState(id=i.id, cids=i.cids) for i in items],
        )
        _write_json_atomic(run_dir / ISSUES_FILE, [i.model_dump() for i in issues])
        _write_json_atomic(run_dir / RUN_FILE, meta.model_dump())
        return cls(run_dir)

    # ---- reading --------------------------------------------------------------------

    def load(self) -> RunMeta:
        with self._lock:
            data = json.loads((self.run_dir / RUN_FILE).read_text(encoding="utf-8"))
            return RunMeta.model_validate(data)

    def save(self, meta: RunMeta) -> None:
        with self._lock:
            _write_json_atomic(self.run_dir / RUN_FILE, meta.model_dump())

    def issues(self) -> dict[int, Issue]:
        data = json.loads((self.run_dir / ISSUES_FILE).read_text(encoding="utf-8"))
        return {d["cid"]: Issue.model_validate(d) for d in data}

    def item(self, item_id: str) -> ItemState:
        for item in self.load().items:
            if item.id == item_id:
                return item
        raise RunError(f"作業項目が見つかりません: {item_id}")

    def status_counts(self) -> dict[str, int]:
        counts = {"pending": 0, "in_progress": 0, "done": 0, "error": 0}
        for item in self.load().items:
            counts[item.status] += 1
        return counts

    # ---- progress ---------------------------------------------------------------------

    def claim_next(self) -> ItemState | None:
        """Mark the next pending item as in progress and return it."""
        with self._lock:
            meta = self.load()
            for item in meta.items:
                if item.status == "pending":
                    item.status = "in_progress"
                    item.attempts += 1
                    item.error = ""
                    item.started_at = time.time()
                    self.save(meta)
                    return item
            return None

    def _update(self, item_id: str, **changes: Any) -> ItemState:
        with self._lock:
            meta = self.load()
            for item in meta.items:
                if item.id == item_id:
                    for key, value in changes.items():
                        setattr(item, key, value)
                    self.save(meta)
                    return item
            raise RunError(f"作業項目が見つかりません: {item_id}")

    def _finish(self, item_id: str, status: ItemStatus, error: str) -> ItemState:
        with self._lock:
            item = self.item(item_id)
            now = time.time()
            seconds = now - item.started_at if item.started_at else None
            return self._update(item_id, status=status, error=error, finished_at=now, seconds=seconds)

    def mark_done(self, item_id: str) -> ItemState:
        return self._finish(item_id, "done", "")

    def mark_error(self, item_id: str, message: str) -> ItemState:
        return self._finish(item_id, "error", message)

    def mark_applied(self, item_id: str, result: dict[str, Any]) -> ItemState:
        return self._update(item_id, applied=True, apply_result=result)

    def reset_for_resume(self) -> dict[str, int]:
        """Interrupted and failed items go back to pending (spec D-15, D-51)."""
        with self._lock:
            meta = self.load()
            reset = {"in_progress": 0, "error": 0}
            for item in meta.items:
                if item.status in reset:
                    reset[item.status] += 1
                    item.status = "pending"
            self.save(meta)
            return reset

    def split_group(self, item_id: str, excluded: list[int]) -> list[str]:
        """Move CIDs out of a group into their own pending items (spec D-53)."""
        with self._lock:
            meta = self.load()
            group = next((i for i in meta.items if i.id == item_id), None)
            if group is None or not group.is_group:
                raise RunError(f"グループではありません: {item_id}")
            excluded_set = set(excluded)
            unknown = excluded_set - set(group.cids)
            if unknown:
                raise RunError(f"グループに含まれない CID です: {sorted(unknown)}")
            remaining = [c for c in group.cids if c not in excluded_set]
            if not remaining:
                raise RunError("グループのすべての CID を除外することはできません")
            group.cids = remaining
            new_ids = []
            for cid in excluded:
                meta.items.append(ItemState(id=str(cid), cids=[cid], split_from=item_id))
                new_ids.append(str(cid))
            self.save(meta)
            return new_ids
