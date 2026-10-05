"""MCP server ``coverity-triage``: communication with Coverity Connect only.

Each tool returns within about 20 seconds, because the Copilot runtime stops an MCP tool call after
30 seconds by default. When work remains, the tool returns ``done: false`` and the agent calls it
again with the same arguments; work already saved is not repeated.

Results go to the files that the scripts of the skill ``coverity-triage-scripts`` name, so that large
data does not pass through the conversation.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from .connect import check_auth
from .coverity import CoverityClient, CoverityError, make_client
from .models import TriageAttributes, TriageEntry
from .settings import SettingsError, load_coverity_config, load_filter

BUDGET = 20.0  # seconds one tool call may use
DEFAULT_LIMIT = 100
MAX_CIDS = 50
CALL_AGAIN = "続きがあります。同じ引数でもう一度呼んでください"

mcp = MCPServer(
    name="coverity-triage",
    instructions=(
        "Coverity Connect との通信だけを行うツールです。引数のパスは、スキル coverity-triage-scripts の"
        "スクリプト（ct.py）が返したものを絶対パスのまま渡します。"
        "結果に done: false があれば、同じ引数でもう一度呼びます。"
    ),
)


def tool(title: str, **hints: bool):
    """Register a tool and turn expected errors into messages the agent can act on."""
    def register(func):
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except (SettingsError, CoverityError, ValueError, OSError) as exc:
                raise ToolError(str(exc)) from exc
        annotations = ToolAnnotations(title=title, open_world_hint=False, **hints)
        return mcp.tool(title=title, annotations=annotations)(wrapper)
    return register


class Deadline:
    def __init__(self, budget: float = BUDGET):
        self.started = time.monotonic()
        self.budget = budget

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def left(self) -> float:
        return self.budget - self.elapsed()

    def allows(self, client: CoverityClient, requests: int = 1) -> bool:
        """Whether ``requests`` more requests are expected to end within the budget."""
        return self.elapsed() + requests * client.request_seconds() <= self.budget


def _path(value: str, name: str) -> Path:
    # The server runs in its own folder (uv run --directory), so a relative path would point there.
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} は絶対パスで指定してください: {value}")
    return path


def _folder(value: str, name: str) -> Path:
    path = _path(value, name)
    if not path.is_dir():
        raise ValueError(f"{name} のフォルダがありません: {path}（ct.py が作ったパスを渡してください）")
    return path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"ファイルを読めません: {path}: {exc}") from exc


def _write_json(path: Path, data: Any) -> None:
    """Write via a temporary file so an interrupted call never leaves half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@tool("Coverity 接続の確認", read_only_hint=True)
def check_connection(repo_root: str) -> dict[str, Any]:
    """設定ファイル（<repo_root>/.coverity-triage/config.yaml）の Coverity に、認証情報の環境変数で
    1 回だけ読み取りの問い合わせをし、認証できるかを確かめる（10 秒以内）。認証キーの値は返さない。"""
    config = load_coverity_config(_folder(repo_root, "repo_root"))
    if config.api == "fake":
        make_client(config).close()  # checks that the file exists
        return {"ok": True, "result": "偽データを使う設定です（coverity.api: fake）", "fake_data": config.fake_data}
    out = check_auth(config)
    out["url"] = config.url
    if config.url.lower().startswith("http://"):
        out["warning"] = ("URL が http です。認証キーが暗号化されずに送られます。"
                          "https が使えるか Coverity の管理者に確認してください")
    return out


@tool("Coverity の警告の検索", read_only_hint=False, destructive_hint=False, idempotent_hint=True)
def search_issues(repo_root: str, filter_file: str, output_file: str) -> dict[str, Any]:
    """条件のファイル（ct.py new-run が作る filter.json）で Coverity の警告を検索し、output_file
    （issues.json）に保存する。条件の limit 件で止める。最新スナップショットの解析リビジョンも記録する。
    done が false なら、同じ引数でもう一度呼ぶ（保存済みの続きから検索する）。"""
    deadline = Deadline()
    config = load_coverity_config(_folder(repo_root, "repo_root"))
    filter_path = _path(filter_file, "filter_file")
    spec = load_filter(filter_path)
    out = _path(output_file, "output_file")
    _folder(str(out.parent), "output_file の保存先")
    filter_sha = _sha(filter_path.read_bytes())[:12]
    state = _read_json(out) if out.is_file() else None
    if not isinstance(state, dict) or state.get("filter_sha") != filter_sha:
        state = {"filter_sha": filter_sha, "stream": spec.streams[0], "limit": spec.limit or DEFAULT_LIMIT,
                 "started_at": _now(), "finished_at": None, "coverity_total": None, "next_offset": 0,
                 "limited": False, "revision_field": config.revision_field or None,
                 "revision_checked": not config.revision_field, "analyzed_revision": None,
                 "revision_error": None, "issues": [], "requests": []}
    limit = state["limit"]
    client = make_client(config)
    try:
        if state["next_offset"] is not None and deadline.allows(client):
            page = client.search(spec, limit - len(state["issues"]), state["next_offset"], deadline.left())
            known = {i["cid"] for i in state["issues"]}
            # A new snapshot between calls can shift the pages; never keep a CID twice.
            found = [i.model_dump(exclude_none=True) for i in page.issues if i.cid not in known]
            state["issues"] += found
            state["coverity_total"] = page.total
            if len(state["issues"]) >= limit:
                state["limited"] = len(state["issues"]) > limit or page.next_offset is not None
                state["issues"] = state["issues"][:limit]
                state["next_offset"] = None
            else:
                state["next_offset"] = page.next_offset
        if state["next_offset"] is None and not state["revision_checked"] and deadline.allows(client, 2):
            try:
                state["analyzed_revision"] = client.snapshot_revision(state["stream"], state["revision_field"])
            except CoverityError as exc:
                if exc.fatal:
                    raise
                state["revision_error"] = str(exc)  # the run goes on with the local code
            state["revision_checked"] = True
    finally:
        state["requests"] += client.requests
        client.close()
        done = state["next_offset"] is None and state["revision_checked"]
        if done and not state["finished_at"]:
            state["finished_at"] = _now()
        _write_json(out, state)
    return {"done": done, "found": len(state["issues"]), "limit": limit, "limited": state["limited"],
            "coverity_total": state["coverity_total"], "analyzed_revision": state["analyzed_revision"],
            "revision_error": state["revision_error"], "seconds": round(deadline.elapsed(), 1),
            "output_file": str(out), "next": None if done else CALL_AGAIN}


@tool("Coverity の警告経路の取得", read_only_hint=False, destructive_hint=False, idempotent_hint=True)
def get_issues(repo_root: str, stream: str, cids: list[int], output_dir: str) -> dict[str, Any]:
    """CID ごとに警告経路（イベント）とチェッカーの説明を取り、output_dir に issue-<CID>.json として
    保存する。取れなかった CID は、理由を error に書いた issue-<CID>.json を保存する。
    保存済みの CID は取り直さない。done が false なら、同じ引数でもう一度呼ぶ。"""
    deadline = Deadline()
    config = load_coverity_config(_folder(repo_root, "repo_root"))
    folder = _folder(output_dir, "output_dir")
    wanted = list(dict.fromkeys(cids))
    if not wanted or len(wanted) > MAX_CIDS:
        raise ValueError(f"cids は 1〜{MAX_CIDS} 件で指定してください（{len(wanted)} 件）")
    saved: list[int] = []
    already: list[int] = []
    failed: dict[str, str] = {}
    remaining: list[int] = []
    client = make_client(config)
    try:
        for cid in wanted:
            file = folder / f"issue-{cid}.json"
            if file.is_file():
                already.append(cid)
            elif not deadline.allows(client):
                remaining.append(cid)
            else:
                try:
                    detail = client.issue_detail(cid, stream)
                except CoverityError as exc:
                    if exc.fatal:
                        raise
                    failed[str(cid)] = str(exc)
                    _write_json(file, {"cid": cid, "stream": stream, "fetched_at": _now(), "error": str(exc)})
                    continue
                _write_json(file, {"fetched_at": _now(), **detail.model_dump(exclude_none=True)})
                saved.append(cid)
    finally:
        client.close()
    done = not remaining
    return {"done": done, "saved": saved, "already_saved": already, "failed": failed,
            "remaining": remaining, "seconds": round(deadline.elapsed(), 1),
            "next": None if done else CALL_AGAIN}


@tool("Coverity へのトリアージの書き戻し", read_only_hint=False, destructive_hint=True, idempotent_hint=True)
def update_triage(repo_root: str, plan_file: str, confirmation_token: str) -> dict[str, Any]:
    """反映の内容（ct.py preview が作る apply-plan.json）の triage の各項目を、Coverity のトリアージ
    （分類・対応・重大度・コメント）に書き込む。confirmation_token は ct.py preview が返した確認用の
    文字列で、人が件数を見て同意した後にだけ渡す。ファイルの中身と合わなければ何も書かない。
    結果は apply-plan.result.json に残し、書き込み済みの項目は書き直さない。
    done が false なら、同じ引数でもう一度呼ぶ。"""
    deadline = Deadline()
    config = load_coverity_config(_folder(repo_root, "repo_root"))
    plan_path = _path(plan_file, "plan_file")
    raw = plan_path.read_bytes()
    token = _sha(raw)[:12]
    if confirmation_token.strip() != token:
        raise ValueError("確認用の文字列が反映の内容と一致しません。ct.py preview をやり直し、"
                         "件数を人に見せて同意を得てから呼んでください")
    try:
        entries = [TriageEntry.model_validate(e) for e in json.loads(raw.decode("utf-8")).get("triage") or []]
    except (ValueError, AttributeError, ValidationError) as exc:
        raise ValueError(f"反映の内容を読めません: {plan_path}: {exc}") from exc
    result_path = plan_path.with_name(plan_path.stem + ".result.json")
    result = _read_json(result_path) if result_path.is_file() else {}
    result.setdefault("written", {})
    result.setdefault("failed", {})
    result.setdefault("requests", [])
    written_now: list[str] = []
    remaining: list[str] = []
    client = make_client(config)
    try:
        for entry in entries:
            # Keyed by content: an entry the person changed after writing is written again.
            key = _sha(entry.model_dump_json().encode("utf-8"))[:16]
            if key in result["written"] or result["failed"].get(key, {}).get("token") == token:
                continue
            if not deadline.allows(client):
                remaining.append(entry.id)
                continue
            attributes = TriageAttributes(classification=entry.classification, action=entry.action,
                                          severity=entry.severity)
            try:
                client.write_triage(entry.cids, attributes, entry.comment)
            except CoverityError as exc:
                if exc.fatal:
                    raise
                result["failed"][key] = {"id": entry.id, "cids": entry.cids, "error": str(exc),
                                         "token": token, "at": _now()}
                continue
            result["written"][key] = {"id": entry.id, "cids": entry.cids, "at": _now()}
            result["failed"].pop(key, None)
            written_now.append(entry.id)
            _write_json(result_path, result)
    finally:
        result["requests"] += client.requests
        client.close()
        _write_json(result_path, result)
    keys = {_sha(e.model_dump_json().encode("utf-8"))[:16] for e in entries}
    failed = [{"id": v["id"], "cids": v["cids"], "error": v["error"]}
              for k, v in result["failed"].items() if k in keys]
    done = not remaining
    return {"done": done, "written": written_now,
            "written_total": sum(1 for k in result["written"] if k in keys), "entries": len(entries),
            "failed": failed, "remaining": remaining, "result_file": str(result_path),
            "seconds": round(deadline.elapsed(), 1), "next": None if done else CALL_AGAIN}


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
