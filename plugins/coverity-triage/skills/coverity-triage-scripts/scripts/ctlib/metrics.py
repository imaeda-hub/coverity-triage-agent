"""Option ``metrics``: how often the AI's recommendation is taken, edits, and time per item."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import read_json, write_json
from .models import TriageResult

METRICS_FILE = "metrics.json"


def decisions(run: Any, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """One record per item of an apply plan: the AI's proposal next to the person's decision."""
    records: dict[str, dict[str, Any]] = {}
    entries = ([("deviation", e) for e in plan["triage"]] + [("fix", e) for e in plan["code"] if e["kind"] == "fix"]
               + [("reject", e) for e in plan["reject"]])
    for approval, entry in entries:
        result = TriageResult.model_validate(read_json(run.result_file(entry["id"])))
        record = {"item": entry["id"], "cids": entry["cids"], "recommendation": result.recommendation,
                  "confidence": result.confidence, "approval": approval,
                  "seconds": run.state.item(entry["id"]).seconds}
        if approval == "deviation":
            proposed = result.deviation
            record["comment_edited"] = entry["comment"].strip() != proposed.comment.strip()
            record["attributes_edited"] = any(entry[k] != getattr(proposed, k)
                                              for k in ("classification", "action", "severity"))
            record["comment_before"], record["comment_after"] = proposed.comment, entry["comment"]
        records[entry["id"]] = record
    return list(records.values())


def record(run: Any, plan: dict[str, Any]) -> None:
    path = run.dir / METRICS_FILE
    current = {r["item"]: r for r in read_json(path, [])} if path.is_file() else {}
    for r in decisions(run, plan):
        current[r["item"]] = r
    write_json(path, list(current.values()))


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def aggregate(output: Path) -> dict[str, Any]:
    """Totals over every run in the output folder."""
    records: list[dict[str, Any]] = []
    runs = 0
    for path in sorted(output.glob(f"*/{METRICS_FILE}")):
        runs += 1
        records += read_json(path)
    approvals = {"fix": 0, "deviation": 0, "reject": 0}
    by_confidence: dict[str, list[int]] = {}
    taken = edited = deviations = 0
    for r in records:
        approvals[r["approval"]] += 1
        stats = by_confidence.setdefault(r["confidence"], [0, 0])
        stats[0] += 1
        if r["approval"] == r["recommendation"]:
            taken += 1
            stats[1] += 1
        if r["approval"] == "deviation":
            deviations += 1
            edited += bool(r.get("comment_edited") or r.get("attributes_edited"))
    seconds = [r["seconds"] for r in records if r.get("seconds") is not None]
    return {"runs": runs, "items": len(records), "cids": sum(len(r["cids"]) for r in records),
            "approvals": approvals, "recommendation_taken_rate": _rate(taken, len(records)),
            "taken_rate_by_confidence": {k: _rate(v[1], v[0]) for k, v in by_confidence.items()},
            "deviation_edited_rate": _rate(edited, deviations),
            "average_seconds_per_item": round(sum(seconds) / len(seconds), 1) if seconds else None}
