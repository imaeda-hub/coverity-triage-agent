"""Detailed reports (``cid/<ID>.md``) and the summary (``summary.md``) with its approval column.

The worker subagent writes structured data (result.json); this module lays it out, so every report
has the same shape and the approval column and the editable deviation section can be read back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import CtError, display_text
from .models import Attributes, Event, Issue, TriageResult

JUDGEMENT_JA = {"false_positive": "誤検知", "true_bug": "本物のバグ", "intentional": "意図的",
                "undetermined": "判定できない"}
PLAN_JA = {"fix": "修正", "deviation": "逸脱"}
CONFIDENCE_JA = {"high": "高", "medium": "中", "low": "低"}
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
DRIFT_JA = {"none": "なし", "detected": "あり", "unknown": "不明"}
APPROVALS = {"修正": "fix", "逸脱": "deviation", "却下": "reject"}

DEV_BEGIN = "<!-- ct:begin deviation -->"
DEV_END = "<!-- ct:end deviation -->"
MAX_DIFF_LINES = 400
SUMMARY_HEADER = "| 承認 | ID | 推奨 | 確信度 | 見立て | 詳細 |"


@dataclass
class ItemReport:
    """Everything needed to render one item."""

    item_id: str
    issues: list[Issue]
    events: dict[int, list[Event]]
    checker_description: str
    result: TriageResult
    saved: dict[str, dict[str, Any]] = field(default_factory=dict)   # "fix" / "annotation" -> saved fix
    verify: dict[str, Any] = field(default_factory=dict)
    analyzed_revision: str | None = None
    latest_revision: str | None = None
    seconds: float | None = None
    notes: list[str] = field(default_factory=list)
    run_dir: Path | None = None

    def rel(self, path: str) -> str:
        """Paths inside the run folder, relative to the report (cid/<ID>.md)."""
        if self.run_dir:
            try:
                return "../" + Path(path).resolve().relative_to(self.run_dir.resolve()).as_posix()
            except ValueError:
                pass
        return path


def item_title(item_id: str, count: int) -> str:
    return f"{item_id}（{count} 件）" if item_id.startswith("G") else item_id


def _attrs(attrs: Attributes) -> list[str]:
    return [f"- Classification: {attrs.classification}", f"- Action: {attrs.action}",
            f"- Severity: {attrs.severity}"]


def _diff_block(path: str | None) -> list[str]:
    if not path or not Path(path).is_file():
        return []
    lines = display_text(Path(path).read_bytes()).splitlines()
    shown = lines[:MAX_DIFF_LINES]
    # A fence longer than any run of backticks in the diff, so the diff cannot close it.
    fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", "\n".join(shown))), default=0) + 1)
    out = [fence + "diff", *shown, fence]
    if len(lines) > MAX_DIFF_LINES:
        out.append(f"（差分が長いため先頭 {MAX_DIFF_LINES} 行だけ表示しています。全体は差分ファイルを見てください）")
    return out


def render_item(r: ItemReport) -> str:
    res, main = r.result, r.issues[0]
    out: list[str] = [f"# {item_title(r.item_id, len(r.issues))} — {main.checker}（{main.impact or '-'}）", ""]

    # 1. conclusion
    notes = [f"リビジョンのずれ {DRIFT_JA[res.revision_drift.status]}"
             + (f"（{res.revision_drift.detail}）" if res.revision_drift.detail else "")]
    if res.fix.exceeded_constraints:
        notes.append("修正の制約を超えた点あり: " + "、".join(res.fix.exceeded_constraints))
    if res.fix.already_fixed_on_latest:
        notes.append("最新のリビジョンでは直っています")
    notes += r.notes
    out += ["## 1. 結論", "",
            f"- 推奨: **{PLAN_JA[res.recommendation]}** ／ 確信度: **{CONFIDENCE_JA[res.confidence]}**"
            f"（{res.confidence_reason}）",
            f"- 見立て: {JUDGEMENT_JA[res.verdict.judgement]}（{res.verdict.summary}）",
            f"- 注意: {' ／ '.join(notes)}", ""]

    # 2. overview
    out += ["## 2. 警告の概要", "", "| CID | チェッカー | ファイル | 関数 | 行 | Impact | CWE |",
            "|---|---|---|---|---|---|---|"]
    for i in r.issues:
        out.append(f"| {i.cid} | {i.checker} | {i.file} | {i.function or '-'} | {i.line or '-'} | "
                   f"{i.impact or '-'} | {i.cwe or '-'} |")
    if r.checker_description:
        out += ["", f"チェッカーの説明: {r.checker_description}"]
    out.append("")

    # 3. evidence
    out += ["## 3. 真偽の根拠", ""]
    for i in r.issues:
        out += [f"警告経路（CID {i.cid}）:", ""]
        events = r.events.get(i.cid) or []
        out += [f"{n}. {'★ ' if e.main else ''}{e.file}:{e.line or '-'} `{e.tag}` {e.description}"
                for n, e in enumerate(events, 1)] or ["（警告経路を取得できませんでした）"]
        out.append("")
    out += ["調査結果:", "", res.verdict.rationale, ""]
    out += [f"- {ev.file}:{ev.line or '-'} {ev.note}" for ev in res.verdict.evidence]
    out.append("")

    # 4. deviation (editable)
    dev = res.deviation
    out += ["## 4. 案A: 逸脱", "",
            "この節の Classification / Action / Severity と逸脱コメントは書き換えられます。"
            "「逸脱」で反映すると、書き換えた内容が Coverity に登録されます。", "",
            DEV_BEGIN, *_attrs(dev), "- 逸脱コメント:", "", dev.comment, DEV_END, ""]
    annotation = r.saved.get("annotation")
    if annotation:
        out += ["アノテーション（ソースに入れる注釈）の差分:", "", *_diff_block(annotation.get("patch")), ""]

    # 5. fix (diff inline)
    fix = res.fix
    out += ["## 5. 案B: 修正", "", f"- 概要: {fix.summary}", f"- 影響とリスク: {fix.impact}"]
    saved = r.saved.get("fix")
    if saved:
        if saved.get("branch"):
            out.append(f"- ブランチ: `{saved['branch']}`")
        out += [f"- 差分ファイル: [{Path(saved['patch']).name}]({r.rel(saved['patch'])})", "",
                *_diff_block(saved.get("patch"))]
    else:
        out.append("- 差分: なし（最新のリビジョンでは直っているため）" if fix.already_fixed_on_latest
                   else "- 差分: なし")
    out += ["", "修正した場合の Classification / Action / Severity（参考。「修正」で反映しても Coverity には書き込みません）:",
            "", *_attrs(fix), ""]

    # 6. verification (option); the processing section takes its number when it is absent
    if r.verify:
        out += ["## 6. ビルドでの検証", "", *_verify_lines(r.verify), ""]

    out += [f"## {7 if r.verify else 6}. 処理情報", "",
            f"- 調べたコード: {('解析リビジョン ' + _short(r.analyzed_revision)) if r.analyzed_revision else '手元のコード'}",
            f"- 修正の元にしたリビジョン: {_short(r.latest_revision) if r.latest_revision else '-'}"]
    if r.seconds is not None:
        out.append(f"- 処理時間: {r.seconds:.0f} 秒")
    if res.group_excluded_cids:
        out.append(f"- グループから外して個別の作業に戻した CID: {res.group_excluded_cids}")
    return "\n".join(out) + "\n"


def _short(revision: str) -> str:
    """A git commit as its first 10 characters; svn revisions stay as they are."""
    return revision[:10] if re.fullmatch(r"[0-9a-f]{40}", revision) else revision


def _verify_lines(verify: dict[str, Any]) -> list[str]:
    mode = {"build": "ビルド", "build+analyze": "ビルドと Coverity の再解析"}.get(verify.get("mode"), "-")
    lines = [f"- 方法: {mode}（この実行の修正案をすべて当てて 1 回で確かめています）",
             f"- 結果: {verify_label(verify)}"]
    lines += [f"- 要確認: {p}" for p in verify.get("problems", [])]
    if verify.get("note"):
        lines.append(f"- 補足: {verify['note']}")
    if "resolved_cids" in verify:
        lines += [f"- 消えた警告: {verify['resolved_cids'] or 'なし'}",
                  f"- 残った警告: {verify['remaining_cids'] or 'なし'}",
                  f"- 変更したファイルで増えた警告: {verify.get('new_issue_count', 0)} 件"]
    if verify.get("log"):
        lines.append(f"- ログ: `{verify['log']}`")
    if verify.get("log_tail"):
        lines += ["", "```", verify["log_tail"].strip(), "```"]
    return lines


def verify_label(verify: dict[str, Any] | None) -> str:
    if not verify:
        return "-"
    if verify.get("applied") is False:
        return "当てられない"
    if verify.get("build_ok") is None:
        return "確かめられない"
    if not verify["build_ok"]:
        return "ビルド失敗"
    if verify.get("remaining_cids"):
        return "警告が残る"
    if verify.get("new_issue_count"):
        return f"警告が {verify['new_issue_count']} 件増える"
    return "問題なし"


def read_deviation(markdown: str) -> tuple[Attributes, str]:
    """The (possibly edited) deviation section of a detailed report."""
    match = re.search(re.escape(DEV_BEGIN) + r"(.*?)" + re.escape(DEV_END), markdown, re.S)
    if not match:
        raise CtError("逸脱の節の目印（<!-- ct:begin deviation --> と <!-- ct:end deviation -->）が"
                      "見つかりません。目印の行は消さないでください")
    body = match.group(1)
    values = {}
    for key in ("Classification", "Action", "Severity"):
        m = re.search(rf"^\s*-\s*{key}\s*:\s*(.+?)\s*$", body, re.M)
        if not m:
            raise CtError(f"逸脱の節に {key} がありません")
        values[key.lower()] = m.group(1)
    m = re.search(r"^\s*-\s*逸脱コメント\s*:\s*$(.*)", body, re.M | re.S)
    if not m or not m.group(1).strip():
        raise CtError("逸脱コメントが空です")
    return Attributes(**values), m.group(1).strip()


# ---- summary -------------------------------------------------------------------------------


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_summary(title: str, filter_name: str, reports: list[ItemReport], errors: list[tuple[str, str]],
                   unprocessed: list[str], approvals: dict[str, str]) -> str:
    """``approvals`` keeps what a person already wrote in the approval column."""
    reverse = {v: k for k, v in APPROVALS.items()}
    done = sorted(reports, key=lambda r: (CONFIDENCE_ORDER[r.result.confidence], r.item_id))
    total = sum(len(r.issues) for r in reports)
    groups = sum(1 for r in reports if r.item_id.startswith("G"))
    out = [f"# トリアージ結果 {title}", "",
           f"条件: {filter_name} ／ 警告 {total} 件（作業 {len(reports)}、うちグループ {groups}）"
           + (f" ／ エラー {len(errors)}" if errors else "") + (f" ／ 未処理 {len(unprocessed)}" if unprocessed else ""),
           "",
           "承認欄には「修正」「逸脱」「却下」のどれかを書きます（AI の推奨を入れてあります）。"
           "空欄の行は反映しません。逸脱コメントを直すときは、詳細レポートの「案A: 逸脱」を書き換えます。",
           "書き終えたら `/coverity-apply` で反映します。", "",
           SUMMARY_HEADER, "|---|---|---|---|---|---|"]
    for r in done:
        res = r.result
        approval = reverse.get(approvals.get(r.item_id, ""), PLAN_JA[res.recommendation])
        row = [approval, item_title(r.item_id, len(r.issues)), PLAN_JA[res.recommendation],
               CONFIDENCE_JA[res.confidence], res.verdict.summary, f"[詳細](cid/{r.item_id}.md)"]
        out.append("| " + " | ".join(_cell(c) for c in row) + " |")
    if errors:
        out += ["", "## 処理できなかった作業", "", "| ID | 理由 |", "|---|---|"]
        out += [f"| {_cell(i)} | {_cell(m)} |" for i, m in errors]
    if unprocessed:
        out += ["", "## 未処理の作業", "", "`/coverity-run` で続きから処理できます: " + "、".join(unprocessed)]
    return "\n".join(out) + "\n"


def read_approvals(markdown: str) -> dict[str, str]:
    """``{item id: "fix" | "deviation" | "reject"}`` for rows whose approval column is filled in."""
    lines = markdown.splitlines()
    start = next((n for n, line in enumerate(lines) if re.match(r"^\|\s*承認\s*\|\s*ID\s*\|", line)), None)
    if start is None:
        raise CtError("一覧の表（| 承認 | ID | ...）が見つかりません。表の見出しの行は消さないでください")
    approvals: dict[str, str] = {}
    problems: list[str] = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if len(cells) < 2:
            continue
        m = re.match(r"^(G\d+|\d+)", cells[1])
        if not m:
            problems.append(f"ID の欄を読めません: {cells[1]}")
            continue
        value = cells[0].strip("*` ")
        if not value:
            continue
        if value not in APPROVALS:
            problems.append(f"{m.group(1)} の承認欄「{value}」は「修正」「逸脱」「却下」のどれかにしてください")
            continue
        approvals[m.group(1)] = APPROVALS[value]
    if problems:
        raise CtError("一覧の承認欄に直すところがあります", problems=problems)
    return approvals
