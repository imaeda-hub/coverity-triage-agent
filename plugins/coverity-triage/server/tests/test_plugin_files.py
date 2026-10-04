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
