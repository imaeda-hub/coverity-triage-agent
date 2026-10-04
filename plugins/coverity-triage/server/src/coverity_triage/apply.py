"""Applying approved items: Coverity write-back, pull requests, svn patches (spec D-27 to D-31, D-63)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import config as cfg
from . import metrics
from .models import TriageAttributes, TriageResult
from .report import read_approvals, read_deviation
from .runs import Run, ServiceError
from .vcs import GitVcs, SvnVcs


def _plan(run: Run) -> list[dict[str, Any]]:
    summary = run.dir / "summary.md"
    if not summary.is_file():
        raise ServiceError("一覧サマリがありません。先に build_summary を実行してください")
    approvals = read_approvals(summary.read_text(encoding="utf-8"))
    plan = []
    for item in run.store.load().items:
        if item.id not in approvals or item.applied or item.status != "done":
            continue
        action = approvals[item.id]
        fixes = run.read_json(item.id, ".fixes")
        entry: dict[str, Any] = {"item": item.id, "cids": item.cids, "action": action}
        if action == "fix" and "fix" not in fixes:
            raise ServiceError(f"{item.id}: 修正のコードが保存されていないため「修正」は反映できません")
        if action == "deviation":
            attrs, comment = read_deviation((run.dir / "cid" / f"{item.id}.md").read_text(encoding="utf-8"))
            entry.update(attributes=attrs.model_dump(), comment=comment,
                         annotation=run.annotation_enabled() and "annotation" in fixes)
        plan.append(entry)
    return plan


def _token(plan: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def preview_apply(run_dir: str) -> dict[str, Any]:
    """Counts to show the person before applying (spec D-63)."""
    run = Run(run_dir)
    plan = _plan(run)
    counts = {"fix": 0, "deviation": 0, "reject": 0}
    for entry in plan:
        counts[entry["action"]] += 1
    token = _token(plan)
    run.log("preview_apply", counts=counts, token=token)
    return {"counts": {"修正": counts["fix"], "逸脱": counts["deviation"], "却下": counts["reject"]},
            "items": [{"item": e["item"], "action": e["action"], "cids": e["cids"]} for e in plan],
            "confirmation_token": token,
            "message": (f"修正 {counts['fix']} 件 / 逸脱 {counts['deviation']} 件 / 却下 {counts['reject']} 件を反映します。"
                        "よろしいですか？（同意を得てから apply_approvals に confirmation_token を渡してください）")}


def _pr_text(run: Run, item_id: str, kind: str) -> tuple[str, str]:
    result = TriageResult.model_validate(run.read_json(item_id, ""))
    cids = run.store.item(item_id).cids
    label = "修正" if kind == "fix" else "逸脱アノテーション"
    title = f"Coverity CID {', '.join(map(str, cids[:5]))}{' ほか' if len(cids) > 5 else ''}: {label}"
    body = (f"## 見立て\n{result.verdict.summary}\n\n{result.verdict.rationale}\n\n"
            f"## {label}の内容\n{result.fix.summary if kind == 'fix' else result.deviation.comment}\n\n"
            f"## 影響範囲とリスク\n{result.fix.impact}\n\n"
            f"確信度: {result.confidence}（Coverity トリアージエージェントが作成し、人間が承認）\n")
    return title, body


def apply_approvals(run_dir: str, confirmation_token: str) -> dict[str, Any]:
    run = Run(run_dir)
    plan = _plan(run)
    if _token(plan) != confirmation_token:
        raise ServiceError("確認後に一覧サマリまたはレポートが変更されました。preview_apply からやり直してください")
    results, metric_records, per_run_commits = [], [], []
    for entry in plan:
        item_id, action = entry["item"], entry["action"]
        fixes = run.read_json(item_id, ".fixes")
        outcome: dict[str, Any] = {"item": item_id, "action": action}
        try:
            kinds = ["fix"] if action == "fix" else (["annotation"] if entry.get("annotation") else [])
            done_before = run.store.item(item_id).apply_result
            if action == "deviation" and done_before.get("coverity") == "登録済み":
                outcome["coverity"] = "登録済み（前回の反映で登録済みのため省略）"
            elif action == "deviation":
                spec = cfg.FilterSpec.model_validate(run.meta.filter)
                run.client.write_triage(entry["cids"], TriageAttributes.model_validate(entry["attributes"]),
                                        entry["comment"], spec)
                outcome["coverity"] = "登録済み"
                run.store.record_apply_progress(item_id, {"coverity": "登録済み"})
            for kind in kinds:
                outcome[kind] = _apply_code(run, item_id, kind, fixes[kind], per_run_commits)
            if action == "reject" and run.store.item(item_id).is_group:
                cfg.add_no_grouping(run.meta.repo_root, entry["cids"])
                outcome["no_grouping"] = "次回は個別に処理します"
            if not (isinstance(run.vcs, GitVcs) and run.config.vcs.branch_mode == "per_run" and kinds):
                run.store.mark_applied(item_id, outcome)
            outcome["ok"] = True
        except Exception as exc:  # keep going with the other items
            outcome.update(ok=False, error=str(exc))
        results.append(outcome)
        metric_records.append(_metric(run, item_id, entry))

    if per_run_commits:
        results.append(_apply_per_run(run, per_run_commits))

    metrics.write_run_metrics(run.dir, metric_records)
    run.log("apply_approvals", results=results)
    return {"results": results}


def _apply_code(run: Run, item_id: str, kind: str, fix: dict[str, Any],
                per_run_commits: list[tuple[str, str, str]]) -> dict[str, Any]:
    if isinstance(run.vcs, SvnVcs):
        run.vcs.apply_patch(fix["patch_path"])
        return {"svn_patch": "ワーキングコピーに適用しました（コミットは人間が行います）"}
    assert isinstance(run.vcs, GitVcs)
    if run.config.vcs.branch_mode == "per_run":
        per_run_commits.append((item_id, kind, fix["commit"]))
        return {"queued": "実行ごとのブランチにまとめます"}
    run.vcs.push(fix["branch"])
    title, body = _pr_text(run, item_id, kind)
    return {"branch": fix["branch"], "pull_request": run.vcs.create_pull_request(fix["branch"], title, body)}


def _apply_per_run(run: Run, commits: list[tuple[str, str, str]]) -> dict[str, Any]:
    assert isinstance(run.vcs, GitVcs)
    keyed = [(f"{item}:{kind}", commit) for item, kind, commit in commits]
    try:
        branch, included, conflicted = run.vcs.compose_run_branch(keyed)
        if not included:
            return {"run_branch": None, "ok": False, "conflicted": conflicted}
        run.vcs.push(branch)
        items = sorted({k.split(":")[0] for k in included})
        body = "\n".join(f"- {i}: {_pr_text(run, i, 'fix')[0]}" for i in items)
        url = run.vcs.create_pull_request(branch, f"Coverity トリアージ {run.meta.run_id}", body)
        for item in items:
            run.store.mark_applied(item, {"run_branch": branch, "pull_request": url})
        return {"run_branch": branch, "pull_request": url, "included": included,
                "conflicted": conflicted, "ok": not conflicted}
    except Exception as exc:
        return {"run_branch": None, "ok": False, "error": str(exc)}


def _metric(run: Run, item_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    result = TriageResult.model_validate(run.read_json(item_id, ""))
    record = {"item_id": item_id, "cids": entry["cids"], "recommendation": result.recommendation,
              "confidence": result.confidence, "approval": entry["action"],
              "seconds": run.store.item(item_id).seconds}
    if entry["action"] == "deviation":
        before_attrs = {k: getattr(result.deviation, k) for k in ("classification", "action", "severity")}
        record.update(comment_edited=entry["comment"].strip() != result.deviation.comment.strip(),
                      attributes_edited=entry["attributes"] != before_attrs,
                      comment_before=result.deviation.comment, comment_after=entry["comment"])
    return record
