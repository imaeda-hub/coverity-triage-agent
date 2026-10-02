"""Operations behind the MCP tools (design 3, 4).

Every operation after ``start_run`` takes the run folder, so the server keeps no hidden
state and a run can be resumed from any session (spec D-15).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import config as cfg
from . import metrics, oplog
from .coverity import CoverityClient, make_client
from .grouping import build_work_items
from .models import IssueDetail, TriageAttributes, TriageResult
from .pathmap import PathMapper
from .report import ItemReport, read_approvals, read_deviation, render_item, render_summary
from .run_state import ItemState, RunStore
from .vcs import GitVcs, SvnVcs, Vcs, make_vcs
from .verify import Verifier
from .workspace import OverlayTree, ReadOnlyTree

TEMPLATES = Path(__file__).parent / "templates"
WORKSPACES = ("analyzed", "fix", "annotation")
EDITABLE = ("fix", "annotation")


class ServiceError(Exception):
    """An error message meant for the agent (and the person) to read."""


class Run:
    """One run folder with its settings, store, VCS and Coverity client."""

    def __init__(self, run_dir: str | Path, client: CoverityClient | None = None):
        self.store = RunStore(run_dir)
        self.dir = self.store.run_dir
        self.meta = self.store.load()
        self.config = cfg.load_project_config(self.meta.repo_root)
        self.vcs: Vcs = make_vcs(self.meta.repo_root, self.config.vcs, self.dir, self.meta.run_id)
        self._client = client

    @property
    def client(self) -> CoverityClient:
        if self._client is None:
            self._client = make_client(self.config.coverity)
        return self._client

    def log(self, event: str, **fields: Any) -> None:
        oplog.log(self.dir, event, self.config.secret_values(), **fields)

    def results_path(self, item_id: str, suffix: str = "") -> Path:
        return self.dir / "results" / f"{item_id}{suffix}.json"

    def read_json(self, item_id: str, suffix: str) -> dict[str, Any]:
        path = self.results_path(item_id, suffix)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def write_json(self, item_id: str, suffix: str, data: Any) -> None:
        self.results_path(item_id, suffix).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def annotation_enabled(self) -> bool:
        return self.config.deviation_target == "coverity+annotation"

    def tree(self, item_id: str, workspace: str):
        if workspace not in WORKSPACES:
            raise ServiceError(f"workspace は {WORKSPACES} のいずれかです")
        if workspace == "annotation" and not self.annotation_enabled():
            raise ServiceError("設定 deviation_target が coverity のため、アノテーション用の作業領域はありません")
        if workspace == "analyzed":
            return ReadOnlyTree(self.vcs.investigation_root(self.meta.analyzed_revision))
        return self.vcs.overlay(item_id, workspace)

    def require_in_progress(self, item_id: str) -> ItemState:
        item = self.store.item(item_id)
        if item.status != "in_progress":
            raise ServiceError(f"{item_id} は処理中ではありません（状態: {item.status}）")
        return item

    def details(self, item_id: str) -> list[IssueDetail]:
        cached = self.read_json(item_id, ".details")
        item = self.store.item(item_id)
        if cached and sorted(cached) == sorted(str(c) for c in item.cids):
            return [IssueDetail.model_validate(cached[str(c)]) for c in item.cids]
        issues = self.store.issues()
        mapper = PathMapper(self.meta.repo_root, self.config.coverity.path_strip_prefixes)
        details = {}
        for c in item.cids:
            detail, notes = mapper.map_detail(self.client.get_issue_detail(issues[c]))
            merged = list(dict.fromkeys(issues[c].path_notes + notes))
            details[str(c)] = detail.model_copy(update={"issue": detail.issue.model_copy(update={"path_notes": merged})})
        self.write_json(item_id, ".details", {k: v.model_dump() for k, v in details.items()})
        return [details[str(c)] for c in item.cids]


# ---- setup and run management -----------------------------------------------------------------


def init_project(repo_root: str, vcs_type: str = "git") -> dict[str, Any]:
    """Create settings and filter templates in the target repository (spec D-61)."""
    if vcs_type not in ("git", "svn"):
        raise ServiceError("vcs_type は git か svn です")
    target = cfg.config_dir(repo_root)
    created, skipped = [], []
    for name in ("config.yaml", "filters/untriaged-high.yaml", "fake-issues.yaml"):
        dest = target / name
        if dest.exists():
            skipped.append(str(dest))
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = (TEMPLATES / name).read_text(encoding="utf-8").replace("{{VCS_TYPE}}", vcs_type)
        dest.write_text(text, encoding="utf-8")
        created.append(str(dest))
    return {"created": created, "skipped_existing": skipped}


def _resolve_analyzed_revision(config: cfg.ProjectConfig, vcs: Vcs, client: CoverityClient,
                               spec: cfg.FilterSpec) -> tuple[str | None, str, str]:
    """Snapshot record, then the filter, then the local code (spec D-17)."""
    note = ""
    if config.coverity.revision_field:
        revision = client.snapshot_revision(spec, config.coverity.revision_field)
        if revision:
            if vcs.has_revision(revision):
                return revision, "snapshot", note
            note = f"スナップショットのリビジョン {revision} がリポジトリに見つかりません。"
    if spec.revision:
        if vcs.has_revision(spec.revision):
            return spec.revision, "filter", note
        note += f"条件ファイルのリビジョン {spec.revision} がリポジトリに見つかりません。"
    return None, "local", note + "手元のコードで調査し、ずれを検出して報告します。"


def start_run(repo_root: str, filter_file: str, overrides: dict[str, Any] | None = None,
              verify_mode: str | None = None, client: CoverityClient | None = None) -> dict[str, Any]:
    config = cfg.load_project_config(repo_root)
    spec = cfg.load_filter(repo_root, filter_file, overrides)
    mode = verify_mode or config.verify.default
    if mode not in ("none", "build", "build+analyze"):
        raise ServiceError("verify_mode は none / build / build+analyze のいずれかです")
    client = client or make_client(config.coverity)

    mapper = PathMapper(repo_root, config.coverity.path_strip_prefixes)
    issues = []
    for found_issue in client.search_issues(spec):
        mapped, notes = mapper.map_issue(found_issue)
        issues.append(mapped.model_copy(update={"path_notes": notes}))
    limit = spec.max_items or config.max_items
    found = len(issues)
    issues = issues[:limit]
    items = build_work_items(issues, cfg.load_no_grouping(repo_root))

    output_dir = Path(output_dir_of(repo_root))
    probe = make_vcs(repo_root, config.vcs, output_dir / "_probe", "probe")
    revision, source, note = _resolve_analyzed_revision(config, probe, client, spec)

    store = RunStore.create(output_dir, repo_root=repo_root, filter_name=spec.name,
                            filter_data=spec.model_dump(), verify_mode=mode, issues=issues,
                            items=items, analyzed_revision=revision,
                            analyzed_revision_source=source)
    shutil.copy2(cfg.config_dir(repo_root) / cfg.CONFIG_FILE_NAME, store.run_dir / "config.snapshot.yaml")
    run = Run(store.run_dir, client)
    run.log("start_run", filter=spec.model_dump(), found=found, limit=limit,
            items=len(items), analyzed_revision=revision, revision_source=source)
    return {
        "run_dir": str(store.run_dir), "found": found, "processing": len(issues),
        "truncated_by_limit": found > limit, "items": len(items),
        "groups": sum(1 for i in items if i.is_group), "verify_mode": mode,
        "parallel": config.parallel, "analyzed_revision": revision,
        "analyzed_revision_source": source, "note": note,
    }


def resume_run(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    reset = run.store.reset_for_resume()
    run.log("resume_run", reset=reset)
    return {"run_dir": str(run.dir), "reset": reset, "status": run.store.status_counts(),
            "parallel": run.config.parallel}


def get_run_status(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    meta = run.store.load()
    return {"run_dir": str(run.dir), "status": run.store.status_counts(),
            "errors": [{"item": i.id, "error": i.error} for i in meta.items if i.status == "error"],
            "summary_exists": (run.dir / "summary.md").is_file()}


def next_work_item(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    item = run.store.claim_next()
    if item is None:
        return {"item": None, "status": run.store.status_counts()}
    issues = run.store.issues()
    run.log("claim", item=item.id)
    return {"item": item.id, "cids": item.cids, "is_group": item.is_group,
            "issues": [issues[c].model_dump(exclude_none=True) for c in item.cids]}


# ---- worker tools ---------------------------------------------------------------------------


def get_issue_detail(run_dir: str, item_id: str) -> dict[str, Any]:
    run = Run(run_dir)
    details = run.details(item_id)
    run.log("get_issue_detail", item=item_id)
    return {"item": item_id, "details": [d.model_dump(exclude_none=True) for d in details]}


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


def verify_fix(run_dir: str, item_id: str, kind: str = "fix", mode: str | None = None) -> dict[str, Any]:
    run = Run(run_dir)
    item = run.require_in_progress(item_id)
    mode = mode or run.meta.verify_mode
    if kind not in run.read_json(item_id, ".fixes"):
        raise ServiceError(f"{kind} の修正が保存されていません。先に save_fix を実行してください")
    issues = run.store.issues()
    result = Verifier(run.config.verify, run.dir).run(
        mode, item_id, kind, run.tree(item_id, kind), [issues[c] for c in item.cids])
    verify = run.read_json(item_id, ".verify")
    verify[kind] = result
    run.write_json(item_id, ".verify", verify)
    run.log("verify_fix", item=item_id, kind=kind, mode=mode, result={k: v for k, v in result.items() if k != "log_tail"})
    return result


def _item_report(run: Run, item_id: str, result: TriageResult,
                 seconds: float | None = None) -> ItemReport:
    item = run.store.item(item_id)
    latest = run.dir / "work" / "latest-revision.txt"
    return ItemReport(
        item_id=item_id, cids=item.cids, details=run.details(item_id), result=result,
        fixes=run.read_json(item_id, ".fixes"), verify=run.read_json(item_id, ".verify"),
        analyzed_revision=run.meta.analyzed_revision,
        latest_revision=latest.read_text(encoding="utf-8").strip() if latest.is_file() else None,
        seconds=item.seconds if seconds is None else seconds,
    )


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
    report = render_item(_item_report(run, item_id, parsed, elapsed))
    (run.dir / "cid" / f"{item_id}.md").write_text(report, encoding="utf-8")
    run.store.mark_done(item_id)
    run.log("submit_result", item=item_id, recommendation=parsed.recommendation,
            confidence=parsed.confidence, split=split)
    return {"item": item_id, "report": str(run.dir / "cid" / f"{item_id}.md"),
            "split_into_new_items": split, "status": run.store.status_counts()}


def report_error(run_dir: str, item_id: str, message: str) -> dict[str, Any]:
    run = Run(run_dir)
    run.store.mark_error(item_id, message)
    run.log("report_error", item=item_id, message=message)
    return {"item": item_id, "status": run.store.status_counts()}


def build_summary(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    meta = run.store.load()
    summary_path = run.dir / "summary.md"
    kept: dict[str, str] = {}
    if summary_path.is_file():
        try:
            kept = read_approvals(summary_path.read_text(encoding="utf-8"))
        except Exception:
            kept = {}
    reports, errors, unprocessed = [], [], []
    for item in meta.items:
        if item.status == "done":
            reports.append(_item_report(run, item.id, TriageResult.model_validate(run.read_json(item.id, ""))))
        elif item.status == "error":
            errors.append((item.id, item.error))
        else:
            unprocessed.append(item.id)
    summary_path.write_text(render_summary(meta.model_dump(), reports, errors, unprocessed, kept),
                            encoding="utf-8")
    run.log("build_summary", items=len(reports), errors=len(errors), unprocessed=len(unprocessed))
    return {"summary": str(summary_path), "items": len(reports), "errors": len(errors),
            "unprocessed": unprocessed}


# ---- apply (after approval) -----------------------------------------------------------------


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


def get_stats(output_dir: str) -> dict[str, Any]:
    if not Path(output_dir).is_dir():
        raise ServiceError(f"出力先フォルダが見つかりません: {output_dir}")
    return metrics.aggregate(output_dir)


def output_dir_of(repo_root: str) -> str:
    config = cfg.load_project_config(repo_root)
    path = Path(config.output_dir)
    return str((path if path.is_absolute() else Path(repo_root) / path).resolve())

