"""HTML reports, approvals.json, and runs made before approvals.json (summary.md and cid/<ID>.md)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctlib import approvals, report
from ctlib.common import CtError
from ctlib.run import Run
from test_flow import RESULT, approve, git_identity, repo, run_ct, start_run, summary_rows, work_item  # noqa: F401
from test_options import set_config

SCRIPT = "<script>alert(1)</script>"
DEVIATION = {"classification": "False Positive", "action": "Ignore", "severity": "Unspecified",
             "comment": "判定区分：誤検知。理由：呼び出し元で検証済み。"}


def two_items(capsys, repo: Path, **changes) -> str:
    """A finished run: 20001 (fix, high) and 20002 (deviation, low), with the summary made."""
    run_dir = start_run(capsys, repo, "--limit", "2")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    work_item(capsys, run_dir, **changes)
    work_item(capsys, run_dir, file="src/other.c", recommendation="deviation", confidence="low", deviation=DEVIATION)
    run_ct(capsys, "summary", "--run", run_dir)
    return run_dir


def test_ai_text_is_escaped(capsys, repo):
    verdict = {**RESULT["verdict"], "summary": f"見立て {SCRIPT}", "rationale": f'理由 "{SCRIPT}" & <b>'}
    run_dir = two_items(capsys, repo, verdict=verdict, confidence_reason=SCRIPT,
                        fix={**RESULT["fix"], "summary": SCRIPT, "impact": "<img src=x onerror=alert(1)>"},
                        deviation={**RESULT["deviation"], "comment": SCRIPT})
    for page in (Path(run_dir) / "cid" / "20001.html", Path(run_dir) / "summary.html"):
        text = page.read_text(encoding="utf-8")
        assert "<script" not in text and "<img" not in text and "<b>" not in text, page.name
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text, page.name
    detail = (Path(run_dir) / "cid" / "20001.html").read_text(encoding="utf-8")
    assert "&quot;&lt;script&gt;" in detail and "&amp; &lt;b&gt;" in detail
    assert detail.startswith("<!doctype html>") and '<meta charset="utf-8">' in detail


def test_diff_is_colored_line_by_line():
    text = "\n".join(["diff --git a/x.c b/x.c", "index 1..2 100644", "--- a/x.c", "+++ b/x.c",
                      "@@ -1,3 +1,3 @@ int f(void)", " keep <tag>", "-old & gone", "--- a removed line",
                      "+new", "+++ an added line", "\\ No newline at end of file",
                      "Index: y.c", "===================================================================",
                      "--- y.c\t(revision 1)", "+++ y.c\t(working copy)", "@@ -1 +1 @@", "-a", "+b"])
    block = report.diff_html(text)
    assert block.startswith('<pre class="diff">') and block.endswith("</pre>")
    spans = [line for line in block.removeprefix('<pre class="diff">').split("</span>") if line != "</pre>"]
    kinds = [s.split(">", 1)[0] for s in spans]
    assert kinds == ['<span class="meta"', '<span class="meta"', '<span class="meta"', '<span class="meta"',
                     '<span class="hunk"', "<span", '<span class="del"', '<span class="del"', '<span class="add"',
                     '<span class="add"', '<span class="meta"', '<span class="meta"', '<span class="meta"',
                     '<span class="meta"', '<span class="meta"', '<span class="hunk"', '<span class="del"',
                     '<span class="add"']
    assert " keep &lt;tag&gt;\n" in block and "-old &amp; gone\n" in block
    long = report.diff_html("\n".join(["@@ -0,0 +1,10 @@"] + [f"+{n}" for n in range(10)]), limit=3)
    assert long.count('<span class="add">') == 2 and "先頭 3 行" in long


def test_approvals_defaults_update_and_validation(capsys, repo):
    run_dir = two_items(capsys, repo)
    run = Run(run_dir)
    saved = json.loads((Path(run_dir) / "approvals.json").read_text(encoding="utf-8"))
    assert saved["version"] == 1 and saved["updated_at"]
    assert {k: (v["decision"], v["edited"]) for k, v in saved["items"].items()} == {
        "20001": ("fix", False), "20002": ("deviation", False)}
    assert approvals.decisions(run) == {"20001": "fix", "20002": "deviation"}

    # an edited deviation is kept until the AI's proposal is sent back
    edit = {**DEVIATION, "comment": "  人が直したコメント  ", "severity": "Minor"}
    entry = approve(run_dir, "20002", "deviation", edit)
    assert entry["edited"] and entry["deviation"] == {**edit, "comment": "人が直したコメント"}
    detail = (Path(run_dir) / "cid" / "20002.html").read_text(encoding="utf-8")
    assert "人が直したコメント" in detail and "AI の案を見る" in detail and DEVIATION["comment"] in detail
    assert approve(run_dir, "20002", "reject")["deviation"]["comment"] == "人が直したコメント"  # None keeps it
    assert approvals.deviation_for(run, "20002")[1] == "人が直したコメント"
    back = approve(run_dir, "20002", "deviation", DEVIATION)
    assert "deviation" not in back and not back["edited"]
    attrs, comment = approvals.deviation_for(run, "20002")
    assert comment == DEVIATION["comment"] and attrs.classification == "False Positive"

    # a choice of the person survives ct.py summary
    approve(run_dir, "20001", None)
    run_ct(capsys, "summary", "--run", run_dir)
    assert approvals.load(run)["20001"]["decision"] is None and approvals.load(run)["20001"]["edited"]
    assert ("（空）", "20001") in summary_rows((Path(run_dir) / "summary.html").read_text(encoding="utf-8"))

    with pytest.raises(CtError) as bad:
        approve(run_dir, "20002", "deviation", {"classification": "", "action": "Ignore", "severity": "A\nB",
                                                "comment": "x"})
    assert bad.value.details["problems"] == ["Classification が空です", "Severity に改行は入れられません"]
    assert approve(run_dir, "20002", "null")["decision"] is None


def test_preview_registers_the_edited_deviation(capsys, repo):
    run_dir = two_items(capsys, repo)
    edit = {**DEVIATION, "classification": "Intentional", "comment": "画面で直した逸脱コメント"}
    approve(run_dir, "20002", "deviation", edit)
    preview = run_ct(capsys, "preview", "--run", run_dir)
    assert preview["counts"] == {"修正": 1, "逸脱": 1, "却下": 0}
    plan = json.loads(Path(preview["plan_file"]).read_text(encoding="utf-8"))
    assert plan["triage"] == [{"id": "20002", "cids": [20002], "classification": "Intentional", "action": "Ignore",
                               "severity": "Unspecified", "comment": "画面で直した逸脱コメント"}]


def test_preview_needs_the_summary(capsys, repo):
    run_dir = start_run(capsys, repo, "--limit", "2")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    work_item(capsys, run_dir)
    refused = run_ct(capsys, "preview", "--run", run_dir, ok=False)
    assert refused["error"].startswith("一覧がありません")

    # approved in the screen while the run goes on: the report shows it, the summary waits for ct.py summary
    approve(run_dir, "20001", "reject")
    assert "承認 却下" in (Path(run_dir) / "cid" / "20001.html").read_text(encoding="utf-8")
    assert not (Path(run_dir) / "summary.html").exists()
    assert run_ct(capsys, "preview", "--run", run_dir, ok=False)["error"].startswith("一覧がありません")
    work_item(capsys, run_dir, file="src/other.c", recommendation="deviation", deviation=DEVIATION)
    run_ct(capsys, "summary", "--run", run_dir)
    assert approvals.decisions(Run(run_dir)) == {"20001": "reject", "20002": "deviation"}
    assert run_ct(capsys, "preview", "--run", run_dir)["counts"] == {"修正": 0, "逸脱": 1, "却下": 1}


def test_an_item_finished_after_the_summary_gets_the_recommendation(capsys, repo):
    """A resumed run: the summary was made before the last item finished, and is not made again yet."""
    run_dir = start_run(capsys, repo, "--limit", "2")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    work_item(capsys, run_dir)
    assert run_ct(capsys, "summary", "--run", run_dir)["unprocessed"] == ["20002"]
    approve(run_dir, "20001", "reject")
    work_item(capsys, run_dir, file="src/other.c", recommendation="deviation", deviation=DEVIATION)
    run = Run(run_dir)
    assert approvals.decisions(run) == {"20001": "reject", "20002": "deviation"}  # the person's choice is kept
    assert not approvals.load(run)["20002"]["edited"]
    assert run_ct(capsys, "preview", "--run", run_dir)["counts"] == {"修正": 0, "逸脱": 1, "却下": 1}


def test_run_made_before_approvals_json(capsys, repo):
    """summary.md (approval column) and cid/<ID>.md (deviation section) are read until approvals.json exists."""
    set_config(repo, options={"markdown": True})
    run_dir = two_items(capsys, repo)
    run = Run(run_dir)
    for page in ("approvals.json", "summary.html", "cid/20001.html", "cid/20002.html"):  # made by 0.4
        (Path(run_dir) / page).unlink()
    table = Path(run_dir) / "summary.md"
    original = table.read_text(encoding="utf-8")
    table.write_text(original.replace("| 修正 | 20001 |", "| 却下 | 20001 |"), encoding="utf-8")
    detail = Path(run_dir) / "cid" / "20002.md"
    detail.write_text(detail.read_text(encoding="utf-8").replace("呼び出し元で検証済み。", "read_config で検証済み。"),
                      encoding="utf-8")

    loaded = approvals.load(run)
    assert loaded["20001"]["decision"] == "reject" and loaded["20001"]["edited"]
    assert loaded["20002"]["deviation"]["comment"].endswith("read_config で検証済み。")
    preview = run_ct(capsys, "preview", "--run", run_dir)
    assert preview["counts"] == {"修正": 0, "逸脱": 1, "却下": 1}
    plan = json.loads(Path(preview["plan_file"]).read_text(encoding="utf-8"))
    assert plan["triage"][0]["comment"].endswith("read_config で検証済み。")

    table.write_text(original.replace("| 修正 | 20001 |", "| なおす | 20001 |"), encoding="utf-8")
    refused = run_ct(capsys, "preview", "--run", run_dir, ok=False)
    assert refused["problems"] == ["20001 の承認欄「なおす」は「修正」「逸脱」「却下」のどれかにしてください"]

    # ct.py summary takes the approvals over into approvals.json
    table.write_text(original.replace("| 修正 | 20001 |", "| 却下 | 20001 |"), encoding="utf-8")
    edited = detail.read_text(encoding="utf-8")
    run_ct(capsys, "summary", "--run", run_dir)
    saved = json.loads((Path(run_dir) / "approvals.json").read_text(encoding="utf-8"))["items"]
    assert saved["20001"]["decision"] == "reject"
    assert saved["20002"]["deviation"]["comment"].endswith("read_config で検証済み。")
    assert detail.read_text(encoding="utf-8") == edited  # the person's file is kept
    assert (Path(run_dir) / "cid" / "20002.html").is_file()
    assert ("却下", "20001") in summary_rows((Path(run_dir) / "summary.html").read_text(encoding="utf-8"))


def test_retry_starts_without_the_old_progress(capsys, repo):
    run_dir = start_run(capsys, repo, "--limit", "1")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    item = run_ct(capsys, "next", "--run", run_dir)
    progress = Path(item["item_dir"]) / "progress.json"
    progress.write_text(json.dumps({"item": item["item"], "phase": "fix", "note": "直している"}), encoding="utf-8")
    run_ct(capsys, "fail", "--run", run_dir, "--item", item["item"], "--reason", "止まった")
    run_ct(capsys, "resume", "--run", run_dir)
    assert run_ct(capsys, "next", "--run", run_dir)["item"] == item["item"]
    assert not progress.exists()
