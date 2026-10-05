"""The marketplace entry and the plugin manifest must agree (design 6)."""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]


def test_marketplace_points_to_the_plugin():
    market = json.loads((REPO / ".github" / "plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert market["name"] and market["owner"]["name"]
    [entry] = market["plugins"]
    plugin_dir = REPO / entry["source"]
    manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    assert entry["name"] == manifest["name"]
    assert entry["version"] == manifest["version"]
    assert entry["description"] == manifest["description"]


PLUGIN = REPO / "plugins" / "coverity-triage"
SPEC = "https://agent-plugins.org/schemas/1.0.0/"


def test_manifests_follow_agent_plugins_1_0():
    """Agent Plugins 1.0 §5 and §7.2: wrong $schema or fields disable the plugin or its MCP."""
    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["$schema"] == SPEC + "plugin.schema.json"
    assert set(manifest) <= {"$schema", "name", "version", "description", "author", "homepage",
                             "repository", "license", "keywords", "extensions"}
    mcp = json.loads((PLUGIN / "mcp.json").read_text(encoding="utf-8"))
    assert mcp["$schema"] == SPEC + "mcp.schema.json"
    assert set(mcp) == {"$schema", "mcpServers"}
    for server in mcp["mcpServers"].values():
        assert server["type"] == "stdio"
        assert set(server) <= {"type", "command", "args", "env", "cwd"}
        assert " " not in server["command"]  # a single executable token


ENTRY_SKILLS = {"coverity-setup", "coverity-run", "coverity-apply", "coverity-help", "coverity-selftest"}


def _frontmatter(path):
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])


def test_skills_entry_points_and_knowledge():
    """Entry points are user-only skills; knowledge skills stay out of the / menu (D-69)."""
    skills = {p.parent.name: _frontmatter(p) for p in (PLUGIN / "skills").glob("*/SKILL.md")}
    assert ENTRY_SKILLS <= set(skills)
    for name, fm in skills.items():
        assert fm["name"] == name and fm["description"]
        if name in ENTRY_SKILLS:
            assert fm.get("disable-model-invocation") is True
        else:
            assert fm.get("user-invocable") is False
    assert not (PLUGIN / "com.github.copilot" / "commands").exists()


def test_worker_agent_tools_exist():
    import anyio

    from coverity_triage import mcp_server
    tools = {t.name for t in anyio.run(mcp_server.mcp.list_tools)}
    [agent] = (PLUGIN / "com.github.copilot" / "agents").glob("*.agent.md")
    fm = _frontmatter(agent)
    assert fm["name"] == "coverity-triage-worker" and fm["user-invocable"] is False
    refs = [t.split("/", 1)[1] for t in fm["tools"] if t.startswith("coverity-triage/")]
    assert refs and set(refs) <= tools
    assert set(fm["tools"]) - {"read", "skill"} == {f"coverity-triage/{r}" for r in refs}
    assert {"read", "skill"} <= set(fm["tools"])  # needed to load the investigation skills
    assert "apply_approvals" not in refs
    from coverity_triage import selftest
    assert sorted(selftest.WORKER_TOOLS) == sorted(refs)  # what /coverity-selftest expects (1-6)


def test_skill_file_links_stay_inside_the_skill():
    """Agent Skills: reference files with paths relative to the skill root."""
    import re
    for path in (PLUGIN / "skills").glob("*/SKILL.md"):
        for target in re.findall(r"\]\(([^)#]+)\)", path.read_text(encoding="utf-8")):
            if "://" in target:
                continue
            assert not target.startswith(("../", "/")), (path, target)
            assert (path.parent / target).is_file(), (path, target)


def test_mcp_server_keeps_its_environment_in_plugin_data():
    """Agent Plugins §9.1: virtual environments belong in PLUGIN_DATA, not the plugin root."""
    server = json.loads((PLUGIN / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["coverity-triage"]
    assert server["env"]["UV_PROJECT_ENVIRONMENT"].startswith("${PLUGIN_DATA}/")
    assert "--frozen" in server["args"]
