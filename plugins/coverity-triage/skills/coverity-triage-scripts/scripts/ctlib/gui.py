"""Screen of ``ct.py gui``: the settings of /coverity-setup, the results of the runs, and the monitor.

A small web page served only to this PC (127.0.0.1) and opened in the default browser.

- Settings (instead of the questions in chat): it reads and writes the same things as the questions,
  ``.coverity-triage/config.yaml``, ``filters/*.yaml``, ``knowledge.md``, ``no-grouping.yaml`` and the
  person's credentials (user environment variables).
- Results: the runs in the output folder, their summary and detailed reports, patches and fixed
  files, and the approvals the person enters (``approvals.json``, read by ``ct.py preview``).
- Monitor: what the main agent and its subagents are doing, and the serious errors (``activity.py``).

The page talks to this server with a random token, so other pages and programs cannot use it; the
files of a run, shown inside the page, are read with the same token in a cookie. One server per
repository: it keeps running until the person presses 「サーバを止める」, and ``ct.py gui`` opens the
running one again (found through a file in ``~/.coverity-triage/gui/``; a server whose place in that
file another one has taken stops, so none is left running where nothing can find it).
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import html
import http.client
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
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

import yaml
from pydantic import BaseModel, ValidationError

from . import activity, approvals, assess, envvars, prepare
from .common import CtError, display_text, file_lock, read_json, resolve_path, spawn_detached, write_text
from .config import (CONFIG_DIR, CONFIG_FILE, FILTERS_DIR, KNOWLEDGE_TEMPLATE, NO_GROUPING_FILE, FilterSpec,
                     ProjectConfig, knowledge_path, load_config, output_dir)
from .models import Issue, TriageResult
from .report import CONFIDENCE_ORDER, REPORT_CSS, diff_html, item_title
from .run import RUN_FILE, ItemState, Run, list_runs, unfinished

PAGE = Path(__file__).with_name("gui.html")
TOKEN_ENV = "CT_GUI_TOKEN"
STATE_DIR_ENV = "CT_GUI_STATE_DIR"  # where the files of the running servers are (tests use a temporary folder)
READY_SECONDS = 30
PING_SECONDS = 3
WATCH_SECONDS = 3  # how often a server checks that it is still the one in its repository's file
VIEWS = ("monitor", "summary", "settings")
RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]+)?$")
RUNS_LISTED = 50
MAX_VIEW_LINES = 20000
FORBIDDEN_TEXT = "この画面は ct.py gui が示したアドレス（?t= が付いたもの）で開いてください"
NOT_FOUND_TEXT = "ファイルが見つかりません"
SCREEN_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
              "img-src data:; frame-src 'self'; frame-ancestors 'none'")
# Files of a run hold text written by the AI: no script runs in them. allow-same-origin keeps the
# links between them working (a document with an opaque origin would not send the SameSite cookie).
FILE_CSP = ("sandbox allow-same-origin; default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "frame-ancestors 'self'")
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
    if not str(verify.get("build_command") or "").strip():
        if verify.get("default", "none") != "none":
            problems.append("ビルドでの検証: ビルドのコマンドを入れてください")
        elif verify.get("initial") is True:  # new-run refuses it the same way
            problems.append("初回解析: ビルドのコマンドを入れてください")
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


# ---- runs: results and monitor -------------------------------------------------------------


class NotFound(CtError):
    """A run, an item or a file that the screen does not show (HTTP 404)."""


APPLIED_JA = {"fix": "修正", "annotation": "逸脱の注釈", "coverity": "Coverity への逸脱の登録", "reject": "却下"}
FIXED_JA = {"fix": "修正後のファイル", "annotation": "注釈を入れたファイル"}


def find_run(repo: Path, run_id: str) -> Run:
    """A run folder directly in the output folder of the settings, by its name only."""
    if not RUN_ID.match(run_id or ""):
        raise NotFound(f"実行が見つかりません: {run_id}")
    output = output_dir(repo, load_config(repo))
    if not (output / run_id / RUN_FILE).is_file():
        raise NotFound(f"実行が見つかりません: {run_id}")
    try:
        run = Run(output / run_id)
    except ValidationError as exc:
        raise CtError(f"run.json を読めません: {output / run_id}: {exc}") from exc
    if run.dir.parent != output:  # a link to a folder somewhere else
        raise NotFound(f"実行が見つかりません: {run_id}")
    return run


def _find_item(run: Run, item_id: str) -> ItemState:
    for item in run.state.items:
        if item.id == item_id:
            return item
    raise NotFound(f"作業がありません: {item_id}")


def _result(run: Run, item_id: str) -> TriageResult | None:
    try:
        return TriageResult.model_validate(read_json(run.result_file(item_id)))
    except (CtError, ValidationError):
        return None


def _issues(run: Run) -> dict[int, Issue]:
    """The CIDs of the run, with repository-relative paths when ``plan`` has made them."""
    for read in (run.mapped_issues, run.issues):
        try:
            return read()
        except (CtError, ValidationError):
            continue
    return {}


def _unfinished(run: Run) -> bool:
    return unfinished(run.state, run.dir)


def _run_info(run: Run) -> dict[str, Any]:
    state = run.state
    counts = state.counts()
    # an item the integration took into another one is not counted (it is shown inside that one)
    return {"run_id": run.dir.name, "run_dir": str(run.dir), "created_at": state.created_at,
            "filter_name": state.filter_name, "stream": state.stream, "verify_mode": state.verify_mode,
            "counts": counts, "total": len(state.items) - counts.get("merged", 0), "unfinished": _unfinished(run)}


def _logged_lately(run: Run) -> bool:
    try:
        return time.time() - (run.dir / "operations.log").stat().st_mtime < activity.STALL_SECONDS
    except OSError:
        return False


def _run_row(run: Run) -> dict[str, Any]:
    has_summary = any(run.summary_file(ext).is_file() for ext in ("html", "md"))
    if _unfinished(run) or not has_summary or _logged_lately(run):
        stage, label = activity.run_stage(run)
    else:  # a finished run quiet for long: its log is not read for each of the older runs (same as the monitor)
        stage, label = "done", activity.done_label(run)
    return {**_run_info(run), "summary": has_summary, "stage": stage, "stage_label": label,
            "applied": sum(1 for i in run.state.items if i.applied)}


def runs_list(repo: Path) -> dict[str, Any]:
    """The runs in the output folder of the settings, newest first."""
    try:
        output = output_dir(repo, load_config(repo))
    except CtError as exc:
        return {"ok": True, "runs": [], "output_dir": None, "note": f"実行を探せません: {exc}"}
    found = []
    for folder in list_runs(output):
        if len(found) >= RUNS_LISTED:
            break
        if not RUN_ID.match(folder.name):
            continue
        try:
            run = Run(folder)
        except (CtError, ValidationError):
            continue
        if run.dir.parent == output:  # not a link to a folder somewhere else (find_run would refuse it)
            found.append(_run_row(run))
    out: dict[str, Any] = {"ok": True, "runs": found, "output_dir": str(output)}
    if not found:
        out["note"] = f"まだ実行がありません（/coverity-run で始めます。結果を置く場所: {output}）"
    return out


def _item_row(run: Run, item: ItemState, issues: dict[int, Issue],
              decided: dict[str, dict[str, Any]], summary_made: bool) -> dict[str, Any]:
    from .triage import review_of  # late import: triage is the larger module

    result = _result(run, item.id)
    review = review_of(run, item) if item.status == "done" else {}
    if result is not None:  # the confidence the reports show: the lowest of the worker, the review and the build
        verify = read_json(run.dir / "verify.json", {}).get(item.id) if (run.dir / "verify.json").is_file() else None
        result = assess.effective(result, review, verify)
    main = issues.get(item.cids[0]) if item.cids else None
    entry = decided.get(item.id)
    recommendation = result.recommendation if result else None
    default = approvals.default_decision(run, item.id, result) if result else None
    report = run.report_file(item.id)
    applied = [part for part in APPLIED_JA if part in item.applied]
    return {"id": item.id, "title": item_title(item.id, len(item.cids)), "cids": list(item.cids),
            "checker": main.checker if main else "", "file": main.file if main else "",
            "line": main.line if main else None, "impact": (main.impact or "") if main else "",
            "recommendation": recommendation, "confidence": result.confidence if result else None,
            "judgement": result.verdict.judgement if result else None,
            "summary": result.verdict.summary if result else "",
            "default_decision": default,  # the recommendation as an approval (None: not applied)
            "already_fixed": bool(result and result.recommendation == "fix" and default is None),
            # What preview will apply: until ct.py summary puts the recommendation in, the recommendation;
            # after it, an item without an approval (a row of an old summary.md that could not be read) is not.
            "decision": entry["decision"] if entry else None if summary_made else default,
            "edited": bool(entry and entry["edited"]), "saved": entry is not None, "status": item.status,
            "applied": bool(applied), "applied_parts": applied,
            # the independent review (option agents.review): pass / flag / unverified, None without one
            "review": review.get("outcome") if review.get("outcome") in ("pass", "flag", "rework", "unverified") else None,
            "merged_from": list(item.merged_from),
            "report": f"cid/{item.id}.html" if report.is_file() else None}


def run_summary(repo: Path, run_id: str) -> dict[str, Any]:
    """The table of the screen's results: finished items with their approvals, low confidence first."""
    run = find_run(repo, run_id)
    decided = approvals.load(run)
    issues = _issues(run)
    made = approvals.summary_made(run)
    items, errors, unprocessed = [], [], []
    for item in run.state.items:
        if item.status == "done":
            items.append(_item_row(run, item, issues, decided, made))
        elif item.status == "error":
            errors.append({"id": item.id, "cids": list(item.cids), "reason": item.error})
        elif item.status != "merged":  # merged items are shown inside the item that took them
            unprocessed.append(item.id)
    items.sort(key=lambda r: (CONFIDENCE_ORDER.get(r["confidence"], -1), r["id"]))  # as in summary.html
    return {"ok": True, "run": _run_info(run), "items": items, "errors": errors, "unprocessed": unprocessed,
            "summary_html": run.summary_file().is_file()}


def _file_urls(run_id: str, rel: str) -> dict[str, str]:
    path = quote(rel, safe="/")
    return {"url": f"/view/{run_id}/{path}", "raw_url": f"/files/{run_id}/{path}"}


def item_detail(repo: Path, run_id: str, item_id: str) -> dict[str, Any]:
    """One finished item for the approval panel: the deviation to register, and links to its files."""
    run = find_run(repo, run_id)
    item = _find_item(run, item_id)
    if item.status != "done":
        raise CtError(f"{item_id} はまだ終わっていません（状態: {item.status}）")
    result = _result(run, item_id)
    if result is None:
        raise CtError(f"{item_id} の結果ファイルを読めません: {run.result_file(item_id)}")
    decided = approvals.load(run)
    note = None
    try:
        attrs, comment = approvals.deviation_for(run, item_id)
        deviation = {**attrs.model_dump(), "comment": comment}
    except CtError as exc:  # a run made before approvals.json whose cid/<ID>.md lost its markers
        deviation, note = result.deviation.model_dump(), f"{exc}（AI の案を出しています。保存すると直せます）"
    saved = read_json(run.item_dir(item_id) / "saved.json", {})
    patches, fixed = [], []
    for kind in ("fix", "annotation"):
        entry = saved.get(kind) if isinstance(saved, dict) else None
        if not isinstance(entry, dict):
            continue
        name = Path(str(entry.get("patch") or "")).name
        if name and (run.dir / "patches" / name).is_file():
            patches.append({"kind": kind, "name": name, **_file_urls(run_id, f"patches/{name}")})
        for rel in entry.get("files") or []:
            rel = str(rel).replace("\\", "/")
            if (run.dir / "fixed" / item_id / kind / rel).is_file():  # a deleted file has no copy
                fixed.append({"kind": kind, "path": rel, **_file_urls(run_id, f"fixed/{item_id}/{kind}/{rel}")})
    report = run.report_file(item_id)
    out = {"ok": True, "item": _item_row(run, item, _issues(run), decided, approvals.summary_made(run)),
           "deviation": deviation,
           "ai_deviation": result.deviation.model_dump(),
           "deviation_edited": bool((decided.get(item_id) or {}).get("deviation")),
           "patches": patches, "fixed_files": fixed,
           "report_url": f"/files/{run_id}/cid/{quote(item_id)}.html" if report.is_file() else None}
    if note:
        out["deviation_note"] = note
    return out


def set_approval(repo: Path, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Save the person's approval of one item; the summary and its report are written again."""
    run = find_run(repo, run_id)
    item_id = body.get("item")
    if not isinstance(item_id, str) or not item_id:
        raise CtError("承認する作業（item）を指定してください")
    item = _find_item(run, item_id)
    if (run.state.integration or run.state.review) and not approvals.summary_made(run):
        # the integration and the review may still change the item: approvals wait for the summary
        raise CtError("統合・検証が終わって一覧ができてから承認できます（/coverity-run が終わるまでお待ちください）")
    before = approvals.load(run).get(item_id) or {}
    entry = approvals.update(run, item_id, body.get("decision"), body.get("deviation"))
    out = {"ok": True, "item": item_id, **entry}
    applied = [APPLIED_JA[part] for part in APPLIED_JA if part in item.applied]
    if applied and (before.get("decision"), before.get("deviation")) != (entry["decision"], entry.get("deviation")):
        out["warning"] = (f"{item_id} はもう反映しています（{'、'.join(applied)}）。承認を変えても、反映したものは"
                          "元に戻りません。まだ反映していない部分は、次の /coverity-apply で反映します")
    return out


# ---- files of a run (/files and /view) -------------------------------------------------------


def _plain_name(name: str) -> bool:
    return name not in ("", ".", "..") and not any(c in name for c in "/\\:\0")


def _shown(parts: list[str], view: bool) -> bool:
    """Only the reports, the patches and the fixed files; /view only the patches and the fixed files."""
    head, name = parts[0], parts[-1]
    if head == "patches":
        return len(parts) == 2 and name.endswith(".patch")
    if head == "fixed":
        return len(parts) >= 2
    if view:
        return False
    return parts == ["summary.html"] or (head == "cid" and len(parts) == 2 and name.endswith(".html"))


def run_file(repo: Path, run_id: str, rel: str, view: bool = False) -> tuple[Path, list[str]]:
    """The file ``rel`` (as in the URL) of a run, and its path parts; NotFound for anything else.

    No ``..``, no encoded separators and no links, which could point anywhere on this PC."""
    try:
        parts = [unquote(p, errors="strict") for p in rel.split("/")]
    except UnicodeDecodeError:
        raise NotFound(rel) from None
    if not all(_plain_name(p) for p in parts) or not _shown(parts, view):
        raise NotFound(rel)
    run = find_run(repo, run_id)
    path = run.dir
    for name in parts:
        path = path / name
        if path.is_symlink() or path.is_junction():
            raise NotFound(rel)
    if not path.is_file() or run.dir not in resolve_path(path).parents:
        raise NotFound(rel)
    return path, parts


def _charset(data: bytes) -> str:
    """UTF-8, or Shift_JIS for files saved in cp932 (common on Japanese Windows)."""
    for codec, label in (("utf-8", "utf-8"), ("cp932", "shift_jis")):
        try:
            data.decode(codec)
            return label
        except UnicodeDecodeError:
            continue
    return "utf-8"


def _content_type(parts: list[str], data: bytes) -> str:
    """The reports are pages; everything else, an HTML file among the fixed files too, is shown as text."""
    report = parts[0] in ("summary.html", "cid")
    return "text/html; charset=utf-8" if report else f"text/plain; charset={_charset(data)}"


VIEW_CSS = """
.head h1 { font-size: 1.2rem; overflow-wrap: anywhere; }
.head .actions { margin: 6px 0 0; display: flex; flex-wrap: wrap; gap: 4px 16px; }
pre.code { margin: 6px 0; padding: 10px 0; background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; overflow-x: auto; font-size: .84rem; line-height: 1.5; counter-reset: line; }
pre.code > span { display: block; padding: 0 12px 0 0; min-height: 1.5em; }
pre.code > span::before { counter-increment: line; content: counter(line); display: inline-block; width: 4.5em;
  padding-right: .8em; margin-right: .8em; text-align: right; color: var(--meta-fg);
  border-right: 1px solid var(--border); user-select: none; -webkit-user-select: none; }
@media print { pre.code { white-space: pre-wrap; overflow-wrap: anywhere; overflow: visible; } }
"""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _numbered(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        return '<p class="muted">（空のファイルです）</p>'
    rows = "".join(f"<span>{_e(line)}\n</span>" for line in lines[:MAX_VIEW_LINES])
    block = f'<pre class="code">{rows}</pre>'
    if len(lines) > MAX_VIEW_LINES:
        block += (f'\n<p class="muted">長いため先頭 {MAX_VIEW_LINES} 行だけ表示しています。'
                  "全体は「元のファイル」で見てください。</p>")
    return block


def view_page(run_id: str, parts: list[str], data: bytes) -> str:
    """A patch with colored lines, or a fixed file with line numbers. Everything is escaped here; no script."""
    rel = "/".join(parts)
    if parts[0] == "patches":
        label, shown = "差分ファイル", parts[-1]
    else:
        kind = parts[2] if len(parts) > 3 else ""
        label = f"{parts[1]} の{FIXED_JA[kind]}" if kind in FIXED_JA else "修正後のファイル"
        shown = "/".join(parts[3:]) if kind in FIXED_JA else rel
    if b"\0" in data[:8192]:
        body = '<p class="muted">文字のファイルではないため、ここには表示できません。</p>'
    elif parts[0] == "patches":
        body = diff_html(display_text(data), MAX_VIEW_LINES)
    else:
        body = _numbered(display_text(data))
    raw = f"/files/{run_id}/{quote(rel, safe='/')}"
    header = (f'<header class="head">\n<p class="crumbs">{_e(label)}</p>\n<h1><code>{_e(shown)}</code></h1>\n'
              f'<p class="actions"><span class="muted">実行 {_e(run_id)} の <code>{_e(rel)}</code></span>'
              f'<a href="{_e(raw)}">元のファイル</a></p>\n</header>')
    return ('<!doctype html>\n<html lang="ja">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{_e(shown)}</title>\n<style>{REPORT_CSS}{VIEW_CSS}</style>\n</head>\n'
            f'<body>\n<div class="page">\n{header}\n{body}\n</div>\n</body>\n</html>\n')


# ---- web server ----------------------------------------------------------------------------

RUN_ROUTE = re.compile(r"^/api/runs/([^/]+)/(summary|activity|approvals|items/([^/]+))$")


@functools.cache
def code_version() -> str:
    """Fingerprint of these scripts: a server started by other (older) scripts is replaced, not reused."""
    digest = hashlib.sha256()
    folder = Path(__file__).parent
    for path in [*sorted(folder.glob("*.py")), PAGE, folder.parent / "ct.py"]:
        with contextlib.suppress(OSError):
            digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes())
    return digest.hexdigest()[:16]


class SettingsServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, repo: Path, token: str, port: int = 0):
        super().__init__(("127.0.0.1", port), Handler)
        self.repo = repo
        self.token = token
        self.version = code_version()
        self.closed_by = ""
        self.saved: list[str] = []
        self.build = TrialBuild(repo)

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    @property
    def cookie_name(self) -> str:
        # Cookies are shared by every port of 127.0.0.1: one name per server, so two screens do not mix.
        return f"ct_token_{self.port}"

    def record(self) -> dict[str, Any]:
        """What ``ct.py gui`` needs to find and reuse this server."""
        return {"url": self.url, "pid": os.getpid(), "port": self.port, "token": self.token,
                "repo": str(self.repo), "version": self.version}

    def close_later(self, reason: str) -> None:
        self.closed_by = self.closed_by or reason
        threading.Thread(target=self.shutdown, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server: SettingsServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - name from the base class
        pass  # requests carry the token; keep them out of the logs

    def _send(self, status: int, body: bytes, content_type: str, *, framed: bool = False,
              cookie: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        # The files of a run are shown inside the page (iframe); the page itself is never framed.
        self.send_header("X-Frame-Options", "SAMEORIGIN" if framed else "DENY")
        self.send_header("Content-Security-Policy", FILE_CSP if framed else SCREEN_CSP)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _allowed(self, token: str | None) -> bool:
        port = self.server.port
        host_ok = self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")
        return host_ok and token is not None and secrets.compare_digest(token, self.server.token)

    def _cookie_allowed(self) -> bool:
        for header in self.headers.get_all("Cookie") or []:
            for pair in header.split(";"):
                name, _, value = pair.strip().partition("=")
                if name == self.server.cookie_name and self._allowed(value):
                    return True
        return False

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(length).decode("utf-8") or "{}") if length else {}
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data

    def _answer(self, handle: Any, *args: Any) -> None:
        """The JSON answer of an API, with what went wrong instead of a dropped connection."""
        try:
            self._json(handle(*args))
        except NotFound as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
        except CtError as exc:
            self._json({"ok": False, "error": str(exc), **exc.details})
        except OSError as exc:
            self._json({"ok": False, "error": f"ファイルの操作に失敗しました: {exc}"})
        except Exception as exc:  # noqa: BLE001 - the screen shows it; the server keeps running
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_GET(self) -> None:  # noqa: N802 - name from the base class
        parts = urlsplit(self.path)
        if parts.path == "/":
            token = (parse_qs(parts.query).get("t") or [None])[0]
            if not self._allowed(token):
                self._send(HTTPStatus.FORBIDDEN, FORBIDDEN_TEXT.encode(), "text/plain; charset=utf-8")
                return
            cookie = f"{self.server.cookie_name}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"
            self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8", cookie=cookie)
            return
        if parts.path.startswith(("/files/", "/view/")):
            self._file(parts.path)
            return
        if not self._allowed(self.headers.get("X-CT-Token")):
            self._json({"ok": False, "error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return
        self._answer(self._get, parts.path, parse_qs(parts.query))

    def _get(self, path: str, query: dict[str, list[str]]) -> dict[str, Any]:
        repo = self.server.repo
        if path == "/api/state":
            return {"ok": True, **load_state(repo)}
        if path == "/api/credentials":
            names = [(query.get(key) or [""])[0] for key in ("user_env", "key_env")]
            return {"ok": True, **credentials(*names)}
        if path == "/api/trial-build":
            return {"ok": True, **self.server.build.state}
        if path == "/api/ping":
            return {"ok": True, "repo": str(repo), "version": self.server.version}
        if path == "/api/runs":
            return runs_list(repo)
        route = RUN_ROUTE.match(path)
        if route and route.group(2) == "summary":
            return run_summary(repo, route.group(1))
        if route and route.group(2) == "activity":
            return {"ok": True, **activity.board(find_run(repo, route.group(1)))}
        if route and route.group(3) is not None:
            return item_detail(repo, route.group(1), unquote(route.group(3)))
        raise NotFound("not found")

    def _file(self, path: str) -> None:
        """``/files/<run id>/<path>``: a file of a run as it is; ``/view/<run id>/<path>``: its page."""
        if not (self._cookie_allowed() or self._allowed(self.headers.get("X-CT-Token"))):
            self._send(HTTPStatus.FORBIDDEN, FORBIDDEN_TEXT.encode(), "text/plain; charset=utf-8", framed=True)
            return
        kind, _, rest = path[1:].partition("/")
        run_id, _, rel = rest.partition("/")
        try:
            target, parts = run_file(self.server.repo, run_id, rel, view=kind == "view")
            data = target.read_bytes()
        except (CtError, OSError):
            self._send(HTTPStatus.NOT_FOUND, NOT_FOUND_TEXT.encode(), "text/plain; charset=utf-8", framed=True)
            return
        if kind == "view":
            self._send(HTTPStatus.OK, view_page(run_id, parts, data).encode("utf-8"), "text/html; charset=utf-8",
                       framed=True)
        else:
            self._send(HTTPStatus.OK, data, _content_type(parts, data), framed=True)

    def do_POST(self) -> None:  # noqa: N802 - name from the base class
        path = urlsplit(self.path).path
        if not self._allowed(self.headers.get("X-CT-Token")) or self.headers.get("Content-Type") != "application/json":
            self._json({"ok": False, "error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return
        try:
            body = self._body()
        except (ValueError, UnicodeDecodeError) as exc:
            self._json({"ok": False, "error": f"送られた内容を読めません: {exc}"}, HTTPStatus.BAD_REQUEST)
            return
        self._answer(self._post, path, body)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path == "/api/save":
            result = save(self.server.repo, body)
            self.server.saved.extend(result.get("written", []) + result.get("deleted", []))
            return result
        if path == "/api/trial-build":
            return self.server.build.start(body)
        if path == "/api/close":
            self.server.close_later("button")
            return {"ok": True}
        route = RUN_ROUTE.match(path)
        if route and route.group(2) == "approvals":
            return set_approval(self.server.repo, route.group(1), body)
        raise NotFound("not found")


# ---- starting, reusing and stopping ----------------------------------------------------------


def state_dir() -> Path:
    return Path(os.environ.get(STATE_DIR_ENV) or Path.home() / ".coverity-triage" / "gui")


def server_file(repo: Path) -> Path:
    """The file of the server running for this repository (its address and token)."""
    return state_dir() / f"{hashlib.sha256(os.path.normcase(str(repo)).encode('utf-8')).hexdigest()[:16]}.json"


def _remember(path: Path, server: SettingsServer) -> bool:
    """Best effort: without the file, the next ``ct.py gui`` starts another server."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # it holds the token
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(server.record(), f)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def _read_server_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not isinstance(data.get("port"), int):
        return None
    return data


def _forget(path: Path, token: str) -> None:
    """Remove the file when it is still this server's (a newer server may have replaced it)."""
    found = _read_server_file(path)
    if found is not None and found["token"] == token:
        with contextlib.suppress(OSError):
            path.unlink()


def _watch(server: SettingsServer, path: Path, stopped: threading.Event) -> None:
    """Stop once another server has taken this repository's file (``ct.py gui --foreground``, or a file that
    could not be read): ct.py gui can no longer find this one, and one server per repository is kept.

    This is not an idle stop: a server that is still in the file runs until 「サーバを止める」, and a file
    that is gone or broken never stops it."""
    while not stopped.wait(WATCH_SECONDS):
        found = _read_server_file(path)
        if found is not None and found["token"] != server.token:
            server.close_later("replaced")
            return


def _call(port: int, token: str, method: str, path: str) -> dict[str, Any] | None:
    """Ask a server on this PC; None when nothing (or something else) answers."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=PING_SECONDS)
    headers = {"Host": f"127.0.0.1:{port}", "X-CT-Token": token}
    if method == "POST":
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=b"{}" if method == "POST" else None, headers=headers)
        res = conn.getresponse()
        data = json.loads(res.read().decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    return data if res.status == HTTPStatus.OK and isinstance(data, dict) and data.get("ok") else None


def _running(repo: Path, path: Path) -> dict[str, Any] | None:
    """The server of this repository's file, if it still answers and runs these scripts."""
    found = _read_server_file(path)
    if found is None:
        return None
    answer = _call(found["port"], found["token"], "GET", "/api/ping")
    if answer is None or answer.get("repo") != str(repo):
        return None  # stopped (or the PC was restarted): a new server replaces the file
    if answer.get("version") != code_version():
        _call(found["port"], found["token"], "POST", "/api/close")  # started by other scripts: replaced
        return None
    return {"url": f"http://127.0.0.1:{found['port']}/?t={found['token']}", "pid": found.get("pid")}


def _fragment(repo: Path, run_dir: str | None, view: str | None) -> tuple[str, str | None]:
    """``#run=<run id>&view=<view>`` that the page reads, and a note when the run cannot be shown."""
    if view is not None and view not in VIEWS:
        raise CtError(f"--view は {' / '.join(VIEWS)} のどれかです（今は {view}）")
    if run_dir is None:
        return ("#" + urlencode({"view": view}) if view else ""), None
    try:
        run = Run(run_dir)
    except ValidationError as exc:
        raise CtError(f"run.json を読めません: {run_dir}: {exc}") from exc
    if not RUN_ID.match(run.dir.name):
        raise CtError(f"new-run が作った実行フォルダを指定してください: {run.dir}")
    try:
        listed = run.dir.parent == output_dir(repo, load_config(repo))
    except CtError:
        listed = False
    note = None if listed else ("この実行フォルダは、設定の「結果を置く場所」の中にないため、"
                                "画面の「結果」と「監視」には出ません")
    return "#" + urlencode({"run": run.dir.name, "view": view or "monitor"}), note


def serve(repo_root: str, token: str | None = None, ready_file: str | None = None, open_browser: bool = False,
          run_dir: str | None = None, view: str | None = None) -> dict[str, Any]:
    """Serve the screen until the person presses 「サーバを止める」; it never stops because nobody uses it.

    A server already running for the repository is replaced: it stops when it sees this one in the file."""
    repo = resolve_path(repo_root)
    prepare.detect(str(repo))  # stop early when the folder is missing
    fragment, _ = _fragment(repo, run_dir, view)
    server = SettingsServer(repo, token or secrets.token_urlsafe(24))
    path = server_file(repo)
    remembered = _remember(path, server)  # a server running for the repository stops when it sees this one
    stopped = threading.Event()
    opened = False
    try:
        if ready_file:  # written whole at once: ct.py gui reads it as soon as it appears
            write_text(Path(ready_file), json.dumps({"url": server.url, "pid": os.getpid()}))
        opened = webbrowser.open(server.url + fragment) if open_browser else False
        if remembered:  # started last: a shutdown asked for before serve_forever runs would be lost
            threading.Thread(target=_watch, args=(server, path, stopped), daemon=True).start()
        server.serve_forever(poll_interval=0.2)
    finally:
        stopped.set()
        server.server_close()
        _forget(path, server.token)
    return {"ok": True, "url": server.url, "browser_opened": opened, "closed_by": server.closed_by,
            "saved": sorted(set(server.saved))}


def _spawn(args: list[str], env: dict[str, str], cwd: Path, log: Any) -> subprocess.Popen[bytes]:
    """Start the server so that it keeps running after this command (and its terminal) returns."""
    return spawn_detached(args, env, cwd, log)


def _start(repo: Path) -> dict[str, Any]:
    """A new server in the background; returns once it answers."""
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
            raise CtError("画面を起動できませんでした", log=str(log_path), log_tail=tail)
        time.sleep(0.1)
    return {"url": json.loads(ready.read_text(encoding="utf-8"))["url"], "pid": proc.pid, "log": str(log_path)}


NEXT = {
    "settings": "ブラウザに画面が開いたことを人に伝え、設定を保存したらチャットで知らせてもらう。"
                "画面は開いたままにして、結果の閲覧と監視にも使える",
    "monitor": "ブラウザに監視の画面が開いたことを人に伝え（AI の動きと重大なエラーを見られる）、そのまま作業を続ける",
    "summary": "ブラウザに結果の画面が開いたことを人に伝え、「結果」で承認欄を確かめてから "
               "/coverity-apply を使うよう案内する",
}


def launch(repo_root: str, open_browser: bool = True, run_dir: str | None = None,
           view: str | None = None) -> dict[str, Any]:
    """Open the screen: the server already running for this repository, or a new one in the background."""
    repo = resolve_path(repo_root)
    prepare.detect(str(repo))
    fragment, note = _fragment(repo, run_dir, view)
    path = server_file(repo)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock: Any = file_lock(path, timeout=READY_SECONDS + 15)  # two at once start only one server
    except OSError:
        lock = contextlib.nullcontext()
    with lock:
        running = _running(repo, path)
        started = running or _start(repo)
    url = started["url"] + fragment
    opened = webbrowser.open(url) if open_browser else False
    out = {"ok": True, "url": url, "browser_opened": opened, "reused": running is not None, "pid": started["pid"],
           "next": NEXT[view or ("monitor" if run_dir else "settings")]
                   + "。画面が止まるのは、画面の「サーバを止める」を押したときだけ。"
                     "開かなければ url を人に渡して、ブラウザで開いてもらう"}
    if "log" in started:
        out["log"] = started["log"]
    if note:
        out["note"] = note
    return out
