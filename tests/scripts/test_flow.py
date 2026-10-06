"""The whole /coverity-run and /coverity-apply flow with the fake Coverity data.

The agent's part is played by the test: it calls ct.py and the MCP tools in the order the skills
give, and writes what the worker subagent would write (an edit in the fix copy and result.json).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import ct
from conftest import SAMPLE
from coverity_triage import server

RESULT = {
    "verdict": {"judgement": "true_bug", "summary": "fgets が失敗したとき fp を閉じていない",
                "rationale": "19 行目で fgets が NULL を返すと 20 行目で return し、fclose を通らない。",
                "evidence": [{"file": "src/reader.c", "line": 20, "note": "fclose の前に return"}]},
    "recommendation": "fix", "confidence": "high", "confidence_reason": "警告経路どおりに成立する",
    "deviation": {"classification": "Bug", "action": "Fix Required", "severity": "Major",
                  "comment": "判定区分：本物のバグ。理由：fgets 失敗時に fp を閉じていない。"},
    "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Major",
            "summary": "return の前に fclose(fp) を追加", "impact": "関数の外から見た動きは変わらない"},
    "revision_drift": {"status": "none"},
}


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "test")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "test@example.com")


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(SAMPLE, root)
    config = root / ".coverity-triage" / "config.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["output_dir"] = str(tmp_path / "out")
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    git("init", "-q", "-b", "main", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "sample", cwd=root)
    git("remote", "add", "origin", str(origin), cwd=root)
    git("push", "-q", "origin", "main", cwd=root)
    return root


def run_ct(capsys, *args: str, ok: bool = True) -> dict:
    code = ct.main([str(a) for a in args])
    out = json.loads(capsys.readouterr().out)
    assert (code == 0) == ok, out
    assert out.get("ok", True) == ok, out
    return out


def start_run(capsys, repo: Path, *extra: str) -> dict:
    new = run_ct(capsys, "new-run", "--repo", repo, "--filter", "all.yaml", *extra)
    found = server.search_issues(str(repo), new["filter_file"], new["issues_file"])
    assert found["done"]
    return new


def work_item(capsys, run_dir: str, edit: bool = True, file: str = "src/reader.c", **changes) -> dict:
    """One item, as the agent and the worker subagent do it."""
    item = run_ct(capsys, "next", "--run", run_dir)
    if item["item"] is None:
        return item
    got = server.get_issues(item["repo_root"], item["stream"], item["cids"], item["item_dir"])
    assert got["done"]
    brief = run_ct(capsys, "brief", "--run", run_dir, "--item", item["item"])
    text = Path(brief["brief"]).read_text(encoding="utf-8")
    fix_dir = Path(run_dir) / "work" / "fix-1"
    assert str(fix_dir) in text and brief["result_file"] in text
    if edit:
        source = fix_dir / file
        source.write_bytes((source.read_bytes() if source.exists() else b"") + f"/* fix {item['item']} */\n".encode())
    result = {**RESULT, "item": item["item"], **changes}
    Path(brief["result_file"]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return {**item, "finish": run_ct(capsys, "finish", "--run", run_dir, "--item", item["item"])}


def test_run_and_apply(capsys, repo, tmp_path):
    new = start_run(capsys, repo)
    run_dir = new["run_dir"]
    assert Path(run_dir).parent == tmp_path / "out"
    planned = run_ct(capsys, "plan", "--run", run_dir)
    assert planned["issues"] == 6 and planned["items"] == 4
    assert planned["groups"] == [{"id": "G1", "cids": [20004, 20005, 20006]}]
    assert planned["analyzed_revision"] is None  # empty in the fake data: the local code is read

    deviation = {**RESULT["deviation"], "classification": "False Positive", "action": "Ignore",
                 "severity": "Unspecified", "comment": "判定区分：誤検知。理由：呼び出し元で検証済み。"}
    done = []
    while True:
        item = work_item(capsys, run_dir, **({"recommendation": "deviation", "confidence": "low",
                                              "deviation": deviation} if len(done) == 1 else {}))
        if item["item"] is None:
            break
        done.append(item["item"])
        assert item["finish"]["warnings"] == []
    assert done == ["20001", "20002", "20003", "G1"]

    report = (Path(run_dir) / "cid" / "20001.md").read_text(encoding="utf-8")
    assert "## 4. 案A: 逸脱" in report and "## 5. 案B: 修正" in report
    assert "```diff" in report and "+/* fix 20001 */" in report  # the diff is in the report
    assert "coverity-fix/cid-20001" in git("branch", "--list", "coverity-fix/*", cwd=repo)

    summary = run_ct(capsys, "summary", "--run", run_dir)
    assert summary["recommended"] == {"fix": 3, "deviation": 1}
    assert sorted(summary["removed_work_copies"]) == ["fix-1"]
    table = Path(summary["summary"]).read_text(encoding="utf-8")
    assert "| 承認 | ID | 推奨 | 確信度 | 見立て | 詳細 |" in table
    rows = [line for line in table.splitlines() if line.startswith("| ") and "[詳細]" in line]
    assert rows[0].startswith("| 逸脱 | 20002 |")  # low confidence first, approval pre-filled
    assert any(r.startswith("| 修正 | G1（3 件） |") for r in rows)

    # The person rejects 20003 and edits the deviation comment of 20002.
    Path(summary["summary"]).write_text(table.replace("| 修正 | 20003 |", "| 却下 | 20003 |"), encoding="utf-8")
    detail = Path(run_dir) / "cid" / "20002.md"
    detail.write_text(detail.read_text(encoding="utf-8").replace("呼び出し元で検証済み。", "read_config で検証済み。"),
                      encoding="utf-8")

    preview = run_ct(capsys, "preview", "--run", run_dir)
    assert preview["counts"] == {"修正": 2, "逸脱": 1, "却下": 1}
    written = server.update_triage(str(repo), preview["plan_file"], preview["confirmation_token"])
    assert written["done"] and written["written"] == ["20002"]
    writes = (repo / ".coverity-triage" / "fake-issues.yaml.writes.jsonl").read_text(encoding="utf-8")
    assert "read_config で検証済み。" in writes and '"False Positive"' in writes

    run_ct(capsys, "apply-code", "--run", run_dir, "--token", "wrong", ok=False)
    applied = run_ct(capsys, "apply-code", "--run", run_dir, "--token", preview["confirmation_token"])
    assert [p["branch"] for p in applied["pull_requests"]] == ["coverity-fix/cid-20001", f"coverity-fix/{Path(run_dir).name}-G1"]
    assert applied["coverity"] == {"registered": ["20002"], "not_registered": []}
    assert applied["rejected"] == ["20003"]
    remote = git("ls-remote", "--heads", "origin", cwd=repo)
    assert "refs/heads/coverity-fix/cid-20001" in remote and "refs/heads/coverity-fix/cid-20003" not in remote

    again = run_ct(capsys, "preview", "--run", run_dir)
    assert again["counts"] == {"修正": 0, "逸脱": 0, "却下": 0}
    assert sorted(again["already_applied"]) == ["20001", "20002", "20003", "G1"]
    assert git("status", "--porcelain", cwd=repo) == ""  # the person's files were never touched


def test_finish_reports_what_to_fix(capsys, repo):
    run_dir = start_run(capsys, repo, "--limit", "1")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    item = run_ct(capsys, "next", "--run", run_dir)
    run_ct(capsys, "brief", "--run", run_dir, "--item", item["item"], ok=False)  # warning path not fetched yet
    server.get_issues(item["repo_root"], item["stream"], item["cids"], item["item_dir"])
    brief = run_ct(capsys, "brief", "--run", run_dir, "--item", item["item"])

    missing = run_ct(capsys, "finish", "--run", run_dir, "--item", item["item"], ok=False)
    assert "結果ファイルがありません" in missing["error"]
    Path(brief["result_file"]).write_text(json.dumps({**RESULT, "item": "999", "confidence": "sure"}), encoding="utf-8")
    wrong = run_ct(capsys, "finish", "--run", run_dir, "--item", item["item"], ok=False)
    assert any(p.startswith("confidence") for p in wrong["problems"])
    Path(brief["result_file"]).write_text(json.dumps({**RESULT, "item": item["item"]}), encoding="utf-8")
    unchanged = run_ct(capsys, "finish", "--run", run_dir, "--item", item["item"], ok=False)
    assert "変更がありません" in unchanged["error"]

    failed = run_ct(capsys, "fail", "--run", run_dir, "--item", item["item"], "--reason", "直らない")
    assert failed["counts"]["error"] == 1
    resumed = run_ct(capsys, "resume", "--run", run_dir)
    assert resumed["back_to_pending"] == {"in_progress": 0, "error": 1}


def test_group_member_goes_back_to_its_own_item(capsys, repo):
    run_dir = start_run(capsys, repo, "--checker", "MISRA*")["run_dir"]
    planned = run_ct(capsys, "plan", "--run", run_dir)
    assert planned["groups"] == [{"id": "G1", "cids": [20004, 20005, 20006]}]
    item = work_item(capsys, run_dir, group_excluded_cids=[20006])
    assert item["finish"]["split_into"] == ["20006"]
    nxt = work_item(capsys, run_dir)
    assert nxt["item"] == "20006"
    status = run_ct(capsys, "status", "--run", run_dir)
    assert status["counts"]["done"] == 2


def test_worker_editing_the_persons_files_is_reported(capsys, repo):
    run_dir = start_run(capsys, repo, "--limit", "1")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    item = run_ct(capsys, "next", "--run", run_dir)
    server.get_issues(item["repo_root"], item["stream"], item["cids"], item["item_dir"])
    brief = run_ct(capsys, "brief", "--run", run_dir, "--item", item["item"])
    (repo / "src" / "reader.c").write_text("changed\n", encoding="utf-8")  # the wrong folder
    fix = Path(run_dir) / "work" / "fix-1" / "src" / "reader.c"
    fix.write_bytes(fix.read_bytes() + b"/* fix */\n")
    Path(brief["result_file"]).write_text(json.dumps({**RESULT, "item": item["item"]}), encoding="utf-8")
    done = run_ct(capsys, "finish", "--run", run_dir, "--item", item["item"])
    assert any("利用者のリポジトリ" in w for w in done["warnings"])


def test_runs_lists_unfinished_runs(capsys, repo):
    run_dir = start_run(capsys, repo)["run_dir"]
    listed = run_ct(capsys, "runs", "--repo", repo)
    assert listed["runs"][0]["run_dir"] == run_dir and listed["runs"][0]["unfinished"]


def test_output_inside_the_repository_is_refused(capsys, repo):
    config = repo / ".coverity-triage" / "config.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["output_dir"] = "out"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    refused = run_ct(capsys, "new-run", "--repo", repo, "--filter", "all.yaml", ok=False)
    assert "リポジトリの中" in refused["error"]


@pytest.mark.skipif(shutil.which("svnadmin") is None, reason="svn is not installed")
def test_svn_run_and_apply(capsys, tmp_path):
    server_repo = tmp_path / "svnrepo"
    subprocess.run(["svnadmin", "create", str(server_repo)], check=True)
    url = server_repo.as_uri()
    wc = tmp_path / "wc"
    subprocess.run(["svn", "checkout", "-q", url, str(wc)], check=True)
    shutil.copytree(SAMPLE, wc, dirs_exist_ok=True)
    config = wc / ".coverity-triage" / "config.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["vcs"] = {"type": "svn"}
    data["output_dir"] = str(tmp_path / "out")
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    subprocess.run(["svn", "add", "-q", "--force", "."], cwd=wc, check=True)
    subprocess.run(["svn", "commit", "-q", "-m", "sample"], cwd=wc, check=True)
    subprocess.run(["svn", "update", "-q"], cwd=wc, check=True)

    run_dir = start_run(capsys, wc, "--limit", "1")["run_dir"]
    planned = run_ct(capsys, "plan", "--run", run_dir)
    assert planned["latest_revision"] == "1"
    item = work_item(capsys, run_dir)
    assert item["item"] == "20001"
    patch = Path(run_dir) / "patches" / "20001-fix.patch"
    assert b"+/* fix 20001 */" in patch.read_bytes()
    summary = run_ct(capsys, "summary", "--run", run_dir)
    assert summary["removed_work_copies"] == ["fix-1"]
    preview = run_ct(capsys, "preview", "--run", run_dir)
    applied = run_ct(capsys, "apply-code", "--run", run_dir, "--token", preview["confirmation_token"])
    assert applied["svn_applied"] == ["20001:fix"]
    assert "/* fix 20001 */" in (wc / "src" / "reader.c").read_text(encoding="utf-8")


def test_work_copy_failure_stops_with_the_item_recorded(capsys, repo, monkeypatch):
    run_dir = start_run(capsys, repo, "--limit", "1")["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)

    def broken(self, dest, revision):
        from ctlib.common import CtError
        raise CtError("no space left on device")

    monkeypatch.setattr("ctlib.vcs.GitVcs.reset_copy", broken)
    failed = run_ct(capsys, "next", "--run", run_dir, ok=False)
    assert "修正用のコピーを用意できませんでした" in failed["error"] and "止めて" in failed["next"]
    status = run_ct(capsys, "status", "--run", run_dir)
    assert status["counts"]["error"] == 1 and "no space left" in status["errors"][0]["reason"]
