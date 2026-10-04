"""Project knowledge drawn from the person's changes (spec D-78)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from . import config as cfg
from . import metrics
from .models import TriageResult
from .runs import Run

LABELS = {"fix": "修正", "deviation": "逸脱", "reject": "却下"}


def knowledge_candidates(run_dir: str) -> dict[str, Any]:
    """Items of an applied run where the person did not simply take the AI's proposal.

    The agent turns them into short, general lessons for ``knowledge.md`` and asks the
    person which ones to add.
    """
    run = Run(run_dir)
    path = run.dir / metrics.METRICS_FILE
    records = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    issues = run.store.issues()
    candidates = []
    for r in records:
        changed = r["approval"] != r["recommendation"]
        edited = bool(r.get("comment_edited") or r.get("attributes_edited"))
        if not (changed or edited or r["approval"] == "reject"):
            continue
        result = TriageResult.model_validate(run.read_json(r["item_id"], ""))
        first = issues.get(r["cids"][0]) if r["cids"] else None
        entry: dict[str, Any] = {
            "item": r["item_id"], "cids": r["cids"],
            "checker": first.checker if first else "", "file": first.file if first else "",
            "function": first.function if first else None,
            "ai_judgement": result.verdict.judgement, "ai_summary": result.verdict.summary,
            "ai_recommendation": LABELS[r["recommendation"]], "human_decision": LABELS[r["approval"]],
        }
        if r.get("comment_edited"):
            entry.update(ai_comment=r.get("comment_before", ""), human_comment=r.get("comment_after", ""))
        if r.get("attributes_edited"):
            entry["ai_attributes"] = {k: getattr(result.deviation, k) for k in ("classification", "action", "severity")}
        candidates.append(entry)
    return {"candidates": candidates, "current_knowledge": cfg.load_knowledge(run.meta.repo_root),
            "note": "候補が無ければ何もしない。追記は人が選んだものだけ add_knowledge で行う"}


def add_knowledge(run_dir: str, entries: list[str]) -> dict[str, Any]:
    """Append lessons the person approved to ``knowledge.md`` (spec D-78)."""
    run = Run(run_dir)
    path = cfg.append_knowledge(run.meta.repo_root, entries,
                                f"{date.today().isoformat()}、実行 {run.meta.run_id}")
    run.log("add_knowledge", entries=entries)
    return {"file": str(path), "added": len([e for e in entries if e.strip()]),
            "next": "内容を確認してコミットし、チームで共有してください"}
