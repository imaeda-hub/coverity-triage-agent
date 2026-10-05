"""Start the server exactly as the plugin's mcp.json says and talk to it over stdio."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from conftest import PLUGIN as PLUGIN_ROOT
from conftest import SAMPLE
from test_server import CONFIG


def server_parameters() -> StdioServerParameters:
    entry = json.loads((PLUGIN_ROOT / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["coverity-triage"]
    assert entry["type"] == "stdio" and set(entry) <= {"type", "command", "args"}
    args = [a.replace("${PLUGIN_ROOT}", str(PLUGIN_ROOT)) for a in entry["args"]]
    assert not any("${" in a for a in args), "only ${PLUGIN_ROOT} is expanded by every client"
    return StdioServerParameters(command=entry["command"], args=args)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_server_starts_from_mcp_json(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".coverity-triage").mkdir(parents=True)
    shutil.copyfile(SAMPLE / ".coverity-triage" / "fake-issues.yaml", repo / ".coverity-triage" / "fake-issues.yaml")
    (repo / ".coverity-triage" / "config.yaml").write_text(CONFIG, encoding="utf-8")

    async def talk() -> None:
        with anyio.fail_after(120):
            await session_checks(repo)

    anyio.run(talk)


async def session_checks(repo: Path) -> None:
    async with stdio_client(server_parameters()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = sorted(t.name for t in (await session.list_tools()).tools)
        assert names == ["check_connection", "get_issues", "search_issues", "update_triage"]
        result = await session.call_tool("check_connection", {"repo_root": str(repo)})
        assert not result.is_error and result.structured_content["ok"]
        failed = await session.call_tool("check_connection", {"repo_root": "relative"})
        assert failed.is_error and "絶対パス" in failed.content[0].text
