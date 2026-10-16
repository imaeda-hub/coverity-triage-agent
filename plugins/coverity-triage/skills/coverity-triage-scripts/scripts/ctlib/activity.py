"""What the agents of a run are doing, for the monitor of the screen (``ct.py gui``).

Read only from the files of the run folder; nothing here changes a file:

- ``run.json`` (state of every item) and ``operations.log`` (steps of ct.py and calls of the MCP server)
- ``items/<ID>/progress.json``: milestones the worker subagent writes (best effort; it has no clock,
  so the time is the file's mtime). Without it a worker is shown from the log alone.
- problems kept in other files: ``items/*/issue-*.json`` (warning paths Coverity did not return),
  ``verify.json`` (build verification) and ``apply-plan.result.json`` (write-back to Coverity).

``board()`` is what the screen draws; ``alerts()`` are its serious errors and warnings.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from . import oplog
from .config import DEFAULT_PARALLEL
from .run import ItemState, Run

STALL_SECONDS = 600  # an item with no sign of work for this long may have stopped
RECENT = 40
MAX_ALERTS = 100
TEXT_CHARS = 160  # one line of the screen
DETAIL_CHARS = 4000
PROGRESS_FILE = "progress.json"
LEVELS = ("info", "warning", "critical")

STAGES = {
    "search": "Coverity で警告を検索中",
    "plan": "作業の一覧を作っている",
    "items": "警告を調べている",
    "verify": "ビルドで確かめている",
    "summary": "一覧を作っている",
    "apply": "反映の途中",
    "done": "終わりました",
}
WORKER_STAGES = {
    "idle": "空き",
    "copy": "修正用のコピーを用意中",
    "fetch": "警告経路を取得中",
    "brief": "指示を作っている",
    "investigate": "調査中",
    "retry": "結果を直している",
    "finishing": "結果を確かめている",
}
PHASES = {
    "read": "コードを読む",
    "judge": "真偽を判断",
    "fix": "修正を作る",
    "deviation": "逸脱を書く",
    "result": "結果を書く",
}
PLAN_JA = {"fix": "修正", "deviation": "逸脱"}
CONFIDENCE_JA = {"high": "高", "medium": "中", "low": "低"}


# ---- small helpers ---------------------------------------------------------------------------


def _epoch(text: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(epoch: float | None) -> str:
    if epoch is None:
        return ""
    try:
        return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _short(text: Any, limit: int = TEXT_CHARS) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def _detail(*parts: Any) -> str:
    lines: list[str] = []
    for part in parts:
        if isinstance(part, (list, tuple)):
            lines += [f"- {p}" for p in part if p]
        elif part:
            lines.append(str(part))
    text = "\n".join(lines)
    return text if len(text) <= DETAIL_CHARS else text[:DETAIL_CHARS - 1] + "…"


def _item(record: dict[str, Any]) -> str | None:
    value = record.get("item")
    return None if value in (None, "") else str(value)


def _level(record: dict[str, Any]) -> str:
    # A failed item stops its work; older logs have no level at all.
    if record.get("event") == "fail":
        return "critical"
    level = record.get("level")
    return level if level in LEVELS else "info"


def _source(record: dict[str, Any]) -> str:
    return "mcp" if record.get("source") == "mcp" else "ct"


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return len(value) if isinstance(value, (list, tuple, dict)) else 0


def _last(records: list[dict[str, Any]], *events: str, command: str | None = None) -> int:
    """Index of the last record of one of ``events`` (or of an error of ``command``); -1 if none."""
    for i in range(len(records) - 1, -1, -1):
        record = records[i]
        if record["event"] in events or (command and record["event"] == "error"
                                         and record.get("command") == command):
            return i
    return -1


# ---- one line per record ---------------------------------------------------------------------


def _mcp_text(tool: str, record: dict[str, Any]) -> str:
    phase, item = record.get("phase"), _item(record)
    error = _short(record.get("error"), 100)
    if tool == "search_issues":
        if phase == "start":
            return "Coverity で警告を検索し始めた"
        if phase == "error":
            return f"Coverity での検索が失敗した: {error}"
        notes = [f"{_count(record.get('found'))} 件"]
        if record.get("revision_error"):
            notes.append("解析リビジョンを取れなかった")
        if not record.get("done"):
            notes.append("続きがある")
        return f"Coverity で警告を検索した（{'、'.join(notes)}）"
    if tool == "get_issues":
        who = f"{item} の" if item else ""
        if phase == "start":
            return f"{who}警告経路を Coverity から取り始めた"
        if phase == "error":
            return f"{who}警告経路を取れなかった: {error}"
        notes = [f"新しく {_count(record.get('saved'))} 件"]
        if record.get("failed"):
            notes.append(f"取れなかった {_count(record.get('failed'))} 件")
        if not record.get("done"):
            notes.append("続きがある")
        return f"{who}警告経路を取った（{'、'.join(notes)}）"
    if tool == "update_triage":
        if phase == "start":
            return "Coverity にトリアージを書き込み始めた"
        if phase == "error":
            return f"Coverity への書き込みが失敗した: {error}"
        notes = [f"{_count(record.get('written_total'))}/{_count(record.get('entries'))} 件"]
        if record.get("failed"):
            notes.append(f"うまくいかなかった {_count(record.get('failed'))} 件")
        if not record.get("done"):
            notes.append("続きがある")
        return f"Coverity にトリアージを書き込んだ（{'、'.join(notes)}）"
    return f"MCP の {tool}（{phase or '-'}）"


def _text(record: dict[str, Any]) -> str:
    """One line of plain Japanese for a record of operations.log."""
    try:
        return _short(_describe(record))
    except (AttributeError, TypeError, ValueError):
        return _short(" ".join(p for p in (record["event"], _item(record)) if p))


def _describe(record: dict[str, Any]) -> str:
    event, item = record["event"], _item(record)
    if event.startswith("mcp:"):
        return _mcp_text(event[4:], record)
    if event == "new-run":
        flt = record.get("filter") if isinstance(record.get("filter"), dict) else {}
        return f"実行を始めた（条件 {flt.get('name') or '-'}、上限 {record.get('limit', '-')} 件）"
    if event == "plan":
        groups = _count(record.get("groups"))
        return (f"作業の一覧を作った（警告 {record.get('issues', '-')} 件、作業 {record.get('items', '-')} 件"
                + (f"、うちグループ {groups} 件" if groups else "") + "）")
    if event == "copy":
        return (f"{item} の修正用のコピーを" + ("作り始めた" if record.get("create") else "元に戻し始めた")
                + f"（スロット {record.get('slot', '-')}" + ("、初回は時間がかかる" if record.get("create") else "") + "）")
    if event == "next":
        attempt = record.get("attempt") or 1
        return (f"{item} の調査を始めた（スロット {record.get('slot', '-')}"
                + (f"、{attempt} 回目" if isinstance(attempt, int) and attempt > 1 else "") + "）")
    if event == "brief":
        return f"{item} の指示を作った（サブエージェントが調べる）"
    if event == "finish":
        notes = [f"推奨 {PLAN_JA.get(record.get('recommendation'), '-')}",
                 f"確信度 {CONFIDENCE_JA.get(record.get('confidence'), '-')}"]
        if record.get("split"):
            notes.append(f"{_count(record.get('split'))} 件を個別の作業に戻した")
        if record.get("warnings"):
            notes.append(f"注意 {_count(record.get('warnings'))} 件")
        return f"{item} を終えた（{'、'.join(notes)}）"
    if event == "fail":
        return f"{item} を処理できなかった: {record.get('reason', '')}"
    if event == "resume":
        reset = record.get("reset") if isinstance(record.get("reset"), dict) else {}
        back = sum(v for v in reset.values() if isinstance(v, int))
        return f"実行を再開した（戻した作業 {back} 件）"
    if event == "verify-start":
        return f"ビルドでの確かめを始めた（{_count(record.get('items'))} 件）"
    if event == "verify":
        notes = ["ビルド " + ("成功" if record.get("build_ok") else "失敗")]
        if record.get("downgraded"):
            notes.append(f"確信度を下げた {_count(record.get('downgraded'))} 件")
        return f"ビルドでの確かめが終わった（{'、'.join(notes)}）"
    if event == "summary":
        notes = [f"{record.get('items', '-')} 件", f"処理できなかった作業 {record.get('errors', 0)} 件"]
        if record.get("unprocessed"):
            notes.append(f"まだの作業 {_count(record.get('unprocessed'))} 件")
        return f"一覧を作った（{'、'.join(notes)}）"
    if event == "preview":
        counts = record.get("counts") if isinstance(record.get("counts"), dict) else {}
        return "反映の内容を作った（" + "、".join(f"{k} {v}" for k, v in counts.items()) + "）"
    if event == "apply-code":
        errors = _count(record.get("errors"))
        return "反映した" + (f"（うまくいかなかったもの {errors} 件）" if errors else "")
    if event == "add-knowledge":
        return f"知識を {_count(record.get('entries'))} 件書き足した"
    if event == "error":
        command = record.get("command") or "-"
        if _level(record) == "warning" and command == "finish":
            return f"{item} の結果ファイルに直すところがあった（サブエージェントが直す）"
        return f"{item + ' の ' if item else ''}ct.py {command} が失敗した: {record.get('error', '')}"
    return " ".join(p for p in (event, item) if p)


# ---- the run as a whole ----------------------------------------------------------------------


def _parallel(run: Run) -> int:
    """Number of slots: the settings that ``ct.py next`` uses, else the copy made at new-run."""
    try:
        return run.config.parallel
    except Exception:  # noqa: BLE001 - a broken or moved repository must not stop the monitor
        pass
    data = None
    try:
        data = yaml.safe_load((run.dir / "config.snapshot.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        pass
    value = data.get("parallel") if isinstance(data, dict) else None
    return value if isinstance(value, int) and 1 <= value <= 8 else DEFAULT_PARALLEL


def _summary_exists(run: Run) -> bool:
    return any((run.dir / name).is_file() for name in ("summary.html", "summary.md"))


def _applying(records: list[dict[str, Any]], now: float) -> bool:
    """/coverity-apply is under way: preview or update_triage after the last apply-code, lately.

    A preview the person did not agree to is not followed by anything: after STALL_SECONDS the
    run counts as done again (as in the list of runs, which reads the log only when it changed lately)."""
    last = _last(records, "preview", "mcp:update_triage")
    if last <= _last(records, "apply-code"):
        return False
    when = _epoch(records[last].get("time"))
    return when is not None and now - when < STALL_SECONDS


def done_label(run: Run) -> str:
    """Label of a finished run, from run.json alone (the list of runs uses it without reading the log)."""
    applied = any(i.applied for i in run.state.items)
    return "反映まで終わりました" if applied else "一覧ができました（承認を待っています）"


def _stage(run: Run, records: list[dict[str, Any]], now: float) -> tuple[str, str]:
    state = run.state
    counts = state.counts()
    if not state.planned:
        found = _json(run.dir / "issues.json")
        stage = "plan" if isinstance(found, dict) and found.get("finished_at") else "search"
    elif counts["pending"] or counts["in_progress"]:
        stage = "items"
    elif _last(records, "verify-start") > _last(records, "verify", command="verify"):
        stage = "verify"
    elif not _summary_exists(run):
        stage = "summary"
    elif _applying(records, now):
        stage = "apply"
    else:
        stage = "done"
    return stage, done_label(run) if stage == "done" else STAGES[stage]


def run_stage(run: Run, now: float | None = None) -> tuple[str, str]:
    """The stage of the run and its label, as in ``board()`` (for the list of runs)."""
    return _stage(run, oplog.read(run.dir), time.time() if now is None else now)


def _main(stage: str, counts: dict[str, int], free: int, records: list[dict[str, Any]],
          now: float) -> dict[str, Any]:
    """The main agent (Copilot): what it is doing, and its last step."""
    if stage == "items":
        if counts["pending"] and free:
            label = "作業を配っている"
        elif counts["pending"]:
            label = "サブエージェントの終わりを待っている"
        else:
            label = "残りの作業の終わりを待っている"
    elif stage == "apply":  # after preview the person is asked to agree; update_triage writes to Coverity
        writing = records[_last(records, "preview", "mcp:update_triage")]["event"] == "mcp:update_triage"
        label = "Coverity に書き込んでいる" if writing else "反映の同意を待っている"
    else:
        label = {"search": "Coverity で警告を検索している", "done": "待機中"}.get(stage, STAGES[stage])
    last = records[-1] if records else None
    if last is None:
        return {"label": label, "last_event": None, "last_time": None, "idle_seconds": None}
    parts = [last["event"], last.get("command") if last["event"] == "error" else last.get("phase"), _item(last)]
    when = _epoch(last.get("time"))
    return {"label": label, "last_event": " ".join(str(p) for p in parts if p),
            "last_time": last.get("time"), "idle_seconds": None if when is None else max(0, int(now - when))}


# ---- the workers -----------------------------------------------------------------------------


def _attempt(records: list[dict[str, Any]], item: ItemState) -> list[dict[str, Any]]:
    """Records of the item's current attempt: from its last ``copy`` or ``next`` on."""
    start = -1
    for i, record in enumerate(records):
        if record["event"] in ("copy", "next") and _item(record) == item.id:
            start = i
    if start >= 0:
        return [r for r in records[start:] if _item(r) == item.id]
    since = _iso(item.started)
    return [r for r in records if _item(r) == item.id and str(r.get("time", "")) >= since]


def _progress(run: Run, item: ItemState) -> tuple[dict[str, Any], float | None]:
    """The worker's milestone of this attempt (an older file is from an earlier attempt)."""
    path = run.item_dir(item.id) / PROGRESS_FILE
    mtime = _mtime(path)
    if mtime is None or (item.started and mtime < item.started - 2):
        return {}, None
    data = _json(path)
    if not isinstance(data, dict) or (data.get("item") not in (None, "") and str(data.get("item")) != item.id):
        return {}, None
    return data, mtime


def _newest_file(folder: Path) -> float | None:
    newest = None
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        mtime = entry.stat().st_mtime
                        newest = mtime if newest is None else max(newest, mtime)
                except OSError:
                    continue
    except OSError:
        return None
    return newest


def _idle_worker(slot: int) -> dict[str, Any]:
    return {"slot": slot, "item": None, "cids": [], "is_group": False, "attempts": 0, "stage": "idle",
            "stage_label": WORKER_STAGES["idle"], "phase": None, "phase_label": "", "phase_index": 0,
            "phase_total": len(PHASES), "note": "", "elapsed_seconds": None, "idle_seconds": None,
            "stalled": False}


def _worker(run: Run, item: ItemState, slot: int, records: list[dict[str, Any]], now: float) -> dict[str, Any]:
    mine = _attempt(records, item)
    folder = run.item_dir(item.id)
    stage, since = "fetch", item.started
    copying = bool(mine) and mine[0]["event"] == "copy" and not any(r["event"] == "next" for r in mine)
    if copying:  # ct.py next is still creating or resetting the work copy
        stage, since = "copy", _epoch(mine[0].get("time")) or since
    for record in mine:
        event = record["event"]
        if event == "mcp:get_issues":
            stage = "brief" if record.get("phase") == "end" and record.get("done") else "fetch"
        elif event == "brief":
            stage = "investigate"
        elif event == "error" and record.get("command") == "finish" and _level(record) == "warning":
            stage = "retry"
        else:
            continue
        since = _epoch(record.get("time")) or since
    if not copying and not any(r["event"] in ("mcp:get_issues", "brief") for r in mine):
        # No log of this attempt (an older run, or the log was lost): judge from the files.
        brief = _mtime(folder / "brief.md")
        if brief is not None and (not item.started or brief >= item.started - 2):
            stage, since = "investigate", brief
    result = _mtime(run.result_file(item.id))
    if stage in ("investigate", "retry") and result is not None and (since is None or result >= since + 1):
        stage = "finishing"  # the worker wrote its result; the main agent checks it with ct.py finish

    progress, progress_time = _progress(run, item)
    if copying:
        progress, progress_time = ({"note": "初めて使うスロットは、リポジトリの大きさにより数分から数十分かかります"}
                                   if mine[0].get("create") else {}), None
    phase = progress.get("phase") if progress.get("phase") in PHASES else None
    seen = [t for t in (item.started, progress_time, _newest_file(folder),
                        *(_epoch(r.get("time")) for r in mine)) if t is not None]
    idle = max(0, int(now - max(seen))) if seen else None
    return {"slot": slot, "item": item.id, "cids": list(item.cids), "is_group": item.is_group,
            "attempts": item.attempts, "stage": stage, "stage_label": WORKER_STAGES[stage], "phase": phase,
            "phase_label": PHASES.get(phase or "", ""), "phase_index": list(PHASES).index(phase) + 1 if phase else 0,
            "phase_total": len(PHASES), "note": _short(progress.get("note"), 200),
            "elapsed_seconds": max(0, int(now - item.started)) if item.started else None,
            # a copy of a large repository is made by svn / git without any sign of life on the way
            "idle_seconds": idle, "stalled": not copying and idle is not None and idle >= STALL_SECONDS,
            "last_seen": _iso(max(seen)) if seen else None}


def _workers(run: Run, parallel: int, records: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    busy = [i for i in run.state.items if i.status == "in_progress"]
    by_slot: dict[int, ItemState] = {}
    others = []
    for item in busy:
        if item.slot and item.slot >= 1 and item.slot not in by_slot:
            by_slot[item.slot] = item
        else:
            others.append(item)  # not expected; still shown
    slots = max([parallel, *by_slot])
    out = []
    for slot in range(1, slots + 1):
        item = by_slot.get(slot)
        out.append(_idle_worker(slot) if item is None else _worker(run, item, slot, records, now))
    for n, item in enumerate(others, slots + 1):
        out.append(_worker(run, item, n, records, now))
    return out


# ---- alerts ----------------------------------------------------------------------------------


def _alert(time_text: Any, level: str, source: str, item: str | None, title: str, detail: str) -> dict[str, Any]:
    return {"time": str(time_text or ""), "level": level, "source": source, "item": item,
            "title": _short(title), "detail": detail}


def _log_alert(record: dict[str, Any]) -> dict[str, Any]:
    event, item, level = record["event"], _item(record), _level(record)
    tool = event[4:] if event.startswith("mcp:") else None
    title = _text(record)
    detail = _detail(record.get("error") or record.get("reason"), record.get("problems"))
    if event == "fail":
        title = f"{item} を処理できませんでした"
    elif event == "error":
        command = record.get("command") or "-"
        title = (f"{item} の結果ファイルに直すところがありました（サブエージェントに直してもらいます）"
                 if level == "warning" and command == "finish" else
                 f"{item + ' の ' if item else ''}ct.py {command} が失敗しました")
    elif tool and record.get("phase") == "error":
        title = f"MCP の {tool} が失敗しました" + (f"（{item}）" if item else "")
    elif tool == "search_issues" and record.get("revision_error"):
        title = "解析したリビジョンを Coverity から取れませんでした（手元のコードで調べます）"
        detail = _detail(record.get("revision_error"))
    elif tool == "get_issues" and record.get("failed"):
        title = f"{item or ''} の警告経路のうち {_count(record.get('failed'))} 件を取れませんでした"
        detail = _detail(record.get("failed"))
    elif tool == "update_triage" and record.get("failed"):
        title = f"Coverity への書き込みが {_count(record.get('failed'))} 件うまくいきませんでした"
        detail = _detail(record.get("failed"))
    return _alert(record.get("time"), level, _source(record), item, title, detail)


def _issue_alerts(run: Run) -> tuple[list[dict[str, Any]], set[str]]:
    """Warning paths that Coverity did not return (the item goes on with what it has)."""
    alerts, cids = [], set()
    for path in sorted(run.dir.glob("items/*/issue-*.json")):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b'"error"' not in raw:  # most files hold a warning path; parse only the few that may not
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if not isinstance(data, dict) or not data.get("error"):
            continue
        cid = str(data.get("cid") or path.stem.removeprefix("issue-"))
        cids.add(cid)
        alerts.append(_alert(data.get("fetched_at") or _iso(_mtime(path)), "warning", "mcp", path.parent.name,
                             f"CID {cid} の警告経路を Coverity から取れませんでした（{path.parent.name}）",
                             _detail(data.get("error"))))
    return alerts, cids


def _apply_alerts(run: Run) -> tuple[list[dict[str, Any]], set[str]]:
    """Deviations that Coverity refused to write (they stay until a new preview writes them)."""
    data = _json(run.dir / "apply-plan.result.json")
    failed = data.get("failed") if isinstance(data, dict) else None
    alerts, ids = [], set()
    for entry in (failed.values() if isinstance(failed, dict) else []):
        if not isinstance(entry, dict):
            continue
        item = str(entry.get("id") or "")
        ids.add(item)
        cids = ", ".join(str(c) for c in entry.get("cids") or [])
        alerts.append(_alert(entry.get("at"), "critical", "apply", item or None,
                             f"{item} の逸脱を Coverity に書き込めませんでした",
                             _detail(entry.get("error"), f"CID {cids}" if cids else "")))
    return alerts, ids


def _verify_alerts(run: Run) -> list[dict[str, Any]]:
    path = run.dir / "verify.json"
    data = _json(path)
    if not isinstance(data, dict):
        return []
    when = _iso(_mtime(path))
    return [_alert(when, "warning", "verify", str(item_id), f"{item_id} の修正案は、ビルドでの確かめで問題がありました",
                   _detail(entry.get("problems"), entry.get("log_tail")))
            for item_id, entry in data.items() if isinstance(entry, dict) and entry.get("build_ok") is False]


def _stall_alert(worker: dict[str, Any], now: float) -> dict[str, Any]:
    minutes = (worker["idle_seconds"] or 0) // 60
    where = "、".join(p for p in (worker["stage_label"], worker["phase_label"]) if p)
    return _alert(_iso(now), "warning", "stall", worker["item"],
                  f"{worker['item']} が {minutes} 分動いていません（止まっているかもしれません）",
                  _detail(f"スロット {worker['slot']}: {where}", worker["note"],
                          f"最後に動きがあった時刻: {worker['last_seen'] or '-'}"))


def _alerts(run: Run, records: list[dict[str, Any]], workers: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    issue_alerts, issue_cids = _issue_alerts(run)
    apply_alerts, apply_ids = _apply_alerts(run)
    found: list[dict[str, Any]] = []
    messages: dict[str, tuple[int, dict[str, Any]]] = {}
    for record in records:
        if _level(record) == "info":
            continue
        failed = record.get("failed")
        if isinstance(failed, list) and failed and (
                (record["event"] == "mcp:get_issues" and {str(c) for c in failed} <= issue_cids)
                or (record["event"] == "mcp:update_triage" and {str(c) for c in failed} <= apply_ids)):
            continue  # the files below say the same per CID / item
        message = record.get("reason") or record.get("error")
        if isinstance(message, str) and message:
            messages[message] = (len(found), record)
        found.append(_log_alert(record))
    for item in run.state.items:
        if item.status != "error":
            continue
        if item.error in messages:  # already in the log (ct.py fail, or the error of ct.py next)
            index, record = messages[item.error]
            if _item(record) is None:
                found[index] = _log_alert({**record, "item": item.id})
            continue
        ended = item.started + item.seconds if item.started and item.seconds is not None else None
        found.append(_alert(_iso(ended), "critical", "ct", item.id, f"{item.id} を処理できませんでした",
                            _detail(item.error)))
    found += issue_alerts + apply_alerts + _verify_alerts(run)
    found += [_stall_alert(w, now) for w in workers if w["stalled"]]
    found.reverse()  # among equal times, the later one first
    found.sort(key=lambda a: a["time"], reverse=True)
    return found[:MAX_ALERTS]


# ---- the board -------------------------------------------------------------------------------


def board(run: Run, now: float | None = None) -> dict[str, Any]:
    """State of the run for the monitor: the main agent, one slot per worker, recent steps, alerts."""
    now = time.time() if now is None else now
    records = oplog.read(run.dir)
    state = run.state
    counts = state.counts()
    parallel = _parallel(run)
    stage, stage_label = _stage(run, records, now)
    workers = _workers(run, parallel, records, now)
    free = sum(1 for w in workers[:parallel] if w["item"] is None)
    recent = [{"time": r.get("time"), "level": _level(r), "source": _source(r), "event": r["event"],
               "item": _item(r), "text": _text(r)} for r in reversed(records[-RECENT:])]
    alerts = _alerts(run, records, workers, now)
    for worker in workers:
        worker.pop("last_seen", None)
    return {"run_id": state.run_id, "created_at": state.created_at, "filter_name": state.filter_name,
            "stage": stage, "stage_label": stage_label, "main": _main(stage, counts, free, records, now),
            "counts": counts, "total": len(state.items), "parallel": parallel, "workers": workers,
            "recent": recent, "alerts": alerts}


def alerts(run: Run, now: float | None = None) -> list[dict[str, Any]]:
    """Serious errors and warnings of the run, newest first (the same as ``board()["alerts"]``)."""
    return board(run, now)["alerts"]
