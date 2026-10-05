"""Tools of the worker subagent: one work item from warning path to submitted result."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import config as cfg
from .models import IssueDetail, TriageResult
from .report import render_item
from .runs import Run, ServiceError, item_report
from .workspace import OverlayTree, ReadOnlyTree

EDITABLE = ("fix", "annotation")


def get_issue_detail(run_dir: str, item_id: str) -> dict[str, Any]:
    run = Run(run_dir)
    details = run.details(item_id)
    run.log("get_issue_detail", item=item_id, coverity_requests=run.coverity_requests())
    return {"item": item_id, "details": [d.model_dump(exclude_none=True) for d in details],
            "project_knowledge": cfg.load_knowledge(run.meta.repo_root)}


def _drift_check(root: Path, details: list[IssueDetail]) -> list[str]:
    """Heuristics when investigating local code instead of the analyzed revision (D-16)."""
    findings = []
    tree = ReadOnlyTree(root)
    for d in details:
        files = {d.issue.file} | {e.file for e in d.events}
        for rel in sorted(files):
            try:
                path = tree.locate(rel)
            except Exception:
                continue
            if not path.is_file():
                findings.append(f"CID {d.issue.cid}: ファイルがありません {rel}")
                continue
            text = path.read_bytes()
            count = text.count(b"\n") + 1
            lines = [e.line for e in d.events if e.file == rel and e.line]
            if lines and max(lines) > count:
                findings.append(f"CID {d.issue.cid}: {rel} の行数 {count} が警告の行 {max(lines)} より少ない")
            if rel == d.issue.file and d.issue.function and d.issue.function.encode() not in text:
                findings.append(f"CID {d.issue.cid}: {rel} に関数 {d.issue.function} が見つかりません")
    return findings


def prepare_workspaces(run_dir: str, item_id: str) -> dict[str, Any]:
    run = Run(run_dir)
    run.require_in_progress(item_id)
    analyzed_root = run.vcs.investigation_root(run.meta.analyzed_revision)
    latest, _ = run.vcs.latest()
    result = {
        "item": item_id,
        "workspaces": {"analyzed": "調査用（読み取り専用）", "fix": "修正案用"}
        | ({"annotation": "アノテーション用"} if run.annotation_enabled() else {}),
        "analyzed_revision": run.meta.analyzed_revision or "手元のコード",
        "analyzed_revision_source": run.meta.analyzed_revision_source,
        "latest_revision": latest,
        "deviation_target": run.config.deviation_target,
        "verify_mode": run.meta.verify_mode,
    }
    path_notes = [n for d in run.details(item_id) for n in d.issue.path_notes]
    if path_notes:
        result["path_notes"] = path_notes
    if run.meta.analyzed_revision is None:
        findings = _drift_check(analyzed_root, run.details(item_id))
        result["drift_check"] = findings or ["機械的な確認ではずれは見つかりませんでした（コード内容の確認は必要）"]
    run.log("prepare_workspaces", item=item_id, latest=latest)
    return result


def read_source(run_dir: str, item_id: str, workspace: str, path: str,
                start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
    return Run(run_dir).tree(item_id, workspace).read_lines(path, start_line, end_line)


def search_source(run_dir: str, item_id: str, workspace: str, pattern: str,
                  glob: str | None = None) -> dict[str, Any]:
    return Run(run_dir).tree(item_id, workspace).search(pattern, glob)


def edit_source(run_dir: str, item_id: str, workspace: str, path: str,
                old_text: str, new_text: str) -> dict[str, Any]:
    run = Run(run_dir)
    run.require_in_progress(item_id)
    if workspace not in EDITABLE:
        raise ServiceError("編集できるのは fix / annotation の作業領域だけです")
    tree = run.tree(item_id, workspace)
    assert isinstance(tree, OverlayTree)
    result = tree.edit(path, old_text, new_text, run.config.ascii_file_encoding)
    run.log("edit_source", item=item_id, workspace=workspace, path=result["path"])
    return result


def save_fix(run_dir: str, item_id: str, kind: str, message: str) -> dict[str, Any]:
    run = Run(run_dir)
    item = run.require_in_progress(item_id)
    if kind not in EDITABLE:
        raise ServiceError("kind は fix か annotation です")
    run.tree(item_id, kind)  # validates annotation availability
    saved = run.vcs.save_fix(item_id, kind, message, item.cids)
    fixes = run.read_json(item_id, ".fixes")
    fixes[kind] = saved.__dict__
    run.write_json(item_id, ".fixes", fixes)
    run.log("save_fix", item=item_id, kind=kind, files=saved.files, branch=saved.branch)
    return {"kind": kind, **saved.__dict__}


def submit_result(run_dir: str, item_id: str,
                  result: dict[str, Any] | TriageResult) -> dict[str, Any]:
    run = Run(run_dir)
    item = run.require_in_progress(item_id)
    try:
        parsed = result if isinstance(result, TriageResult) else TriageResult.model_validate(result)
    except ValidationError as exc:
        raise ServiceError(f"提出データが不正です。修正して再提出してください:\n{exc}") from exc
    if parsed.work_item != item_id:
        raise ServiceError(f"work_item が {item_id} と一致しません")
    fixes = run.read_json(item_id, ".fixes")
    if "fix" not in fixes and not parsed.fix.already_fixed_on_latest:
        raise ServiceError("修正案のコードが保存されていません（save_fix kind=fix）。"
                           "最新リビジョンで解消済みの場合は fix.already_fixed_on_latest を true にしてください")
    if run.annotation_enabled() and "annotation" not in fixes:
        raise ServiceError("アノテーションの差分が保存されていません（save_fix kind=annotation）")
    split = []
    if parsed.group_excluded_cids:
        if not item.is_group:
            raise ServiceError("group_excluded_cids はグループの場合だけ指定できます")
        split = run.store.split_group(item_id, parsed.group_excluded_cids)
        (run.results_path(item_id, ".details")).unlink(missing_ok=True)
    run.write_json(item_id, "", parsed.model_dump())
    elapsed = time.time() - item.started_at if item.started_at else None
    report = render_item(item_report(run, item_id, parsed, elapsed))
    (run.dir / "cid" / f"{item_id}.md").write_text(report, encoding="utf-8")
    run.store.mark_done(item_id)
    run.log("submit_result", item=item_id, recommendation=parsed.recommendation,
            confidence=parsed.confidence, split=split)
    return {"item": item_id, "report": str(run.dir / "cid" / f"{item_id}.md"),
            "split_into_new_items": split, "status": run.store.status_counts()}
