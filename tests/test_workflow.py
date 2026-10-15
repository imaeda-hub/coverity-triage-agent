"""Dynamic workflow coverity-triage-run (Copilot CLI): its diagram agrees with extension.mjs, the command
coverity-triage-call works, and the workflow runs from search to summary with the fake Coverity data.

extension.mjs runs on Node with a test double of the SDK (tests/workflow/); the role agents are played there.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import PLUGIN, REPO, SAMPLE
from coverity_triage import cli

DIAGRAM = REPO / "docs" / "dynamic-workflow.md"
EXTENSION = PLUGIN / "com.github.copilot" / "extensions" / "coverity-triage-run" / "extension.mjs"
AGENTS = PLUGIN / "com.github.copilot" / "agents"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None or shutil.which("uv") is None, reason="node and uv are needed")

RESULT = {
    "verdict": {"judgement": "true_bug", "summary": "fgets が失敗したとき fp を閉じていない",
                "rationale": "fgets が NULL を返すと return し、fclose を通らない。",
                "evidence": [{"file": "src/reader.c", "line": 20, "note": "fclose の前に return"}]},
    "recommendation": "fix", "confidence": "high", "confidence_reason": "警告経路どおりに成立する",
    "revision_drift": {"status": "none", "detail": ""},
    "deviation": {"classification": "Bug", "action": "Fix Required", "severity": "Major",
                  "comment": "判定区分：本物のバグ。理由：fgets 失敗時に fp を閉じていない。"},
    "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Major",
            "summary": "return の前に fclose(fp) を追加", "impact": "関数の外から見た動きは変わらない",
            "exceeded_constraints": [], "already_fixed_on_latest": False},
}


def section(title: str) -> str:
    text = DIAGRAM.read_text(encoding="utf-8")
    return re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.M | re.S).group(1)


def column(title: str, index: int = 0) -> list[str]:
    rows = [line.split("|")[1:-1] for line in section(title).splitlines() if line.startswith("|")][2:]
    return [row[index].strip().strip("`") for row in rows]


def harness(tmp_path: Path, **config) -> dict:
    path = tmp_path / "harness.json"
    path.write_text(json.dumps({"extension": str(EXTENSION), **config}, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run([NODE, str(REPO / "tests" / "workflow" / "harness.mjs"), str(path)],
                          capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ---- the diagram is extension.mjs ---------------------------------------------------------------------------


def test_every_box_of_the_diagram_is_marked_in_extension_mjs():
    blocks = re.findall(r"```mermaid\n(.*?)```", DIAGRAM.read_text(encoding="utf-8"), re.S)
    boxes = {box for block in blocks for box in re.findall(r"\b([A-Z][0-9]+)[\[{(]", block)}
    marks = set(re.findall(r"// \[([A-Z][0-9]+)\]", EXTENSION.read_text(encoding="utf-8")))
    assert boxes and boxes == marks


def test_agents_of_the_diagram_are_the_agents_extension_mjs_calls():
    block = re.search(r"^const AGENTS = \{\n(.*?)^\};", EXTENSION.read_text(encoding="utf-8"), re.M | re.S).group(1)
    names = re.findall(r'"(coverity-triage-[a-z]+)"', block)
    assert column("呼ぶ AI", 1) == names
    for name in names:
        text = (AGENTS / f"{name}.agent.md").read_text(encoding="utf-8")
        meta = yaml.safe_load(text.split("---")[1])
        assert meta["name"] == name and meta["user-invocable"] is False
        assert set(meta["tools"]) <= {"read", "search", "edit", "skill"}  # no terminal, no MCP tools


@needs_node
def test_registration_matches_the_diagram(tmp_path):
    registered = harness(tmp_path, mode="meta")
    [meta] = registered["metas"]
    assert meta["name"] == "coverity-triage-run" and "limits" not in meta
    assert [p["title"] for p in meta["phases"]] == column("画面に出る段階")
    assert list(meta["argsSchema"]["properties"]) == column("始めるときに渡すもの")
    assert registered["requestedEnvironmentVariables"] == ["COV_USER", "COV_AUTH_KEY"]


# ---- coverity-triage-call ------------------------------------------------------------------------------------


def coverity_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "plain"
    (repo / ".coverity-triage").mkdir(parents=True)
    shutil.copy(SAMPLE / ".coverity-triage" / "fake-issues.yaml", repo / ".coverity-triage")
    (repo / ".coverity-triage" / "config.yaml").write_text(
        "coverity:\n  url: https://coverity.example.co.jp\n  api: fake\n  fake_data: fake-issues.yaml\n", encoding="utf-8")
    return repo


def test_call_runs_the_read_tools(tmp_path):
    repo = coverity_repo(tmp_path)
    filter_file = tmp_path / "filter.json"
    filter_file.write_text(json.dumps({"streams": ["Sample-main"]}), encoding="utf-8")
    found = cli.run("search_issues", json.dumps({"repo_root": str(repo), "filter_file": str(filter_file),
                                                 "output_file": str(tmp_path / "issues.json")}))
    assert found["ok"] and found["done"] and found["found"] == 6
    got = cli.run("get_issues", json.dumps({"repo_root": str(repo), "stream": "Sample-main", "cids": [20001],
                                             "output_dir": str(tmp_path)}))
    assert got["ok"] and got["saved"] == [20001]


@pytest.mark.parametrize("name, text", [("update_triage", "{}"), ("check_connection", '{"repo_root": "x"}'),
                                         ("check_connection", "{}"), ("check_connection", "[]")])
def test_call_refuses(name, text):
    out = cli.run(name, text)
    assert out["ok"] is False and out["error"]


# ---- the workflow runs ---------------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(SAMPLE, root)
    config = root / ".coverity-triage" / "config.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data.update(output_dir=str(tmp_path / "out"), parallel=2)
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    git = {"cwd": root, "check": True, "capture_output": True}
    subprocess.run(["git", "init", "-q", "--bare", str(tmp_path / "origin.git")], **git)
    subprocess.run(["git", "init", "-q", "-b", "main"], **git)
    subprocess.run(["git", "add", "-A"], **git)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "sample"], **git)
    subprocess.run(["git", "remote", "add", "origin", str(tmp_path / "origin.git")], **git)
    subprocess.run(["git", "push", "-q", "origin", "main"], **git)
    return root


@needs_node
def test_workflow_runs_from_search_to_summary(repo, tmp_path, monkeypatch):
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "t@example.com" if "EMAIL" in key else "t")
    out = harness(tmp_path, mode="run", args={"repo_root": str(repo), "filter": "all.yaml"}, result=RESULT,
                  send_back=["20002"])
    assert out["ok"], out
    result = out["result"]
    assert out["phases"] == ["準備", "検索", "計画", "調査", "一覧"]
    assert result["found"] == 6 and result["counts"]["done"] == 4 and result["errors"] == []
    assert result["recommended"] == {"fix": 4, "deviation": 0} and Path(result["summary"]).is_file()
    labels = sorted(c["label"] for c in out["calls"] if c["label"].startswith("20002#"))
    assert labels == ["20002#1-deviation", "20002#1-fix", "20002#1-fix1", "20002#1-investigate",
                      "20002#1-review0", "20002#1-review1"]  # only the fixer redid its part
    assert "20002: 確認役の指摘で直します（1 回目）" in out["logs"]

    again = harness(tmp_path, mode="run", args={"run_dir": result["run_dir"]}, result=RESULT)
    assert again["ok"] and again["calls"] == [] and again["result"]["counts"]["done"] == 4


@needs_node
def test_workflow_needs_repo_root_or_run_dir(tmp_path):
    out = harness(tmp_path, mode="run", args={})
    assert not out["ok"] and "repo_root" in out["error"]
