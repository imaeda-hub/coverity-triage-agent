"""The plugin's files follow the formats the clients read, and the skills name only what exists."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
import yaml

import ct
from conftest import PLUGIN, REPO
from coverity_triage import server

ENTRY_SKILLS = {"coverity-setup", "coverity-run", "coverity-apply", "coverity-help", "coverity-selftest"}
AGENT_SKILLS = {"coverity-triage-scripts"}
WORKER_SKILLS = {"triage-investigation", "checker-knowledge", "code-fix", "deviation-comment", "triage-report"}
WORKER = PLUGIN / "com.github.copilot" / "agents" / "coverity-triage-worker.agent.md"


def frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert match, f"{path} has no frontmatter"
    return yaml.safe_load(match.group(1)), match.group(2)


def skill_files() -> dict[str, Path]:
    return {p.parent.name: p for p in (PLUGIN / "skills").glob("*/SKILL.md")}


def test_plugin_manifest_and_marketplace():
    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    # Agent Plugins 1.0: a closed manifest with $schema and name required.
    assert set(manifest) <= {"$schema", "name", "version", "description", "author", "homepage", "repository",
                             "license", "keywords", "extensions"}
    assert manifest["$schema"].startswith("https://agent-plugins.org/schemas/1.0.0/")
    marketplace = json.loads((REPO / ".github" / "plugin" / "marketplace.json").read_text(encoding="utf-8"))
    entry = marketplace["plugins"][0]
    assert entry["name"] == manifest["name"] and entry["version"] == manifest["version"]
    assert (REPO / entry["source"]).resolve() == PLUGIN.resolve()


def test_mcp_json_uses_only_what_every_client_expands():
    config = json.loads((PLUGIN / "mcp.json").read_text(encoding="utf-8"))
    assert config["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    entry = config["mcpServers"]["coverity-triage"]
    assert set(entry) == {"type", "command", "args"}
    text = json.dumps(entry)
    assert "${PLUGIN_DATA}" not in text  # VS Code does not expand it
    assert entry["args"][-1] == "coverity-triage-mcp"
    assert "--native-tls" in entry["args"]  # company proxies replace certificates; use the OS store


def test_skills_frontmatter():
    skills = skill_files()
    assert set(skills) == ENTRY_SKILLS | AGENT_SKILLS | WORKER_SKILLS
    for name, path in skills.items():
        meta, _ = frontmatter(path)
        assert meta["name"] == name and re.fullmatch(r"[a-z0-9-]{1,64}", name)
        assert 0 < len(meta["description"]) <= 1024
        if name in ENTRY_SKILLS:
            assert meta.get("disable-model-invocation") is True, name
            assert meta.get("allowed-tools") == ["coverity-triage", "shell(uv run:*)"], name
        else:
            assert meta.get("user-invocable") is False, name
            assert "allowed-tools" not in meta, name


def test_worker_agent():
    meta, body = frontmatter(WORKER)
    assert WORKER.name == f"{meta['name']}.agent.md"
    assert meta["tools"] == ["read", "search", "edit", "skill"]  # no terminal, no MCP tools
    assert meta["model"] == "GPT-6 Luna" and meta["models"] == ["gpt-6-luna"] and meta["modelPolicy"] == "required"
    assert meta["user-invocable"] is False
    for skill in WORKER_SKILLS:
        assert f"`{skill}`" in body, skill


def test_skills_name_only_existing_commands_and_tools():
    parser = ct.parser()
    commands = set(parser._subparsers._group_actions[0].choices)  # noqa: SLF001 - argparse has no public API for this
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    texts = {p: p.read_text(encoding="utf-8") for p in (PLUGIN / "skills").rglob("*.md")}
    texts[WORKER] = WORKER.read_text(encoding="utf-8")
    for path, text in texts.items():
        for command in re.findall(r"ct\.py ([a-z][a-z-]+)", text):
            assert command in commands, f"{path}: ct.py {command}"
        for tool in re.findall(r"MCP の `([a-z_]+)`", text):
            assert tool in tools, f"{path}: MCP {tool}"
        for old in ("read_source", "edit_source", "save_fix", "submit_result", "wait_job", "start_run", "GITHUB_TOKEN"):
            assert old not in text, f"{path}: {old}"


@pytest.mark.parametrize("name", sorted(ENTRY_SKILLS))
def test_entry_skills_read_the_scripts_skill(name):
    _, body = frontmatter(skill_files()[name])
    if "ct.py" in body:
        assert "`coverity-triage-scripts`" in body
