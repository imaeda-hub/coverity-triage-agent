"""Markdown reports and the approval column (spec D-18 to D-20, D-54 to D-56, D-62).

The AI submits structured data; this module renders it, so the layout is always the
same and the approval column and the editable deviation section can be read back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Issue, IssueDetail, TriageAttributes, TriageResult

JUDGEMENT_JA = {"false_positive": "誤検知", "true_bug": "本物のバグ",
                "intentional": "意図的", "undetermined": "判定不能"}
PLAN_JA = {"fix": "修正", "deviation": "逸脱"}
CONFIDENCE_JA = {"high": "高", "medium": "中", "low": "低"}
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
DRIFT_JA = {"none": "なし", "detected": "あり", "unknown": "不明"}
APPROVALS = {"修正": "fix", "逸脱": "deviation", "却下": "reject"}

DEV_BEGIN = "<!-- ct:begin deviation -->"
DEV_END = "<!-- ct:end deviation -->"


class ReportError(Exception):
    """Raised when an edited report cannot be read back."""


@dataclass
class ItemReport:
    """Everything needed to render one work item."""

    item_id: str
    cids: list[int]
    details: list[IssueDetail]
    result: TriageResult
    fixes: dict[str, dict[str, Any]] = field(default_factory=dict)   # kind -> saved fix
    verify: dict[str, dict[str, Any]] = field(default_factory=dict)  # kind -> result
    analyzed_revision: str | None = None
    latest_revision: str | None = None
    seconds: float | None = None
    error: str = ""
    run_dir: str | None = None

    def rel(self, path: str) -> str:
        """Paths inside the run folder are shown relative to the report (cid/<id>.md)."""
        if self.run_dir:
            try:
                return "../" + Path(path).resolve().relative_to(Path(self.run_dir).resolve()).as_posix()
            except ValueError:
                pass
        return path

    @property
    def main(self) -> Issue:
        return self.details[0].issue


def _verify_label(verify: dict[str, Any] | None) -> str:
    if not verify or verify.get("mode") in (None, "none"):
        return "-"
    if verify.get("applied") is False:
        return "適用不可"
    if verify.get("build_ok") is None:
        return "未確認"
    if not verify.get("build_ok"):
        return "失敗"
    if verify.get("remaining_cids"):
        return "警告残"
    if verify.get("new_issue_count"):
        return f"新規{verify['new_issue_count']}件"
    return "成功"


def _attrs(attrs: TriageAttributes) -> str:
    return (f"- Classification: {attrs.classification}\n- Action: {attrs.action}\n"
            f"- Severity: {attrs.severity}\n")


def _verify_block(verify: dict[str, Any]) -> str:
    mode = {"build": "ビルドのみ", "build+analyze": "ビルド＋再解析"}.get(verify.get("mode"), verify.get("mode"))
    lines = [f"- 方法: {mode}" + ("（実行内の全修正案をまとめて 1 回で検証）" if verify.get("batch") else ""),
             f"- 判定: {_verify_label(verify)}"]
    for problem in verify.get("problems", []):
        lines.append(f"- 要確認: {problem}")
    if verify.get("note"):
        lines.append(f"- 補足: {verify['note']}")
    if "resolved_cids" in verify:
        lines.append(f"- 解消した CID: {verify['resolved_cids'] or 'なし'}")
        lines.append(f"- 残っている CID: {verify['remaining_cids'] or 'なし'}")
        lines.append(f"- この修正案が変更したファイルでの新しい警告: {verify.get('new_issue_count', 0)} 件")
        for issue in verify.get("new_issues", [])[:10]:
            lines.append(f"  - {issue['checker']} {issue['file']} {issue.get('function') or ''}")
    if verify.get("batch"):
        lines.append("- 注意: まとめて検証しているため、修正案同士の影響は区別しきれません")
    if verify.get("detail"):
        lines.append(f"- 詳細: {verify['detail']}")
    if verify.get("log"):
        lines.append(f"- ログ: `{verify['log']}`")
    if verify.get("log_tail"):
        lines.append("\n```\n" + verify["log_tail"].strip() + "\n```")
    return "\n".join(lines) + "\n"


def render_item(report: ItemReport) -> str:
    r, main = report.result, report.main
    title_cid = f"{report.item_id}（{len(report.cids)} 件）" if report.item_id.startswith("G") else f"CID {report.item_id}"
    notes = [f"リビジョンのずれ {DRIFT_JA[r.revision_drift.status]}"
             + (f"（{r.revision_drift.detail}）" if r.revision_drift.detail else "")]
    notes.append("制約超過 " + ("あり: " + "、".join(r.fix.exceeded_constraints) if r.fix.exceeded_constraints else "なし"))
    if r.fix.already_fixed_on_latest:
        notes.append("最新リビジョンでは解消済み")
    path_notes = list(dict.fromkeys(n for d in report.details for n in d.issue.path_notes))
    notes.extend(path_notes)

    out = [f"# {title_cid} — {main.checker}（{main.impact or '-'}）\n",
           "## 1. 結論\n",
           f"- 推奨: {PLAN_JA[r.recommendation]} ／ 確信度: {CONFIDENCE_JA[r.confidence]}（{r.confidence_reason}）",
           f"- 見立て: {JUDGEMENT_JA[r.verdict.judgement]}（{r.verdict.summary}）",
           f"- 注意: {' ／ '.join(notes)}\n",
           "## 2. 警告の概要\n",
           "| CID | チェッカー | ファイル | 関数 | 行 | Impact | CWE |",
           "|---|---|---|---|---|---|---|"]
    for d in report.details:
        i = d.issue
        out.append(f"| {i.cid} | {i.checker} | {i.file} | {i.function or '-'} | {i.line or '-'} | "
                   f"{i.impact or '-'} | {i.cwe or '-'} |")
    first = report.details[0]
    if first.checker_description:
        out.append(f"\nチェッカーの説明: {first.checker_description}")
    out.append("\n## 3. 真偽の根拠\n")
    out.append("### 警告経路（Coverity のイベント）\n")
    for e in first.events:
        mark = "**★** " if e.main else ""
        out.append(f"- {mark}{e.file}:{e.line or '-'} `{e.tag}` {e.description}")
    out.append("\n### 調査結果\n")
    out.append(r.verdict.rationale + "\n")
    for ev in r.verdict.evidence:
        out.append(f"- {ev.file}:{ev.line or '-'} {ev.note}")

    out.append("\n## 4. 案A: 逸脱\n")
    out.append("この節の Classification / Action / Severity と逸脱コメントは手直しできます。"
               "承認の反映時に、手直し後の内容が Coverity に登録されます。\n")
    out.append(DEV_BEGIN)
    out.append(_attrs(r.deviation) + "- 逸脱コメント:\n")
    out.append(r.deviation.comment.strip())
    out.append(DEV_END + "\n")
    annotation = report.fixes.get("annotation")
    if annotation:
        out.append(f"- アノテーション差分: `{report.rel(annotation['patch_path'])}`"
                   + (f" ／ ブランチ: `{annotation['branch']}`" if annotation.get("branch") else ""))

    out.append("\n## 5. 案B: 修正\n")
    out.append(f"- 概要: {r.fix.summary}")
    out.append(f"- 影響範囲とリスク: {r.fix.impact}")
    if r.fix.exceeded_constraints:
        out.append(f"- 超えた制約: {'、'.join(r.fix.exceeded_constraints)}")
    fix = report.fixes.get("fix")
    if fix:
        out.append(f"- 差分: `{report.rel(fix['patch_path'])}`")
        if fix.get("branch"):
            out.append(f"- ブランチ: `{fix['branch']}`")
        out.append(f"- 修正後ファイル: `{report.rel(fix['mirror_dir'])}`")
    else:
        out.append("- 差分: なし（修正コードは保存されていません）")
    out.append("\n修正した場合の Classification / Action / Severity（参考。「修正」の承認時は Coverity に書き戻しません）:\n")
    out.append(_attrs(r.fix))

    out.append("## 6. 自動検証の結果\n")
    if report.verify:
        for kind, verify in report.verify.items():
            out.append(f"### {'修正' if kind == 'fix' else 'アノテーション'}\n")
            out.append(_verify_block({**verify, "log": report.rel(verify["log"])} if verify.get("log") else verify))
    else:
        out.append("実施していません。\n")

    out.append("## 7. 処理情報\n")
    out.append(f"- 調査の起点リビジョン: {report.analyzed_revision or '手元のコード'}")
    out.append(f"- 修正の起点リビジョン: {report.latest_revision or '-'}")
    if report.seconds is not None:
        out.append(f"- 処理時間: {report.seconds:.0f} 秒")
    if report.result.group_excluded_cids:
        out.append(f"- グループから外して個別処理に戻した CID: {report.result.group_excluded_cids}")
    return "\n".join(out) + "\n"


def read_deviation(markdown: str) -> tuple[TriageAttributes, str]:
    """Read back the (possibly edited) deviation section of a per-item report."""
    match = re.search(re.escape(DEV_BEGIN) + r"(.*?)" + re.escape(DEV_END), markdown, re.S)
    if not match:
        raise ReportError("逸脱の節の目印（ct:begin / ct:end）が見つかりません。目印の行は消さないでください")
    body = match.group(1)
    values = {}
    for key in ("Classification", "Action", "Severity"):
        m = re.search(rf"^\s*-\s*{key}\s*:\s*(.+?)\s*$", body, re.M)
        if not m:
            raise ReportError(f"逸脱の節に {key} がありません")
        values[key.lower()] = m.group(1)
    m = re.search(r"^\s*-\s*逸脱コメント\s*:\s*$(.*)", body, re.M | re.S)
    if not m or not m.group(1).strip():
        raise ReportError("逸脱コメントが空です")
    return TriageAttributes(**values), m.group(1).strip()


# ---- summary -------------------------------------------------------------------------------


SUMMARY_HEADER = ["承認", "CID", "推奨", "確信度", "チェッカー", "Impact", "場所", "見立て",
                  "グループ", "検証", "ずれ", "詳細"]


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_summary(meta: dict[str, Any], reports: list[ItemReport],
                   errors: list[tuple[str, str]], unprocessed: list[str],
                   approvals: dict[str, str] | None = None) -> str:
    """Render the summary list. ``approvals`` keeps values a person already entered."""
    approvals = approvals or {}
    reverse = {v: k for k, v in APPROVALS.items()}
    done = sorted(reports, key=lambda r: (CONFIDENCE_ORDER[r.result.confidence], r.item_id))
    total_cids = sum(len(r.cids) for r in reports) + len(errors) + len(unprocessed)
    groups = sum(1 for r in reports if r.item_id.startswith("G"))
    out = [f"# トリアージ結果 {meta['created_at'].replace('T', ' ')[:16]}",
           f"条件: {meta['filter_name']} ／ 対象 {total_cids} 件（グループ {groups}）／ エラー {len(errors)} 件",
           "",
           "承認列には「修正 / 逸脱 / 却下」のいずれかを書きます（推奨案を下書き済み）。"
           "空欄の行は反映しません。逸脱コメントの手直しは各詳細レポートで行います。",
           "",
           "| " + " | ".join(SUMMARY_HEADER) + " |",
           "|" + "---|" * len(SUMMARY_HEADER)]
    for r in done:
        main = r.main
        cid = f"{r.item_id}（{len(r.cids)} 件）" if r.item_id.startswith("G") else r.item_id
        place = f"{main.file} / {main.function}()" if main.function else main.file
        verify = r.verify.get("fix") or r.verify.get("annotation")
        approval = reverse.get(approvals.get(r.item_id, ""), PLAN_JA[r.result.recommendation])
        row = [approval, cid, PLAN_JA[r.result.recommendation],
               CONFIDENCE_JA[r.result.confidence], main.checker, main.impact or "-", place,
               r.result.verdict.summary, r.item_id if r.item_id.startswith("G") else "-",
               _verify_label(verify), DRIFT_JA[r.result.revision_drift.status],
               f"[→](cid/{r.item_id}.md)"]
        out.append("| " + " | ".join(_cell(c) for c in row) + " |")
    if errors:
        out += ["", "## エラー", "", "| CID | 内容 |", "|---|---|"]
        out += [f"| {_cell(i)} | {_cell(m)} |" for i, m in errors]
    if unprocessed:
        out += ["", "## 未処理", "", "再開（resume）で続きを処理できます: " + ", ".join(unprocessed)]
    return "\n".join(out) + "\n"


def read_approvals(markdown: str) -> dict[str, str]:
    """Return ``{item_id: "fix" | "deviation" | "reject"}`` for rows with an approval."""
    lines = markdown.splitlines()
    try:
        start = next(i for i, line in enumerate(lines)
                     if re.match(r"^\|\s*承認\s*\|\s*CID\s*\|", line))
    except StopIteration as exc:
        raise ReportError("一覧サマリの表（承認 | CID ...）が見つかりません") from exc
    approvals: dict[str, str] = {}
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if len(cells) < 2:
            continue
        m = re.match(r"^(G\d+|\d+)", cells[1])
        if not m:
            raise ReportError(f"CID 列を読めません: {cells[1]}")
        value = cells[0]
        if not value:
            continue
        if value not in APPROVALS:
            raise ReportError(f"{m.group(1)} の承認列が不正です: 「{value}」（修正 / 逸脱 / 却下 のいずれか）")
        approvals[m.group(1)] = APPROVALS[value]
    return approvals
