"""The settings screen of /coverity-setup (ct.py gui): what it reads, what it writes, and its web server."""

from __future__ import annotations

import http.client
import json
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from coverity_triage import settings as server_settings
from ctlib import config, envvars, gui, prepare
from test_flow import git, git_identity, run_ct  # noqa: F401 - git_identity is a fixture

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
    server_settings.load_coverity_config(product)  # the MCP server accepts what was written
    first = yaml.safe_load((folder / "filters" / "untriaged.yaml").read_text(encoding="utf-8"))
    assert first == {"name": "未トリアージ（20 件まで）", "project": "P", "streams": ["P-main"],
                     "triage": {"classification": ["Unclassified"]}, "limit": 20}
    assert (folder / "knowledge.md").read_text(encoding="utf-8") == config.KNOWLEDGE_TEMPLATE

    # Same shape as the questions in chat.
    other = product.parent / "other"
    subprocess.run(["git", "clone", "-q", str(product), str(other)], check=True)
    prepare.init_config(str(other), "https://cov.example:8443/", "P", "P-main", "develop", None, [], True)
    assert yaml.safe_load((other / ".coverity-triage" / "config.yaml").read_text(encoding="utf-8")) == saved

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
    cfg["options"] = {"annotation": True, "per_run_branch": True, "knowledge_suggestions": True, "metrics": True}
    cfg["verify"] = {"default": "build+analyze", "setup_command": 'envset.bat "{root}" x', "build_dir": "src",
                     "build_command": "make", "cov_build_args": "--dir i2", "cov_analyze_args": "--dir i2 --all"}
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


def request(url: str, method: str, path: str, body: dict | None = None, *, token: str | None = None,
            host: str | None = None, content_type: str = "application/json") -> tuple[int, dict | str]:
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    headers = {"Host": host or parts.netloc}
    if token:
        headers["X-CT-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = content_type
    conn.request(method, path, body=data, headers=headers)
    res = conn.getresponse()
    raw = res.read().decode("utf-8")
    conn.close()
    try:
        return res.status, json.loads(raw)
    except ValueError:
        return res.status, raw


@pytest.fixture
def running(product, saved_env):
    server = gui.SettingsServer(product, "token-123")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_the_server_answers_only_its_page(running):
    url = running.url
    status, page = request(url, "GET", "/?t=token-123")
    assert status == 200 and "Coverity トリアージの設定" in page
    assert request(url, "GET", "/?t=wrong")[0] == 403
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

    status, closed = request(url, "POST", "/api/close", {}, token="token-123")
    assert closed == {"ok": True}
    deadline = time.monotonic() + 5
    while not running.closed_by and time.monotonic() < deadline:
        time.sleep(0.05)
    assert running.closed_by == "button" and running.saved


def test_the_screen_stops_when_the_page_is_gone(running, monkeypatch):
    monkeypatch.setattr(gui, "IDLE_SECONDS", 0)
    monkeypatch.setattr(gui.time, "sleep", lambda _: None)
    running.last_seen = time.monotonic() - 1
    running.watch_idle()
    assert running.closed_by == "idle"


def test_launch_runs_in_the_background(capsys, product):
    started = run_ct(capsys, "gui", "--repo", product, "--no-browser")
    assert started["url"].startswith("http://127.0.0.1:") and not started["browser_opened"]
    token = started["url"].split("?t=", 1)[1]
    status, state = request(started["url"], "GET", "/api/state", token=token)
    assert status == 200 and state["vcs"] == "git"
    assert request(started["url"], "POST", "/api/close", {}, token=token)[1] == {"ok": True}
    deadline = time.monotonic() + 15
    log = Path(started["log"])
    while time.monotonic() < deadline and '"closed_by"' not in log.read_text(encoding="utf-8", errors="replace"):
        time.sleep(0.1)
    assert '"closed_by": "button"' in log.read_text(encoding="utf-8", errors="replace")


def test_launch_reports_a_missing_folder(capsys, tmp_path):
    result = run_ct(capsys, "gui", "--repo", tmp_path / "nothing", "--no-browser", ok=False)
    assert "フォルダがありません" in result["error"]
