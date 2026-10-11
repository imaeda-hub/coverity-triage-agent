"""The dynamic workflow coverity-triage-run (Copilot CLI): its diagram (docs/dynamic-workflow.md) and extension.mjs
agree, and the workflow runs /coverity-run's steps from start to summary with the fake Coverity data.

extension.mjs is loaded by Node with a test double for the SDK the Copilot CLI provides (tests/workflow/), whose
context follows the SDK's docs/workflows.md. The four role agents are played by the harness.
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

WORKFLOW_MD = REPO / "docs" / "dynamic-workflow.md"
EXTENSION = PLUGIN / "com.github.copilot" / "extensions" / "coverity-triage-run" / "extension.mjs"
HARNESS = REPO / "tests" / "workflow" / "harness.mjs"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None or shutil.which("uv") is None, reason="node and uv are needed")

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


def md_section(title: str) -> str:
    text = WORKFLOW_MD.read_text(encoding="utf-8")
    match = re.search(rf"^##+ {re.escape(title)}\n(.*?)(?=^##+ |\Z)", text, re.M | re.S)
    assert match, title
    return match.group(1)


def table_column(section: str, column: int = 0) -> list[str]:
    rows = [line.split("|")[1:-1] for line in section.splitlines() if line.startswith("|")]
    return [r[column].strip().strip("`") for r in rows[2:]]  # skip the header and the rule


def mermaid_nodes() -> set[str]:
    blocks = re.findall(r"```mermaid\n(.*?)```", WORKFLOW_MD.read_text(encoding="utf-8"), re.S)
    # Node ids (A1, L5, ...) are mermaid identifiers: in the source, not in the rendered picture.
    return {n for block in blocks for n in re.findall(r"(?<![\w\[])([A-Z][0-9]+)(?=[\[{(])", block)}


# ---- the structure file and extension.mjs agree --------------------------------------------------


def test_every_node_of_the_diagrams_is_marked_in_extension_mjs():
    nodes = mermaid_nodes()
    marked = set(re.findall(r"// \[([A-Z][0-9]+)\]", EXTENSION.read_text(encoding="utf-8")))
    assert nodes and nodes == marked


def test_phases_and_phase_calls_match_the_structure_file():
    phases = table_column(md_section("画面に出る段階"))
    source = EXTENSION.read_text(encoding="utf-8")
    assert re.findall(r'\{ title: "([^"]+)", detail:', source) == phases
    assert sorted(set(re.findall(r'ctx\.phase\("([^"]+)"\)', source))) == sorted(phases)


def harness(tmp_path: Path, extension: Path, **config) -> dict:
    path = tmp_path / "harness.json"
    path.write_text(json.dumps({"extension": str(extension), **config}, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run([NODE, str(HARNESS), str(path)], capture_output=True, text=True, encoding="utf-8",
                          timeout=600)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@needs_node
def test_registration_matches_the_structure_file(tmp_path):
    registered = harness(tmp_path, EXTENSION, mode="meta")
    assert registered["requestedEnvironmentVariables"] == ["COV_USER", "COV_AUTH_KEY"]
    [meta] = registered["workflows"]
    assert meta["name"] == "coverity-triage-run" and "limits" not in meta
    assert [p["title"] for p in meta["phases"]] == table_column(md_section("画面に出る段階"))
    assert list(meta["argsSchema"]["properties"]) == table_column(md_section("始めるときに渡すもの"))
    # Only the keywords the SDK honours (docs/workflows.md, WorkflowJsonSchema).
    honoured = {"type", "required", "enum", "const", "properties", "items", "anyOf", "oneOf", "allOf"}

    def keywords(schema):
        if isinstance(schema, dict):
            assert set(schema) <= honoured, schema
            for key, value in schema.items():
                if key == "properties":
                    for sub in value.values():
                        keywords(sub)
                elif key in ("items",):
                    keywords(value)
                elif key in ("anyOf", "oneOf", "allOf"):
                    for sub in value:
                        keywords(sub)

    keywords(meta["argsSchema"])


# ---- the workflow runs ----------------------------------------------------------------------------


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(SAMPLE, root)
    config = root / ".coverity-triage" / "config.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["output_dir"] = str(tmp_path / "out")
    data["parallel"] = 2
    config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    git("init", "-q", "-b", "main", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "sample", cwd=root)
    git("remote", "add", "origin", str(origin), cwd=root)
    git("push", "-q", "origin", "main", cwd=root)
    return root


@needs_node
def test_workflow_runs_from_search_to_summary(repo, tmp_path):
    out = harness(tmp_path, EXTENSION, mode="run", args={"repo_root": str(repo), "filter": "all.yaml"},
                  result=RESULT, bad_first=["20002"])
    assert out["ok"], out
    result = out["result"]
    assert out["phases"] == ["準備", "検索", "計画", "調査", "一覧"]
    assert result["found"] == 6 and result["errors"] == [] and result["warnings"] == []
    assert result["counts"]["done"] == 4 and result["recommended"] == {"fix": 4, "deviation": 0}
    assert Path(result["summary"]).is_file()
    assert "20002: 修正を推奨（確信度 高）" in out["logs"]

    labels = sorted(a["label"] for a in out["agents"] if a["label"].startswith("20002#"))
    # the reviewer sent 20002's fix back once: only the fixer redoes it, then the reviewer looks again
    assert labels == ["20002#1-deviation", "20002#1-fix", "20002#1-fix1", "20002#1-investigate",
                      "20002#1-review", "20002#1-review1"]
    assert len(out["agents"]) == 4 * 4 + 2
    assert {a["agent"] for a in out["agents"]} == {f"coverity-triage-{r}" for r in
                                                   ("investigator", "fixer", "deviation", "reviewer")}
    assert "20002: 確認役の指摘で直します（1 回目）" in out["logs"]

    # Running it again with run_dir finds nothing left to do and keeps the summary.
    again = harness(tmp_path, EXTENSION, mode="run", args={"run_dir": result["run_dir"]}, result=RESULT)
    assert again["ok"], again
    assert again["agents"] == [] and again["result"]["counts"]["done"] == 4


@needs_node
def test_workflow_needs_repo_root_or_run_dir(tmp_path):
    out = harness(tmp_path, EXTENSION, mode="run", args={})
    assert not out["ok"] and "repo_root" in out["error"]


def test_extension_is_where_agent_plugins_ship_extensions():
    # Copilot CLI 1.0.79: "Agent Plugins spec plugins can now ship extensions under a com.github.copilot/extensions/
    # directory"; each extension is a subdirectory with extension.mjs (docs: About extensions for Copilot CLI).
    folders = [p for p in (PLUGIN / "com.github.copilot" / "extensions").iterdir()]
    assert [p.name for p in folders] == ["coverity-triage-run"] and EXTENSION.is_file()
    assert {p.name for p in EXTENSION.parent.iterdir()} == {"extension.mjs"}


def test_agents_in_the_diagram_are_the_agents_extension_mjs_calls():
    drawn = table_column(md_section("呼ぶ AI"), 1)
    block = re.search(r"^const AGENTS = \{\n(.*?)^\};", EXTENSION.read_text(encoding="utf-8"), re.M | re.S).group(1)
    called = re.findall(r'"(coverity-triage-[a-z]+)"', block)
    assert drawn == called
    for name in called:
        assert (PLUGIN / "com.github.copilot" / "agents" / f"{name}.agent.md").is_file(), name
