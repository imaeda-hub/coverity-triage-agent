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


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_probe_then_initialize_like_copilot():
    """An auto-negotiating client probes server/discover (2026-07-28) and then falls back to
    initialize on the same connection. The handshake must still be accepted."""
    import json
    import subprocess

    params = server_parameters()
    proc = subprocess.Popen([params.command, *params.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, encoding="utf-8")

    def send(message: dict) -> dict:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    try:
        meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
                "io.modelcontextprotocol/clientCapabilities": {}}
        probe = send({"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}})
        assert "error" in probe and probe["error"]["code"] != -32022
        init = send({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                     "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "0"}}})
        assert init["result"]["protocolVersion"] == "2025-11-25"
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        tools = send({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
        assert len(tools["result"]["tools"]) == 4
    finally:
        proc.stdin.close()
        proc.wait(30)
