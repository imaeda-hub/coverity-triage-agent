"""Option ``knowledge_suggestions``: lessons for knowledge.md from what the person changed."""

from __future__ import annotations

from datetime import date
from typing import Any

from . import oplog
from .common import CtError, read_json
from .config import KNOWLEDGE_TEMPLATE, knowledge_path
from .metrics import decisions
from .run import Run

LABELS = {"fix": "修正", "deviation": "逸脱", "reject": "却下"}


def candidates(run_dir: str) -> dict[str, Any]:
    """Items of the last apply where the person did not simply take the AI's proposal."""
    run = Run(run_dir)
    plan = read_json(run.dir / "apply-plan.json", {})
    if not plan:
        raise CtError("反映の内容（apply-plan.json）がありません。/coverity-apply の後に使います")
    issues = run.mapped_issues()
    found = []
    for r in decisions(run, plan):
        changed = r["approval"] != r["recommendation"]
        edited = r.get("comment_edited") or r.get("attributes_edited")
        if not (changed or edited or r["approval"] == "reject"):
            continue
        first = issues.get(r["cids"][0])
        result = read_json(run.result_file(r["item"]))
        entry = {"item": r["item"], "cids": r["cids"], "checker": first.checker if first else "",
                 "file": first.file if first else "", "function": first.function if first else None,
                 "ai_summary": result["verdict"]["summary"], "ai_recommendation": LABELS[r["recommendation"]],
                 "person_decision": LABELS[r["approval"]]}
        if r.get("comment_edited"):
            entry.update(ai_comment=r["comment_before"], person_comment=r["comment_after"])
        found.append(entry)
    path = knowledge_path(run.repo)
    return {"ok": True, "candidates": found, "knowledge_file": str(path),
            "current_knowledge": path.read_text(encoding="utf-8") if path.is_file() else "",
            "next": ("候補から、ほかの警告にも使える短い知識（1 項目 1 行）を考えて人に見せ、"
                     "人が選んだものだけを ct.py add-knowledge で追記する" if found else "候補はありません")}


def add(run_dir: str, entries: list[str]) -> dict[str, Any]:
    """Append the lessons the person chose."""
    run = Run(run_dir)
    lines = [" ".join(e.split()) for e in entries if e.strip()]
    if not lines:
        raise CtError("追記する内容がありません")
    path = knowledge_path(run.repo)
    current = path.read_text(encoding="utf-8") if path.is_file() else KNOWLEDGE_TEMPLATE
    block = f"\n## 追記（{date.today().isoformat()}、実行 {run.state.run_id}）\n\n" + "".join(f"- {line}\n" for line in lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(current.rstrip("\n") + "\n" + block, encoding="utf-8")
    oplog.log(run.dir, "add-knowledge", entries=lines)
    return {"ok": True, "file": str(path), "added": len(lines),
            "next": "内容を確かめてコミットし、チームで共有するよう人に伝える"}
