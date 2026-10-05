"""Options (off by default): annotation, per_run_branch, verify, knowledge_suggestions, metrics, parallel."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coverity_triage import server
from test_flow import RESULT, git, repo, run_ct, start_run, work_item  # noqa: F401 - fixtures


def set_config(repo: Path, **values) -> None:
    path = repo / ".coverity-triage" / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key, value in values.items():
        if isinstance(value, dict):
            data[key] = {**(data.get(key) or {}), **value}
        else:
            data[key] = value
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def approve_all(run_dir: str, value: str) -> None:
    path = Path(run_dir) / "summary.md"
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("| ") and "[詳細]" in line:
            line = "| " + value + " |" + line.split("|", 2)[2]
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_per_run_branch(capsys, repo):
    set_config(repo, options={"per_run_branch": True})
    run_dir = start_run(capsys, repo, "--limit", "3")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    for _ in range(3):
        assert work_item(capsys, run_dir)["finish"]["ok"]
    assert git("branch", "--list", "coverity-fix/*", cwd=repo) == ""  # no branch per CID
    run_ct(capsys, "summary", "--run", run_dir)
    approve_all(run_dir, "修正")
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
    assert "coverity[leaked_storage:FALSE]" in (Path(run_dir) / "cid" / "20001.md").read_text(encoding="utf-8")
    run_ct(capsys, "summary", "--run", run_dir)
    approve_all(run_dir, "逸脱")
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
    report = (Path(run_dir) / "cid" / "20001.md").read_text(encoding="utf-8")
    assert "## 6. ビルドでの検証" in report
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
    assert "まとめた検証に入れられませんでした" in (Path(run_dir) / "cid" / "20002.md").read_text(encoding="utf-8")


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
    table = Path(run_dir) / "summary.md"
    table.write_text(table.read_text(encoding="utf-8").replace("| 修正 | 20002 |", "| 逸脱 | 20002 |"), encoding="utf-8")
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


def test_approval_typos_are_listed(capsys, repo):
    run_dir = start_run(capsys, repo, "--limit", "1")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    work_item(capsys, run_dir)
    run_ct(capsys, "summary", "--run", run_dir)
    approve_all(run_dir, "なおす")
    refused = run_ct(capsys, "preview", "--run", run_dir, ok=False)
    assert refused["problems"] == ["20001 の承認欄「なおす」は「修正」「逸脱」「却下」のどれかにしてください"]
    approve_all(run_dir, "")
    nothing = run_ct(capsys, "preview", "--run", run_dir)
    assert nothing["counts"] == {"修正": 0, "逸脱": 0, "却下": 0}
