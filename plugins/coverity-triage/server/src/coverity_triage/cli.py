"""Command ``coverity-triage-call TOOL``: run one read tool of this MCP server as a plain command.

For the dynamic workflow coverity-triage-run, whose extension code cannot call MCP tools (the Copilot SDK
documents no such call). The tool's arguments come as one JSON object on stdin; the result goes to stdout as
one JSON object, with ``"ok": false`` and exit code 1 when the tool refused. Writing back to Coverity is not
offered: that stays with /coverity-apply, after a person agreed.
"""

from __future__ import annotations

import json
import sys

from mcp.server.mcpserver.exceptions import ToolError

from .server import check_connection, get_issues, search_issues

TOOLS = {f.__name__: f for f in (check_connection, search_issues, get_issues)}


def run(name: str, text: str) -> dict:
    if name not in TOOLS:
        return {"ok": False, "error": f"使えるツールは {', '.join(TOOLS)} です: {name}"}
    try:
        arguments = json.loads(text or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("JSON のオブジェクトではありません")
        return {"ok": True, **TOOLS[name](**arguments)}
    except (ValueError, TypeError, ToolError) as exc:
        return {"ok": False, "error": str(exc)}


def main() -> int:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    result = run(sys.argv[1] if len(sys.argv) == 2 else "", sys.stdin.read())
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1
