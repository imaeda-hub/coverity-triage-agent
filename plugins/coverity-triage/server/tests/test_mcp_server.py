import anyio

from coverity_triage import mcp_server


def test_tools_are_registered_with_schemas():
    tools = anyio.run(mcp_server.mcp.list_tools)
    names = {t.name for t in tools}
    assert {"start_run", "next_work_item", "edit_source", "submit_result",
            "preview_apply", "apply_approvals", "get_stats"} <= names
    edit = next(t for t in tools if t.name == "edit_source")
    assert set(edit.input_schema["required"]) == {"run_dir", "item_id", "workspace", "path", "old_text", "new_text"}


def test_expected_errors_become_tool_errors(tmp_path):
    async def call():
        return await mcp_server.mcp.call_tool("get_run_status", {"run_dir": str(tmp_path)})
    try:
        anyio.run(call)
    except Exception as exc:
        assert "実行フォルダではありません" in str(exc)
    else:
        raise AssertionError("error expected")
