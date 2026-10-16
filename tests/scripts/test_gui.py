"""The screen of ct.py gui: the settings it reads and writes, the results and the monitor of the runs, and
its web server (one per repository, reused until the person stops it)."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import quote, urlsplit

import pytest
import yaml

from coverity_triage import settings as server_settings
from ctlib import activity, approvals, config, envvars, gui, prepare
from ctlib.run import ItemState, Run
from test_flow import (RESULT, git, git_identity, repo, run_ct, start_run, summary_rows,  # noqa: F401 - fixtures
                       work_item)
from test_options import set_config
from test_reports import DEVIATION, two_items

KEY = "s3cr3t-key-value"


@pytest.fixture
def product(tmp_path) -> Path:
    root = tmp_path / "product"
    root.mkdir()
    git("init", "-q", "-b", "develop", cwd=root)
    (root / "a.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "first", cwd=root)
    return root


@pytest.fixture
def saved_env(monkeypatch) -> dict[str, str]:
    """User environment variables as if on Windows, kept in a dict."""
    store: dict[str, str] = {}
    monkeypatch.setattr(envvars, "can_set_user_env", lambda: True)
    monkeypatch.setattr(envvars, "set_user_env", store.__setitem__)
    monkeypatch.setattr(envvars, "get_env", store.get)
    monkeypatch.setattr(prepare, "get_env", store.get)
    return store


def new_settings(state: dict, **changes) -> dict:
    """What the screen sends for a new setup: the defaults it shows, plus what the person typed."""
    cfg = json.loads(json.dumps(state["config"]))
    cfg["coverity"]["url"] = "https://cov.example:8443/"
    for path, value in changes.items():
        *parents, last = path.split("__")
        node = cfg
        for key in parents:
            node = node[key]
        node[last] = value
    first = {"name": "未トリアージ（20 件まで）", "project": "P", "streams": ["P-main"], "checkers": [], "impacts": [],
             "triage": {"status": [], "classification": ["Unclassified"], "action": []}, "limit": 20}
    return {"config": cfg, "filters": {"save": [{"file": "untriaged.yaml", "new": True, "data": first}], "delete": []},
            "knowledge": state["knowledge"]["text"]}


def test_new_setup_writes_what_init_config_writes(product, saved_env):
    state = gui.load_state(product)
    assert not state["config_exists"] and state["filters"] == [] and not state["knowledge"]["exists"]
    assert state["config"]["vcs"] == {"type": "git", "base_branch": "develop", "branch_prefix": "coverity-fix/"}
    assert state["credentials"] == {"user_env": "COV_USER", "key_env": "COV_AUTH_KEY", "user": "", "key_set": False,
                                    "can_save": True}

    payload = new_settings(state)
    payload["credentials"] = [{"env": "COV_USER", "value": "taro"}, {"env": "COV_AUTH_KEY", "value": KEY}]
    result = gui.save(product, payload)
    assert result["ok"], result
    assert saved_env == {"COV_USER": "taro", "COV_AUTH_KEY": KEY}
    assert len(result["written"]) == 3 and result["environment_variables"] == ["COV_USER", "COV_AUTH_KEY"]

    folder = product / ".coverity-triage"
    text = (folder / "config.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Coverity トリアージの設定") and KEY not in text
    saved = yaml.safe_load(text)
    assert saved["coverity"]["url"] == "https://cov.example:8443"
    assert "ca_file" not in saved["coverity"] and "fake_data" not in saved["coverity"]
    # The options of the initial analysis, the integration and the review are written off, as their defaults.
    assert (saved["verify"]["initial"], saved["verify"]["cov_commit_args"], saved["verify"]["timeout_minutes"]) == (
        False, "", 180)
    assert saved["agents"] == {"integration": False, "review": False, "max_rework": 1}
    server_settings.load_coverity_config(product)  # the MCP server accepts what was written
    first = yaml.safe_load((folder / "filters" / "untriaged.yaml").read_text(encoding="utf-8"))
    assert first == {"name": "未トリアージ（20 件まで）", "project": "P", "streams": ["P-main"],
                     "triage": {"classification": ["Unclassified"]}, "limit": 20}
    assert (folder / "knowledge.md").read_text(encoding="utf-8") == config.KNOWLEDGE_TEMPLATE

    # Same shape as the questions in chat (they may leave out a part that keeps its defaults, such as agents).
    other = product.parent / "other"
    subprocess.run(["git", "clone", "-q", str(product), str(other)], check=True)
    prepare.init_config(str(other), "https://cov.example:8443/", "P", "P-main", "develop", None, [], True)
    chat = yaml.safe_load((other / ".coverity-triage" / "config.yaml").read_text(encoding="utf-8"))
    assert chat == {key: value for key, value in saved.items() if key in chat}
    assert config.load_config(other).model_dump() == config.load_config(product).model_dump()

    again = gui.load_state(product)
    assert again["config_exists"] and again["config_error"] is None
    assert again["credentials"]["user"] == "taro" and again["credentials"]["key_set"]
    assert KEY not in json.dumps(again, ensure_ascii=False)


@pytest.fixture
def home_agents(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "user_agents_dir", lambda: tmp_path / "home" / ".copilot" / "agents")


def test_doctor_has_nothing_to_ask_after_the_screen(capsys, product, saved_env, home_agents):
    payload = new_settings(gui.load_state(product), output_dir=str(product.parent / "out"))
    payload["credentials"] = [{"env": "COV_USER", "value": "u"}, {"env": "COV_AUTH_KEY", "value": "k"}]
    assert gui.save(product, payload)["ok"]
    run_ct(capsys, "install-agent")
    report = run_ct(capsys, "doctor", "--repo", product)
    # The MCP server's environment is made by ct.py prewarm, not by the screen.
    assert [c["check"] for c in report["checks"] if c["status"] == "ng"] in ([], ["MCP サーバの Python 環境"])


def test_every_setting_can_be_changed(product, saved_env):
    assert gui.save(product, new_settings(gui.load_state(product)))["ok"]
    state = gui.load_state(product)
    cfg = state["config"]
    cfg["coverity"].update({"api": "fake", "user_env": "MY_USER", "key_env": "MY_KEY", "triage_store": "Store 2",
                            "revision_field": None, "path_strip_prefixes": ["C:/build/"], "ca_file": "ca.pem",
                            "fake_data": "fake.yaml"})
    cfg["vcs"].update({"base_branch": "release", "branch_prefix": "fix/"})
    cfg.update({"output_dir": "../elsewhere", "max_items": 30, "parallel": 3})
    cfg["options"] = {"annotation": True, "per_run_branch": True, "knowledge_suggestions": True, "metrics": True,
                      "markdown": True}
    cfg["verify"] = {"default": "build+analyze", "initial": True, "setup_command": 'envset.bat "{root}" x',
                     "build_dir": "src", "build_command": "make", "cov_build_args": "--dir i2",
                     "cov_analyze_args": "--dir i2 --all", "cov_commit_args": "--auth-key-file key.json",
                     "timeout_minutes": 60}
    cfg["agents"] = {"integration": True, "review": True, "max_rework": 2}
    misra = {"name": "MISRA", "project": "P", "streams": ["P-main"], "checkers": ["MISRA C-2012 *"],
             "impacts": ["High"], "triage": {"status": ["New"], "classification": [], "action": ["Undecided"]},
             "limit": None}
    result = gui.save(product, {"config": cfg, "knowledge": "# 知識\n",
                                "filters": {"save": [{"file": "misra.yaml", "new": True, "data": misra}],
                                            "delete": ["untriaged.yaml"]},
                                "no_grouping": [20002, 20001, 20002]})
    assert result["ok"], result

    folder = product / ".coverity-triage"
    saved = config.load_config(product)
    assert saved.model_dump() == cfg
    assert not (folder / "filters" / "untriaged.yaml").exists()
    assert yaml.safe_load((folder / "filters" / "misra.yaml").read_text(encoding="utf-8")) == {
        "name": "MISRA", "project": "P", "streams": ["P-main"], "checkers": ["MISRA C-2012 *"], "impacts": ["High"],
        "triage": {"status": ["New"], "action": ["Undecided"]}}
    assert config.load_no_grouping(product) == {20001, 20002}
    assert (folder / "knowledge.md").read_text(encoding="utf-8") == "# 知識\n"

    state = gui.load_state(product)
    assert state["config"] == cfg and state["no_grouping"] == [20001, 20002]
    assert state["credentials"]["user_env"] == "MY_USER"


def test_nothing_is_written_when_something_is_wrong(product, saved_env):
    state = gui.load_state(product)
    payload = new_settings(state, output_dir="out", max_items=0)
    payload["config"]["coverity"]["url"] = "cov.example"
    payload["config"]["verify"]["default"] = "build"
    payload["filters"]["save"].append({"file": "../x.yaml", "new": True, "data": {}})
    payload["filters"]["save"].append({"file": "two.yaml", "new": True, "data": {"streams": ["a", "b"]}})
    payload["credentials"] = [{"env": "BAD NAME", "value": "x"}, {"env": "COV_AUTH_KEY", "value": "a\nb"}]
    result = gui.save(product, payload)
    assert not result["ok"]
    text = "\n".join(result["problems"])
    for expected in ("URL", "max_items", "ビルドのコマンド", "../x.yaml", "two.yaml のストリーム", "BAD NAME",
                     "COV_AUTH_KEY: 1 行"):
        assert expected in text, expected
    assert not (product / ".coverity-triage").exists() and saved_env == {}

    inside = gui.save(product, new_settings(state, output_dir="out"))
    assert not inside["ok"] and "リポジトリの外" in inside["problems"][0]


def test_credentials_are_refused_where_they_cannot_be_saved(product, monkeypatch):
    monkeypatch.setattr(envvars, "can_set_user_env", lambda: False)
    payload = new_settings(gui.load_state(product)) | {"credentials": [{"env": "COV_AUTH_KEY", "value": KEY}]}
    result = gui.save(product, payload)
    assert not result["ok"] and "Windows" in result["problems"][0]
    assert gui.load_state(product)["credentials"]["can_save"] is False


def test_a_broken_config_is_shown_with_what_to_fix(product, saved_env):
    folder = product / ".coverity-triage"
    (folder / "filters").mkdir(parents=True)
    (folder / "config.yaml").write_text(
        "# 手で書いた説明\ncoverity: {url: https://x}\nvcs: {type: git}\nparallel: 99\n", encoding="utf-8")
    (folder / "filters" / "bad.yaml").write_text("streams: [a]\nunknown: 1\n", encoding="utf-8")
    state = gui.load_state(product)
    assert "parallel" in state["config_error"] and state["config"]["parallel"] == 99
    assert state["config"]["coverity"]["url"] == "https://x"
    assert "unknown" in state["filters"][0]["error"] and state["filters"][0]["data"]["streams"] == ["a"]

    (folder / "config.yaml").write_text("# 手で書いた説明\ncoverity: just text\nvcs: {type: git}\n", encoding="utf-8")
    assert gui.load_state(product)["config"]["coverity"]["user_env"] == "COV_USER"  # shown with the defaults
    state["config"]["parallel"] = 2
    fixed = {"streams": ["a"]}
    result = gui.save(product, {"config": state["config"], "filters": {"save": [{"file": "bad.yaml", "data": fixed}]}})
    assert result["ok"], result
    assert (folder / "config.yaml").read_text(encoding="utf-8").startswith("# 手で書いた説明\n")
    assert gui.load_state(product)["config_error"] is None


def test_trial_build_uses_the_values_on_the_screen(product, saved_env):
    state = gui.load_state(product)
    cfg = new_settings(state, output_dir=str(product.parent / "out"))["config"]
    build = gui.TrialBuild(product)
    cfg["verify"]["build_command"] = ""
    assert not build.start({"config": cfg})["ok"]
    cfg["verify"]["build_command"] = "git --version"
    assert build.start({"config": cfg})["ok"]
    deadline = time.monotonic() + 60
    while build.running() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert build.state["status"] == "done", build.state
    assert build.state["result"]["build_ok"] and build.state["commands"]["build_command"] == "git --version"
    assert not (product / ".coverity-triage").exists()  # nothing saved


# ---- web server ----------------------------------------------------------------------------

TOKEN = "token-123"


@pytest.fixture(autouse=True)
def gui_state(tmp_path, monkeypatch) -> Iterator[Path]:
    """The files of the running screens go to the test's folder, and no browser is opened."""
    folder = tmp_path / "gui-state"
    monkeypatch.setenv(gui.STATE_DIR_ENV, str(folder))
    monkeypatch.setattr(gui.webbrowser, "open", lambda url: False)
    monkeypatch.setattr(gui.tempfile, "tempdir", str(tmp_path))  # the logs of the background servers
    yield folder
    for path in folder.glob("*.json"):  # a server that a failed test left running
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
            found = json.loads(path.read_text(encoding="utf-8"))
            close(f"http://127.0.0.1:{found['port']}/?t={found['token']}")


def send(url: str, method: str, path: str, body: dict | None = None, *, token: str | None = None,
         host: str | None = None, content_type: str = "application/json",
         cookie: str | None = None) -> tuple[int, dict[str, str], bytes]:
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    headers = {"Host": host or parts.netloc}
    if token:
        headers["X-CT-Token"] = token
    if cookie:
        headers["Cookie"] = cookie
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = content_type
    conn.request(method, path, body=data, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    return res.status, {k.lower(): v for k, v in res.getheaders()}, raw


def request(url: str, method: str, path: str, body: dict | None = None, **options) -> tuple[int, dict | str]:
    status, _, raw = send(url, method, path, body, **options)
    text = raw.decode("utf-8")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


@contextlib.contextmanager
def serving(root: Path) -> Iterator[gui.SettingsServer]:
    server = gui.SettingsServer(root, TOKEN)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def running(product, saved_env):
    with serving(product) as server:
        yield server


def token_of(url: str) -> str:
    return urlsplit(url).query.removeprefix("t=")


def close(url: str) -> None:
    """Press 「サーバを止める」 (nothing to do when the server is gone already)."""
    with contextlib.suppress(OSError):
        request(url, "POST", "/api/close", {}, token=token_of(url))


def wait_until(check, seconds: float = 15) -> bool:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


def test_the_server_answers_only_its_page(running):
    url = running.url
    status, headers, page = send(url, "GET", "/?t=token-123")
    assert status == 200 and "Coverity トリアージ" in page.decode("utf-8")
    assert headers["set-cookie"] == f"ct_token_{running.port}=token-123; HttpOnly; SameSite=Strict; Path=/"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    status, headers, _ = send(url, "GET", "/?t=wrong")
    assert status == 403 and "set-cookie" not in headers
    assert request(url, "GET", "/api/state")[0] == 403
    assert request(url, "GET", "/api/state", token="wrong")[0] == 403
    assert request(url, "GET", "/api/state", token="token-123", host="evil.example:80")[0] == 403
    assert request(url, "POST", "/api/close", {}, token="token-123", content_type="text/plain")[0] == 403

    status, state = request(url, "GET", "/api/state", token="token-123")
    assert status == 200 and state["ok"] and state["repo_root"] == str(running.repo)

    payload = new_settings(state, output_dir=str(running.repo.parent / "out"))
    payload["credentials"] = [{"env": "COV_AUTH_KEY", "value": KEY}]
    status, saved = request(url, "POST", "/api/save", payload, token="token-123")
    assert status == 200 and saved["ok"], saved
    status, creds = request(url, "GET", "/api/credentials?user_env=COV_USER&key_env=COV_AUTH_KEY", token="token-123")
    assert creds["key_set"] and KEY not in json.dumps(creds)
    status, ping = request(url, "GET", "/api/ping", token="token-123")  # saving does not stop the screen
    assert ping == {"ok": True, "repo": str(running.repo), "version": gui.code_version()}
    assert request(url, "POST", "/api/nothing", {}, token="token-123")[0] == 404

    status, closed = request(url, "POST", "/api/close", {}, token="token-123")
    assert closed == {"ok": True}
    assert wait_until(lambda: running.closed_by == "button", 5) and running.saved


def test_launch_runs_in_the_background_and_is_reused(capsys, product):
    started = run_ct(capsys, "gui", "--repo", product, "--no-browser")
    path = gui.server_file(product.resolve())
    try:
        assert started["url"].startswith("http://127.0.0.1:") and not started["browser_opened"]
        assert not started["reused"] and "サーバを止める" in started["next"]
        token = token_of(started["url"])
        found = json.loads(path.read_text(encoding="utf-8"))
        assert found["url"] == started["url"] and found["token"] == token and found["pid"] == started["pid"]
        assert found["port"] == urlsplit(started["url"]).port and found["repo"] == str(product.resolve())
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600  # it holds the token
        status, state = request(started["url"], "GET", "/api/state", token=token)
        assert status == 200 and state["vcs"] == "git"

        again = run_ct(capsys, "gui", "--repo", product, "--no-browser")
        assert again["reused"] and again["url"] == started["url"] and again["pid"] == started["pid"]
        assert "log" not in again
    finally:
        close(started["url"])
    log = Path(started["log"])
    assert wait_until(lambda: '"closed_by": "button"' in log.read_text(encoding="utf-8", errors="replace"))
    assert wait_until(lambda: not path.exists())  # the next ct.py gui starts a new server


def test_a_stopped_or_older_server_is_replaced(capsys, product):
    path = gui.server_file(product.resolve())
    path.parent.mkdir(parents=True)
    with serving(product.resolve()) as old:
        old.version = "older-scripts"
        path.write_text(json.dumps(old.record()), encoding="utf-8")
        started = run_ct(capsys, "gui", "--repo", product, "--no-browser")
        try:
            assert not started["reused"] and token_of(started["url"]) != TOKEN
            assert wait_until(lambda: old.closed_by == "button", 5)  # the server of the other scripts stopped
            assert json.loads(path.read_text(encoding="utf-8"))["token"] == token_of(started["url"])
        finally:
            close(started["url"])
    assert wait_until(lambda: not path.exists())

    # A file left by a server that is gone (killed, or the PC was restarted).
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    path.write_text(json.dumps({"url": f"http://127.0.0.1:{port}/?t=gone", "pid": 1, "port": port, "token": "gone",
                                "repo": str(product.resolve())}), encoding="utf-8")
    started = run_ct(capsys, "gui", "--repo", product, "--no-browser")
    try:
        assert not started["reused"]
        assert json.loads(path.read_text(encoding="utf-8"))["token"] == token_of(started["url"])
    finally:
        close(started["url"])
    assert wait_until(lambda: not path.exists())


def test_a_server_whose_place_another_took_stops(product, monkeypatch):
    """ct.py gui --foreground, or a file that could not be read: the server ct.py gui can no longer find stops."""
    monkeypatch.setattr(gui, "WATCH_SECONDS", 0.05)
    path = gui.server_file(product.resolve())
    results: dict[str, dict] = {}

    def start(name: str) -> tuple[threading.Thread, str]:
        before = path.read_text(encoding="utf-8") if path.exists() else None
        thread = threading.Thread(target=lambda: results.update({name: gui.serve(str(product))}), daemon=True)
        thread.start()
        assert wait_until(lambda: path.exists() and path.read_text(encoding="utf-8") != before)
        return thread, json.loads(path.read_text(encoding="utf-8"))["url"]

    first, first_url = start("first")
    try:
        path.write_text("{not json", encoding="utf-8")  # gone or broken: never a reason to stop
        time.sleep(0.5)
        assert request(first_url, "GET", "/api/ping", token=token_of(first_url))[0] == 200 and "first" not in results
        second, second_url = start("second")
        first.join(10)
        assert results["first"]["closed_by"] == "replaced"
        assert json.loads(path.read_text(encoding="utf-8"))["token"] == token_of(second_url)  # not removed by the first
        assert request(second_url, "GET", "/api/ping", token=token_of(second_url))[0] == 200
        close(second_url)
        second.join(10)
        assert results["second"]["closed_by"] == "button" and not path.exists()
    finally:
        close(first_url)


def test_gui_opens_a_run(capsys, repo):
    run_dir = two_items(capsys, repo)
    run_id = Path(run_dir).name
    started = run_ct(capsys, "gui", "--repo", repo, "--run", run_dir, "--no-browser")
    try:
        token = token_of(started["url"])
        assert started["url"].endswith(f"/?t={token}#run={run_id}&view=monitor") and "note" not in started
        assert "監視" in started["next"]
        status, board = request(started["url"], "GET", f"/api/runs/{run_id}/activity", token=token)
        assert status == 200 and board["ok"] and board["run_id"] == run_id

        again = run_ct(capsys, "gui", "--repo", repo, "--run", run_dir, "--view", "summary", "--no-browser")
        assert again["reused"] and again["url"].endswith(f"#run={run_id}&view=summary")
        assert "/coverity-apply" in again["next"]
        settings = run_ct(capsys, "gui", "--repo", repo, "--view", "settings", "--no-browser")
        assert settings["reused"] and settings["url"].endswith(f"/?t={token}#view=settings")

        moved = shutil.copytree(run_dir, repo.parent / "elsewhere" / run_id)
        outside = run_ct(capsys, "gui", "--repo", repo, "--run", moved, "--no-browser")
        assert outside["reused"] and "結果を置く場所" in outside["note"]
        wrong = run_ct(capsys, "gui", "--repo", repo, "--run", repo, "--no-browser", ok=False)
        assert "実行フォルダではありません" in wrong["error"]
    finally:
        close(started["url"])
    assert wait_until(lambda: not gui.server_file(repo.resolve()).exists())


ITEM_KEYS = {"id", "title", "cids", "checker", "file", "line", "impact", "recommendation", "confidence", "summary",
             "decision", "edited", "status", "applied", "report"}
BOARD_KEYS = {"run_id", "created_at", "filter_name", "stage", "stage_label", "main", "counts", "total", "parallel",
              "workers", "recent", "alerts"}


def test_results_and_approvals_in_the_screen(capsys, repo):
    run_dir = two_items(capsys, repo)
    run_id = Path(run_dir).name
    with serving(repo) as server:
        url = server.url

        def approve_in_screen(body: dict) -> tuple[int, dict]:
            return request(url, "POST", f"/api/runs/{run_id}/approvals", body, token=TOKEN)

        status, runs = request(url, "GET", "/api/runs", token=TOKEN)
        assert status == 200 and runs["ok"] and runs["output_dir"] == str(Path(run_dir).parent)
        (row,) = runs["runs"]
        assert (row["run_id"], row["run_dir"], row["total"], row["counts"]["done"]) == (run_id, run_dir, 2, 2)
        assert row["summary"] and not row["unfinished"] and row["stage"] == "done" and row["filter_name"]
        board = request(url, "GET", f"/api/runs/{run_id}/activity", token=TOKEN)[1]
        assert row["stage_label"] == board["stage_label"]  # the same words as the monitor

        status, summary = request(url, "GET", f"/api/runs/{run_id}/summary", token=TOKEN)
        assert status == 200 and summary["ok"] and summary["summary_html"]
        assert summary["run"]["run_id"] == run_id and summary["errors"] == [] and summary["unprocessed"] == []
        low, high = summary["items"]  # low confidence first, as in summary.html
        assert ITEM_KEYS <= set(high)
        assert (low["id"], low["confidence"], low["recommendation"], low["decision"]) == ("20002", "low", "deviation",
                                                                                          "deviation")
        assert (high["id"], high["decision"], high["edited"], high["applied"]) == ("20001", "fix", False, False)
        assert high["cids"] == [20001] and high["file"] == "src/reader.c" and high["report"] == "cid/20001.html"
        assert high["summary"] == RESULT["verdict"]["summary"] and high["status"] == "done"

        status, detail = request(url, "GET", f"/api/runs/{run_id}/items/20002", token=TOKEN)
        assert status == 200 and detail["item"]["id"] == "20002"
        assert detail["deviation"] == detail["ai_deviation"] == DEVIATION and not detail["deviation_edited"]
        assert detail["report_url"] == f"/files/{run_id}/cid/20002.html"
        assert detail["patches"] == [{"kind": "fix", "name": "20002-fix.patch",
                                      "url": f"/view/{run_id}/patches/20002-fix.patch",
                                      "raw_url": f"/files/{run_id}/patches/20002-fix.patch"}]
        assert detail["fixed_files"] == [{"kind": "fix", "path": "src/other.c",
                                          "url": f"/view/{run_id}/fixed/20002/fix/src/other.c",
                                          "raw_url": f"/files/{run_id}/fixed/20002/fix/src/other.c"}]
        for link in (detail["report_url"], detail["patches"][0]["url"], detail["patches"][0]["raw_url"],
                     detail["fixed_files"][0]["url"], detail["fixed_files"][0]["raw_url"]):
            assert send(url, "GET", link, token=TOKEN)[0] == 200, link

        # The person rejects 20001 and edits the deviation comment of 20002.
        status, saved = approve_in_screen({"item": "20001", "decision": "reject", "deviation": None})
        assert status == 200 and saved["ok"] and saved["item"] == "20001" and saved["decision"] == "reject"
        assert saved["edited"] and "warning" not in saved
        edit = {**DEVIATION, "comment": "画面で直した逸脱コメント"}
        status, saved = approve_in_screen({"item": "20002", "decision": "deviation", "deviation": edit})
        assert saved["ok"] and saved["deviation"] == edit and saved["edited"]
        stored = json.loads((Path(run_dir) / "approvals.json").read_text(encoding="utf-8"))["items"]
        assert stored["20001"]["decision"] == "reject" and stored["20002"]["deviation"] == edit
        assert ("却下", "20001") in summary_rows((Path(run_dir) / "summary.html").read_text(encoding="utf-8"))
        assert "画面で直した逸脱コメント" in (Path(run_dir) / "cid" / "20002.html").read_text(encoding="utf-8")
        detail = request(url, "GET", f"/api/runs/{run_id}/items/20002", token=TOKEN)[1]
        assert detail["deviation"] == edit and detail["ai_deviation"] == DEVIATION and detail["deviation_edited"]
        assert approvals.decisions(Run(run_dir)) == {"20001": "reject", "20002": "deviation"}
        rows = {i["id"]: i for i in request(url, "GET", f"/api/runs/{run_id}/summary", token=TOKEN)[1]["items"]}
        assert rows["20001"]["decision"] == "reject" and rows["20001"]["edited"] and rows["20002"]["edited"]

        # What cannot be saved.
        status, wrong = approve_in_screen({"item": "20001", "decision": "maybe"})
        assert not wrong["ok"] and "fix / deviation / reject" in wrong["error"]
        status, wrong = approve_in_screen({"item": "20002", "decision": "deviation",
                                           "deviation": {**DEVIATION, "comment": " "}})
        assert not wrong["ok"] and wrong["problems"] == ["逸脱コメント が空です"]
        assert not approve_in_screen({"decision": "fix"})[1]["ok"]
        assert approve_in_screen({"item": "99999", "decision": "fix"})[0] == 404
        assert request(url, "POST", f"/api/runs/{run_id}/approvals", {"item": "20001"}, token="wrong")[0] == 403
        assert approvals.decisions(Run(run_dir)) == {"20001": "reject", "20002": "deviation"}

        # An item already applied: the change is saved, with a warning.
        with Run(run_dir).update() as state:
            state.item("20001").applied.update({"fix": {"branch": "coverity-fix/cid-20001", "pushed": True}},
                                               at="2026-10-15T10:00:00")
        status, saved = approve_in_screen({"item": "20001", "decision": "fix"})
        assert saved["ok"] and saved["decision"] == "fix" and "もう反映しています（修正）" in saved["warning"]
        assert "warning" not in approve_in_screen({"item": "20001", "decision": "fix"})[1]  # nothing changed
        rows = {i["id"]: i for i in request(url, "GET", f"/api/runs/{run_id}/summary", token=TOKEN)[1]["items"]}
        assert rows["20001"]["applied"] and rows["20001"]["applied_parts"] == ["fix"]

        # Items that failed or are not finished, and the monitor.
        with Run(run_dir).update() as state:
            state.items += [ItemState(id="20003", cids=[20003], status="error", error="警告経路を取れませんでした"),
                            ItemState(id="20004", cids=[20004])]
        summary = request(url, "GET", f"/api/runs/{run_id}/summary", token=TOKEN)[1]
        assert summary["errors"] == [{"id": "20003", "cids": [20003], "reason": "警告経路を取れませんでした"}]
        assert summary["unprocessed"] == ["20004"] and len(summary["items"]) == 2
        status, unfinished = request(url, "GET", f"/api/runs/{run_id}/items/20003", token=TOKEN)
        assert not unfinished["ok"] and "まだ終わっていません" in unfinished["error"]
        status, board = request(url, "GET", f"/api/runs/{run_id}/activity", token=TOKEN)
        assert status == 200 and board["ok"] and BOARD_KEYS <= set(board)
        assert board["run_id"] == run_id and board["stage"] == "items" and board["total"] == 4
        assert len(board["workers"]) == board["parallel"] and {w["stage"] for w in board["workers"]} == {"idle"}
        assert any(a["item"] == "20003" and a["level"] == "critical" for a in board["alerts"])
        assert board["recent"] and {"time", "level", "source", "event", "item", "text"} <= set(board["recent"][0])
        (row,) = request(url, "GET", "/api/runs", token=TOKEN)[1]["runs"]
        assert row["unfinished"] and row["stage"] == "items"

        # Runs and items that are not there.
        for path in ("/api/runs/20990101-000000/summary", "/api/runs/..%2f..%2f/summary",
                     f"/api/runs/{run_id}/nothing", f"/api/runs/{run_id}/items/99999",
                     f"/api/runs/{run_id}/approvals"):
            status, answer = request(url, "GET", path, token=TOKEN)
            assert status == 404 and not answer["ok"], path
        assert request(url, "GET", f"/api/runs/{run_id}/summary")[0] == 403


def test_a_run_made_before_approvals_json_in_the_screen(capsys, repo):
    """Approvals in summary.md and an edited cid/<ID>.md: shown, and taken over at the first change."""
    set_config(repo, options={"markdown": True})
    run_dir = two_items(capsys, repo)
    run_id = Path(run_dir).name
    folder = Path(run_dir)
    for name in ("approvals.json", "summary.html", "cid/20001.html", "cid/20002.html"):
        (folder / name).unlink()
    table = folder / "summary.md"
    note = "画面（ct.py gui）の「結果」で入力します"  # not in a summary.md made before 0.4
    table.write_text(table.read_text(encoding="utf-8").replace("| 修正 | 20001 |", "| 却下 | 20001 |")
                     .replace(note, ""), encoding="utf-8")
    detail = folder / "cid" / "20002.md"
    detail.write_text(detail.read_text(encoding="utf-8").replace("<!-- ct:end deviation -->", ""), encoding="utf-8")
    old_report = (folder / "cid" / "20001.md").read_text(encoding="utf-8")
    set_config(repo, options={"markdown": False})  # the default of 0.4: only the HTML pages are made

    with serving(repo) as server:
        summary = request(server.url, "GET", f"/api/runs/{run_id}/summary", token=TOKEN)[1]
        assert summary["ok"] and not summary["summary_html"]
        rows = {i["id"]: i for i in summary["items"]}
        assert rows["20001"]["decision"] == "reject" and rows["20001"]["edited"] and rows["20001"]["report"] is None
        item = request(server.url, "GET", f"/api/runs/{run_id}/items/20002", token=TOKEN)[1]
        assert item["ok"] and item["deviation"] == DEVIATION and item["report_url"] is None
        assert "目印" in item["deviation_note"]  # the broken section is not used; the AI's proposal is shown

        edit = {**DEVIATION, "comment": "画面で直した逸脱コメント"}
        saved = request(server.url, "POST", f"/api/runs/{run_id}/approvals",
                        {"item": "20002", "decision": "deviation", "deviation": edit}, token=TOKEN)[1]
        assert saved["ok"], saved
        stored = json.loads((folder / "approvals.json").read_text(encoding="utf-8"))["items"]
        assert stored["20001"]["decision"] == "reject" and stored["20002"]["deviation"] == edit
        # every page the new summary.html links to is made, not only the changed item's
        page = (folder / "summary.html").read_text(encoding="utf-8")
        assert summary_rows(page) == [("逸脱", "20002"), ("却下", "20001")]
        for _, item_id in summary_rows(page):
            assert (folder / "cid" / f"{item_id}.html").is_file(), item_id
        assert "却下" in (folder / "cid" / "20001.html").read_text(encoding="utf-8")
        assert (folder / "cid" / "20001.md").read_text(encoding="utf-8") == old_report  # the person's file is kept
        # the old table shows the approvals now in force, and that it is no longer where they are entered
        table_text = table.read_text(encoding="utf-8")
        assert "| 却下 | 20001 |" in table_text and "| 逸脱 | 20002 |" in table_text and note in table_text
        item = request(server.url, "GET", f"/api/runs/{run_id}/items/20002", token=TOKEN)[1]
        assert item["deviation"] == edit and "deviation_note" not in item
        assert item["report_url"] == f"/files/{run_id}/cid/20002.html"
        assert request(server.url, "GET", f"/api/runs/{run_id}/items/20001", token=TOKEN)[1]["report_url"]


def test_the_decision_shown_is_the_one_preview_applies(capsys, repo):
    run_dir = start_run(capsys, repo, "--limit", "2")["run_dir"]
    run_id = Path(run_dir).name
    run_ct(capsys, "plan", "--run", run_dir)
    work_item(capsys, run_dir)
    # before ct.py summary: the recommendation, which the summary puts in
    assert [(i["id"], i["decision"]) for i in gui.run_summary(repo, run_id)["items"]] == [("20001", "fix")]
    work_item(capsys, run_dir, file="src/other.c")
    run_ct(capsys, "summary", "--run", run_dir)
    path = Path(run_dir) / "approvals.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["items"]["20002"]  # an approval that is missing (as an unreadable row of an old summary.md)
    path.write_text(json.dumps(data), encoding="utf-8")
    assert approvals.decisions(Run(run_dir)) == {"20001": "fix"}
    rows = {i["id"]: (i["decision"], i["edited"]) for i in gui.run_summary(repo, run_id)["items"]}
    assert rows == {"20001": ("fix", False), "20002": (None, False)}
    assert gui.item_detail(repo, run_id, "20002")["item"]["decision"] is None


def test_the_list_of_runs_and_the_monitor_agree_after_a_preview(capsys, repo):
    run_dir = two_items(capsys, repo)
    run_ct(capsys, "preview", "--run", run_dir)

    def stages() -> tuple[tuple[str, str], tuple[str, str]]:
        (row,) = gui.runs_list(repo)["runs"]
        board = activity.board(Run(run_dir))
        return (row["stage"], row["stage_label"]), (board["stage"], board["stage_label"])

    assert stages() == (("apply", "反映の途中"),) * 2
    # The person did not agree, and nothing followed: once the log is old, both say the run is done.
    log = Path(run_dir) / "operations.log"
    old = time.time() - activity.STALL_SECONDS - 60
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    stamp = datetime.fromtimestamp(old).isoformat(timespec="seconds")
    log.write_text("".join(json.dumps({**r, "time": stamp}) + "\n" for r in records), encoding="utf-8")
    os.utime(log, (old, old))
    assert stages() == (("done", "一覧ができました（承認を待っています）"),) * 2


def test_runs_need_the_settings(running):
    status, runs = request(running.url, "GET", "/api/runs", token=TOKEN)
    assert status == 200 and runs["ok"] and runs["runs"] == [] and "設定ファイルがありません" in runs["note"]
    status, answer = request(running.url, "GET", "/api/runs/20260101-000000/summary", token=TOKEN)
    assert not answer["ok"] and "設定ファイルがありません" in answer["error"]
    assert send(running.url, "GET", "/files/20260101-000000/summary.html", token=TOKEN)[0] == 404


def test_files_of_a_run_stay_inside_it(capsys, repo, tmp_path):
    run_dir = two_items(capsys, repo)
    run_id = Path(run_dir).name
    folder = Path(run_dir)
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    (tmp_path / "elsewhere" / "src").mkdir(parents=True)
    (tmp_path / "elsewhere" / "src" / "reader.c").write_text("SECRET", encoding="utf-8")
    moved = shutil.copytree(folder, tmp_path / "moved" / "20990102-000000")
    links = {folder / "fixed" / "20001" / "fix" / "link.c": secret, folder / "patches" / "link.patch": secret,
             folder / "fixed" / "linked": tmp_path / "elsewhere", folder.parent / "20990102-000000": moved}
    for link, target in links.items():
        try:
            link.symlink_to(target)
        except OSError:  # Windows without the right to make links
            pass

    with serving(repo) as server:
        url = server.url
        jar = send(url, "GET", f"/?t={TOKEN}")[1]["set-cookie"].split(";")[0]
        shown = {f"/files/{run_id}/summary.html": "text/html; charset=utf-8",
                 f"/files/{run_id}/cid/20001.html": "text/html; charset=utf-8",
                 f"/files/{run_id}/patches/20001-fix.patch": "text/plain; charset=utf-8",
                 f"/files/{run_id}/fixed/20001/fix/src/reader.c": "text/plain; charset=utf-8",
                 f"/view/{run_id}/patches/20001-fix.patch": "text/html; charset=utf-8",
                 f"/view/{run_id}/fixed/20001/fix/src/reader.c": "text/html; charset=utf-8"}
        for path, kind in shown.items():
            status, headers, _ = send(url, "GET", path, cookie=f"other=1; {jar}; x=y")
            assert status == 200 and headers["content-type"] == kind, path
            policy = headers["content-security-policy"]
            assert policy.startswith("sandbox ") and "default-src 'none'" in policy and "script-src" not in policy
            assert "frame-ancestors 'self'" in policy and headers["x-frame-options"] == "SAMEORIGIN"
            assert send(url, "GET", path)[0] == 403, path  # neither the cookie nor the token
            assert send(url, "GET", path, cookie=f"ct_token_{server.port}=wrong")[0] == 403, path
            assert send(url, "GET", path, cookie=f"ct_token_1={TOKEN}")[0] == 403, path  # another server's cookie
            assert send(url, "GET", path, cookie=jar, host="evil.example:80")[0] == 403, path
            assert send(url, "GET", path, token=TOKEN)[0] == 200, path
        page = send(url, "GET", f"/files/{run_id}/summary.html", cookie=jar)[2].decode("utf-8")
        assert 'href="cid/20001.html"' in page
        raw = send(url, "GET", f"/files/{run_id}/fixed/20001/fix/src/reader.c", cookie=jar)[2]
        assert raw == (folder / "fixed" / "20001" / "fix" / "src" / "reader.c").read_bytes()

        refused = [f"/files/{run_id}/../run.json", f"/files/{run_id}/cid/../run.json",
                   f"/files/{run_id}/%2e%2e/run.json", f"/files/{run_id}/cid/..%2f..%2f..%2fsecret.txt",
                   f"/files/{run_id}/cid%2f..%2frun.json", f"/files/{run_id}/fixed/20001/fix/..%5c..%5c..%5crun.json",
                   f"/files/{run_id}/fixed/%2e%2e/%2e%2e/run.json", f"/files/{run_id}/fixed/%c0%ae%c0%ae/run.json",
                   f"/files/{run_id}/{quote(str(secret))}", f"/files/{run_id}//{secret.name}",
                   f"/files/{run_id}/C:%5cWindows%5cwin.ini", f"/files/{run_id}/patches/20001-fix.patch%00.html",
                   f"/files/{run_id}/run.json", f"/files/{run_id}/operations.log", f"/files/{run_id}/approvals.json",
                   f"/files/{run_id}/items/20001/result.json", f"/files/{run_id}/cid/20001.html/",
                   f"/files/{run_id}/fixed/20001/fix/link.c", f"/files/{run_id}/patches/link.patch",
                   f"/files/{run_id}/fixed/linked/src/reader.c", "/files/20990102-000000/summary.html",
                   f"/files/../{run_id}/summary.html", "/files/%2e%2e/summary.html",
                   "/files/20990101-000000/summary.html",
                   f"/view/{run_id}/summary.html", f"/view/{run_id}/cid/20001.html", f"/view/{run_id}/../run.json",
                   f"/view/{run_id}/fixed/20001/fix/link.c", f"/view/{run_id}/fixed/linked/src/reader.c"]
        for path in refused:
            status, _, raw = send(url, "GET", path, cookie=jar)
            assert status == 404 and b"SECRET" not in raw, path
        run_ids = [r["run_id"] for r in request(url, "GET", "/api/runs", token=TOKEN)[1]["runs"]]
        assert run_ids == [run_id]  # the linked run folder is not listed either


def test_view_pages_are_escaped(capsys, repo):
    run_dir = two_items(capsys, repo)
    run_id = Path(run_dir).name
    folder = Path(run_dir)
    (folder / "fixed" / "20001" / "fix" / "src" / "reader.c").write_text(
        "<script>alert(1)</script>\nint x = 1 & 2;\n", encoding="utf-8")
    patch = folder / "patches" / "20001-fix.patch"
    patch.write_bytes(patch.read_bytes() + b"+<img src=x onerror=alert(1)>\n")
    (folder / "fixed" / "20002" / "fix" / "src" / "other.c").write_bytes("/* 日本語のコメント */\n".encode("cp932"))
    (folder / "fixed" / "20002" / "fix" / "a.out").write_bytes(b"\x7fELF\x00\x00\x01")
    (folder / "fixed" / "20002" / "fix" / "help.html").write_text("<script>alert(1)</script>", encoding="utf-8")

    with serving(repo) as server:
        def page(path: str) -> str:
            status, headers, raw = send(server.url, "GET", path, token=TOKEN)
            assert status == 200 and headers["content-type"] == "text/html; charset=utf-8", path
            return raw.decode("utf-8")

        fixed = page(f"/view/{run_id}/fixed/20001/fix/src/reader.c")
        assert fixed.startswith("<!doctype html>") and "<script" not in fixed
        assert "&lt;script&gt;alert(1)&lt;/script&gt;\n</span>" in fixed and "int x = 1 &amp; 2;" in fixed
        assert '<pre class="code">' in fixed and fixed.count("<span>") == 2  # one line each, numbered by CSS
        assert f'<a href="/files/{run_id}/fixed/20001/fix/src/reader.c">元のファイル</a>' in fixed
        assert "20001 の修正後のファイル" in fixed and "<code>src/reader.c</code>" in fixed

        diff = page(f"/view/{run_id}/patches/20001-fix.patch")
        assert '<pre class="diff">' in diff and '<span class="add">+/* fix 20001 */' in diff
        assert "<img" not in diff and "+&lt;img src=x onerror=alert(1)&gt;" in diff
        assert f'<a href="/files/{run_id}/patches/20001-fix.patch">元のファイル</a>' in diff

        assert "日本語のコメント" in page(f"/view/{run_id}/fixed/20002/fix/src/other.c")  # saved in cp932
        status, headers, raw = send(server.url, "GET", f"/files/{run_id}/fixed/20002/fix/src/other.c", token=TOKEN)
        assert headers["content-type"] == "text/plain; charset=shift_jis" and "日本語" in raw.decode("cp932")
        assert "文字のファイルではない" in page(f"/view/{run_id}/fixed/20002/fix/a.out")
        status, headers, _ = send(server.url, "GET", f"/files/{run_id}/fixed/20002/fix/help.html", token=TOKEN)
        assert headers["content-type"] == "text/plain; charset=utf-8"  # a fixed file of the product is text


def test_launch_reports_a_missing_folder(capsys, tmp_path):
    result = run_ct(capsys, "gui", "--repo", tmp_path / "nothing", "--no-browser", ok=False)
    assert "フォルダがありません" in result["error"]


def test_a_pasted_key_file_saves_only_the_key(product, saved_env):
    payload = new_settings(gui.load_state(product))
    pasted = json.dumps({"comment": "made in Coverity Connect", "username": "taro", "key": KEY})
    payload["credentials"] = [{"env": "COV_AUTH_KEY", "value": "  " + pasted + "\n"}]
    assert gui.save(product, payload)["ok"]
    assert saved_env == {"COV_AUTH_KEY": KEY}

    for broken in ('{"key": ', '{"username": "taro"}'):
        payload["credentials"] = [{"env": "COV_AUTH_KEY", "value": broken}]
        result = gui.save(product, payload)
        assert not result["ok"] and "キーの部分だけ" in "\n".join(result["problems"]), broken
    assert gui.auth_key_value("plain-key") == ("plain-key", None)


def test_the_page_keeps_to_its_rules():
    """No test drives the page in a browser; these keep the rules that a change could break unnoticed."""
    page = gui.PAGE.read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", page, re.S)
    assert len(scripts) == 1 and scripts[0][0].strip() == ""  # one inline script, nothing loaded from outside
    assert not re.search(r"<link\b|@import|url\(\s*['\"]?https?:|\bsrc=\"https?:", page)
    # Text from the AI and from Coverity is put in with textContent only.
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", page)
    # The settings no longer end the screen; only 「サーバを止める」 stops it.
    assert "終了" not in page and "サーバを止める" in page and '"/api/close"' in page
    defaults = config.ProjectConfig.model_validate({"coverity": {"url": ""}, "vcs": {"type": "git"}}).model_dump()
    for key in sorted(set(re.findall(r'data-bind="([^"]+)"', page))):
        value: object = defaults
        for part in key.split("."):
            assert isinstance(value, dict) and part in value, f"data-bind={key} is not a setting"
            value = value[part]
    assert 'data-bind="options.markdown"' in page
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: the syntax of the script is not checked")
    checked = subprocess.run([node, "--check", "-"], input=scripts[0][1], capture_output=True, text=True,
                             encoding="utf-8", timeout=60)
    assert checked.returncode == 0, checked.stderr
