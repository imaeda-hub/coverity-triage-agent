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
