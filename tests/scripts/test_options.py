"""Options (off by default): annotation, per_run_branch, verify, knowledge_suggestions, metrics, parallel, markdown."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coverity_triage import server
from ctlib.common import CtError
from ctlib.run import Run
from test_flow import RESULT, approve, git, repo, run_ct, start_run, summary_rows, work_item  # noqa: F401 - fixtures


def set_config(repo: Path, **values) -> None:
    path = repo / ".coverity-triage" / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key, value in values.items():
        if isinstance(value, dict):
            data[key] = {**(data.get(key) or {}), **value}
        else:
            data[key] = value
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def approve_all(run_dir: str, decision: str | None) -> None:
    for item in Run(run_dir).state.items:
        if item.status == "done":
            approve(run_dir, item.id, decision)


def test_per_run_branch(capsys, repo):
    set_config(repo, options={"per_run_branch": True})
    run_dir = start_run(capsys, repo, "--limit", "3")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    for _ in range(3):
        assert work_item(capsys, run_dir)["finish"]["ok"]
    assert git("branch", "--list", "coverity-fix/*", cwd=repo) == ""  # no branch per CID
    run_ct(capsys, "summary", "--run", run_dir)
    approve_all(run_dir, "fix")
    preview = run_ct(capsys, "preview", "--run", run_dir)
    applied = run_ct(capsys, "apply-code", "--run", run_dir, "--token", preview["confirmation_token"])
    # All three items edit the end of the same file, so only the first one fits on one branch.
    assert len(applied["pull_requests"]) == 1
    pr = applied["pull_requests"][0]
    assert pr["branch"] == f"coverity-fix/run-{Path(run_dir).name}" and pr["ids"] == ["20001:fix"]
    assert len(applied["errors"]) == 2 and applied["next"].startswith("errors があれば")
    assert f"refs/heads/{pr['branch']}" in git("ls-remote", "--heads", "origin", cwd=repo)


def test_annotation(capsys, repo):
    set_config(repo, options={"annotation": True})
    run_dir = start_run(capsys, repo, "--limit", "1")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    item = run_ct(capsys, "next", "--run", run_dir)
    server.get_issues(item["repo_root"], item["stream"], item["cids"], item["item_dir"])
    brief = run_ct(capsys, "brief", "--run", run_dir, "--item", item["item"])
    annotation_dir = Path(run_dir) / "work" / "annotation-1"
    assert str(annotation_dir) in Path(brief["brief"]).read_text(encoding="utf-8")
    fix = Path(run_dir) / "work" / "fix-1" / "src" / "reader.c"
    fix.write_bytes(fix.read_bytes() + b"/* fix */\n")
    Path(brief["result_file"]).write_text(json.dumps({**RESULT, "item": item["item"]}), encoding="utf-8")
    missing = run_ct(capsys, "finish", "--run", run_dir, "--item", item["item"], ok=False)
    assert "アノテーション" in missing["error"]
    source = annotation_dir / "src" / "reader.c"
    text = source.read_text(encoding="utf-8")  # newlines read as \n whatever the checkout uses
    marked = text.replace("        return -1;\n    }\n    fclose",
                          "        /* coverity[leaked_storage:FALSE] */\n        return -1;\n    }\n    fclose")
    assert marked != text
    source.write_text(marked, encoding="utf-8")
    run_ct(capsys, "finish", "--run", run_dir, "--item", item["item"])
    report = (Path(run_dir) / "cid" / "20001.html").read_text(encoding="utf-8")
    assert "coverity[leaked_storage:FALSE]" in report and 'href="../patches/20001-annotation.patch"' in report
    run_ct(capsys, "summary", "--run", run_dir)
    approve_all(run_dir, "deviation")
    preview = run_ct(capsys, "preview", "--run", run_dir)
    assert preview["counts"]["逸脱"] == 1
    server.update_triage(str(repo), preview["plan_file"], preview["confirmation_token"])
    applied = run_ct(capsys, "apply-code", "--run", run_dir, "--token", preview["confirmation_token"])
    assert [p["branch"] for p in applied["pull_requests"]] == ["coverity-fix/cid-20001-annotation"]


@pytest.mark.parametrize("command,works", [("git --version", True), ("git no-such-command", False)])
def test_verify_by_build(capsys, repo, command, works):
    set_config(repo, verify={"build_command": command})
    run_dir = start_run(capsys, repo, "--limit", "2", "--verify", "build")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    work_item(capsys, run_dir)
    work_item(capsys, run_dir, file="src/other.c", confidence="medium")  # changes that do not overlap
    checked = run_ct(capsys, "verify", "--run", run_dir)
    assert checked["build_ok"] is works and checked["items"] == 2
    assert checked["downgraded_to_low"] == ([] if works else ["20001", "20002"])
    report = (Path(run_dir) / "cid" / "20001.html").read_text(encoding="utf-8")
    assert "<h2>ビルドでの検証</h2>" in report
    assert ("問題なし" in report) is works
    summary = run_ct(capsys, "summary", "--run", run_dir)
    assert "verify-latest" not in summary["removed_work_copies"]


def test_fixes_that_overlap_are_not_downgraded(capsys, repo):
    set_config(repo, verify={"build_command": "git --version"})
    run_dir = start_run(capsys, repo, "--limit", "2", "--verify", "build")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    work_item(capsys, run_dir)
    work_item(capsys, run_dir)  # the same place as the first fix
    checked = run_ct(capsys, "verify", "--run", run_dir)
    assert checked["build_ok"] and checked["downgraded_to_low"] == []
    assert "まとめた検証に入れられませんでした" in (Path(run_dir) / "cid" / "20002.html").read_text(encoding="utf-8")


def test_verify_needs_settings(capsys, repo):
    refused = run_ct(capsys, "new-run", "--repo", repo, "--filter", "all.yaml", "--verify", "build", ok=False)
    assert "ビルドでの検証の設定" in refused["error"]


def test_knowledge_and_metrics(capsys, repo):
    set_config(repo, options={"knowledge_suggestions": True, "metrics": True})
    run_dir = start_run(capsys, repo, "--limit", "2")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    work_item(capsys, run_dir)
    work_item(capsys, run_dir)
    run_ct(capsys, "summary", "--run", run_dir)
    approve(run_dir, "20002", "deviation")
    preview = run_ct(capsys, "preview", "--run", run_dir)
    server.update_triage(str(repo), preview["plan_file"], preview["confirmation_token"])
    run_ct(capsys, "apply-code", "--run", run_dir, "--token", preview["confirmation_token"])

    found = run_ct(capsys, "knowledge-candidates", "--run", run_dir)
    assert [c["item"] for c in found["candidates"]] == ["20002"]
    assert found["candidates"][0]["person_decision"] == "逸脱"
    run_ct(capsys, "add-knowledge", "--run", run_dir, "--entry", "read_config は path を必ず検証する")
    assert "- read_config は path を必ず検証する" in (repo / ".coverity-triage" / "knowledge.md").read_text(encoding="utf-8")

    stats = run_ct(capsys, "stats", "--repo", repo)
    assert stats["items"] == 2 and stats["recommendation_taken_rate"] == 0.5
    assert stats["approvals"] == {"fix": 1, "deviation": 1, "reject": 0}


def test_parallel_items_get_their_own_work_copies(capsys, repo):
    set_config(repo, parallel=2)
    run_dir = start_run(capsys, repo, "--limit", "3")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    first = run_ct(capsys, "next", "--run", run_dir)
    second = run_ct(capsys, "next", "--run", run_dir)
    third = run_ct(capsys, "next", "--run", run_dir)
    assert (first["item"], second["item"], third["item"]) == ("20001", "20002", None)
    assert "parallel" in third["next"]
    assert (Path(run_dir) / "work" / "fix-1").is_dir() and (Path(run_dir) / "work" / "fix-2").is_dir()
    server.get_issues(second["repo_root"], second["stream"], second["cids"], second["item_dir"])
    brief = run_ct(capsys, "brief", "--run", run_dir, "--item", "20002")
    assert str(Path(run_dir) / "work" / "fix-2") in Path(brief["brief"]).read_text(encoding="utf-8")


def test_independent_items_run_at_the_same_time(capsys, repo):
    """Two independent CIDs are both taken before either finishes, and finish in any order (#35)."""
    run_dir = start_run(capsys, repo, "--limit", "2")["run_dir"]
    planned = run_ct(capsys, "plan", "--run", run_dir)
    assert planned["items"] == 2 and planned["groups"] == []
    assert planned["parallel"] == 2 and planned["parallel_setting"] == 3  # the default, capped by the items
    assert "2 回" in planned["next"]
    first = run_ct(capsys, "next", "--run", run_dir)
    assert first["can_start_more"] == 1 and "あと 1 件" in first["next"]
    second = run_ct(capsys, "next", "--run", run_dir)
    assert second["can_start_more"] == 0 and second["counts"]["in_progress"] == 2
    assert run_ct(capsys, "next", "--run", run_dir)["item"] is None

    briefs = {}
    for item in (first, second):
        server.get_issues(item["repo_root"], item["stream"], item["cids"], item["item_dir"])
        briefs[item["item"]] = run_ct(capsys, "brief", "--run", run_dir, "--item", item["item"])
    for slot, item in ((1, first), (2, second)):
        fix_dir = Path(run_dir) / "work" / f"fix-{slot}"
        assert str(fix_dir) in Path(briefs[item["item"]]["brief"]).read_text(encoding="utf-8")
        source = fix_dir / "src" / "reader.c"
        source.write_bytes(source.read_bytes() + f"/* fix {item['item']} */\n".encode())
        Path(briefs[item["item"]]["result_file"]).write_text(
            json.dumps({**RESULT, "item": item["item"]}, ensure_ascii=False), encoding="utf-8")

    done_second = run_ct(capsys, "finish", "--run", run_dir, "--item", second["item"])
    assert done_second["counts"] == {"pending": 0, "in_progress": 1, "done": 1, "error": 0}
    done_first = run_ct(capsys, "finish", "--run", run_dir, "--item", first["item"])
    assert done_first["counts"] == {"pending": 0, "in_progress": 0, "done": 2, "error": 0}
    for item, other in ((first, second), (second, first)):
        patch = (Path(run_dir) / "patches" / f"{item['item']}-fix.patch").read_text(encoding="utf-8")
        assert f"fix {item['item']}" in patch and f"fix {other['item']}" not in patch
    assert run_ct(capsys, "summary", "--run", run_dir)["ok"]


def test_shared_analyzed_copy_is_not_reset_while_in_use(capsys, repo):
    run_dir = start_run(capsys, repo, "--limit", "3")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    state_file = Path(run_dir) / "run.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["analyzed_revision"] = git("rev-parse", "HEAD", cwd=repo).strip()
    state_file.write_text(json.dumps(state), encoding="utf-8")
    run_ct(capsys, "next", "--run", run_dir)
    marker = Path(run_dir) / "work" / "analyzed" / "being-read.txt"
    marker.write_text("x", encoding="utf-8")
    run_ct(capsys, "next", "--run", run_dir)
    assert marker.exists()  # the first item's worker is still reading this copy


def test_wrong_approvals_are_refused(capsys, repo):
    run_dir = start_run(capsys, repo, "--limit", "2")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    work_item(capsys, run_dir)
    run_ct(capsys, "next", "--run", run_dir)  # 20002 is still being worked on
    run_ct(capsys, "summary", "--run", run_dir)
    with pytest.raises(CtError, match="fix / deviation / reject"):
        approve(run_dir, "20001", "なおす")
    with pytest.raises(CtError, match="まだ終わっていません"):
        approve(run_dir, "20002", "fix")
    with pytest.raises(CtError, match="作業がありません"):
        approve(run_dir, "99999", "fix")
    with pytest.raises(CtError) as empty:
        approve(run_dir, "20001", "deviation", {**RESULT["deviation"], "comment": "  "})
    assert empty.value.details["problems"] == ["逸脱コメント が空です"]
    approve_all(run_dir, None)
    nothing = run_ct(capsys, "preview", "--run", run_dir)
    assert nothing["counts"] == {"修正": 0, "逸脱": 0, "却下": 0} and "画面" in nothing["next"]


def test_markdown_copies(capsys, repo):
    set_config(repo, options={"markdown": True})
    run_dir = start_run(capsys, repo, "--limit", "2")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    work_item(capsys, run_dir)
    work_item(capsys, run_dir, recommendation="deviation")
    for name in ("20001.html", "20001.md", "20002.html", "20002.md"):
        assert (Path(run_dir) / "cid" / name).is_file(), name
    detail = (Path(run_dir) / "cid" / "20001.md").read_text(encoding="utf-8")
    assert "## 4. 案A: 逸脱" in detail and "```diff" in detail and "ct.py gui" in detail
    summary = run_ct(capsys, "summary", "--run", run_dir)
    assert summary["summary"].endswith("summary.html")
    table = (Path(run_dir) / "summary.md").read_text(encoding="utf-8")
    assert "| 逸脱 | 20002 | 逸脱 |" in table and "ct.py gui" in table and "[詳細](cid/20001.md)" in table
    approve(run_dir, "20002", "reject")
    assert "| 却下 | 20002 | 逸脱 |" in (Path(run_dir) / "summary.md").read_text(encoding="utf-8")
    assert ("却下", "20002") in summary_rows((Path(run_dir) / "summary.html").read_text(encoding="utf-8"))
    preview = run_ct(capsys, "preview", "--run", run_dir)
    assert preview["counts"] == {"修正": 1, "逸脱": 0, "却下": 1}
