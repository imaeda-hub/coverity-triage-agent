"""Run folders: start, resume, progress, summary and batch verification (design 3, 4).

Every operation after ``start_run`` takes the run folder, so the server keeps no hidden
state and a run can be resumed from any session (spec D-15).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from . import config as cfg
from . import metrics, oplog
from .coverity import CoverityClient, make_client
from .grouping import build_work_items
from .models import IssueDetail, TriageResult
from .pathmap import PathMapper
from .report import ItemReport, read_approvals, render_item, render_summary
from .run_state import ItemState, RunStore
from .vcs import GitVcs, Vcs, make_vcs
from .verify import BatchItem, Verifier
from .workspace import ReadOnlyTree

WORKSPACES = ("analyzed", "fix", "annotation")


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


def output_dir_of(repo_root: str) -> str:
    config = cfg.load_project_config(repo_root)
    path = Path(config.output_dir)
    return str((path if path.is_absolute() else Path(repo_root) / path).resolve())


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
            "parallel": run.config.parallel, "verify_mode": run.meta.verify_mode}


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


def report_error(run_dir: str, item_id: str, message: str) -> dict[str, Any]:
    run = Run(run_dir)
    run.store.mark_error(item_id, message)
    run.log("report_error", item=item_id, message=message)
    return {"item": item_id, "status": run.store.status_counts()}


def item_report(run: Run, item_id: str, result: TriageResult,
                 seconds: float | None = None) -> ItemReport:
    item = run.store.item(item_id)
    latest = run.dir / "work" / "latest-revision.txt"
    return ItemReport(
        item_id=item_id, cids=item.cids, details=run.details(item_id), result=result,
        fixes=run.read_json(item_id, ".fixes"), verify=run.read_json(item_id, ".verify"),
        analyzed_revision=run.meta.analyzed_revision,
        latest_revision=latest.read_text(encoding="utf-8").strip() if latest.is_file() else None,
        seconds=item.seconds if seconds is None else seconds,
        run_dir=str(run.dir),
    )


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
            reports.append(item_report(run, item.id, TriageResult.model_validate(run.read_json(item.id, ""))))
        elif item.status == "error":
            errors.append((item.id, item.error))
        else:
            unprocessed.append(item.id)
    summary_path.write_text(render_summary(meta.model_dump(), reports, errors, unprocessed, kept),
                            encoding="utf-8")
    run.log("build_summary", items=len(reports), errors=len(errors), unprocessed=len(unprocessed))
    return {"summary": str(summary_path), "items": len(reports), "errors": len(errors),
            "unprocessed": unprocessed}


def verify_run(run_dir: str, mode: str | None = None) -> dict[str, Any]:
    """Verify all saved fixes of the run at once, after investigation (spec D-72 to D-74)."""
    run = Run(run_dir)
    mode = mode or run.meta.verify_mode
    if mode == "none":
        return {"mode": "none", "message": "自動検証は指定されていません"}
    issues = run.store.issues()
    strip = 1 if isinstance(run.vcs, GitVcs) else 0
    batch = []
    for item in run.store.load().items:
        fix = run.read_json(item.id, ".fixes").get("fix")
        if item.status == "done" and fix:
            batch.append(BatchItem(item.id, fix["patch_path"], fix["files"],
                                   [issues[c] for c in item.cids], strip))
    if not batch:
        return {"mode": mode, "message": "検証する修正案がありません"}
    _, latest_root = run.vcs.latest()
    run.log("verify_run_start", mode=mode, items=[b.item_id for b in batch])
    outcome = Verifier(run.config.verify, run.dir).run_batch(mode, latest_root, batch)

    downgraded = []
    for item_id, verify in outcome.items.items():
        run.write_json(item_id, ".verify", {"fix": verify})
        data = run.read_json(item_id, "")
        if verify.get("problems"):
            original = data.get("confidence_before_verify", data["confidence"])
            data["confidence_before_verify"] = original
            data["confidence"] = "low"
            base_reason = data.get("confidence_reason_before_verify", data["confidence_reason"])
            data["confidence_reason_before_verify"] = base_reason
            data["confidence_reason"] = f"{base_reason}（自動検証で問題: {'、'.join(verify['problems'])}）"
            downgraded.append(item_id)
        run.write_json(item_id, "", data)
        parsed = TriageResult.model_validate(data)
        (run.dir / "cid" / f"{item_id}.md").write_text(render_item(item_report(run, item_id, parsed)),
                                                        encoding="utf-8")
    run.log("verify_run", mode=mode, build_ok=outcome.build_ok, seconds=round(outcome.seconds),
            downgraded=downgraded, error=outcome.error)
    return {"mode": mode, "build_ok": outcome.build_ok, "seconds": round(outcome.seconds),
            "items": len(outcome.items), "downgraded_to_low": downgraded,
            "unassigned_new_issues": len(outcome.unassigned_new_issues),
            "error": outcome.error, "log": outcome.log}


def get_stats(output_dir: str) -> dict[str, Any]:
    if not Path(output_dir).is_dir():
        raise ServiceError(f"出力先フォルダが見つかりません: {output_dir}")
    return metrics.aggregate(output_dir)
