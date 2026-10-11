"""Command ``coverity-triage-call``: one read tool of the MCP server, run outside MCP.

The dynamic workflow ``coverity-triage-run`` (an extension of the Copilot CLI) has no official way to
call an MCP tool, so it runs this command as a child process instead. The arguments are the tool's
arguments as one JSON object on stdin; the result is one JSON object on stdout (``"ok": false`` and
exit code 1 when the tool refused). Writing back to Coverity (``update_triage``) is not offered here:
it is done only by /coverity-apply after a person agreed.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import server

TOOLS = {"check_connection": server.check_connection, "search_issues": server.search_issues,
         "get_issues": server.get_issues}


def call(name: str, arguments: Any) -> dict[str, Any]:
    tool = TOOLS.get(name)
    if tool is None:
        return {"ok": False, "error": f"ツールの名前は {', '.join(TOOLS)} のどれかです: {name}"}
    if not isinstance(arguments, dict):
        return {"ok": False, "error": "引数は JSON のオブジェクトで渡してください"}
    try:
        return {"ok": True, **tool(**arguments)}
    except ToolError as exc:
        return {"ok": False, "error": str(exc)}
    except TypeError as exc:  # missing or unknown argument
        return {"ok": False, "error": f"引数が合いません: {exc}"}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")  # the caller reads and writes UTF-8 on every OS
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        result = {"ok": False, "error": "使い方: coverity-triage-call <ツールの名前>（引数の JSON は標準入力）"}
    else:
        try:
            arguments = json.loads(sys.stdin.read() or "{}")
        except ValueError as exc:
            result = {"ok": False, "error": f"引数の JSON を読めません: {exc}"}
        else:
            result = call(args[0], arguments)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
