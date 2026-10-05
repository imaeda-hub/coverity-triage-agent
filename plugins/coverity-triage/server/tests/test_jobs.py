"""Long tools return within the client's 30-second limit (spec D-83) and the auth check (D-84)."""

import threading

import anyio
import httpx
import pytest

from coverity_triage import connect, jobs, mcp_server
from coverity_triage.config import CoverityConfig


@pytest.fixture
def short_wait(monkeypatch):
    monkeypatch.setattr(jobs, "WAIT_SECONDS", 0.2)


def test_fast_job_returns_its_result_directly():
    assert jobs.run("add", lambda a, b: {"sum": a + b}, 1, 2) == {"sum": 3}


def test_slow_job_returns_running_then_result(short_wait):
    release = threading.Event()
    calls = []

    def slow(x):
        calls.append(x)
        release.wait(5)
        return {"value": x}

    first = jobs.run("slow", slow, 7)
    assert first["status"] == "running" and "wait_job" in first["next"]
    again = jobs.run("slow", slow, 7)  # the same call joins the running job
    assert again["job_id"] == first["job_id"]
    assert jobs.wait(first["job_id"])["status"] == "running"
    release.set()
    assert jobs.wait(first["job_id"]) == {"value": 7}
    assert calls == [7]
    with pytest.raises(ValueError, match="見つかりません"):
        jobs.wait(first["job_id"])


def test_errors_reach_the_caller(short_wait):
    release = threading.Event()

    def fails():
        release.wait(5)
        raise ValueError("だめでした")

    job_id = jobs.run("fails", fails)["job_id"]
    release.set()
    with pytest.raises(ValueError, match="だめでした"):
        jobs.wait(job_id)


def test_wait_job_tool_and_tool_errors(short_wait, tmp_path):
    async def call(name, args):
        return await mcp_server.mcp.call_tool(name, args)
    with pytest.raises(Exception, match="見つかりません"):
        anyio.run(call, "wait_job", {"job_id": "nope"})
    with pytest.raises(Exception, match="ファイルが見つかりません|config"):
        anyio.run(call, "start_run", {"repo_root": str(tmp_path), "filter_file": "x.yaml"})


# ---- auth check -------------------------------------------------------------------------------


def config():
    return CoverityConfig(url="http://cov.example:8080")


def responder(status=200, exc=None):
    def handle(request):
        if exc is not None:
            raise exc
        assert request.url.path == "/api/v2/issues/columns" and "authorization" in request.headers
        return httpx.Response(status, json=[])
    return httpx.MockTransport(handle)


def test_check_auth(monkeypatch):
    monkeypatch.delenv("COV_USER", raising=False)
    monkeypatch.delenv("COV_AUTH_KEY", raising=False)
    missing = connect.check_auth(config(), responder())
    assert not missing["ok"] and "なし" in missing["user"] and "設定されていません" in missing["result"]

    monkeypatch.setenv("COV_USER", "alice")
    monkeypatch.setenv("COV_AUTH_KEY", "k" * 32)
    ok = connect.check_auth(config(), responder())
    assert ok["ok"] and "32 文字" in ok["key"] and "k" * 32 not in str(ok) and ok["seconds"] is not None
    assert "401" in connect.check_auth(config(), responder(401))["result"]
    slow = connect.check_auth(config(), responder(exc=httpx.ReadTimeout("slow")))
    assert not slow["ok"] and "10 秒以内に応答がありません" in slow["result"]
    down = connect.check_auth(config(), responder(exc=httpx.ConnectError("refused")))
    assert not down["ok"] and "接続できません" in down["result"]
