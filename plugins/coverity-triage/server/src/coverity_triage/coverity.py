"""Access to Coverity Connect (spec D-3, D-31, D-33, D-40).

The rest of the server only depends on :class:`CoverityClient`. The connection to
Coverity Connect is in :mod:`.connect` (spec D-77). :class:`FakeCoverityClient` reads issues
from a local YAML file and is used for trials of the plugin and for tests.
"""

from __future__ import annotations

import fnmatch
import json
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml

from .config import CoverityConfig, FilterSpec
from .models import Event, Issue, IssueDetail, TriageAttributes


class CoverityError(Exception):
    """Raised when Coverity cannot be reached or returns an error."""


def filter_matches(issue: Issue, spec: FilterSpec, project: str | None = None) -> bool:
    """Values inside one key are OR-ed, keys are AND-ed (design 5.1)."""
    def any_of(value: str | None, allowed: list[str]) -> bool:
        return not allowed or (value is not None and value in allowed)

    if spec.project and project is not None and project != spec.project:
        return False
    if spec.checkers and not any(fnmatch.fnmatchcase(issue.checker, p) for p in spec.checkers):
        return False
    return (any_of(issue.stream, spec.streams) and any_of(issue.impact, spec.impacts)
            and any_of(issue.status, spec.triage.status)
            and any_of(issue.classification, spec.triage.classification)
            and any_of(issue.action, spec.triage.action))


class CoverityClient(ABC):
    #: Number of matching issues the server reported for the last search (None if unknown).
    last_total: int | None = None

    @abstractmethod
    def search_issues(self, spec: FilterSpec, limit: int | None = None) -> list[Issue]:
        """Issues matching the filter, in the server's order; at most ``limit`` when given
        (the search stops as soon as enough are found)."""

    def take_request_log(self) -> list[dict[str, Any]]:
        """Requests sent to Coverity since the last call, with their time (spec D-85)."""
        log, self._request_log = getattr(self, "_request_log", []), []
        return log

    @abstractmethod
    def get_issue_detail(self, issue: Issue) -> IssueDetail:
        """Warning path (events) and checker documentation."""

    @abstractmethod
    def snapshot_revision(self, spec: FilterSpec, field: str) -> str | None:
        """git commit / svn revision recorded in the latest snapshot, if any (spec D-17)."""

    @abstractmethod
    def write_triage(self, cids: list[int], attributes: TriageAttributes, comment: str,
                     spec: FilterSpec) -> None:
        """Register classification, action, severity and comment (spec D-31)."""


class FakeCoverityClient(CoverityClient):
    """Issues from a YAML file. Writes are appended to ``<file>.writes.jsonl``."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise CoverityError(f"偽データのファイルが見つかりません: {self.path}")
        self.data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self._lock = threading.Lock()

    def _records(self) -> list[dict]:
        return self.data.get("issues") or []

    def _issue(self, record: dict) -> Issue:
        fields = {k: v for k, v in record.items() if k in Issue.model_fields}
        return Issue.model_validate(fields)

    def search_issues(self, spec: FilterSpec, limit: int | None = None) -> list[Issue]:
        found = [self._issue(r) for r in self._records()
                 if filter_matches(self._issue(r), spec, r.get("project"))]
        self.last_total = len(found)
        return found[:limit] if limit else found

    def get_issue_detail(self, issue: Issue) -> IssueDetail:
        for record in self._records():
            if record.get("cid") == issue.cid:
                return IssueDetail(
                    issue=self._issue(record),
                    events=[Event.model_validate(e) for e in record.get("events") or []],
                    checker_description=record.get("checker_description", ""),
                    checker_remediation=record.get("checker_remediation", ""),
                )
        raise CoverityError(f"CID が見つかりません: {issue.cid}")

    def snapshot_revision(self, spec: FilterSpec, field: str) -> str | None:
        snapshot = self.data.get("snapshot") or {}
        value = snapshot.get(field)
        return str(value) if value else None

    def write_triage(self, cids, attributes, comment, spec) -> None:
        record = {"cids": cids, **attributes.model_dump(), "comment": comment}
        with self._lock, open(str(self.path) + ".writes.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def make_client(config: CoverityConfig) -> CoverityClient:
    if config.api == "fake":
        if not config.fake_data:
            raise CoverityError("coverity.api が fake のときは coverity.fake_data を指定してください")
        return FakeCoverityClient(config.fake_data)
    from .connect import ConnectClient
    return ConnectClient(config)
