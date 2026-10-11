"""Command coverity-triage-call: the read tools of the MCP server for the dynamic workflow."""

from __future__ import annotations

import io
import json

import pytest

from coverity_triage import cli
from test_server import repo, run, write_filter  # noqa: F401 - fixtures


def call(monkeypatch, capsys, name: str, arguments) -> tuple[int, dict]:
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(arguments).encode("utf-8")),
                                                       encoding="utf-8"))
    code = cli.main([name])
    return code, json.loads(capsys.readouterr().out)


def test_search_and_get_issues(monkeypatch, capsys, repo, run):  # noqa: F811
    output = run / "issues.json"
    code, found = call(monkeypatch, capsys, "search_issues", {"repo_root": str(repo),
                                                              "filter_file": str(write_filter(run)),
                                                              "output_file": str(output)})
    assert code == 0 and found["ok"] and found["done"] and found["found"] == 6
    cid = json.loads(output.read_text(encoding="utf-8"))["issues"][0]["cid"]
    code, got = call(monkeypatch, capsys, "get_issues", {"repo_root": str(repo), "stream": "Sample-main",
                                                         "cids": [cid], "output_dir": str(run)})
    assert code == 0 and got["saved"] == [cid] and (run / f"issue-{cid}.json").is_file()


@pytest.mark.parametrize("name, arguments, message", [
    ("check_connection", {"repo_root": "relative"}, "絶対パス"),
    ("check_connection", {}, "引数が合いません"),
    ("update_triage", {}, "どれか"),  # writing back is only for /coverity-apply
    ("search_issues", [], "オブジェクト"),
])
def test_refusals(monkeypatch, capsys, name, arguments, message):
    code, out = call(monkeypatch, capsys, name, arguments)
    assert code == 1 and not out["ok"] and message in out["error"]
