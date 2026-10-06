"""Commands of /coverity-run: new-run, plan, next, brief, finish, fail, resume, status, runs, summary."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import oplog
from .common import CtError, file_lock, read_json, write_json, write_text
from .config import (CONFIG_DIR, CONFIG_FILE, FilterSpec, knowledge_path, load_config, load_filter,
                     load_no_grouping, output_dir)
from .grouping import build_work_items
from .models import Event, Issue, IssueDetail, TriageResult
from .pathmap import PathMapper
from .report import ItemReport, read_approvals, render_item, render_summary
from .run import ItemState, Run, create_run, finish_item, list_runs
from .vcs import detect

VCS_LOCK_SECONDS = 1800  # creating a work copy of a large repository can take long


def _repo(path: str) -> Path:
    repo = Path(path).resolve()
    if not repo.is_dir():
        raise CtError(f"フォルダがありません: {repo}")
    if detect(repo) is None:
        raise CtError(f"git / svn のリポジトリの一番上のフォルダを指定してください: {repo}")
    return repo


def _log(run: Run, event: str, **fields: Any) -> None:
    oplog.log(run.dir, event, oplog.secrets_of(run.config), **fields)


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


# ---- new-run ---------------------------------------------------------------------------------


def new_run(repo_root: str, filter_name: str | None, overrides: dict[str, Any], limit: int | None,
            verify: str | None) -> dict[str, Any]:
    repo = _repo(repo_root)
    config = load_config(repo)
    if filter_name is None:
        names = sorted(p.name for p in (repo / CONFIG_DIR / "filters").glob("*.yaml"))
        if not names:
            raise CtError("条件ファイル（.coverity-triage/filters/*.yaml）がありません（/coverity-setup で作ります）")
        filter_name = "untriaged.yaml" if "untriaged.yaml" in names else names[0]
    spec = load_filter(repo, filter_name, overrides)
    if len(spec.streams) != 1:
        raise CtError("条件にはストリームを 1 つだけ指定してください", streams=spec.streams)
    requested = limit or spec.limit or config.max_items
    run_limit = min(requested, config.max_items)
    mode = verify or config.verify.default
    if mode not in ("none", "build", "build+analyze"):
        raise CtError("検証の方法は none / build / build+analyze のどれかです")
    if mode != "none" and not config.verify.build_command:
        raise CtError("ビルドでの検証の設定がありません（/coverity-setup の「ビルドでの検証」で設定します）")
    output = output_dir(repo, config)
    if _inside(output, repo):
        raise CtError(f"出力先がリポジトリの中です: {output}（設定 output_dir をリポジトリの外にしてください）")

    run = create_run(output, repo=repo, filter_name=spec.name, stream=spec.streams[0], limit=run_limit,
                     verify_mode=mode)
    write_json(run.dir / "filter.json", spec.model_copy(update={"limit": run_limit}).model_dump())
    shutil.copyfile(repo / CONFIG_DIR / CONFIG_FILE, run.dir / "config.snapshot.yaml")
    _log(run, "new-run", filter=spec.model_dump(), limit=run_limit, verify=mode)
    out = {"ok": True, "run_dir": str(run.dir), "repo_root": str(repo), "stream": spec.streams[0],
           "filter_file": str(run.dir / "filter.json"), "issues_file": str(run.dir / "issues.json"),
           "work_dir": str(run.dir / "work"), "limit": run_limit, "verify": mode, "filter": spec.model_dump(),
           "next": "MCP の search_issues（repo_root, filter_file, output_file=issues_file）を done になるまで呼び、"
                   "そのあと ct.py plan"}
    if requested > run_limit:
        out["note"] = f"1 回の上限（設定 max_items）が {config.max_items} 件のため、{run_limit} 件にしました"
    return out


# ---- plan ------------------------------------------------------------------------------------


def plan(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    if any(i.status != "pending" for i in run.state.items):
        raise CtError("この実行はもう始まっています（続きは ct.py next、やり直しは ct.py resume）")
    found = read_json(run.dir / "issues.json")
    if not found.get("finished_at"):
        raise CtError("警告の検索が終わっていません。MCP の search_issues を done になるまで呼んでください")
    config = run.config
    mapper = PathMapper(run.repo, config.coverity.path_strip_prefixes)
    mapped, issues = [], []
    for record in found.get("issues") or []:
        path_notes: list[str] = []
        issue = mapper.map_issue(Issue.model_validate(record), path_notes)
        issues.append(issue)
        mapped.append({**issue.model_dump(exclude_none=True), "path_notes": path_notes})
    write_json(run.dir / "issues.mapped.json", mapped)
    items = build_work_items(issues, load_no_grouping(run.repo))

    vcs = run.vcs
    notes = []
    analyzed = found.get("analyzed_revision")
    if found.get("revision_error"):
        notes.append(f"解析リビジョンを Coverity から取れませんでした: {found['revision_error']}")
    if analyzed and not vcs.has_revision(analyzed):
        notes.append(f"Coverity が解析したリビジョン {analyzed} がリポジトリにありません。")
        analyzed = None
    if not analyzed:
        notes.append("解析したリビジョンが分からないため、手元のコードで調べ、警告とのずれを確かめます。")
    latest, latest_note = vcs.resolve_latest()
    if latest_note:
        notes.append(latest_note)

    with run.update() as state:
        state.items = [ItemState(id=i.id, cids=i.cids) for i in items]
        state.planned = True
        state.found = len(issues)
        state.limited = bool(found.get("limited"))
        state.latest_revision = latest
        state.analyzed_revision = analyzed
        state.revision_notes = notes
    groups = [{"id": i.id, "cids": i.cids} for i in items if i.id.startswith("G")]
    _log(run, "plan", issues=len(issues), items=len(items), groups=groups, latest=latest, analyzed=analyzed)
    return {"ok": True, "issues": len(issues), "limited": state.limited, "items": len(items),
            "groups": groups, "latest_revision": latest, "analyzed_revision": analyzed, "notes": notes,
            "parallel": config.parallel,
            "next": "ct.py next で作業を 1 つずつ取り出す" if items else "対象の警告がありません"}


# ---- next ------------------------------------------------------------------------------------


def next_item(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    if not run.state.planned:
        raise CtError("作業の一覧がありません。先に ct.py plan を実行してください")
    config = run.config
    vcs = run.vcs
    fingerprint = vcs.fingerprint()  # to notice if the person's files change while the item is worked on
    with run.update() as state:
        busy = {i.slot for i in state.items if i.status == "in_progress"}
        free = [s for s in range(1, config.parallel + 1) if s not in busy]
        item = next((i for i in state.items if i.status == "pending"), None)
        if item is not None and free:
            item.status, item.slot = "in_progress", free[0]
            item.attempts += 1
            item.started = time.time()
            item.error, item.warnings, item.repo_fingerprint = "", [], fingerprint
    if item is None:
        counts = run.state.counts()
        waiting = counts["in_progress"]
        return {"ok": True, "item": None, "counts": counts,
                "next": (f"処理中の作業が {waiting} 件あります。終わるのを待ってから ct.py summary" if waiting
                         else "残りの作業はありません。ct.py summary で一覧を作る"
                         + ("（処理できなかった作業は ct.py resume で戻してやり直せます）" if counts["error"] else ""))}
    if not free:
        return {"ok": True, "item": None, "counts": run.state.counts(),
                "next": f"同時に進められる数（設定 parallel = {config.parallel}）に達しています。"
                        "処理中の作業を ct.py finish か ct.py fail で終えてから呼んでください"}

    latest = run.state.latest_revision or ""
    try:
        with file_lock(run.dir / "work" / "vcs", timeout=VCS_LOCK_SECONDS):
            vcs.reset_copy(run.copy_dir("fix", item.slot), latest)
            if config.options.annotation:
                vcs.reset_copy(run.copy_dir("annotation", item.slot), latest)
            if run.state.analyzed_revision:
                vcs.reset_copy(run.copy_dir("analyzed"), run.state.analyzed_revision)
    except CtError as exc:
        with run.update() as state:
            finish_item(state.item(item.id), "error", f"修正用のコピーを用意できませんでした: {exc}")
        raise CtError(f"修正用のコピーを用意できませんでした: {exc}",
                      next="ほかの作業でも同じことが起きるため、止めて利用者に伝える（直したら ct.py resume で再開できる）") from exc
    folder = run.item_dir(item.id)
    folder.mkdir(parents=True, exist_ok=True)
    run.result_file(item.id).unlink(missing_ok=True)
    _log(run, "next", item=item.id, slot=item.slot, attempt=item.attempts)
    return {"ok": True, "item": item.id, "cids": item.cids, "is_group": item.is_group,
            "repo_root": str(run.repo), "stream": run.state.stream, "item_dir": str(folder),
            "counts": run.state.counts(),
            "next": "MCP の get_issues（repo_root, stream, cids, output_dir=item_dir）を done になるまで呼び、"
                    "そのあと ct.py brief"}


# ---- brief -----------------------------------------------------------------------------------


def _details(run: Run, item: ItemState) -> dict[int, IssueDetail]:
    folder = run.item_dir(item.id)
    missing = [c for c in item.cids if not (folder / f"issue-{c}.json").is_file()]
    if missing:
        raise CtError("警告経路のファイルがありません。MCP の get_issues を呼んでください", missing_cids=missing,
                      output_dir=str(folder))
    return {c: IssueDetail.model_validate(read_json(folder / f"issue-{c}.json")) for c in item.cids}


def _mapped(run: Run) -> tuple[dict[int, Issue], dict[int, list[str]]]:
    data = read_json(run.dir / "issues.mapped.json")
    return ({d["cid"]: Issue.model_validate(d) for d in data}, {d["cid"]: d.get("path_notes") or [] for d in data})


def _item_view(run: Run, item: ItemState) -> tuple[list[Issue], dict[int, list[Event]], str, list[str]]:
    """Issues and warning paths with repository-relative paths, and notes about the paths."""
    issues, path_notes = _mapped(run)
    details = _details(run, item)
    mapper = PathMapper(run.repo, run.config.coverity.path_strip_prefixes)
    notes: list[str] = []
    events: dict[int, list[Event]] = {}
    description = ""
    for cid in item.cids:
        notes += [n for n in path_notes.get(cid, []) if n not in notes]
        detail = details[cid]
        if detail.error:
            notes.append(f"CID {cid} の警告経路を Coverity から取れませんでした: {detail.error}")
        events[cid] = [mapper.map_event(e, notes) for e in detail.events]
        description = description or detail.checker_description
    return [issues[c] for c in item.cids], events, description, notes


def _drift_check(root: Path, issues: list[Issue], events: dict[int, list[Event]]) -> list[str]:
    """Signs that the local code differs from the analyzed code."""
    findings = []
    for issue in issues:
        files = {issue.file} | {e.file for e in events.get(issue.cid, [])}
        for rel in sorted(files):
            path = root / rel
            if not path.is_file():
                findings.append(f"CID {issue.cid}: ファイルがありません: {rel}")
                continue
            data = path.read_bytes()
            count = data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
            lines = [e.line for e in events.get(issue.cid, []) if e.file == rel and e.line]
            if lines and max(lines) > count:
                findings.append(f"CID {issue.cid}: {rel} は {count} 行しかなく、警告の {max(lines)} 行目がありません")
            if rel == issue.file and issue.function and issue.function.split("::")[-1].encode() not in data:
                findings.append(f"CID {issue.cid}: {rel} に関数 {issue.function} が見つかりません")
    return findings


def brief(run_dir: str, item_id: str) -> dict[str, Any]:
    run = Run(run_dir)
    item = run.state.item(item_id)
    if item.status != "in_progress":
        raise CtError(f"{item_id} は処理中ではありません（状態: {item.status}）")
    issues, events, description, notes = _item_view(run, item)
    config = run.config
    analyzed = run.state.analyzed_revision
    investigate = run.copy_dir("analyzed") if analyzed else run.repo
    drift = [] if analyzed else _drift_check(run.repo, issues, events)
    knowledge = knowledge_path(run.repo)
    result = run.result_file(item_id)
    fix_dir = run.copy_dir("fix", item.slot)

    lines = [f"# 調査の指示: {item_id}", "",
             "## すること", "",
             f"Coverity の警告 {len(issues)} 件（CID {', '.join(map(str, item.cids))}）を調べ、"
             "**修正案と逸脱コメント案の両方**を作り、結果ファイルを書きます。",
             "最初にスキル `triage-investigation` を読み、その手順に沿って進めます。"
             "チェッカー別の観点は `checker-knowledge`、修正の決まりは `code-fix`、逸脱コメントの書き方は "
             "`deviation-comment`、結果ファイルの書き方は `triage-report` にあります。", "",
             "## フォルダとファイル", "",
             "| 用途 | 場所 |", "|---|---|",
             f"| 調べるコード（読むだけ。変更しない） | `{investigate}` |",
             f"| 修正するコード（ここだけを変更する） | `{fix_dir}` |"]
    if config.options.annotation:
        lines.append(f"| アノテーションを入れるコード（逸脱案の注釈だけを入れる） | `{run.copy_dir('annotation', item.slot)}` |")
    lines.append(f"| 結果ファイル（書く） | `{result}` |")
    if knowledge.is_file():
        lines.append(f"| プロジェクトの知識（読む） | `{knowledge}` |")
    lines += ["", "- 下の警告のパスは、どのフォルダでもリポジトリの一番上からの相対パスです。",
              (f"- 調べるコードは、Coverity が解析したリビジョン {analyzed} です。" if analyzed else
               "- Coverity が解析したリビジョンが分からないため、調べるコードは利用者の手元のコードです。"
               "警告の行と内容が合っているか（ずれ）を確かめ、結果の revision_drift に書きます。"
               "このフォルダは利用者の作業中のファイルなので、絶対に変更しないでください。"),
              f"- 修正するコードは、取り込み先の最新のリビジョン {run.state.latest_revision} です。"
              "最新のリビジョンで既に直っていれば、修正はせずに fix.already_fixed_on_latest を true にします。",
              "- 上の表にないフォルダのファイルは変更しません。ターミナルは使いません。", ""]
    lines += ["## 警告", ""]
    for issue in issues:
        lines += [f"### CID {issue.cid} — {issue.checker}（Impact {issue.impact or '-'}）", "",
                  f"- 場所: `{issue.file}`:{issue.line or '-'}" + (f" {issue.function}()" if issue.function else ""),
                  f"- CWE: {issue.cwe or '-'}", "- 警告経路:", ""]
        evs = events.get(issue.cid) or []
        lines += [f"  {n}. {'★ ' if e.main else ''}`{e.file}`:{e.line or '-'} {e.tag} — {e.description}"
                  for n, e in enumerate(evs, 1)] or ["  （取れませんでした）"]
        lines.append("")
    if description:
        lines += ["チェッカーの説明（Coverity）:", "", description, ""]
    if drift:
        lines += ["## 機械的に見つけたずれ", "", *[f"- {d}" for d in drift], ""]
    if notes:
        lines += ["## パスなどの注意", "", *[f"- {n}" for n in notes], ""]
    if item.is_group:
        lines += ["## グループ", "",
                  "同じ原因と思われる警告をまとめた作業です。すべてが同じ原因かを確かめ、原因が違う CID は "
                  "group_excluded_cids に入れます（その CID は個別の作業に戻ります）。修正と逸脱コメントは、"
                  "グループに残す CID に対して作ります。", ""]
    lines += ["## 終わったら", "",
              f"結果ファイル `{result}` を書いたら、呼び出し元に 1 行だけ返します。"
              f"例: 「{item_id}: 修正を推奨（確信度 高）— fgets が失敗したとき fp を閉じていない」", ""]
    path = run.item_dir(item_id) / "brief.md"
    write_text(path, "\n".join(lines))
    _log(run, "brief", item=item_id, drift=len(drift))
    return {"ok": True, "brief": str(path), "result_file": str(result),
            "prompt": f"Coverity の警告を調べます。指示ファイル {path} を読み、書かれた手順に従ってください。",
            "next": "サブエージェント coverity-triage-worker を起動し、prompt をそのまま渡す。終わったら ct.py finish"}


# ---- finish ----------------------------------------------------------------------------------


def _read_result(run: Run, item: ItemState) -> TriageResult:
    path = run.result_file(item.id)
    if not path.is_file():
        raise CtError("結果ファイルがありません", problems=[f"{path} を書いてください（書き方はスキル triage-report）"])
    try:
        result = TriageResult.model_validate(read_json(path))
    except (CtError, ValidationError) as exc:
        problems = ([f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
                    if isinstance(exc, ValidationError) else [str(exc)])
        raise CtError("結果ファイルに直すところがあります", problems=problems) from exc
    problems = []
    if result.item != item.id:
        problems.append(f"item は {item.id} にしてください（今は {result.item}）")
    excluded = set(result.group_excluded_cids)
    if excluded and not item.is_group:
        problems.append("group_excluded_cids はグループのときだけ書きます")
    elif excluded - set(item.cids):
        problems.append(f"group_excluded_cids にグループ外の CID があります: {sorted(excluded - set(item.cids))}")
    elif excluded and not set(item.cids) - excluded:
        problems.append("グループのすべての CID を外すことはできません（1 つは残します）")
    if problems:
        raise CtError("結果ファイルに直すところがあります", problems=problems)
    return result


def _copy_changed(src: Path, files: list[str], dest: Path) -> None:
    for rel in files:
        if (src / rel).is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dest / rel)


def finish(run_dir: str, item_id: str) -> dict[str, Any]:
    run = Run(run_dir)
    item = run.state.item(item_id)
    if item.status != "in_progress":
        raise CtError(f"{item_id} は処理中ではありません（状態: {item.status}）")
    result = _read_result(run, item)
    config, vcs = run.config, run.vcs
    fix_dir = run.copy_dir("fix", item.slot)
    changed = vcs.changed_files(fix_dir)
    warnings = []
    if not changed and not result.fix.already_fixed_on_latest:
        raise CtError("修正するコードに変更がありません", problems=[
            f"{fix_dir} のファイルを修正してください。最新のリビジョンで既に直っている場合は "
            "fix.already_fixed_on_latest を true にしてください"])
    if changed and result.fix.already_fixed_on_latest:
        warnings.append("「最新のリビジョンで直っている」とありますが、修正するコードに変更があります（変更を差分にしました）")
    annotation_dir = run.copy_dir("annotation", item.slot)
    annotated = vcs.changed_files(annotation_dir) if config.options.annotation else []
    if config.options.annotation and not annotated:
        raise CtError("アノテーションが入っていません", problems=[f"{annotation_dir} に注釈を入れてください"])
    if run.state.analyzed_revision and vcs.changed_files(run.copy_dir("analyzed")):
        warnings.append("調べるコード（解析リビジョン）が変更されていました。次の作業の前に元に戻します")
    if item.repo_fingerprint and vcs.fingerprint() != item.repo_fingerprint:
        warnings.append("調べている間に、利用者のリポジトリのファイルが変わりました。"
                        "サブエージェントが誤って変更していないか確かめてください")

    split = []
    with run.update() as state:
        current = state.item(item_id)
        if result.group_excluded_cids:
            current.cids = [c for c in current.cids if c not in result.group_excluded_cids]
            for cid in result.group_excluded_cids:
                state.items.append(ItemState(id=str(cid), cids=[cid], split_from=item_id))
                split.append(str(cid))
    for cid in result.group_excluded_cids:
        target = run.item_dir(str(cid))
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(run.item_dir(item_id) / f"issue-{cid}.json", target / f"issue-{cid}.json")
    item = run.state.item(item_id)

    saved: dict[str, Any] = {}
    run_id = run.state.run_id
    single = f"cid-{item.cids[0]}" if not item.is_group else f"{run_id}-{item_id}"
    for kind, folder, files in (("fix", fix_dir, changed), ("annotation", annotation_dir, annotated)):
        if not files:
            continue
        label = "修正" if kind == "fix" else "逸脱の注釈"
        summary = result.fix.summary if kind == "fix" else result.deviation.classification
        message = f"Coverity CID {', '.join(map(str, item.cids))}: {label}\n\n{summary}\n"
        branch = None if config.options.per_run_branch else (
            config.vcs.branch_prefix + single + ("-annotation" if kind == "annotation" else ""))
        with file_lock(run.dir / "work" / "vcs", timeout=VCS_LOCK_SECONDS):
            fix = vcs.save(folder, run.state.latest_revision or "", message, branch,
                           f"refs/coverity-triage/{run_id}/{item_id}-{kind}", run.dir / "patches" / f"{item_id}-{kind}.patch")
        _copy_changed(folder, files, run.dir / "fixed" / item_id / kind)
        saved[kind] = fix.__dict__
    write_json(run.item_dir(item_id) / "saved.json", saved)

    with run.update() as state:
        current = state.item(item_id)
        current.warnings = warnings
        finish_item(current, "done")
        seconds = current.seconds
    write_text(run.report_file(item_id), render_item(_report(run, item_id, result, saved, seconds)))
    _log(run, "finish", item=item_id, recommendation=result.recommendation, confidence=result.confidence,
         files=changed, split=split, warnings=warnings)
    return {"ok": True, "item": item_id, "recommendation": result.recommendation, "confidence": result.confidence,
            "report": str(run.report_file(item_id)), "split_into": split, "warnings": warnings,
            "counts": run.state.counts(), "next": "ct.py next"}


def _report(run: Run, item_id: str, result: TriageResult, saved: dict[str, Any], seconds: float | None,
            verify: dict[str, Any] | None = None) -> ItemReport:
    item = run.state.item(item_id)
    issues, events, description, notes = _item_view(run, item)
    return ItemReport(item_id=item_id, issues=issues, events=events, checker_description=description,
                      result=result, saved=saved, verify=verify or {}, analyzed_revision=run.state.analyzed_revision,
                      latest_revision=run.state.latest_revision, seconds=seconds,
                      notes=notes + item.warnings, run_dir=run.dir)


def rerender(run: Run, item_id: str, verify: dict[str, Any] | None = None) -> None:
    """Write the detailed report again (after the build verification changed the result)."""
    result = TriageResult.model_validate(read_json(run.result_file(item_id)))
    saved = read_json(run.item_dir(item_id) / "saved.json", {})
    write_text(run.report_file(item_id),
               render_item(_report(run, item_id, result, saved, run.state.item(item_id).seconds, verify)))


# ---- fail / resume / status / runs -----------------------------------------------------------


def fail(run_dir: str, item_id: str, reason: str) -> dict[str, Any]:
    run = Run(run_dir)
    with run.update() as state:
        item = state.item(item_id)
        if item.status != "in_progress":
            raise CtError(f"{item_id} は処理中ではありません（状態: {item.status}）")
        finish_item(item, "error", reason)
    _log(run, "fail", item=item_id, reason=reason)
    return {"ok": True, "item": item_id, "counts": run.state.counts(), "next": "ct.py next"}


def resume(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    reset = {"in_progress": 0, "error": 0}
    with run.update() as state:
        for item in state.items:
            if item.status in reset:
                reset[item.status] += 1
                item.status, item.slot = "pending", None
    _log(run, "resume", reset=reset)
    return {"ok": True, "run_dir": str(run.dir), "repo_root": str(run.repo), "stream": run.state.stream,
            "filter_file": str(run.dir / "filter.json"), "issues_file": str(run.dir / "issues.json"),
            "work_dir": str(run.dir / "work"), "verify": run.state.verify_mode,
            "planned": run.state.planned, "back_to_pending": reset, "counts": run.state.counts(),
            "next": "ct.py next" if run.state.planned else "MCP の search_issues を done まで呼び、ct.py plan"}


def status(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    state = run.state
    return {"ok": True, "run_dir": str(run.dir), "counts": state.counts(),
            "in_progress": [i.id for i in state.items if i.status == "in_progress"],
            "errors": [{"item": i.id, "reason": i.error} for i in state.items if i.status == "error"],
            "summary": str(run.dir / "summary.md") if (run.dir / "summary.md").is_file() else None}


def runs(repo_root: str, limit: int) -> dict[str, Any]:
    repo = _repo(repo_root)
    output = output_dir(repo, load_config(repo))
    found = []
    for folder in list_runs(output)[:limit]:
        try:
            state = Run(folder).state
        except (CtError, ValidationError):
            continue
        counts = state.counts()
        found.append({"run_dir": str(folder), "created_at": state.created_at, "filter": state.filter_name,
                      "planned": state.planned, "counts": counts,
                      "unfinished": not state.planned or counts["pending"] + counts["in_progress"] + counts["error"] > 0,
                      "summary": (folder / "summary.md").is_file(),
                      "applied": sum(1 for i in state.items if i.applied)})
    return {"ok": True, "output_dir": str(output), "runs": found}


# ---- summary ---------------------------------------------------------------------------------


def summary(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    path = run.dir / "summary.md"
    kept: dict[str, str] = {}
    if path.is_file():
        try:
            kept = read_approvals(path.read_text(encoding="utf-8"))
        except CtError:
            kept = {}
    reports, errors, unprocessed = [], [], []
    verify = read_json(run.dir / "verify.json", {})
    for item in run.state.items:
        if item.status == "done":
            result = TriageResult.model_validate(read_json(run.result_file(item.id)))
            saved = read_json(run.item_dir(item.id) / "saved.json", {})
            reports.append(_report(run, item.id, result, saved, item.seconds, verify.get(item.id)))
        elif item.status == "error":
            errors.append((item.id, item.error))
        else:
            unprocessed.append(item.id)
    write_text(path, render_summary(run.state.created_at.replace("T", " ")[:16], run.state.filter_name,
                                    reports, errors, unprocessed, kept))
    removed = []
    if not any(i.status in ("pending", "in_progress") for i in run.state.items):
        removed = cleanup(run)
    counts: dict[str, int] = {"fix": 0, "deviation": 0}
    for r in reports:
        counts[r.result.recommendation] += 1
    _log(run, "summary", items=len(reports), errors=len(errors), unprocessed=unprocessed)
    return {"ok": True, "summary": str(path), "items": len(reports), "recommended": counts,
            "errors": len(errors), "unprocessed": unprocessed, "removed_work_copies": removed,
            "next": "summary の場所と件数を人に伝え、承認欄を確かめてから /coverity-apply を使うよう案内する"}


def cleanup(run: Run) -> list[str]:
    """Remove the work copies once every item is finished; branches and patches stay."""
    work = run.dir / "work"
    removed = []
    vcs = run.vcs
    with file_lock(work / "vcs", timeout=VCS_LOCK_SECONDS):
        for folder in sorted(p for p in work.iterdir() if p.is_dir()):
            vcs.remove_copy(folder)
            removed.append(folder.name)
    return removed


def filter_from(run: Run) -> FilterSpec:
    return FilterSpec.model_validate(read_json(run.dir / "filter.json"))


# ---- verify (option) -------------------------------------------------------------------------


def verify(run_dir: str, mode: str | None) -> dict[str, Any]:
    """Build (and analyze) the latest code with every fix of the run applied, once."""
    from .verify import BatchItem, Verifier
    from .vcs import GitVcs

    run = Run(run_dir)
    mode = mode or run.state.verify_mode
    if mode == "none":
        return {"ok": True, "mode": "none", "message": "ビルドでの検証は使わない設定です"}
    issues, _ = _mapped(run)
    strip = 1 if isinstance(run.vcs, GitVcs) else 0
    batch = []
    for item in run.state.items:
        saved = read_json(run.item_dir(item.id) / "saved.json", {}) if item.status == "done" else {}
        if "fix" in saved:
            batch.append(BatchItem(item.id, saved["fix"]["patch"], saved["fix"]["files"],
                                   [issues[c] for c in item.cids], strip))
    if not batch:
        return {"ok": True, "mode": mode, "message": "確かめる修正案がありません"}
    latest = run.copy_dir("verify-latest")
    if latest.exists():
        shutil.rmtree(latest)
    run.vcs.export(run.state.latest_revision or "", latest)
    _log(run, "verify-start", mode=mode, items=[b.item_id for b in batch])
    outcome = Verifier(run.config.verify, run.dir).run_batch(mode, latest, batch)
    shutil.rmtree(latest, ignore_errors=True)

    downgraded = []
    results = read_json(run.dir / "verify.json", {})
    for item_id, checked in outcome.items.items():
        path = run.result_file(item_id)
        data = read_json(path)
        if checked.get("problems") and data["confidence"] != "low":
            checked["confidence_before"] = data["confidence"]
            data["confidence"] = "low"
            data["confidence_reason"] += f"（ビルドでの検証で問題: {'、'.join(checked['problems'])}）"
            write_json(path, data)
            downgraded.append(item_id)
        results[item_id] = checked
        rerender(run, item_id, checked)
    write_json(run.dir / "verify.json", results)
    _log(run, "verify", mode=mode, build_ok=outcome.build_ok, downgraded=downgraded, error=outcome.error)
    return {"ok": True, "mode": mode, "build_ok": outcome.build_ok, "seconds": round(outcome.seconds),
            "items": len(outcome.items), "downgraded_to_low": sorted(downgraded),
            "unassigned_new_issues": len(outcome.unassigned_new_issues), "error": outcome.error,
            "log": outcome.log, "next": "ct.py summary"}
