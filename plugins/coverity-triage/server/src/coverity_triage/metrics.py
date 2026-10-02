"""Records for measuring the reduction of human work (spec D-44)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

METRICS_FILE = "metrics.json"


def write_run_metrics(run_dir: str | Path, records: list[dict[str, Any]]) -> None:
    """Merge records of this apply into the run's metrics (one record per work item)."""
    path = Path(run_dir) / METRICS_FILE
    current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    by_id = {r["item_id"]: r for r in current}
    for record in records:
        by_id[record["item_id"]] = record
    path.write_text(json.dumps(list(by_id.values()), ensure_ascii=False, indent=2), encoding="utf-8")


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def aggregate(output_dir: str | Path) -> dict[str, Any]:
    """Aggregate metrics of all runs under the output folder."""
    records: list[dict[str, Any]] = []
    runs = 0
    for path in sorted(Path(output_dir).glob(f"*/{METRICS_FILE}")):
        runs += 1
        records.extend(json.loads(path.read_text(encoding="utf-8")))
    approvals = {"fix": 0, "deviation": 0, "reject": 0}
    by_confidence: dict[str, dict[str, int]] = {}
    matched = edited = deviations = cids = 0
    seconds = [r["seconds"] for r in records if r.get("seconds") is not None]
    for r in records:
        approvals[r["approval"]] += 1
        cids += len(r.get("cids", []))
        conf = by_confidence.setdefault(r["confidence"], {"items": 0, "matched": 0})
        conf["items"] += 1
        if r["approval"] == r["recommendation"]:
            matched += 1
            conf["matched"] += 1
        if r["approval"] == "deviation":
            deviations += 1
            edited += 1 if (r.get("comment_edited") or r.get("attributes_edited")) else 0
    return {
        "runs": runs,
        "items": len(records),
        "cids": cids,
        "approvals": approvals,
        "recommendation_adopted_rate": _rate(matched, len(records)),
        "adopted_rate_by_confidence": {k: _rate(v["matched"], v["items"]) for k, v in by_confidence.items()},
        "deviation_edited_rate": _rate(edited, deviations),
        "average_seconds_per_item": round(sum(seconds) / len(seconds), 1) if seconds else None,
    }
