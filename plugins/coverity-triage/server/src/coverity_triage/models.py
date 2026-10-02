"""Data models shared across modules."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Event(BaseModel):
    """One step of the Coverity warning path."""

    file: str
    line: int | None = None
    tag: str = ""
    description: str = ""
    main: bool = False


class Issue(BaseModel):
    """Basic information of one CID (spec D-33)."""

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
    # Notes when the reported path was mapped automatically or could not be mapped.
    path_notes: list[str] = Field(default_factory=list)


class IssueDetail(BaseModel):
    """Issue plus warning path and checker documentation (spec D-33)."""

    issue: Issue
    events: list[Event] = Field(default_factory=list)
    checker_description: str = ""
    checker_remediation: str = ""


Judgement = Literal["false_positive", "true_bug", "intentional", "undetermined"]
Plan = Literal["fix", "deviation"]
Confidence = Literal["high", "medium", "low"]


class TriageAttributes(BaseModel):
    classification: str
    action: str
    severity: str


class Evidence(BaseModel):
    file: str
    line: int | None = None
    note: str = ""


class Verdict(BaseModel):
    judgement: Judgement
    summary: str
    rationale: str
    evidence: list[Evidence] = Field(default_factory=list)

    @field_validator("summary", "rationale")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class DeviationPlan(TriageAttributes):
    comment: str

    @field_validator("comment")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class FixPlan(TriageAttributes):
    summary: str
    impact: str
    exceeded_constraints: list[str] = Field(default_factory=list)
    already_fixed_on_latest: bool = False


class RevisionDrift(BaseModel):
    status: Literal["none", "detected", "unknown"]
    detail: str = ""


class TriageResult(BaseModel):
    """What the worker subagent submits via ``submit_result`` (design 5.5)."""

    work_item: str
    verdict: Verdict
    recommendation: Plan
    confidence: Confidence
    confidence_reason: str
    deviation: DeviationPlan
    fix: FixPlan
    revision_drift: RevisionDrift
    group_excluded_cids: list[int] = Field(default_factory=list)
