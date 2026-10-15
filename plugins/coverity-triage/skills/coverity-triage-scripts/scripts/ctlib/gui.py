"""Settings screen of /coverity-setup, chosen instead of the questions in chat.

A small web page served only to this PC (127.0.0.1) and opened in the default browser. It reads and
writes the same things as the questions in chat: ``.coverity-triage/config.yaml``, ``filters/*.yaml``,
``knowledge.md``, ``no-grouping.yaml`` and the person's credentials (user environment variables).
The page talks to this server with a random token, so other pages and programs cannot use it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml
from pydantic import BaseModel, ValidationError

from . import envvars, prepare
from .common import CtError, resolve_path
from .config import (CONFIG_DIR, CONFIG_FILE, FILTERS_DIR, KNOWLEDGE_TEMPLATE, NO_GROUPING_FILE, FilterSpec,
                     ProjectConfig, knowledge_path, output_dir)

PAGE = Path(__file__).with_name("gui.html")
TOKEN_ENV = "CT_GUI_TOKEN"
READY_SECONDS = 30
# The page asks every 20 seconds; browsers slow timers of hidden tabs down to once a minute.
IDLE_SECONDS = 180
FIRST_VISIT_SECONDS = 1800
NO_GROUPING_HEADER = "# 却下されたグループの CID。次の実行からグループにしない（自動で追記）\n"
FILTER_NAME = re.compile(r"^[^\\/:*?\"<>|.][^\\/:*?\"<>|]{0,80}\.yaml$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

ERROR_TEXT = {
    "missing": "入力してください",
    "greater_than_equal": "{ge} 以上にしてください",
    "less_than_equal": "{le} 以下にしてください",
    "int_parsing": "整数にしてください",
    "int_type": "整数にしてください",
    "string_type": "文字列にしてください",
    "bool_type": "オン / オフで指定してください",
    "literal_error": "次のどれかにしてください: {expected}",
    "extra_forbidden": "この項目は使えません",
}


# ---- reading -------------------------------------------------------------------------------


def _read_yaml(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return None, f"読めません: {exc}"
    if not isinstance(data, dict):
        return None, "一番外側が「キー: 値」の形ではありません"
    return data, None


def _merge(defaults: Any, raw: Any) -> Any:
    """Saved values over the defaults, keeping only the keys the settings know."""
    if isinstance(defaults, dict):
        if not isinstance(raw, dict):
            return defaults
        return {k: _merge(v, raw[k]) if k in raw else v for k, v in defaults.items()}
    return raw


def _problems(exc: ValidationError, where: str) -> list[str]:
    out = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"]) or "（全体）"
        template = ERROR_TEXT.get(err["type"])
        text = template.format(**(err.get("ctx") or {})) if template else err["msg"]
        out.append(f"{where} の {field}: {text}")
    return out


def _config_defaults(found: dict[str, Any]) -> dict[str, Any]:
    vcs = {"type": found["vcs"] or "git", "base_branch": found["base_branch"] or "main"}
    return ProjectConfig.model_validate({"coverity": {"url": ""}, "vcs": vcs}).model_dump()


def _filter_defaults() -> dict[str, Any]:
    return FilterSpec().model_dump()


def credentials(user_env: str, key_env: str) -> dict[str, Any]:
    """The user name may be shown; of the key, only whether it is set."""
    names_ok = bool(ENV_NAME.match(user_env or "")) and bool(ENV_NAME.match(key_env or ""))
    return {"user_env": user_env, "key_env": key_env,
            "user": (envvars.get_env(user_env) or "") if names_ok else "",
            "key_set": bool(envvars.get_env(key_env)) if names_ok else False,
            "can_save": envvars.can_set_user_env()}


def load_state(repo: Path) -> dict[str, Any]:
    """Everything the screen shows, read from the repository and this PC."""
    found = prepare.detect(str(repo))
    folder = repo / CONFIG_DIR
    defaults = _config_defaults(found)
    config, config_error, config_exists = defaults, None, (folder / CONFIG_FILE).is_file()
    if config_exists:
        raw, config_error = _read_yaml(folder / CONFIG_FILE)
        if raw is not None:
            config = _merge(defaults, raw)
            try:
                config = ProjectConfig.model_validate(raw).model_dump()
            except ValidationError as exc:
                config_error = "\n".join(_problems(exc, CONFIG_FILE))

    filters = []
    for path in sorted((folder / FILTERS_DIR).glob("*.yaml")):
        raw, error = _read_yaml(path)
        data = _merge(_filter_defaults(), raw or {})
        if raw is not None:
            try:
                data = FilterSpec.model_validate(raw).model_dump()
            except ValidationError as exc:
                error = "\n".join(_problems(exc, path.name))
        filters.append({"file": path.name, "data": data, "error": error})

    knowledge = knowledge_path(repo)
    no_grouping_file = folder / NO_GROUPING_FILE
    no_grouping, no_grouping_error = [], None
    if no_grouping_file.is_file():
        raw, no_grouping_error = _read_yaml(no_grouping_file)
        try:
            no_grouping = sorted({int(c) for c in (raw or {}).get("cids") or []})
        except (TypeError, ValueError):
            no_grouping_error = "cids に数字でないものがあります"
    return {
        "repo_root": str(repo), "vcs": found["vcs"], "vcs_note": found["vcs_note"],
        "config_exists": config_exists, "config": config, "config_error": config_error,
        "filters": filters,
        "knowledge": {"exists": knowledge.is_file(),
                      "text": knowledge.read_text(encoding="utf-8") if knowledge.is_file() else KNOWLEDGE_TEMPLATE},
        "no_grouping": no_grouping, "no_grouping_error": no_grouping_error,
        "credentials": credentials(config["coverity"]["user_env"], config["coverity"]["key_env"]),
    }


# ---- checking and writing ------------------------------------------------------------------


def _validate(model: type[BaseModel], data: Any, where: str, problems: list[str]) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems.extend(_problems(exc, where))
        return None


def _config_problems(repo: Path, found: dict[str, Any], data: Any, problems: list[str]) -> ProjectConfig | None:
    if not found["vcs"]:
        problems.append(found["vcs_note"])
    if not isinstance(data, dict):
        problems.append("config.yaml の内容がありません")
        return None
    url = str((data.get("coverity") or {}).get("url") or "").strip().rstrip("/")
    if isinstance(data.get("coverity"), dict):
        data["coverity"]["url"] = url
    if not re.match(r"^https?://[^/\s]+", url):
        problems.append("Coverity の URL: http:// か https:// で始まるアドレスを入れてください")
    verify = data.get("verify") if isinstance(data.get("verify"), dict) else {}
    if verify.get("default", "none") != "none" and not str(verify.get("build_command") or "").strip():
        problems.append("ビルドでの検証: ビルドのコマンドを入れてください")
    config = _validate(ProjectConfig, data, CONFIG_FILE, problems)
    if config is None:
        return None
    if found["vcs"] and config.vcs.type != found["vcs"]:
        problems.append(f"バージョン管理: このリポジトリは {found['vcs']} です（設定は {config.vcs.type}）")
    out = output_dir(repo, config)
    if out == repo or repo in out.parents:
        problems.append(f"結果を置く場所: リポジトリの外にしてください（{out}）")
    return config


def _config_yaml(config: ProjectConfig) -> dict[str, Any]:
    data = config.model_dump()
    for key in ("ca_file", "fake_data"):
        if not data["coverity"].get(key):
            del data["coverity"][key]
    if config.vcs.type == "svn":
        del data["vcs"]["base_branch"], data["vcs"]["branch_prefix"]
    return data


def _filter_yaml(spec: FilterSpec) -> dict[str, Any]:
    """Only the keys that select something, as the filters written by hand."""
    data = spec.model_dump()
    data["triage"] = {k: v for k, v in data["triage"].items() if v}
    return {k: v for k, v in data.items() if v not in (None, "", [], {})}


def auth_key_value(value: str) -> tuple[str, str | None]:
    """The key itself when the contents of an authentication key file (JSON) were pasted."""
    text = value.strip()
    if not text.startswith("{"):
        return value, None
    try:
        data = json.loads(text)
    except ValueError:
        return value, "認証キーのファイルの中身（JSON）として読めません。キーの部分だけを貼ってください"
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key.strip():
        return value, "認証キーのファイルの中身に key の値がありません。キーの部分だけを貼ってください"
    return key.strip(), None


def _dump(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def save(repo: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Check everything first; write only when nothing is wrong."""
    found = prepare.detect(str(repo))
    folder = repo / CONFIG_DIR
    problems: list[str] = []
    writes: dict[Path, str] = {}
    deletes: list[Path] = []
    env_writes: list[tuple[str, str]] = []

    if payload.get("config") is not None:
        config = _config_problems(repo, found, payload["config"], problems)
        if config is not None:
            path = folder / CONFIG_FILE
            header = prepare.CONFIG_HEADER
            if path.is_file():
                kept = [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("#")]
                header = "".join(line + "\n" for line in kept) if kept else header
            writes[path] = header + _dump(_config_yaml(config))
    elif not (folder / CONFIG_FILE).is_file():
        problems.append("設定ファイルがまだありません。基本設定を入れてください")

    filters = payload.get("filters") or {}
    existing = {p.name for p in (folder / FILTERS_DIR).glob("*.yaml")}
    for name in filters.get("delete") or []:
        if name not in existing:
            problems.append(f"削除する条件ファイルがありません: {name}")
        else:
            deletes.append(folder / FILTERS_DIR / name)
    for entry in filters.get("save") or []:
        name = str(entry.get("file") or "")
        if not FILTER_NAME.match(name):
            problems.append(f"条件ファイルの名前: 「{name}」は使えません"
                            "（.yaml で終わり、\\ / : * ? \" < > | を含まない名前）")
            continue
        if entry.get("new") and name in existing:
            problems.append(f"条件ファイル {name} はもうあります。別の名前にしてください")
            continue
        spec = _validate(FilterSpec, entry.get("data"), name, problems)
        if spec is None:
            continue
        if len(spec.streams) != 1 or not spec.streams[0].strip():
            problems.append(f"{name} のストリーム: 1 つだけ入れてください")
            continue
        writes[folder / FILTERS_DIR / name] = _dump(_filter_yaml(spec))
    remaining = (existing - {p.name for p in deletes}) | {p.name for p in writes if p.parent.name == FILTERS_DIR}
    if (payload.get("config") is not None or filters.get("save") or filters.get("delete")) and not remaining:
        problems.append("条件ファイルが 1 つもありません。少なくとも 1 つ作ってください")

    if payload.get("knowledge") is not None:
        writes[knowledge_path(repo)] = str(payload["knowledge"])

    if payload.get("no_grouping") is not None:
        try:
            cids = sorted({int(c) for c in payload["no_grouping"]})
            writes[folder / NO_GROUPING_FILE] = NO_GROUPING_HEADER + yaml.safe_dump({"cids": cids})
        except (TypeError, ValueError):
            problems.append("グループ化しない CID: 数字だけにしてください")

    for entry in payload.get("credentials") or []:
        name, value = str(entry.get("env") or ""), str(entry.get("value") or "")
        value, key_problem = auth_key_value(value)
        if key_problem:
            problems.append(f"環境変数 {name}: {key_problem}")
        elif not ENV_NAME.match(name):
            problems.append(f"環境変数の名前「{name}」は使えません（英字・数字・_ だけ）")
        elif not value.strip() or "\n" in value or "\r" in value:
            problems.append(f"環境変数 {name}: 1 行の値を入れてください")
        elif not envvars.can_set_user_env():
            problems.append(f"環境変数 {name}: この PC では保存できません（Windows だけ）。自分で設定してください")
        else:
            env_writes.append((name, value.strip()))

    if problems:
        return {"ok": False, "error": "保存できませんでした。次を直してください", "problems": problems}
    for path, text in writes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for path in deletes:
        path.unlink()
    saved_env = []
    for name, value in env_writes:
        try:
            envvars.set_user_env(name, value)
        except OSError as exc:
            return {"ok": False, "error": f"環境変数 {name} を保存できませんでした: {exc}",
                    "written": [str(p) for p in writes], "deleted": [str(p) for p in deletes]}
        saved_env.append(name)
    return {"ok": True, "written": [str(p) for p in writes], "deleted": [str(p) for p in deletes],
            "environment_variables": saved_env}


class TrialBuild:
    """One trial build at a time, in the background (it can take minutes)."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {"status": "idle"}

    def running(self) -> bool:
        return self.state["status"] == "running"

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        problems: list[str] = []
        config = _config_problems(self.repo, prepare.detect(str(self.repo)), payload.get("config"), problems)
        if config is not None and not config.verify.build_command.strip():
            problems.append("ビルドのコマンドを入れてください")
        if problems:
            return {"ok": False, "error": "試しのビルドを始められません", "problems": problems}
        with self.lock:
            if self.running():
                return {"ok": False, "error": "試しのビルドはもう動いています"}
            self.state = {"status": "running", "started": time.time()}
        threading.Thread(target=self._run, args=(config,), daemon=True).start()
        return {"ok": True, **self.state}

    def _run(self, config: ProjectConfig) -> None:
        v = config.verify
        try:
            result = prepare.trial_build(str(self.repo), v.setup_command, v.build_command, v.build_dir, config=config)
            state = {"status": "done", "result": result}
        except CtError as exc:
            state = {"status": "error", "error": str(exc), **exc.details}
        except Exception as exc:  # noqa: BLE001 - shown on the screen instead of a silent stop
            state = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        state["commands"] = {"setup_command": v.setup_command, "build_dir": v.build_dir,
                             "build_command": v.build_command}
        with self.lock:
            self.state = state


# ---- web server ----------------------------------------------------------------------------


class SettingsServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, repo: Path, token: str, port: int = 0):
        super().__init__(("127.0.0.1", port), Handler)
        self.repo = repo
        self.token = token
        self.started = time.monotonic()
        self.last_seen: float | None = None
        self.closed_by = ""
        self.saved: list[str] = []
        self.build = TrialBuild(repo)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/?t={self.token}"

    def close_later(self, reason: str) -> None:
        self.closed_by = self.closed_by or reason
        threading.Thread(target=self.shutdown, daemon=True).start()

    def watch_idle(self) -> None:
        """Stop when the page has been closed (no requests for a while)."""
        while not self.closed_by:
            time.sleep(5)
            if self.build.running():
                continue
            now = time.monotonic()
            if self.last_seen is None and now - self.started > FIRST_VISIT_SECONDS:
                self.close_later("idle")
            elif self.last_seen is not None and now - self.last_seen > IDLE_SECONDS:
                self.close_later("idle")


class Handler(BaseHTTPRequestHandler):
    server: SettingsServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - name from the base class
        pass  # requests carry the token; keep them out of the logs

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; connect-src 'self'; img-src data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _allowed(self, token: str | None) -> bool:
        port = self.server.server_address[1]
        host_ok = self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")
        return host_ok and token is not None and secrets.compare_digest(token, self.server.token)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(length).decode("utf-8") or "{}") if length else {}
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data

    def do_GET(self) -> None:  # noqa: N802 - name from the base class
        parts = urlsplit(self.path)
        if parts.path == "/":
            if not self._allowed((parse_qs(parts.query).get("t") or [None])[0]):
                self._send(HTTPStatus.FORBIDDEN, "この画面は /coverity-setup が示したアドレスで開いてください".encode(),
                           "text/plain; charset=utf-8")
                return
            self.server.last_seen = time.monotonic()
            self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if not self._allowed(self.headers.get("X-CT-Token")):
            self._json({"ok": False, "error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return
        self.server.last_seen = time.monotonic()
        try:
            if parts.path == "/api/state":
                self._json({"ok": True, **load_state(self.server.repo)})
            elif parts.path == "/api/credentials":
                query = parse_qs(parts.query)
                self._json({"ok": True, **credentials((query.get("user_env") or [""])[0],
                                                      (query.get("key_env") or [""])[0])})
            elif parts.path == "/api/trial-build":
                self._json({"ok": True, **self.server.build.state})
            elif parts.path == "/api/ping":
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        except CtError as exc:
            self._json({"ok": False, "error": str(exc), **exc.details})
        except OSError as exc:
            self._json({"ok": False, "error": f"ファイルの操作に失敗しました: {exc}"})

    def do_POST(self) -> None:  # noqa: N802 - name from the base class
        path = urlsplit(self.path).path
        if not self._allowed(self.headers.get("X-CT-Token")) or self.headers.get("Content-Type") != "application/json":
            self._json({"ok": False, "error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return
        self.server.last_seen = time.monotonic()
        try:
            body = self._body()
            if path == "/api/save":
                result = save(self.server.repo, body)
                self.server.saved.extend(result.get("written", []) + result.get("deleted", []))
                self._json(result)
            elif path == "/api/trial-build":
                self._json(self.server.build.start(body))
            elif path == "/api/close":
                self._json({"ok": True})
                self.server.close_later("button")
            else:
                self._json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, UnicodeDecodeError) as exc:
            self._json({"ok": False, "error": f"送られた内容を読めません: {exc}"}, HTTPStatus.BAD_REQUEST)
        except CtError as exc:
            self._json({"ok": False, "error": str(exc), **exc.details})
        except OSError as exc:
            self._json({"ok": False, "error": f"ファイルの操作に失敗しました: {exc}"})


def serve(repo_root: str, token: str | None = None, ready_file: str | None = None,
          open_browser: bool = False) -> dict[str, Any]:
    """Serve the screen until the person closes it (or leaves the page)."""
    repo = resolve_path(repo_root)
    prepare.detect(str(repo))  # stop early when the folder is missing
    server = SettingsServer(repo, token or secrets.token_urlsafe(24))
    threading.Thread(target=server.watch_idle, daemon=True).start()
    if ready_file:
        Path(ready_file).write_text(json.dumps({"url": server.url, "pid": os.getpid()}), encoding="utf-8")
    opened = webbrowser.open(server.url) if open_browser else False
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return {"ok": True, "url": server.url, "browser_opened": opened, "closed_by": server.closed_by,
            "saved": sorted(set(server.saved))}


def _spawn(args: list[str], env: dict[str, str], cwd: Path, log: Any) -> subprocess.Popen[bytes]:
    """Start the server so that it keeps running after this command (and its terminal) returns."""
    common: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT,
                              "cwd": str(cwd), "env": env, "close_fds": True}
    if sys.platform != "win32":
        return subprocess.Popen(args, start_new_session=True, **common)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:  # leave the terminal's job, so closing the terminal does not stop the screen
        return subprocess.Popen(args, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common)
    except OSError:
        return subprocess.Popen(args, creationflags=flags, **common)


def launch(repo_root: str, open_browser: bool = True) -> dict[str, Any]:
    """Start the screen in the background and return its address at once."""
    repo = resolve_path(repo_root)
    prepare.detect(str(repo))
    token = secrets.token_urlsafe(24)
    folder = Path(tempfile.mkdtemp(prefix="coverity-triage-gui-"))
    ready, log_path = folder / "ready.json", folder / "server.log"
    script = resolve_path(__file__).parents[1] / "ct.py"
    args = [sys.executable, str(script), "gui", "--repo", str(repo), "--serve", "--ready-file", str(ready)]
    with log_path.open("wb") as log:
        proc = _spawn(args, {**os.environ, TOKEN_ENV: token}, folder, log)
    deadline = time.monotonic() + READY_SECONDS
    while not ready.is_file():
        if proc.poll() is not None or time.monotonic() > deadline:
            if proc.poll() is None:
                proc.kill()
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise CtError("設定の画面を起動できませんでした", log=str(log_path), log_tail=tail)
        time.sleep(0.1)
    url = json.loads(ready.read_text(encoding="utf-8"))["url"]
    opened = webbrowser.open(url) if open_browser else False
    return {"ok": True, "url": url, "browser_opened": opened, "pid": proc.pid, "log": str(log_path),
            "next": "ブラウザに設定の画面が開いたことを人に伝え、保存して「終了」を押したらチャットで知らせてもらう。"
                    "開かなければ url を人に渡して、ブラウザで開いてもらう"}
