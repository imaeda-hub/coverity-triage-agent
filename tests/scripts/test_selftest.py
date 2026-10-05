"""/coverity-selftest commands, run against the sample repository they create."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coverity_triage import server
from ctlib import prepare, selftest
from test_flow import git_identity, run_ct, work_item  # noqa: F401 - fixture


@pytest.fixture(autouse=True)
def agents(tmp_path, monkeypatch):
    folder = tmp_path / "agents"
    monkeypatch.setattr(prepare, "user_agents_dir", lambda: folder)
    monkeypatch.setattr(selftest, "agent_state", prepare.agent_state)
    return folder


def test_selftest_with_the_sample(capsys, tmp_path):
    started = run_ct(capsys, "selftest", "start", "--sections", "1,2", "--client", "VS Code",
                     "--base", tmp_path / "results")
    folder = started["dir"]
    assert [c["id"] for c in started["checks"]][:2] == ["1-1", "1-2"]

    static = run_ct(capsys, "selftest", "static", "--dir", folder)
    assert static["1-7"]["status"] == "fail"  # the worker agent is not installed yet

    made = run_ct(capsys, "selftest", "sample", "--dir", folder)
    repo = made["repo_root"]
    new = run_ct(capsys, "new-run", "--repo", repo, "--filter", made["filter"])
    assert Path(new["run_dir"]).parent == Path(folder) / "out"
    assert server.search_issues(repo, new["filter_file"], new["issues_file"])["done"]
    run_dir = new["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    while work_item(capsys, run_dir)["item"] is not None:
        pass
    run_ct(capsys, "summary", "--run", run_dir)

    checked = run_ct(capsys, "selftest", "check-run", "--dir", folder, "--run", run_dir)["checks"]
    assert {k: v["status"] for k, v in checked.items()} == {
        "2-2": "pass", "2-3": "pass", "2-4": "pass", "2-5": "review", "2-6": "pass"}
    assert "20002: 推奨 fix" in checked["2-5"]["detail"]  # the test answered "fix" for every item

    preview = run_ct(capsys, "preview", "--run", run_dir)
    server.update_triage(repo, preview["plan_file"], preview["confirmation_token"])
    run_ct(capsys, "apply-code", "--run", run_dir, "--token", preview["confirmation_token"])
    applied = run_ct(capsys, "selftest", "check-apply", "--dir", folder, "--run", run_dir)
    assert applied["2-8"]["status"] == "pass"

    run_ct(capsys, "selftest", "record", "--dir", folder, "--id", "1-2", "--status", "pass",
           "--actual", "5 つ出た")
    done = run_ct(capsys, "selftest", "report", "--dir", folder)
    assert "1-1" in done["not_recorded"] and "2-7" in done["not_recorded"]
    text = Path(done["report"]).read_text(encoding="utf-8")
    assert "| 1-2 / メニュー |" in text and "5 つ出た" in text
    assert "## 3. 社内の Coverity（読み取りだけ）" in text and "未実施" in text


def test_selftest_checks_coverity_answers(capsys, tmp_path):
    """③ judged from the files the MCP tools wrote (here with the fake data)."""
    started = run_ct(capsys, "selftest", "start", "--sections", "2", "--base", tmp_path / "results")
    made = run_ct(capsys, "selftest", "sample", "--dir", started["dir"])
    repo = made["repo_root"]
    new = run_ct(capsys, "new-run", "--repo", repo, "--filter", made["filter"], "--limit", "1")
    server.search_issues(repo, new["filter_file"], new["issues_file"])
    run_ct(capsys, "plan", "--run", new["run_dir"])
    item = run_ct(capsys, "next", "--run", new["run_dir"])
    server.get_issues(repo, item["stream"], item["cids"], item["item_dir"])
    checked = run_ct(capsys, "selftest", "check-coverity", "--dir", started["dir"], "--run", new["run_dir"])["checks"]
    assert checked["3-3"]["status"] == "pass" and checked["3-4"]["status"] == "pass"
    assert checked["3-5"]["status"] == "review"  # the sample has no analyzed revision


def test_masking(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".coverity-triage").mkdir(parents=True)
    (repo / ".coverity-triage" / "config.yaml").write_text(
        "coverity:\n  url: https://cov.corp.example:8443\nvcs:\n  type: git\n", encoding="utf-8")
    monkeypatch.setattr("ctlib.selftest.get_env", lambda name: {"COV_USER": "taro", "COV_AUTH_KEY": "s3cret"}.get(name))
    mask = selftest.Masker(str(repo))
    text = mask("taro が https://cov.corp.example:8443 に s3cret で接続")
    assert text == "<user> が https://<coverity-host>:8443 に **** で接続"


def test_record_rejects_unknown_checks(capsys, tmp_path):
    started = run_ct(capsys, "selftest", "start", "--sections", "1", "--base", tmp_path)
    bad = run_ct(capsys, "selftest", "record", "--dir", started["dir"], "--id", "9-9", "--status", "pass",
                 "--actual", "x", ok=False)
    assert "9-9" in bad["error"]
    assert json.loads((Path(started["dir"]) / "selftest.json").read_text(encoding="utf-8"))["sections"] == ["1"]
