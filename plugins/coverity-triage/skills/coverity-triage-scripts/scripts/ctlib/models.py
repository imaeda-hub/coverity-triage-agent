"""Data read from the MCP server's files, and the result file the worker subagent writes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Event(BaseModel):
    """One step of the warning path (``issue-<CID>.json``)."""

    model_config = ConfigDict(extra="ignore")

    file: str
    line: int | None = None
    tag: str = ""
    description: str = ""
    main: bool = False


class Issue(BaseModel):
    """One CID (``issues.json``). Same fields as the MCP server's ``models.Issue``."""

    model_config = ConfigDict(extra="ignore")

    cid: int
    checker: str
    file: str
    function: str | None = None
    line: int | None = None
    impact: str | None = None
    category: str | None = None
    cwe: int | None = None
    merge_key: str | None = None
    macro: str | None = None
    stream: str | None = None
    classification: str | None = None
    action: str | None = None
    severity: str | None = None
    status: str | None = None


class IssueDetail(BaseModel):
    """``issue-<CID>.json``: the warning path, or the reason it could not be fetched."""

    model_config = ConfigDict(extra="ignore")

    issue: Issue | None = None
    events: list[Event] = Field(default_factory=list)
    checker_description: str = ""
    error: str | None = None


# ---- result.json (written by the worker subagent; skill triage-report) ----------------------

Judgement = Literal["false_positive", "true_bug", "intentional", "undetermined"]
Plan = Literal["fix", "deviation"]
Confidence = Literal["high", "medium", "low"]


def _text(value: str) -> str:
    if not value.strip():
        raise ValueError("空にできません")
    return value.strip()


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Result):
    file: str
    line: int | None = None
    note: str = ""


class Verdict(_Result):
    judgement: Judgement
    summary: str
    rationale: str
    evidence: list[Evidence] = Field(default_factory=list)

    _check = field_validator("summary", "rationale")(_text)


class Attributes(_Result):
    classification: str
    action: str
    severity: str = "Unspecified"

    _check = field_validator("classification", "action")(_text)


class DeviationPlan(Attributes):
    comment: str

    _comment = field_validator("comment")(_text)


class FixPlan(Attributes):
    summary: str
    impact: str
    exceeded_constraints: list[str] = Field(default_factory=list)
    already_fixed_on_latest: bool = False

    _texts = field_validator("summary", "impact")(_text)


class RevisionDrift(_Result):
    status: Literal["none", "detected", "unknown"]
    detail: str = ""


class TriageResult(_Result):
    item: str
    verdict: Verdict
    recommendation: Plan
    confidence: Confidence
    confidence_reason: str
    deviation: DeviationPlan
    fix: FixPlan
    revision_drift: RevisionDrift
    group_excluded_cids: list[int] = Field(default_factory=list)

    _reason = field_validator("confidence_reason")(_text)
