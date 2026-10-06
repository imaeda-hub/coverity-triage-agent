"""/coverity-setup commands, and settings shared with the MCP server."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

import ct
from coverity_triage import models as server_models
from coverity_triage import settings as server_settings
from ctlib import config, models, prepare
from test_flow import run_ct


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Keep the person's ~/.copilot/agents out of the tests."""
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setattr(prepare, "user_agents_dir", lambda: folder / ".copilot" / "agents")
    return folder


@pytest.fixture
def empty_repo(tmp_path) -> Path:
    root = tmp_path / "product"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "develop"], cwd=root, check=True)
    return root


def test_settings_match_the_mcp_server():
    assert config.CoverityConfig.model_fields.keys() == server_settings.CoverityConfig.model_fields.keys()
    assert models.Issue.model_fields.keys() == server_models.Issue.model_fields.keys()
    assert config.FilterSpec.model_fields.keys() == server_settings.FilterSpec.model_fields.keys()


def test_detect_and_init_config(capsys, empty_repo):
    found = run_ct(capsys, "detect", "--repo", empty_repo)
    assert found["vcs"] == "git" and found["base_branch"] == "develop" and not found["has_config"]
    written = run_ct(capsys, "init-config", "--repo", empty_repo, "--url", "https://cov.example:8443/",
                     "--project", "P", "--stream", "P-main", "--strip-prefix", "C:/build/")
    assert len(written["written"]) == 3
    saved = yaml.safe_load((empty_repo / ".coverity-triage" / "config.yaml").read_text(encoding="utf-8"))
    assert saved["coverity"]["url"] == "https://cov.example:8443"
    assert saved["coverity"]["path_strip_prefixes"] == ["C:/build/"]
    assert saved["vcs"] == {"type": "git", "base_branch": "develop", "branch_prefix": "coverity-fix/"}
    assert saved["options"] == {"annotation": False, "per_run_branch": False, "knowledge_suggestions": False,
                                "metrics": False}
    server_settings.load_coverity_config(empty_repo)  # the MCP server accepts what was written
    first = yaml.safe_load((empty_repo / ".coverity-triage" / "filters" / "untriaged.yaml").read_text(encoding="utf-8"))
    assert first["streams"] == ["P-main"] and first["limit"] == 20
    assert (empty_repo / ".coverity-triage" / "knowledge.md").is_file()
    again = run_ct(capsys, "init-config", "--repo", empty_repo, "--url", "https://x", "--project", "P",
                   "--stream", "S", ok=False)
    assert "もうあります" in again["error"]


def test_init_config_refuses_output_inside_the_repository(capsys, empty_repo):
    refused = run_ct(capsys, "init-config", "--repo", empty_repo, "--url", "https://x", "--project", "P",
                     "--stream", "S", "--output-dir", "out", ok=False)
    assert "リポジトリの外" in refused["error"]


def test_install_agent(capsys, home):
    first = run_ct(capsys, "install-agent")
    assert first["status"] == "installed" and first["restart_needed"]
    assert Path(first["path"]).read_bytes() == prepare.WORKER_AGENT.read_bytes()
    assert run_ct(capsys, "install-agent")["status"] == "unchanged"
    Path(first["path"]).write_text("old", encoding="utf-8")
    assert run_ct(capsys, "install-agent")["status"] == "updated"


def test_doctor_lists_what_is_missing(capsys, empty_repo, monkeypatch):
    monkeypatch.delenv("COV_USER", raising=False)
    monkeypatch.setattr("ctlib.prepare.get_env", lambda name: None)
    report = run_ct(capsys, "doctor", "--repo", empty_repo)
    status = {c["check"]: c["status"] for c in report["checks"]}
    assert not report["ready"]
    assert status["リポジトリ"] == "ok" and status["設定ファイル"] == "ng"
    assert status["サブエージェント coverity-triage-worker"] == "ng"

    run_ct(capsys, "init-config", "--repo", empty_repo, "--url", "http://cov.example", "--project", "P",
           "--stream", "S")
    run_ct(capsys, "install-agent")
    report = run_ct(capsys, "doctor", "--repo", empty_repo)
    status = {c["check"]: c["status"] for c in report["checks"]}
    assert status["Coverity の認証キー"] == "ng" and status["Coverity の URL"] == "warning"
    assert status["取り込み先のブランチ"] == "ng"  # no commit on develop yet
    assert status["git のリモート origin"] == "warning"


def test_unknown_command_options_are_refused(capsys):
    with pytest.raises(SystemExit):
        ct.main(["new-run"])  # --repo is required
    capsys.readouterr()


def test_set_verify_keeps_the_header(capsys, empty_repo):
    run_ct(capsys, "init-config", "--repo", empty_repo, "--url", "https://x", "--project", "P", "--stream", "S")
    run_ct(capsys, "set-verify", "--repo", empty_repo, "--build-command", "make", "--build-dir", "src")
    text = (empty_repo / ".coverity-triage" / "config.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Coverity トリアージの設定")
    saved = yaml.safe_load(text)["verify"]
    assert saved["default"] == "build" and saved["build_command"] == "make" and saved["build_dir"] == "src"
