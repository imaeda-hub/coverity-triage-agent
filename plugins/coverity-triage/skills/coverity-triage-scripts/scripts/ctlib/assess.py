"""The confidence shown to the person: the lowest of the worker's, the review's and the build's.

``result.json`` keeps what the worker wrote. The review (``review.json``, option agents.review) can
only lower the confidence, and so can the build verification for an item whose recommendation is the
fix; a deviation keeps its confidence, because its fix is only the other proposal.
"""

from __future__ import annotations

from typing import Any

from .models import TriageResult

ORDER = {"low": 0, "medium": 1, "high": 2}
JA = {"high": "高", "medium": "中", "low": "低"}


def review_cap(review: dict[str, Any] | None) -> str | None:
    """The highest confidence the review allows (None: no limit)."""
    if not review:
        return None
    cap = review.get("confidence_cap")
    if review.get("outcome") in ("flag", "rework") and not cap:
        cap = "low"
    return cap if cap in ORDER else None


def effective(result: TriageResult, review: dict[str, Any] | None = None,
              verify: dict[str, Any] | None = None) -> TriageResult:
    """The result as the person sees it, with the reasons a lower confidence was given."""
    confidence, reasons = result.confidence, []
    cap = review_cap(review)
    if cap and ORDER[cap] < ORDER[confidence]:
        confidence = cap
        reasons.append(f"独立した検証で「{JA[cap]}」まで（{review.get('note', '').strip() or '要確認'}）")
    if verify and verify.get("problems") and result.recommendation == "fix" and confidence != "low":
        confidence = "low"
        reasons.append("ビルドでの検証で問題: " + "、".join(verify["problems"]))
    if confidence == result.confidence:
        return result
    reason = f"{result.confidence_reason}（{'。'.join(reasons)}）"
    return result.model_copy(update={"confidence": confidence, "confidence_reason": reason})
