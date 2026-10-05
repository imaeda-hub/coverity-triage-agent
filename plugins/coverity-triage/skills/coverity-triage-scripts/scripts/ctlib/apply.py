"""Commands of /coverity-apply: preview (what will be applied, and the confirmation string) and apply-code.

The write-back to Coverity is done by the MCP tool ``update_triage`` between the two commands,
with the same confirmation string; it reads the ``triage`` list of ``apply-plan.json``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import metrics, oplog
from .common import CtError, now, read_json, sha, write_json
from .config import add_no_grouping
from .models import TriageResult
from .report import CONFIDENCE_JA, read_approvals, read_deviation
from .run import Run
from .vcs import GitVcs, SvnVcs

PLAN_FILE = "apply-plan.json"
LABELS = {"fix": "修正", "deviation": "逸脱", "reject": "却下"}


def _token(path: Path) -> str:
    return sha(path.read_bytes())[:12]


def preview(run_dir: str) -> dict[str, Any]:
    run = Run(run_dir)
    summary = run.dir / "summary.md"
    if not summary.is_file():
        raise CtError("一覧 summary.md がありません（/coverity-run の最後に ct.py summary で作ります）")
    approvals = read_approvals(summary.read_text(encoding="utf-8"))
    triage, code, reject, skipped, problems = [], [], [], [], []
    for item in run.state.items:
        decision = approvals.get(item.id)
        if decision is None or item.status != "done":
            continue
        saved = read_json(run.item_dir(item.id) / "saved.json", {})
        # Parts applied by an earlier /coverity-apply are not applied again.
        parts = {"fix": ["fix"], "deviation": ["coverity"] + (["annotation"] if "annotation" in saved else []),
                 "reject": ["reject"]}[decision]
        todo = [p for p in parts if p not in item.applied]
        if not todo:
            skipped.append(item.id)
            continue
        if decision == "fix":
            if "fix" not in saved:
                problems.append(f"{item.id}: 修正の差分がないため「修正」は反映できません（「逸脱」か「却下」にしてください）")
                continue
            code.append({"id": item.id, "cids": item.cids, "kind": "fix", **saved["fix"]})
        elif decision == "deviation":
            if "coverity" in todo:
                try:
                    attrs, comment = read_deviation(run.report_file(item.id).read_text(encoding="utf-8"))
                except CtError as exc:
                    problems.append(f"{item.id}: {exc}")
                    continue
                triage.append({"id": item.id, "cids": item.cids, **attrs.model_dump(), "comment": comment})
            if "annotation" in todo:
                code.append({"id": item.id, "cids": item.cids, "kind": "annotation", **saved["annotation"]})
        else:
            reject.append({"id": item.id, "cids": item.cids, "group": item.is_group})
    if problems:
        raise CtError("反映の前に直すところがあります", problems=problems)
    plan = {"run_id": run.state.run_id, "created_at": now(), "triage": triage, "code": code, "reject": reject}
    path = run.dir / PLAN_FILE
    write_json(path, plan)
    token = _token(path)
    counts = {"修正": sum(1 for c in code if c["kind"] == "fix"), "逸脱": len(triage), "却下": len(reject)}
    oplog.log(run.dir, "preview", counts=counts, token=token)
    return {"ok": True, "counts": counts, "already_applied": skipped,
            "items": [{"id": e["id"], "approval": label, "cids": e["cids"]}
                      for label, entries in (("逸脱", triage), ("修正", [c for c in code if c["kind"] == "fix"]),
                                             ("却下", reject)) for e in entries],
            "plan_file": str(path), "confirmation_token": token,
            "next": "件数を人に見せて同意を得る。同意を得たら、逸脱が 1 件以上あれば MCP の update_triage"
                    "（repo_root, plan_file, confirmation_token）を done まで呼び、そのあと ct.py apply-code --token"}


def _pr_text(run: Run, entry: dict[str, Any]) -> tuple[str, str]:
    result = TriageResult.model_validate(read_json(run.result_file(entry["id"])))
    cids = entry["cids"]
    title = f"Coverity CID {', '.join(map(str, cids[:5]))}{' ほか' if len(cids) > 5 else ''}: {result.fix.summary}"
    if entry["kind"] == "annotation":
        title = f"Coverity CID {', '.join(map(str, cids[:5]))}: 逸脱の注釈"
    body = "\n".join([
        "## 見立て", "", result.verdict.summary, "", result.verdict.rationale, "",
        "## 変更の内容", "", result.fix.summary if entry["kind"] == "fix" else result.deviation.comment, "",
        "## 影響とリスク", "", result.fix.impact, "",
        f"確信度: {CONFIDENCE_JA[result.confidence]}（Coverity トリアージの AI が作成し、人が承認）", ""])
    return title[:200], body


def apply_code(run_dir: str, token: str) -> dict[str, Any]:
    run = Run(run_dir)
    path = run.dir / PLAN_FILE
    if not path.is_file() or _token(path) != token.strip():
        raise CtError("確認用の文字列が反映の内容と一致しません。ct.py preview からやり直してください")
    plan = read_json(path)
    vcs = run.vcs
    outcome: dict[str, dict[str, Any]] = {}
    pull_requests, svn_applied, errors = [], [], []

    # Coverity: what update_triage reported.
    written = read_json(path.with_name("apply-plan.result.json"), {}).get("written", {})
    written_ids = {v["id"] for v in written.values()}
    not_written = [e["id"] for e in plan["triage"] if e["id"] not in written_ids]
    for entry in plan["triage"]:
        if entry["id"] in written_ids:
            outcome.setdefault(entry["id"], {})["coverity"] = "登録済み"

    # Code.
    if isinstance(vcs, GitVcs) and run.config.options.per_run_branch and plan["code"]:
        commits = [(f"{c['id']}:{c['kind']}", c["commit"]) for c in plan["code"]]
        branch, included, conflicted = vcs.combine(run.copy_dir("combine"), run.state.latest_revision or "",
                                                   commits, f"{run.config.vcs.branch_prefix}run-{run.state.run_id}")
        vcs.remove_copy(run.copy_dir("combine"))
        errors += [f"{key}: ほかの修正と同じ箇所を変えているため、まとめたブランチに入れられませんでした" for key in conflicted]
        if branch:
            try:
                vcs.push(branch)
                body = "\n".join(f"- {_pr_text(run, c)[0]}" for c in plan["code"] if f"{c['id']}:{c['kind']}" in included)
                title = f"Coverity トリアージ {run.state.run_id}（{len(included)} 件）"
                pull_requests.append({"ids": included, "branch": branch, "base": run.config.vcs.base_branch,
                                      "title": title, "body": body, "compare_url": vcs.compare_url(branch, title, body)})
                for key in included:
                    outcome.setdefault(key.split(":")[0], {})[key.split(":")[1]] = {"branch": branch, "pushed": True}
            except CtError as exc:
                errors.append(f"{branch}: push できませんでした: {exc}")
    else:
        for entry in plan["code"]:
            key = f"{entry['id']}:{entry['kind']}"
            try:
                if isinstance(vcs, SvnVcs):
                    vcs.apply_patch(Path(entry["patch"]))
                    svn_applied.append(key)
                    outcome.setdefault(entry["id"], {})[entry["kind"]] = {"svn_patch": "適用済み"}
                else:
                    vcs.push(entry["branch"])
                    title, body = _pr_text(run, entry)
                    pull_requests.append({"ids": [key], "branch": entry["branch"], "base": run.config.vcs.base_branch,
                                          "title": title, "body": body,
                                          "compare_url": vcs.compare_url(entry["branch"], title, body)})
                    outcome.setdefault(entry["id"], {})[entry["kind"]] = {"branch": entry["branch"], "pushed": True}
            except CtError as exc:
                errors.append(f"{key}: {exc}")

    # Rejected groups are worked on one by one next time.
    rejected_groups = [c for r in plan["reject"] if r["group"] for c in r["cids"]]
    if rejected_groups:
        add_no_grouping(run.repo, rejected_groups)
    for entry in plan["reject"]:
        outcome.setdefault(entry["id"], {})["reject"] = True

    with run.update() as state:
        for item_id, done in outcome.items():
            state.item(item_id).applied.update(done, at=now())
    if run.config.options.metrics:
        metrics.record(run, plan)
    oplog.log(run.dir, "apply-code", outcome=outcome, errors=errors)
    return {"ok": True, "pull_requests": pull_requests, "svn_applied": svn_applied,
            "coverity": {"registered": sorted(written_ids & {e["id"] for e in plan["triage"]}),
                         "not_registered": not_written},
            "rejected": [r["id"] for r in plan["reject"]], "errors": errors,
            "next": ("errors があれば人に伝える（反映されなかった項目は、もう一度 /coverity-apply で反映できる）。" if errors else "") + ("pull_requests の各項目について、GitHub のプルリクエストを作る（Copilot の GitHub の機能が"
                     "使えればそれで。使えなければ compare_url を人に示す）" if pull_requests else
                     "svn の変更はワーキングコピーに入りました。内容を確かめてコミットするよう人に伝える" if svn_applied
                     else "反映は終わりました")}
