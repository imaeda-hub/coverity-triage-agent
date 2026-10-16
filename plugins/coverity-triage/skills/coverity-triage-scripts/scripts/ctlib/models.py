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


# ---- review.json (written by the reviewer agent; skill triage-review) -----------------------

ReviewOutcome = Literal["pass", "flag", "rework"]


class ReviewFinding(_Result):
    check: str
    ok: bool
    detail: str
    file: str | None = None
    line: int | None = None

    _detail = field_validator("check", "detail")(_text)


class ReviewResult(_Result):
    """The reviewer's independent check of one item. It never changes result.json: the scripts take
    the lower of the worker's confidence and ``confidence_cap`` as the confidence shown to the person."""

    item: str
    outcome: ReviewOutcome
    findings: list[ReviewFinding] = Field(default_factory=list)
    confidence_cap: Confidence | None = None
    rework: list[str] = Field(default_factory=list)  # what the worker is to change (outcome rework)
    note: str  # one or two sentences for the person

    _note = field_validator("note")(_text)


# ---- integration/plan.json (written by the integrator agent; skill triage-integration) ------


class MergedResult(_Result):
    """The result of a merged item, as in result.json (the scripts add ``item``)."""

    verdict: Verdict
    recommendation: Plan
    confidence: Confidence
    confidence_reason: str
    deviation: DeviationPlan
    fix: FixPlan
    revision_drift: RevisionDrift
    # written in result.json too (skill triage-report); the scripts set them for the merged item
    item: str | None = None
    group_excluded_cids: list[int] = Field(default_factory=list)

    _reason = field_validator("confidence_reason")(_text)


class Merge(_Result):
    members: list[str]  # ids of the finished items that have one cause
    reason: str
    # "item:<ID>": the fix of that member covers every warning; "combine": the scripts join the
    # members' fixes; "rework": a worker writes one new fix from the members' results
    fix_source: str
    result: MergedResult

    _reason = field_validator("reason")(_text)


class Conflict(_Result):
    items: list[str]  # items whose fixes change the same lines although their causes differ
    note: str

    _note = field_validator("note")(_text)


class IntegrationPlan(_Result):
    merges: list[Merge] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
