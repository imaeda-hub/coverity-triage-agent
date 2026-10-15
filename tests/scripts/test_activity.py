"""The monitor's view of a run (activity.py), and the errors ct.py and the MCP server leave in its log."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

import ct
from coverity_triage import server
from ctlib import activity, oplog
from ctlib.common import write_json
from ctlib.run import ItemState, Run, RunState
from test_flow import RESULT, git_identity, repo, run_ct, start_run  # noqa: F401 - fixtures

NOW = datetime(2026, 10, 15, 10, 0, 0).timestamp()


def at(seconds_ago: float) -> str:
    return datetime.fromtimestamp(NOW - seconds_ago).isoformat(timespec="seconds")


def touch(path: Path, seconds_ago: float, text: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None or not path.exists():
        path.write_text(text or "", encoding="utf-8")
    os.utime(path, (NOW - seconds_ago, NOW - seconds_ago))
    return path


def make_run(tmp_path: Path, items: list[ItemState], planned: bool = True, parallel: int = 3) -> Path:
    """A run folder written by hand. The repository is gone, so the copy of the settings gives parallel."""
    folder = tmp_path / "out" / "20261015-093000"
    (folder / "items").mkdir(parents=True)
    state = RunState(run_id=folder.name, repo_root=str(tmp_path / "missing-repo"), created_at=at(1800),
                     filter_name="all", stream="Sample-main", limit=100, verify_mode="none", planned=planned,
                     items=items)
    write_json(folder / "run.json", state.model_dump())
    (folder / "config.snapshot.yaml").write_text(f"parallel: {parallel}\n", encoding="utf-8")
    return folder


def busy(item_id: str, slot: int, started_ago: float, cids: list[int] | None = None) -> ItemState:
    return ItemState(id=item_id, cids=cids or [int(item_id)], status="in_progress", slot=slot, attempts=1,
                     started=NOW - started_ago)


def log(folder: Path, seconds_ago: float, event: str, **fields) -> None:
    record = {"time": at(seconds_ago), "event": event, "level": "info", "source": "ct", **fields}
    with open(folder / "operations.log", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def started(folder: Path, item: str, slot: int, seconds_ago: float) -> None:
    log(folder, seconds_ago, "next", item=item, slot=slot, attempt=1)


def mcp(folder: Path, seconds_ago: float, tool: str, phase: str, level: str = "info", **fields) -> None:
    log(folder, seconds_ago, f"mcp:{tool}", phase=phase, **{**fields, "level": level, "source": "mcp"})


# ---- oplog -----------------------------------------------------------------------------------


def test_log_adds_level_and_source_and_read_skips_broken_lines(tmp_path):
    oplog.log(tmp_path, "next", item="20001")
    oplog.log(tmp_path, "fail", ["s3cr3t"], level="critical", item="20001", reason="key s3cr3t")
    with open(tmp_path / "operations.log", "a", encoding="utf-8") as f:
        f.write('{"time": "2026-10-15T10:00:00", "event": "brief", "it\n[1, 2]\n\n{"no_event": 1}\n')
    records = oplog.read(tmp_path)
    assert [(r["event"], r["level"], r["source"]) for r in records] == [("next", "info", "ct"),
                                                                       ("fail", "critical", "ct")]
    assert records[1]["reason"] == "key ****"
    assert oplog.read(tmp_path / "missing") == []


# ---- the board -------------------------------------------------------------------------------


def test_a_run_being_searched_and_planned(tmp_path):
    folder = make_run(tmp_path, [], planned=False)
    board = activity.board(Run(folder), now=NOW)
    assert board["stage"] == "search" and board["parallel"] == 3 and board["total"] == 0
    assert [w["stage"] for w in board["workers"]] == ["idle"] * 3
    assert board["workers"][0]["item"] is None and board["main"]["last_event"] is None
    write_json(folder / "issues.json", {"finished_at": at(5), "issues": []})
    assert activity.board(Run(folder), now=NOW)["stage"] == "plan"


def test_workers_follow_the_log_and_the_progress_file(tmp_path):
    items = [busy("20001", 1, 300), busy("20002", 2, 200), busy("20003", 3, 100),
             ItemState(id="20004", cids=[20004])]
    folder = make_run(tmp_path, items)
    started(folder, "20001", 1, 300)
    started(folder, "20002", 2, 200)
    mcp(folder, 199, "get_issues", "start", item="20001", cids=[20001])
    mcp(folder, 198, "get_issues", "start", item="20002", cids=[20002])
    mcp(folder, 197, "get_issues", "end", item="20002", cids=[20002], done=True, saved=1, failed=[])
    started(folder, "20003", 3, 100)
    log(folder, 90, "brief", item="20003", drift=0)
    touch(folder / "items" / "20003" / "progress.json", 30,
          json.dumps({"item": "20003", "phase": "judge", "note": "呼び出し元 read_config() を確認中"}))

    board = activity.board(Run(folder), now=NOW)
    assert board["stage"] == "items" and board["stage_label"] == "警告を調べている"
    assert board["counts"] == {"pending": 1, "in_progress": 3, "done": 0, "error": 0} and board["total"] == 4
    one, two, three = board["workers"]
    assert (one["slot"], one["item"], one["stage"], one["phase"]) == (1, "20001", "fetch", None)
    assert two["stage"] == "brief" and two["stage_label"] == "指示を作っている"
    assert three["stage"] == "investigate" and three["phase"] == "judge" and three["phase_label"] == "真偽を判断"
    assert (three["phase_index"], three["phase_total"]) == (2, 5)
    assert three["note"] == "呼び出し元 read_config() を確認中"
    assert three["elapsed_seconds"] == 100 and three["idle_seconds"] == 30 and not three["stalled"]
    assert board["main"]["label"] == "サブエージェントの終わりを待っている"
    assert board["main"]["last_event"] == "brief 20003" and board["main"]["idle_seconds"] == 90
    assert board["recent"][0]["text"] == "20003 の指示を作った（サブエージェントが調べる）"
    assert board["recent"][-1]["text"] == "20001 の調査を始めた（スロット 1）"
    assert board["recent"][2]["source"] == "mcp" and "20002 の警告経路を取った" in board["recent"][2]["text"]
    assert board["alerts"] == []


def test_a_worker_whose_result_needs_fixing_and_a_written_result(tmp_path):
    folder = make_run(tmp_path, [busy("20001", 1, 900)], parallel=2)
    started(folder, "20001", 1, 900)
    log(folder, 800, "brief", item="20001")
    touch(folder / "items" / "20001" / "result.json", 120, "{}")
    log(folder, 100, "error", level="warning", command="finish", item="20001",
        error="結果ファイルに直すところがあります", problems=["item は 20001 にしてください（今は 2001）"])
    board = activity.board(Run(folder), now=NOW)
    worker = board["workers"][0]
    assert worker["stage"] == "retry" and worker["stage_label"] == "結果を直している"
    assert [w["stage"] for w in board["workers"]] == ["retry", "idle"]
    assert board["main"]["label"] == "残りの作業の終わりを待っている"
    alert = board["alerts"][0]
    assert alert["level"] == "warning" and alert["source"] == "ct" and alert["item"] == "20001"
    assert "直すところ" in alert["title"] and "今は 2001" in alert["detail"]

    touch(folder / "items" / "20001" / "result.json", 10, "{}")  # the worker wrote it again
    assert activity.board(Run(folder), now=NOW)["workers"][0]["stage"] == "finishing"


def test_progress_of_an_earlier_attempt_is_not_shown(tmp_path):
    folder = make_run(tmp_path, [busy("20001", 1, 60)])
    started(folder, "20001", 1, 60)
    touch(folder / "items" / "20001" / "progress.json", 3600, json.dumps({"item": "20001", "phase": "fix"}))
    worker = activity.board(Run(folder), now=NOW)["workers"][0]
    assert worker["phase"] is None and worker["phase_index"] == 0 and worker["note"] == ""
    touch(folder / "items" / "20001" / "progress.json", 5, json.dumps({"phase": "unknown"}))
    assert activity.board(Run(folder), now=NOW)["workers"][0]["phase"] is None


def test_a_worker_with_no_sign_of_work_is_stalled(tmp_path):
    folder = make_run(tmp_path, [busy("G1", 2, 1500, cids=[20004, 20005, 20006])])
    started(folder, "G1", 2, 1500)
    log(folder, 1400, "brief", item="G1")
    touch(folder / "items" / "G1" / "brief.md", 1400)
    progress = touch(folder / "items" / "G1" / "progress.json", 700, json.dumps({"item": "G1", "phase": "fix"}))
    board = activity.board(Run(folder), now=NOW)
    worker = board["workers"][1]
    assert worker["item"] == "G1" and worker["is_group"] and worker["cids"] == [20004, 20005, 20006]
    assert worker["idle_seconds"] == 700 and worker["stalled"]
    assert worker["elapsed_seconds"] == 1500 and worker["phase_label"] == "修正を作る"
    (alert,) = board["alerts"]
    assert alert["source"] == "stall" and alert["level"] == "warning" and alert["item"] == "G1"
    assert "11 分" in alert["title"] and alert["time"] == at(0)
    touch(progress, 60)  # the worker wrote a new milestone
    assert not activity.board(Run(folder), now=NOW)["workers"][1]["stalled"]
    assert activity.alerts(Run(folder), now=NOW) == []


def test_alerts_gather_the_errors_of_the_run(tmp_path):
    copy_error = "修正用のコピーを用意できませんでした: disk full"
    items = [ItemState(id="20001", cids=[20001], status="error", error="CID が読めない", started=NOW - 900,
                       seconds=60.0),
             ItemState(id="20002", cids=[20002], status="error", error=copy_error, started=NOW - 600, seconds=1.0),
             ItemState(id="20003", cids=[20003], status="error", error="理由はログにない", started=NOW - 500,
                       seconds=2.0),
             ItemState(id="20004", cids=[20004], status="done"),
             ItemState(id="20005", cids=[20005], status="done")]
    folder = make_run(tmp_path, items)
    log(folder, 840, "fail", item="20001", reason="CID が読めない")  # an older log without a level
    log(folder, 599, "error", level="critical", command="next", error=copy_error, problems=[])
    mcp(folder, 400, "get_issues", "end", level="warning", item="20004", done=True, saved=0, failed=[20004])
    write_json(folder / "items" / "20004" / "issue-20004.json",
               {"cid": 20004, "fetched_at": at(400), "error": "CID 20004 が見つかりません"})
    mcp(folder, 300, "update_triage", "error", level="critical", error="確認用の文字列が反映の内容と一致しません")
    write_json(folder / "apply-plan.result.json",
               {"written": {}, "failed": {"k1": {"id": "20005", "cids": [20005], "error": "400: invalid value",
                                                 "token": "t", "at": at(200)}}})
    write_json(folder / "verify.json", {"20004": {"build_ok": False, "problems": ["ビルドに失敗しました"],
                                                  "log_tail": "error: x"},
                                        "20005": {"build_ok": True, "problems": []}})
    os.utime(folder / "verify.json", (NOW - 100, NOW - 100))

    alerts = activity.board(Run(folder), now=NOW)["alerts"]
    assert [(a["source"], a["level"], a["item"]) for a in alerts] == [
        ("verify", "warning", "20004"),
        ("apply", "critical", "20005"),
        ("mcp", "critical", None),
        ("mcp", "warning", "20004"),
        ("ct", "critical", "20003"),
        ("ct", "critical", "20002"),
        ("ct", "critical", "20001")]
    assert alerts[0]["detail"] == "- ビルドに失敗しました\nerror: x"
    assert alerts[1]["title"] == "20005 の逸脱を Coverity に書き込めませんでした" and "400" in alerts[1]["detail"]
    assert alerts[2]["title"] == "MCP の update_triage が失敗しました"
    assert alerts[3]["title"].startswith("CID 20004 の警告経路")  # one alert per CID, not the log line too
    assert alerts[4]["title"] == "20003 を処理できませんでした" and alerts[4]["detail"] == "理由はログにない"
    assert alerts[5]["title"] == "20002 の ct.py next が失敗しました"  # the log line gets the item from run.json
    assert alerts[6]["title"] == "20001 を処理できませんでした" and alerts[6]["detail"] == "CID が読めない"
    assert [a["time"] for a in alerts] == sorted((a["time"] for a in alerts), reverse=True)


def test_broken_files_do_not_stop_the_board(tmp_path):
    folder = make_run(tmp_path, [busy("20001", 1, 60)])
    started(folder, "20001", 1, 60)
    with open(folder / "operations.log", "a", encoding="utf-8") as f:
        f.write('not json\n{"event": "brief", "item": "20001", "time": "後で"}\n{"time": 1}\n{"event": "fi')
    touch(folder / "items" / "20001" / "progress.json", 5, "{broken")
    (folder / "verify.json").write_text("[", encoding="utf-8")
    (folder / "apply-plan.result.json").write_text('{"failed": [1, 2]}', encoding="utf-8")
    (folder / "items" / "20001" / "issue-20001.json").write_text("{", encoding="utf-8")
    (folder / "config.snapshot.yaml").write_text("parallel: [", encoding="utf-8")
    board = activity.board(Run(folder), now=NOW)
    assert board["parallel"] == 3  # the default
    assert [r["event"] for r in board["recent"]] == ["brief", "next"]
    assert board["workers"][0]["stage"] == "investigate" and board["workers"][0]["phase"] is None
    assert board["alerts"] == []


def test_stages_of_the_run_after_the_items(tmp_path):
    folder = make_run(tmp_path, [ItemState(id="20001", cids=[20001], status="done")])
    log(folder, 500, "finish", item="20001", recommendation="fix", confidence="high")
    assert activity.board(Run(folder), now=NOW)["stage"] == "summary"
    log(folder, 400, "verify-start", mode="build", items=["20001"])
    assert activity.board(Run(folder), now=NOW)["stage"] == "verify"
    log(folder, 300, "verify", mode="build", build_ok=True, downgraded=[])
    touch(folder / "summary.html", 250)
    board = activity.board(Run(folder), now=NOW)
    assert board["stage"] == "done" and "承認を待っています" in board["stage_label"]
    assert board["main"]["label"] == "待機中"
    log(folder, 200, "preview", counts={"修正": 1, "逸脱": 0, "却下": 0}, token="abc")
    board = activity.board(Run(folder), now=NOW)
    assert board["stage"] == "apply" and board["recent"][0]["text"] == "反映の内容を作った（修正 1、逸脱 0、却下 0）"
    assert board["main"]["label"] == "反映の同意を待っている"
    assert activity.run_stage(Run(folder), now=NOW) == ("apply", "反映の途中")
    # The person did not agree: nothing follows the preview, and the run is done again (as in the list of runs).
    later = NOW + activity.STALL_SECONDS
    assert activity.board(Run(folder), now=later)["stage"] == "done"
    assert activity.run_stage(Run(folder), now=later) == ("done", "一覧ができました（承認を待っています）")
    mcp(folder, 150, "update_triage", "end", entries=1, written_total=1, done=True)
    assert activity.board(Run(folder), now=NOW)["main"]["label"] == "Coverity に書き込んでいる"
    log(folder, 100, "apply-code", outcome={}, errors=[])
    board = activity.board(Run(folder), now=NOW)
    assert board["stage"] == "done" and board["stage_label"] == "一覧ができました（承認を待っています）"  # nothing applied
    with Run(folder).update() as state:
        state.item("20001").applied.update({"fix": {"branch": "coverity-fix/cid-20001", "pushed": True}}, at=at(100))
    board = activity.board(Run(folder), now=NOW)
    assert board["stage"] == "done" and board["stage_label"] == "反映まで終わりました"
    assert activity.done_label(Run(folder)) == board["stage_label"]
    assert [r["text"] for r in board["recent"]] == [
        "反映した", "Coverity にトリアージを書き込んだ（1/1 件）", "反映の内容を作った（修正 1、逸脱 0、却下 0）",
        "ビルドでの確かめが終わった（ビルド 成功）",
        "ビルドでの確かめを始めた（1 件）", "20001 を終えた（推奨 修正、確信度 高）"]


# ---- with ct.py and the MCP server -----------------------------------------------------------


def test_the_board_of_a_real_run_and_the_errors_ct_py_logs(capsys, repo):  # noqa: F811 - fixture
    new = start_run(capsys, repo)
    run_dir = new["run_dir"]
    run_ct(capsys, "plan", "--run", run_dir)
    item = run_ct(capsys, "next", "--run", run_dir)
    assert server.get_issues(item["repo_root"], item["stream"], item["cids"], item["item_dir"])["done"]
    brief = run_ct(capsys, "brief", "--run", run_dir, "--item", item["item"])
    Path(brief["result_file"]).write_text(json.dumps({**RESULT, "item": "other"}), encoding="utf-8")
    failed = run_ct(capsys, "finish", "--run", run_dir, "--item", item["item"], ok=False)
    run_ct(capsys, "brief", "--run", run_dir, "--item", "G9", ok=False)

    records = oplog.read(run_dir)
    assert [r["event"] for r in records][:4] == ["new-run", "mcp:search_issues", "mcp:search_issues", "plan"]
    first, second = [r for r in records if r["event"] == "error"]
    assert first == {**first, "level": "warning", "source": "ct", "command": "finish", "item": item["item"],
                     "error": failed["error"], "problems": failed["problems"]}
    assert second["level"] == "critical" and second["command"] == "brief" and second["item"] == "G9"
    assert "作業がありません" in second["error"]

    board = activity.board(Run(run_dir))
    worker = board["workers"][0]
    assert worker["item"] == item["item"] and worker["stage"] == "retry"
    assert [w["stage"] for w in board["workers"][1:]] == ["idle", "idle"]
    assert [a["level"] for a in board["alerts"]] == ["critical", "warning"]

    run_ct(capsys, "fail", "--run", run_dir, "--item", item["item"], "--reason", "直らない")
    record = oplog.read(run_dir)[-1]
    assert record == {**record, "event": "fail", "level": "critical", "item": item["item"], "reason": "直らない"}


def test_a_failure_outside_a_run_folder_or_with_a_broken_log_changes_nothing(capsys, repo, tmp_path):  # noqa: F811
    folder = tmp_path / "not-a-run"
    folder.mkdir()
    run_ct(capsys, "status", "--run", folder, ok=False)
    assert not (folder / "operations.log").exists()

    run_dir = start_run(capsys, repo)["run_dir"]
    log_file = Path(run_dir) / "operations.log"
    log_file.unlink()
    log_file.mkdir()  # appending to it fails
    out = run_ct(capsys, "brief", "--run", run_dir, "--item", "G9", ok=False)
    assert "作業がありません" in out["error"]


def test_an_unexpected_error_is_logged_and_still_raised(capsys, repo, monkeypatch):  # noqa: F811 - fixture
    run_dir = start_run(capsys, repo)["run_dir"]

    def broken(run_dir: str) -> dict:
        raise KeyError("issues")

    monkeypatch.setattr(ct.triage, "status", broken)
    with pytest.raises(KeyError):
        ct.main(["status", "--run", run_dir])
    record = oplog.read(run_dir)[-1]
    assert record["event"] == "error" and record["level"] == "critical" and record["command"] == "status"
    assert record["error"] == "KeyError: 'issues'"
