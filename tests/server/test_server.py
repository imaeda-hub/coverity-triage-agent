"""MCP tools with the fake Coverity data (``coverity.api: fake``)."""

from __future__ import annotations

import hashlib
import json
import shutil
import types
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import SAMPLE
from coverity_triage import server
from coverity_triage.coverity import CoverityError, FakeCoverityClient
CONFIG = """coverity:
  url: https://coverity.example.co.jp:8443
  api: fake
  fake_data: fake-issues.yaml
"""


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / ".coverity-triage").mkdir(parents=True)
    shutil.copyfile(SAMPLE / ".coverity-triage" / "fake-issues.yaml", root / ".coverity-triage" / "fake-issues.yaml")
    (root / ".coverity-triage" / "config.yaml").write_text(CONFIG, encoding="utf-8")
    return root


@pytest.fixture
def run(tmp_path) -> Path:
    folder = tmp_path / "out" / "run1"
    folder.mkdir(parents=True)
    return folder


def write_filter(run: Path, **values) -> Path:
    path = run / "filter.json"
    path.write_text(json.dumps({"streams": ["Sample-main"], **values}), encoding="utf-8")
    return path


class Clock:
    """Stands in for ``time`` in the server module; each fake request takes ``step`` seconds."""

    def __init__(self, step: float):
        self.now = 0.0
        self.step = step

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def slow(monkeypatch):
    """Make every fake request take 8 seconds, so a 20-second call fits two."""
    clock = Clock(8.0)
    monkeypatch.setattr(server, "time", types.SimpleNamespace(monotonic=clock.monotonic))

    def timed(method):
        def call(self, *args, **kwargs):
            clock.now += clock.step
            self.requests.append({"request": method.__name__, "status": 200, "seconds": clock.step})
            return method(self, *args, **kwargs)
        return call

    for name in ("search", "issue_detail", "snapshot_revision", "write_triage"):
        monkeypatch.setattr(FakeCoverityClient, name, timed(getattr(FakeCoverityClient, name)))
    monkeypatch.setattr(FakeCoverityClient, "FIRST_REQUEST_SECONDS", clock.step)
    return clock


# ---- check_connection ---------------------------------------------------------------------------


def test_check_connection_with_fake_data(repo):
    out = server.check_connection(str(repo))
    assert out["ok"] and "fake" in out["result"]


def test_check_connection_without_settings(tmp_path):
    with pytest.raises(ToolError, match="設定ファイルがありません"):
        server.check_connection(str(tmp_path))


def test_relative_paths_are_refused(repo):
    with pytest.raises(ToolError, match="絶対パス"):
        server.check_connection("repo")


def test_unknown_coverity_setting_is_refused(repo):
    (repo / ".coverity-triage" / "config.yaml").write_text(CONFIG + "  colour: red\n", encoding="utf-8")
    with pytest.raises(ToolError, match="coverity の値"):
        server.check_connection(str(repo))


# ---- search_issues ------------------------------------------------------------------------------


def test_search_saves_issues_and_revision(repo, run):
    out = server.search_issues(str(repo), str(write_filter(run, limit=100)), str(run / "issues.json"))
    assert out["done"] and out["found"] == 6 and not out["limited"] and out["next"] is None
    saved = json.loads((run / "issues.json").read_text(encoding="utf-8"))
    assert [i["cid"] for i in saved["issues"]] == [20001, 20002, 20003, 20004, 20005, 20006]
    assert saved["issues"][0]["checker"] == "RESOURCE_LEAK" and saved["finished_at"]
    assert saved["revision_checked"] and saved["analyzed_revision"] is None  # empty in the sample


def test_search_stops_at_limit(repo, run):
    out = server.search_issues(str(repo), str(write_filter(run, limit=2)), str(run / "issues.json"))
    assert out["done"] and out["found"] == 2 and out["limited"]


def test_search_applies_the_filter(repo, run):
    flt = write_filter(run, impacts=["High"], checkers=["NULL_*"])
    out = server.search_issues(str(repo), str(flt), str(run / "issues.json"))
    assert out["found"] == 1


def test_search_needs_one_stream(repo, run):
    flt = run / "filter.json"
    flt.write_text(json.dumps({"streams": ["a", "b"]}), encoding="utf-8")
    with pytest.raises(ToolError, match="ストリームを 1 つ"):
        server.search_issues(str(repo), str(flt), str(run / "issues.json"))


def test_search_needs_an_existing_output_folder(repo, run):
    with pytest.raises(ToolError, match="フォルダがありません"):
        server.search_issues(str(repo), str(write_filter(run)), str(run / "missing" / "issues.json"))


def test_search_continues_when_time_runs_out(repo, run, slow):
    flt, issues = str(write_filter(run)), str(run / "issues.json")
    slow.step = 15.0  # the search takes 15 s; the revision (2 requests) no longer fits
    first = server.search_issues(str(repo), flt, issues)
    assert not first["done"] and first["found"] == 6 and first["next"]
    second = server.search_issues(str(repo), flt, issues)
    assert second["done"] and second["found"] == 6
    saved = json.loads(Path(issues).read_text(encoding="utf-8"))
    assert [r["request"] for r in saved["requests"]] == ["search", "snapshot_revision"]


def test_search_starts_over_when_the_filter_changes(repo, run):
    issues = str(run / "issues.json")
    server.search_issues(str(repo), str(write_filter(run, limit=2)), issues)
    out = server.search_issues(str(repo), str(write_filter(run, limit=3)), issues)
    assert out["found"] == 3


# ---- get_issues ---------------------------------------------------------------------------------


def test_get_issues_saves_each_cid(repo, run):
    item = run / "items" / "C001"
    item.mkdir(parents=True)
    out = server.get_issues(str(repo), "Sample-main", [20001, 20003], str(item))
    assert out["done"] and out["saved"] == [20001, 20003] and out["failed"] == {}
    detail = json.loads((item / "issue-20001.json").read_text(encoding="utf-8"))
    assert detail["issue"]["checker"] == "RESOURCE_LEAK"
    assert [e["line"] for e in detail["events"]] == [15, 19, 20]
    again = server.get_issues(str(repo), "Sample-main", [20001, 20003], str(item))
    assert again["saved"] == [] and again["already_saved"] == [20001, 20003]


def test_get_issues_records_a_cid_that_cannot_be_found(repo, run):
    out = server.get_issues(str(repo), "Sample-main", [99999], str(run))
    assert out["done"] and "99999" in out["failed"]
    assert "見つかりません" in json.loads((run / "issue-99999.json").read_text(encoding="utf-8"))["error"]


def test_get_issues_continues_when_time_runs_out(repo, run, slow):
    cids = [20004, 20005, 20006]
    first = server.get_issues(str(repo), "Sample-main", cids, str(run))
    assert not first["done"] and first["saved"] == [20004, 20005] and first["remaining"] == [20006]
    second = server.get_issues(str(repo), "Sample-main", cids, str(run))
    assert second["done"] and second["saved"] == [20006]


def test_get_issues_limits_the_number_of_cids(repo, run):
    with pytest.raises(ToolError, match="cids"):
        server.get_issues(str(repo), "Sample-main", [], str(run))


# ---- update_triage ------------------------------------------------------------------------------


def write_plan(run: Path, entries: list[dict]) -> tuple[Path, str]:
    path = run / "apply-plan.json"
    path.write_text(json.dumps({"triage": entries}, ensure_ascii=False), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()[:12]


ENTRY = {"id": "C002", "cids": [20002], "classification": "False Positive", "action": "Ignore",
         "severity": "Unspecified", "comment": "呼び出し元で検証済みのため誤検知"}


def writes(repo: Path) -> list[dict]:
    log = repo / ".coverity-triage" / "fake-issues.yaml.writes.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def test_update_triage_needs_the_matching_token(repo, run):
    plan, _ = write_plan(run, [ENTRY])
    with pytest.raises(ToolError, match="確認用の文字列"):
        server.update_triage(str(repo), str(plan), "000000000000")
    assert writes(repo) == []


def test_update_triage_writes_once(repo, run):
    plan, token = write_plan(run, [ENTRY, {**ENTRY, "id": "G1", "cids": [20004, 20005, 20006]}])
    out = server.update_triage(str(repo), str(plan), token)
    assert out["done"] and out["written"] == ["C002", "G1"] and out["written_total"] == 2
    assert [w["cids"] for w in writes(repo)] == [[20002], [20004, 20005, 20006]]
    assert writes(repo)[0]["comment"] == ENTRY["comment"]
    again = server.update_triage(str(repo), str(plan), token)
    assert again["done"] and again["written"] == [] and again["written_total"] == 2
    assert len(writes(repo)) == 2
    result = json.loads((run / "apply-plan.result.json").read_text(encoding="utf-8"))
    assert {v["id"] for v in result["written"].values()} == {"C002", "G1"}


def test_update_triage_writes_again_what_the_person_changed(repo, run):
    plan, token = write_plan(run, [ENTRY])
    server.update_triage(str(repo), str(plan), token)
    plan, token = write_plan(run, [{**ENTRY, "comment": "手直ししたコメント"}])
    out = server.update_triage(str(repo), str(plan), token)
    assert out["written"] == ["C002"] and writes(repo)[-1]["comment"] == "手直ししたコメント"


def test_update_triage_records_a_rejected_entry_and_goes_on(repo, run, monkeypatch):
    original = FakeCoverityClient.write_triage

    def reject_first(self, cids, attributes, comment):
        if cids == [20002]:
            raise CoverityError("Coverity REST PUT が失敗しました（400）: invalid value", fatal=False)
        return original(self, cids, attributes, comment)

    monkeypatch.setattr(FakeCoverityClient, "write_triage", reject_first)
    plan, token = write_plan(run, [ENTRY, {**ENTRY, "id": "C003", "cids": [20003]}])
    out = server.update_triage(str(repo), str(plan), token)
    assert out["done"] and out["written"] == ["C003"]
    assert [f["id"] for f in out["failed"]] == ["C002"] and "400" in out["failed"][0]["error"]
    # Not retried with the same token; a new preview (new token) tries again.
    again = server.update_triage(str(repo), str(plan), token)
    assert again["written"] == [] and [f["id"] for f in again["failed"]] == ["C002"]


def test_update_triage_continues_when_time_runs_out(repo, run, slow):
    entries = [{**ENTRY, "id": f"C00{n}", "cids": [20000 + n]} for n in (1, 2, 3)]
    plan, token = write_plan(run, entries)
    first = server.update_triage(str(repo), str(plan), token)
    assert not first["done"] and first["written"] == ["C001", "C002"] and first["remaining"] == ["C003"]
    second = server.update_triage(str(repo), str(plan), token)
    assert second["done"] and second["written"] == ["C003"] and second["written_total"] == 3


def test_update_triage_refuses_an_incomplete_entry(repo, run):
    plan, token = write_plan(run, [{"id": "C001", "cids": [20001], "classification": "", "action": "Ignore"}])
    with pytest.raises(ToolError, match="反映の内容を読めません"):
        server.update_triage(str(repo), str(plan), token)
