"""Dynamic self-test in the person's environment, for /coverity-selftest (spec D-81).

The skill drives the test; checks that follow fixed rules are judged here, and every result
is written to one result folder (``report.md`` and ``raw/``) for later root-cause analysis.
Before anything is written, host names, user names and credentials are masked; the source
code of in-house repositories is never recorded, only its shape (file names, line numbers,
checker names, counts, field names).
"""

from __future__ import annotations

import getpass
import json
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from . import apply, knowledge, onboarding, runs
from . import config as cfg
from .connect import COLUMN_KEYS, REQUIRED_FIELDS, ConnectClient
from .envvars import get_env
from .report import DEV_BEGIN, DEV_END, read_approvals
from .vcs import VcsError, make_vcs, run_cmd

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
ASSETS = PLUGIN_ROOT / "skills" / "coverity-selftest" / "assets"
STATE_FILE = "selftest.json"
REPORT_FILE = "report.md"
DEFAULT_BASE = Path.home() / "coverity-triage-selftest"

SECTIONS = {"1": "プラグインの組み込み", "2": "偽データでの一連の流れ",
            "3": "社内 Coverity 接続（読み取りのみ）", "4": "実ビルドでの自動検証"}
STATUS_JA = {"pass": "成功", "fail": "失敗", "review": "要確認", "info": "記録", "skip": "未実施"}

WORKER_TOOLS = ["get_issue_detail", "prepare_workspaces", "read_source", "search_source",
                "edit_source", "save_fix", "submit_result", "report_error"]
FORBIDDEN_WORKER_TOOLS = ["apply_approvals", "preview_apply", "run_in_terminal", "runInTerminal",
                          "execute", "terminal", "bash", "powershell", "shell"]
SOAP_OPERATIONS = {"defectservice": ["getStreamDefects"],
                   "configurationservice": ["getVersion", "getSnapshotsForStream", "getSnapshotInformation"]}
REST_FEATURES = [((2022, 6), "トリアージの書き戻し（PUT /api/v2/issues/triage）"),
                 ((2023, 9), "警告経路の取得（GET /api/v2/issues/sourceCodeInfo）")]
COV_COMMANDS = ["cov-build", "cov-analyze", "cov-format-errors"]


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    expected: str


CHECKS = [
    Check("1-1", "MCP サーバの起動とツール", "Copilot から coverity-triage のツールがすべて見える"),
    Check("1-2", "Python 環境の場所", "PLUGIN_DATA の venv で動き、プラグイン本体に .venv が無い"),
    Check("1-3", "/ メニューの入口", "入口の Skill 5 つが出て、調査用の Skill 5 つは出ない"),
    Check("1-4", "エージェントの一覧", "coverity-triage-worker が一覧に出ない"),
    Check("1-5", "サブエージェントの起動", "coverity-triage-worker を名前で指定して起動できる"),
    Check("1-6", "サブエージェントのツール制限", "調査用の 8 ツールがあり、反映のツールとターミナルが無い"),
    Check("1-7", "Skill の読み込み", "サブエージェントが Skill triage-investigation を読み込める"),
    Check("1-8", "モデルの固定", "サブエージェントが gpt-6 luna で動く"),
    Check("2-1", "偽データの作業リポジトリ", "作成して git に登録できる"),
    Check("2-2", "トリアージの実行", "すべての作業項目が完了し、summary.md ができる"),
    Check("2-3", "レポートの形", "各レポートに修正案と逸脱コメント案があり、承認列が下書きされている"),
    Check("2-4", "グループ", "CID 20004〜20006 が 1 つのグループにまとまる"),
    Check("2-5", "AI の結論", "想定の結論（skills/coverity-selftest/assets/expected.yaml）と合う"),
    Check("2-6", "反映の安全策", "確認用の文字列が無い・違うと apply_approvals が反映を拒否する"),
    Check("2-7", "反映", "逸脱は書き戻しが記録され、修正はリモートが無いため push のエラーになる"),
    Check("2-8", "知識の追記", "人が変えた項目が候補になり、knowledge.md に追記できる"),
    Check("2-9", "効果測定", "採用状況を集計できる"),
    Check("3-1", "準備状況（doctor）", "すべて ok"),
    Check("3-2", "REST API の接続と認証", "列の一覧を取得できる"),
    Check("3-3", "列キー", "必要な列キーがそろっている"),
    Check("3-4", "警告の検索", "条件ファイルで警告を検索できる"),
    Check("3-5", "SOAP API とバージョン", "必要な操作があり、認証付きで呼べる。書き戻しに必要な 2022.6 以降"),
    Check("3-6", "警告経路（SOAP）", "警告経路のイベントを取得できる"),
    Check("3-7", "警告経路（REST の sourceCodeInfo）", "応答の形を記録する（判定はしない）"),
    Check("3-8", "スナップショットのリビジョン", "設定の項目に値があり、そのリビジョンがリポジトリにある"),
    Check("3-9", "実際の警告 1 件の調査", "詳細レポートに警告経路・修正案・逸脱コメント案がある"),
    Check("4-1", "検証の設定", "ビルドのコマンドが設定されている"),
    Check("4-2", "試しのビルド", "修正前の最新コードをビルドできる"),
    Check("4-3", "Coverity のコマンド", "cov-build / cov-analyze / cov-format-errors（--json-output-v7）が使える"),
    Check("4-4", "修正案を当てた検証（ビルド＋再解析）", "ビルドと再解析が終わり、結果が詳細レポートに載る"),
]
CHECK_IDS = {c.id: c for c in CHECKS}


# ---- result folder ---------------------------------------------------------------------------


class Masker:
    """Replaces credentials, user names and Coverity host names before anything is written."""

    def __init__(self, repo_root: str | None):
        self.secrets: list[str] = []
        self.users = {getpass.getuser(), Path.home().name}
        self.hosts: set[str] = set()
        config = _load_config(repo_root)
        if config is not None:
            self.secrets = [v for v in (get_env(config.coverity.key_env),
                                        get_env(config.vcs.github_token_env)) if v]
            user = get_env(config.coverity.user_env)
            if user:
                self.users.add(user)
            host = urlparse(config.coverity.url).hostname
            if host:
                self.hosts.add(host)

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "****")
        for host in sorted(self.hosts, key=len, reverse=True):
            value = re.sub(re.escape(host), "<coverity-host>", value, flags=re.I)
        for user in sorted((u for u in self.users if len(u) >= 2), key=len, reverse=True):
            value = re.sub(rf"(?<!\w){re.escape(user)}(?!\w)", "<user>", value, flags=re.I)
        return value

    def __call__(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self(v) for v in value]
        return value


def _load_config(repo_root: str | None) -> cfg.ProjectConfig | None:
    if not repo_root or not (cfg.config_dir(repo_root) / cfg.CONFIG_FILE_NAME).is_file():
        return None
    try:
        return cfg.load_project_config(repo_root)
    except cfg.ConfigError:
        return None


class Result:
    """One result folder: state in selftest.json, report.md re-rendered on every record."""

    def __init__(self, result_dir: str | Path):
        self.dir = Path(result_dir)
        path = self.dir / STATE_FILE
        if not path.is_file():
            raise ValueError(f"テスト結果のフォルダではありません: {result_dir}（selftest_start から始めてください）")
        self.state = json.loads(path.read_text(encoding="utf-8"))
        self.mask = Masker(self.state.get("repo_root"))

    def save(self) -> None:
        (self.dir / STATE_FILE).write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.dir / REPORT_FILE).write_text(render_report(self.state), encoding="utf-8")

    def raw(self, name: str, data: Any) -> str:
        """Write raw data (masked) to raw/<name>; returns the path relative to the result folder."""
        folder = self.dir / "raw"
        folder.mkdir(exist_ok=True)
        if isinstance(data, str):
            path = folder / (name if "." in name else f"{name}.txt")
            path.write_text(self.mask(data), encoding="utf-8")
        else:
            path = folder / f"{name}.json"
            path.write_text(json.dumps(self.mask(data), ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
        return path.relative_to(self.dir).as_posix()

    def record(self, check_id: str, status: str, actual: str, detail: str = "",
               raw: Any = None) -> dict[str, Any]:
        if check_id not in CHECK_IDS:
            raise ValueError(f"確認項目 {check_id} はありません（{', '.join(CHECK_IDS)}）")
        if status not in STATUS_JA:
            raise ValueError(f"status は {' / '.join(STATUS_JA)} のいずれかです")
        entry = {"status": status, "actual": self.mask(actual), "detail": self.mask(detail),
                 "time": datetime.now().isoformat(timespec="seconds")}
        if raw is not None:
            entry["raw"] = self.raw(check_id, raw)
        self.state["checks"][check_id] = entry
        return {"check": check_id, "status": status, "actual": entry["actual"]}


def _normalize_sections(sections: list[str] | None) -> list[str]:
    circled = {"①": "1", "②": "2", "③": "3", "④": "4"}
    picked = []
    for s in sections or list(SECTIONS):
        key = circled.get(str(s).strip(), str(s).strip())
        if key not in SECTIONS:
            raise ValueError(f"範囲は ①〜④（1〜4）で指定してください: {s}")
        if key not in picked:
            picked.append(key)
    return sorted(picked)


def start(sections: list[str] | None = None, repo_root: str | None = None, client: str = "",
          out_dir: str | None = None) -> dict[str, Any]:
    picked = _normalize_sections(sections)
    base = Path(out_dir).expanduser() if out_dir else DEFAULT_BASE
    result_dir = base / datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 1
    while result_dir.exists():
        n += 1
        result_dir = base / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{n}"
    result_dir.mkdir(parents=True)
    root = str(Path(repo_root).resolve()) if repo_root else None
    state = {"created_at": datetime.now().isoformat(timespec="seconds"), "sections": picked,
             "repo_root": root, "client": client, "environment": _environment(), "checks": {}}
    (result_dir / STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    result = Result(result_dir)
    for key in SECTIONS:
        if key not in picked:
            continue
        reason = _not_ready(key, root)
        if reason:
            for check in CHECKS:
                if check.id.startswith(f"{key}-"):
                    result.record(check.id, "skip", reason)
    result.save()
    todo = [c.id for c in CHECKS if c.id[0] in picked and c.id not in result.state["checks"]]
    return {"result_dir": str(result_dir), "report": str(result_dir / REPORT_FILE),
            "sections": {k: SECTIONS[k] for k in picked}, "checks_to_run": todo,
            "skipped": {k: v["actual"] for k, v in result.state["checks"].items()}}


def _not_ready(section: str, repo_root: str | None) -> str:
    if section in ("1", "2"):
        return ""
    config = _load_config(repo_root)
    if config is None:
        return "/coverity-setup 済みのリポジトリを開いていないため（.coverity-triage/config.yaml が無い）"
    if config.coverity.api == "fake":
        return "設定が偽データ（coverity.api: fake）のため"
    if section == "4" and not config.verify.build_command:
        return "検証の設定（verify.build_command）が無いため（/coverity-setup の段階 5 で設定できます）"
    return ""


def _environment() -> dict[str, Any]:
    import importlib.metadata as md
    plugin = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    tools = {}
    for name in ("git", "svn", "uv"):
        try:
            out = run_cmd([name, "--version"]).decode("utf-8", "replace").splitlines()
            tools[name] = out[0].strip() if out else "?"
        except VcsError:
            tools[name] = "見つかりません"
    return {"os": platform.platform(), "python": sys.version.split()[0],
            "plugin_version": plugin.get("version"), "mcp_package": md.version("mcp"),
            "python_env": sys.prefix, "commands": tools}


def record(result_dir: str, check_id: str, status: str, actual: str, detail: str = "",
           raw: str | None = None) -> dict[str, Any]:
    """A result the AI observed or the person answered."""
    result = Result(result_dir)
    out = result.record(check_id, status, actual, detail, raw)
    if check_id == "1-5" and status == "fail":
        _skip_worker_checks(result)
        out["skipped"] = WORKER_DEPENDENT
    result.save()
    return out


# ---- steps -----------------------------------------------------------------------------------


def step(name: str, result_dir: str, run_dir: str | None = None, repo_root: str | None = None,
         answer: str | None = None, tool_names: list[str] | None = None) -> dict[str, Any]:
    steps = {"plugin": _step_plugin, "worker": _step_worker, "sample": _step_sample,
             "flow_run": _step_flow_run, "flow_apply": _step_flow_apply,
             "flow_knowledge": _step_flow_knowledge, "coverity": _step_coverity,
             "real_run": _step_real_run, "build": _step_build, "verify": _step_verify}
    if name not in steps:
        raise ValueError(f"step は {' / '.join(steps)} のいずれかです")
    result = Result(result_dir)
    args = {"run_dir": run_dir, "repo_root": repo_root or result.state.get("repo_root"),
            "answer": answer or "", "tool_names": tool_names or []}
    try:
        out = steps[name](result, **args)
    finally:
        result.save()
    out["report"] = str(result.dir / REPORT_FILE)
    return out


def _require(value: str | None, name: str) -> str:
    if not value:
        raise ValueError(f"この step には {name} が必要です")
    return value


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text) is not None


# ① plugin -------------------------------------------------------------------------------------


def _step_plugin(result: Result, answer: str, tool_names: list[str], **_: Any) -> dict[str, Any]:
    missing = [n for n in tool_names if not _mentions(answer, n)]
    result.record("1-1", "fail" if missing else "pass",
                  f"見えないツール: {', '.join(missing)}" if missing else f"{len(tool_names)} 個すべて見える",
                  raw={"server_tools": tool_names, "visible_to_ai": answer})

    env_path = get_env("UV_PROJECT_ENVIRONMENT") or ""
    prefix = Path(sys.prefix).resolve()
    problems = []
    if not env_path or "${" in env_path:
        problems.append(f"UV_PROJECT_ENVIRONMENT が展開されていません（{env_path or '未設定'}）")
    elif prefix != Path(env_path).resolve():
        problems.append(f"Python 環境が UV_PROJECT_ENVIRONMENT と違います（{prefix}）")
    if PLUGIN_ROOT in prefix.parents:
        problems.append("Python 環境がプラグイン本体のフォルダの中にあります")
    if (PLUGIN_ROOT / "server" / ".venv").exists():
        problems.append("プラグイン本体のフォルダに .venv があります（以前の版の残りの可能性）")
    result.record("1-2", "fail" if problems else "pass", "／".join(problems) or f"{prefix}",
                  raw={"sys_prefix": str(prefix), "UV_PROJECT_ENVIRONMENT": env_path,
                       "plugin_root": str(PLUGIN_ROOT)})
    return {"checks": ["1-1", "1-2"]}


def worker_probe_heading() -> str:
    text = (PLUGIN_ROOT / "skills" / "triage-investigation" / "SKILL.md").read_text(encoding="utf-8")
    body = text.split("---", 2)[2] if text.startswith("---") else text
    return next((line.lstrip("#").strip() for line in body.splitlines() if line.startswith("# ")), "")


WORKER_DEPENDENT = ["1-6", "1-7", "1-8"]


def _skip_worker_checks(result: Result) -> None:
    """Checks that need the worker cannot be done once 1-5 failed."""
    for check_id in WORKER_DEPENDENT:
        result.record(check_id, "skip", "1-5 でサブエージェントを起動できなかったため")


def _skip_unfinished(result: Result, check_ids: list[str], why: str) -> dict[str, Any]:
    for check_id in check_ids:
        result.record(check_id, "skip", why)
    return {"checks": check_ids, "skipped": why}


NO_DONE = "完了した作業項目が無いため（2-2 を参照）"


def _step_worker(result: Result, answer: str, **_: Any) -> dict[str, Any]:
    if not answer.strip():
        result.record("1-5", "fail", "サブエージェントから返答がありません")
        _skip_worker_checks(result)
        return {"checks": ["1-5", *WORKER_DEPENDENT]}
    result.record("1-5", "pass", "起動して返答した", raw=answer)
    tools_line = next((line for line in answer.splitlines() if line.strip().upper().startswith("TOOLS:")), None)
    if tools_line is None:
        result.record("1-6", "review", "返答に TOOLS: の行がありません（返答は生データを参照）")
    else:
        listed = tools_line.split(":", 1)[1]
        missing = [t for t in WORKER_TOOLS if not _mentions(listed, t)]
        forbidden = [t for t in FORBIDDEN_WORKER_TOOLS if _mentions(listed, t)]
        problems = ([f"無いツール: {', '.join(missing)}"] if missing else []) + \
                   ([f"使えてはいけないツール: {', '.join(forbidden)}"] if forbidden else [])
        result.record("1-6", "fail" if problems else "pass", "／".join(problems) or "期待どおり",
                      detail=f"申告されたツール: {listed.strip()}\n"
                             "判定はサブエージェントの申告に基づく（実際に使えないかまでは確かめていない）")
    heading = worker_probe_heading()
    skill_line = next((line for line in answer.splitlines() if line.strip().upper().startswith("SKILL:")), "")
    told = skill_line.split(":", 1)[1].strip() if skill_line else ""
    ok = bool(heading) and heading in told
    result.record("1-7", "pass" if ok else "fail",
                  "見出しが一致" if ok else f"見出しが一致しません（返答: {told or 'なし'}）",
                  detail=f"正しい見出し: {heading}")
    return {"checks": ["1-5", "1-6", "1-7"]}


# ② fake data flow -----------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> None:
    run_cmd(["git", "-c", "user.name=coverity-selftest", "-c", "user.email=selftest@localhost",
             "-c", "core.autocrlf=false", *args], cwd)


def _step_sample(result: Result, **_: Any) -> dict[str, Any]:
    target = result.dir / "sample-target"
    try:
        shutil.copytree(ASSETS / "sample-target", target)
        _git(["init", "-q", "-b", "main"], target)
        _git(["add", "."], target)
        _git(["commit", "-q", "-m", "init"], target)
    except (OSError, VcsError) as exc:
        result.record("2-1", "fail", str(exc))
        raise ValueError(f"偽データの作業リポジトリを作れませんでした: {exc}") from exc
    result.record("2-1", "pass", str(target))
    return {"repo_root": str(target), "filter_file": "all.yaml"}


def _reports(run: runs.Run) -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted((run.dir / "cid").glob("*.md"))}


def _report_shape(markdown: str) -> dict[str, Any]:
    """What a per-item report contains, without its text."""
    evidence = markdown.split("### 警告経路", 1)[1].split("###", 1)[0] if "### 警告経路" in markdown else ""
    deviation = markdown.split(DEV_BEGIN, 1)[1] if DEV_BEGIN in markdown else ""
    comment = deviation.split("逸脱コメント:", 1)[1].split("<!--", 1)[0].strip() if "逸脱コメント:" in deviation else ""
    return {"headings": re.findall(r"^#{1,3} .+$", markdown, re.M),
            "events": len(re.findall(r"^- ", evidence, re.M)),
            "deviation_comment": bool(comment),
            "fix_diff": "- 差分: `" in markdown,
            "fix_summary": bool(re.search(r"^- 概要: \S", markdown, re.M))}


def _shape_problems(shape: dict[str, Any]) -> list[str]:
    problems = []
    if not shape["deviation_comment"]:
        problems.append("逸脱コメント案が空")
    if not shape["fix_summary"]:
        problems.append("修正案の概要が無い")
    if not shape["events"]:
        problems.append("警告経路が無い")
    return problems


def _step_flow_run(result: Result, run_dir: str | None, **_: Any) -> dict[str, Any]:
    run = runs.Run(_require(run_dir, "run_dir"))
    meta = run.store.load()
    counts = run.store.status_counts()
    summary = run.dir / "summary.md"
    unfinished = {k: v for k, v in counts.items() if k != "done" and v}
    result.record("2-2", "fail" if unfinished or not summary.is_file() else "pass",
                  f"状態 {counts}、summary.md {'あり' if summary.is_file() else 'なし'}",
                  detail="".join(f"{i.id}: {i.error}\n" for i in meta.items if i.status == "error"),
                  raw={"status": counts, "items": [i.model_dump() for i in meta.items]})
    if summary.is_file():
        result.raw("2-2-summary.md", summary.read_text(encoding="utf-8"))

    reports = _reports(run)
    for item_id, text in reports.items():
        result.raw(f"2-3-{item_id}.md", text)
    problems = []
    done = [i.id for i in meta.items if i.status == "done"]
    if not done:
        result.record("2-3", "skip", NO_DONE)
    else:
        for item_id in done:
            if item_id not in reports:
                problems.append(f"{item_id}: 詳細レポートが無い")
                continue
            problems += [f"{item_id}: {p}" for p in _shape_problems(_report_shape(reports[item_id]))]
        try:
            drafted = read_approvals(summary.read_text(encoding="utf-8")) if summary.is_file() else {}
        except Exception as exc:  # the summary itself is broken
            drafted, problems = {}, problems + [f"承認列を読めない: {exc}"]
        problems += [f"{i}: 承認列が空" for i in done if i not in drafted]
        result.record("2-3", "fail" if problems else "pass",
                      "／".join(problems) or f"{len(done)} 件すべて期待どおり")

    expected = yaml.safe_load((ASSETS / "expected.yaml").read_text(encoding="utf-8"))
    group = set(expected["group"]["cids"])
    grouped = [i for i in meta.items if set(i.cids) == group]
    excluded = []
    for i in meta.items:
        if i.is_group and i.status == "done":
            excluded += run.read_json(i.id, "").get("group_excluded_cids", [])
    if grouped:
        result.record("2-4", "pass", f"{grouped[0].id} にまとまった")
    elif excluded:
        result.record("2-4", "review", f"AI がグループから外した CID: {excluded}",
                      raw={"items": [{"id": i.id, "cids": i.cids} for i in meta.items]})
    else:
        result.record("2-4", "fail", "まとまっていない",
                      raw={"items": [{"id": i.id, "cids": i.cids} for i in meta.items]})

    if not done:
        result.record("2-5", "skip", NO_DONE)
        return {"checks": ["2-2", "2-3", "2-4", "2-5"]}
    differ, compared = [], {}
    for cid, want in expected["items"].items():
        item = next((i for i in meta.items if i.id == cid and i.status == "done"), None)
        if item is None:
            differ.append(f"{cid}: 結論なし（調査が完了していない。2-2 を参照）")
            continue
        data = run.read_json(cid, "")
        compared[cid] = {"expected": want["recommendation"], "actual": data.get("recommendation"),
                         "judgement": data.get("verdict", {}).get("judgement"),
                         "confidence": data.get("confidence"), "summary": data.get("verdict", {}).get("summary")}
        if data.get("recommendation") != want["recommendation"]:
            differ.append(f"{cid}: 想定 {want['recommendation']}（{want['reason']}）、AI {data.get('recommendation')}")
    result.record("2-5", "review" if differ else "pass", "／".join(differ) or "想定どおり", raw=compared)
    return {"checks": ["2-2", "2-3", "2-4", "2-5"]}


def _set_approval(markdown: str, item_id: str, label: str) -> str:
    out = []
    for line in markdown.splitlines():
        cells = line.split("|")
        if len(cells) > 3 and re.match(rf"^\s*{re.escape(item_id)}(?:（|\s*$)", cells[2]):
            cells[1] = f" {label} "
            line = "|".join(cells)
        out.append(line)
    return "\n".join(out) + "\n"


def _step_flow_apply(result: Result, run_dir: str | None, **_: Any) -> dict[str, Any]:
    """Act as the person: change approvals, edit one comment, then apply (A-5 steps 3, 4)."""
    run = runs.Run(_require(run_dir, "run_dir"))
    summary = run.dir / "summary.md"
    text = summary.read_text(encoding="utf-8")
    drafted = read_approvals(text)
    changes = {}
    if "20002" in drafted:
        changes["20002"] = "逸脱"
        report = run.dir / "cid" / "20002.md"
        body = report.read_text(encoding="utf-8")
        report.write_text(body.replace(DEV_END, "（テストで手直しした一文）\n" + DEV_END), encoding="utf-8")
    group = next((i for i in drafted if i.startswith("G")), None)
    if group:
        changes[group] = "却下"
    for item_id, label in changes.items():
        text = _set_approval(text, item_id, label)
    summary.write_text(text, encoding="utf-8")

    refused = []
    for token in ("", "0000"):
        try:
            apply.apply_approvals(str(run.dir), token)
        except runs.ServiceError:
            refused.append(token or "（空）")
    preview = apply.preview_apply(str(run.dir))
    safe = len(refused) == 2
    result.record("2-6", "pass" if safe else "fail",
                  "確認用の文字列が無い・違う場合は拒否した" if safe else f"拒否されなかった: {refused}",
                  raw={"preview": preview, "changed_by_test": changes})

    applied = apply.apply_approvals(str(run.dir), preview["confirmation_token"])
    approvals = read_approvals(text)
    writes_path = Path(run.config.coverity.fake_data or "").with_name("fake-issues.yaml.writes.jsonl")
    writes = writes_path.read_text(encoding="utf-8").splitlines() if writes_path.is_file() else []
    problems = []
    for outcome in applied["results"]:
        action = approvals.get(outcome.get("item", ""))
        if action == "deviation" and outcome.get("coverity") != "登録済み":
            problems.append(f"{outcome['item']}: 逸脱を書き戻せなかった（{outcome.get('error', '')}）")
        if action == "fix" and outcome.get("ok"):
            problems.append(f"{outcome['item']}: リモートが無いのに修正の反映が成功した")
        if action == "reject" and not outcome.get("ok"):
            problems.append(f"{outcome['item']}: 却下の処理に失敗した（{outcome.get('error', '')}）")
    deviations = sum(1 for a in approvals.values() if a == "deviation")
    if len(writes) != deviations:
        problems.append(f"書き戻しの記録 {len(writes)} 件（逸脱 {deviations} 件）")
    raw = {"apply_results": applied["results"], "writes": writes}
    if "20002" not in changes:  # nothing to write back: the run did not finish 20002
        result.record("2-7", "skip", "反映を試す CID 20002 が完了していないため（2-2 を参照）", raw=raw)
    else:
        if not deviations:
            problems.append("逸脱にした項目が無い")
        result.record("2-7", "fail" if problems else "pass", "／".join(problems) or
                      f"逸脱 {deviations} 件を書き戻し、修正は push のエラー（想定どおり）", raw=raw)
    candidates = knowledge.knowledge_candidates(str(run.dir))
    return {"checks": ["2-6", "2-7"], "knowledge_candidates": candidates["candidates"],
            "current_knowledge": candidates["current_knowledge"]}


def _step_flow_knowledge(result: Result, run_dir: str | None, **_: Any) -> dict[str, Any]:
    run = runs.Run(_require(run_dir, "run_dir"))
    if not any(i.status == "done" for i in run.store.load().items):
        return _skip_unfinished(result, ["2-8", "2-9"], NO_DONE)
    candidates = knowledge.knowledge_candidates(str(run.dir))["candidates"]
    text = cfg.load_knowledge(run.meta.repo_root)
    added = f"実行 {run.meta.run_id}" in text
    status = "pass" if candidates and added else "fail"
    result.record("2-8", status, f"候補 {len(candidates)} 件、追記 {'あり' if added else 'なし'}",
                  raw={"candidates": candidates, "knowledge_md": text})
    try:
        stats = runs.get_stats(runs.output_dir_of(run.meta.repo_root))
        ok = stats.get("items", 0) > 0
        result.record("2-9", "pass" if ok else "fail", f"{stats.get('items', 0)} 件を集計", raw=stats)
    except Exception as exc:
        result.record("2-9", "fail", str(exc))
    return {"checks": ["2-8", "2-9"]}


# ③ Coverity Connect ---------------------------------------------------------------------------


def _shape(value: Any, depth: int = 0) -> Any:
    """Field names and types of a JSON value, without its values."""
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        return {k: _shape(v, depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        return [_shape(value[0], depth + 1), f"（{len(value)} 件）"] if value else []
    return type(value).__name__


def _version(text: str) -> tuple[int, int] | None:
    match = re.match(r"(\d{4})\.(\d+)", text or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def _step_coverity(result: Result, repo_root: str | None, **_: Any) -> dict[str, Any]:
    root = _require(repo_root, "repo_root")
    config = cfg.load_project_config(root)
    report = onboarding.doctor(root)
    bad = [c["check"] for c in report["checks"] if c["status"] != "ok"]
    result.record("3-1", "pass" if not bad else ("fail" if not report["ready"] else "review"),
                  f"ok 以外: {', '.join(bad)}" if bad else "すべて ok", raw=report)

    client = ConnectClient(config.coverity)
    try:
        columns = client.columns()
        result.record("3-2", "pass", f"列 {len(columns)} 個", raw=columns)
    except Exception as exc:
        result.record("3-2", "fail", str(exc))
        columns = {}
    keys = set(columns.values())
    required = [COLUMN_KEYS[f] for f in REQUIRED_FIELDS if COLUMN_KEYS[f] not in keys]
    optional = [k for k in COLUMN_KEYS.values() if k not in keys and k not in required]
    result.record("3-3", "fail" if required else ("review" if optional else "pass"),
                  (f"必須の列が無い: {', '.join(required)}。" if required else "")
                  + (f"無い列（その項目は空になる）: {', '.join(optional)}" if optional else "")
                  or "すべてある")

    filters = sorted(p.name for p in (cfg.config_dir(root) / cfg.FILTERS_DIR_NAME).glob("*.yaml"))
    filter_file = onboarding.DEFAULT_FILTER if onboarding.DEFAULT_FILTER in filters else (filters[0] if filters else "")
    issues = []
    try:
        spec = cfg.load_filter(root, filter_file)
        issues = client.search_issues(spec)
        first = issues[0] if issues else None
        result.record("3-4", "pass" if issues else "review",
                      f"{len(issues)} 件（条件ファイル {filter_file}）" + ("" if issues else "。0 件のため 3-6・3-9 は試せません"),
                      raw={"filter": spec.model_dump(), "count": len(issues),
                           "first": first.model_dump(include={"cid", "checker", "file", "line", "function",
                                                               "impact", "classification", "action", "status"})
                           if first else None,
                           "empty_fields_of_first": [k for k, v in first.model_dump().items() if v in (None, "", [])]
                           if first else None})
    except Exception as exc:
        result.record("3-4", "fail", str(exc))
        spec = None

    _check_soap(result, client)
    first = issues[0] if issues else None
    if first is not None:
        try:
            detail = client.get_issue_detail(first)
            result.record("3-6", "pass" if detail.events else "review",
                          f"CID {first.cid}: イベント {len(detail.events)} 件",
                          raw={"cid": first.cid, "events": [{"file": e.file, "line": e.line, "tag": e.tag, "main": e.main}
                                                            for e in detail.events],
                               "has_checker_description": bool(detail.checker_description)})
        except Exception as exc:
            result.record("3-6", "fail", str(exc))
        _check_source_code_info(result, client, first)
    else:
        result.record("3-6", "skip", "検索結果が 0 件のため")
        result.record("3-7", "skip", "検索結果が 0 件のため")
    _check_snapshot(result, client, config, root, spec)
    return {"checks": [f"3-{n}" for n in range(1, 9)],
            "filter_file": filter_file if issues else None,
            "next": "filter_file が null でなければ、その条件で 1 件だけトリアージを実行し、step real_run を呼ぶ"
                    if issues else "検索結果が無いため 3-9 は未実施として記録する"}


def _check_soap(result: Result, client: ConnectClient) -> None:
    found, missing = {}, []
    for service, operations in SOAP_OPERATIONS.items():
        try:
            wsdl = client.http.get(f"/ws/v9/{service}?wsdl").text
        except Exception as exc:
            wsdl = ""
            found[service] = f"WSDL を取得できない: {exc}"
        names = set(re.findall(r'<(?:\w+:)?operation\s+name="([^"]+)"', wsdl))
        for op in operations:
            found[op] = op in names
            if op not in names:
                missing.append(op)
    try:
        response = client._soap("configurationservice", "getVersion", "")
        version = next((n.text or "" for n in response.iter() if n.tag.split("}")[-1] == "externalVersion"), "")
    except Exception as exc:
        version, missing = "", missing + [f"getVersion（{exc}）"]
    current = _version(version)
    features = {feature: (current >= since if current else None) for since, feature in REST_FEATURES}
    problems = [f"無い操作: {', '.join(missing)}"] if missing else []
    if current and current < REST_FEATURES[0][0]:
        problems.append(f"バージョン {version} では REST の書き戻しが使えない")
    result.record("3-5", "fail" if problems else "pass",
                  "／".join(problems) or f"バージョン {version}",
                  raw={"operations": found, "version": version, "features": features})


def _check_source_code_info(result: Result, client: ConnectClient, issue: Any) -> None:
    tried = {}
    for params in ({"cid": issue.cid}, {"cid": issue.cid, "streamName": issue.stream}):
        try:
            resp = client.http.get("/api/v2/issues/sourceCodeInfo", params=params,
                                   auth=(client.user, client.key), headers={"Accept": "application/json"})
            try:
                body = _shape(resp.json())
            except ValueError:
                body = f"JSON ではない応答（{len(resp.content)} バイト）"
            tried[str(sorted(params))] = {"status": resp.status_code, "shape": body}
        except Exception as exc:
            tried[str(sorted(params))] = {"error": str(exc)}
    statuses = [v.get("status") for v in tried.values()]
    result.record("3-7", "info", f"応答 {statuses}", raw=tried)


def _check_snapshot(result: Result, client: ConnectClient, config: cfg.ProjectConfig, root: str,
                    spec: cfg.FilterSpec | None) -> None:
    if spec is None or not spec.streams:
        result.record("3-8", "skip", "条件ファイルにストリームが無いため")
        return
    field = config.coverity.revision_field
    try:
        response = client._soap("configurationservice", "getSnapshotsForStream",
                                f"<streamId><name>{escape(spec.streams[0])}</name></streamId>")
        ids = [int(n.text) for n in response.iter() if n.tag.split("}")[-1] == "id" and (n.text or "").isdigit()]
        info = client._soap("configurationservice", "getSnapshotInformation",
                            f"<snapshotIds><id>{max(ids)}</id></snapshotIds>") if ids else None
    except Exception as exc:
        result.record("3-8", "fail", str(exc))
        return
    record = next((n for n in info.iter() if n.tag.split("}")[-1] == "return"), None) if info is not None else None
    fields = {c.tag.split("}")[-1]: bool((c.text or "").strip()) for c in record} if record is not None else {}
    value = next(((c.text or "").strip() for c in record if c.tag.split("}")[-1] == field), "") if record is not None else ""
    found = False
    if value:
        try:
            found = make_vcs(root, config.vcs, result.dir / "work", "selftest").has_revision(value)
        except Exception:
            found = False
    status = "pass" if value and found else "review"
    actual = (f"{field} = {value}、リポジトリに{'ある' if found else '無い'}" if value
              else f"{field} が空（手元のコードで調査し、ずれを検出する動きになる）")
    result.record("3-8", status, actual, raw={"snapshots": len(ids), "fields_with_value": fields})


def _step_real_run(result: Result, run_dir: str | None, **_: Any) -> dict[str, Any]:
    run = runs.Run(_require(run_dir, "run_dir"))
    meta = run.store.load()
    reports = _reports(run)
    shapes = {item_id: _report_shape(text) for item_id, text in reports.items()}
    done = [i for i in meta.items if i.status == "done"]
    problems = [f"{i.id}: {i.error}" for i in meta.items if i.status == "error"]
    for item in done:
        if item.id not in shapes:
            problems.append(f"{item.id}: 詳細レポートが無い")
        else:
            problems += [f"{item.id}: {p}" for p in _shape_problems(shapes[item.id])]
    result.record("3-9", "fail" if problems or not done else "pass",
                  "／".join(problems) or f"{done[0].id}: 期待どおり",
                  raw={"status": run.store.status_counts(), "report_shapes": shapes,
                       "run_dir": str(run.dir)})
    return {"checks": ["3-9"], "run_dir": str(run.dir)}


# ④ build verification ---------------------------------------------------------------------------


def _error_lines(log: str | Path | None, limit: int = 40) -> list[str]:
    """Lines of a build log that report errors, without the source lines compilers echo."""
    path = Path(log) if log else None
    if path is None or not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    picked = [line.strip()[:300] for line in lines
              if re.search(r"error|エラー|fatal", line, re.I) and not re.match(r"^\s*\d*\s*\|", line)]
    return picked[:limit]


def _step_build(result: Result, repo_root: str | None, **_: Any) -> dict[str, Any]:
    root = _require(repo_root, "repo_root")
    config = cfg.load_project_config(root)
    verify = config.verify
    result.record("4-1", "pass", f"build_dir={verify.build_dir or '（なし）'}、既定の検証={verify.default}",
                  raw={"setup_command": verify.setup_command, "build_dir": verify.build_dir,
                       "build_command": verify.build_command, "cov_build_args": verify.cov_build_args,
                       "cov_analyze_args": verify.cov_analyze_args})
    trial = onboarding.trial_build(root, verify.setup_command, verify.build_command, verify.build_dir)
    result.record("4-2", "pass" if trial["build_ok"] else "fail",
                  f"{'成功' if trial['build_ok'] else '失敗'}（{trial['seconds']} 秒）",
                  raw={"build_ok": trial["build_ok"], "seconds": trial["seconds"],
                       "error_lines": _error_lines(trial["log"])})

    built = Path(trial["built_in"])
    which = "where" if sys.platform == "win32" else "command -v"
    steps = []
    if verify.setup_command:
        steps.append(("call " if sys.platform == "win32" else "") + verify.setup_command.replace("{root}", str(built)))
    found, help_text = {}, ""
    for command in COV_COMMANDS:
        proc = subprocess.run(" && ".join(steps + [f"{which} {command}"]), cwd=built, shell=True,
                              capture_output=True, check=False)
        found[command] = proc.returncode == 0
    if found["cov-format-errors"]:
        proc = subprocess.run(" && ".join(steps + ["cov-format-errors --help"]), cwd=built, shell=True,
                              capture_output=True, check=False)
        help_text = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    json_v7 = "json-output-v7" in help_text
    missing = [c for c, ok in found.items() if not ok]
    problems = ([f"見つからない: {', '.join(missing)}"] if missing else []) + \
               ([] if json_v7 or missing else ["cov-format-errors に --json-output-v7 が無い"])
    result.record("4-3", "fail" if problems else "pass", "／".join(problems) or "すべて使える",
                  raw={"found": found, "json_output_v7": json_v7})
    return {"checks": ["4-1", "4-2", "4-3"], "ready": trial["build_ok"] and not problems}


def _step_verify(result: Result, run_dir: str | None, **_: Any) -> dict[str, Any]:
    run = runs.Run(_require(run_dir, "run_dir"))
    verifies = {i.id: run.read_json(i.id, ".verify").get("fix") for i in run.store.load().items}
    verifies = {k: v for k, v in verifies.items() if v}
    if not verifies:
        result.record("4-4", "fail", "検証の結果がありません（修正案が無い、または verify_run が動いていない）")
        return {"checks": ["4-4"]}
    raw, problems, notes = {}, [], []
    for item_id, v in verifies.items():
        report = (run.dir / "cid" / f"{item_id}.md").read_text(encoding="utf-8")
        shown = "## 6. 自動検証の結果" in report and "実施していません" not in report.split("## 6.", 1)[1]
        raw[item_id] = {k: v.get(k) for k in ("mode", "applied", "build_ok", "resolved_cids", "remaining_cids",
                                               "new_issue_count", "problems", "note")}
        raw[item_id]["error_lines"] = _error_lines(v.get("log"))
        raw[item_id]["shown_in_report"] = shown
        if v.get("mode") != "build+analyze":
            problems.append(f"{item_id}: 検証方法が {v.get('mode')}")
        if not v.get("build_ok"):
            problems.append(f"{item_id}: ビルドまたは解析に失敗")
        if not shown:
            problems.append(f"{item_id}: 詳細レポートに結果が無い")
        if v.get("build_ok") and v.get("problems"):
            notes.append(f"{item_id}: {'、'.join(v['problems'])}")
    analysis = (run.dir / "work" / "verify" / "after.json").is_file()
    if not analysis and not problems:
        problems.append("再解析の結果（cov-format-errors の JSON）が無い")
    status = "fail" if problems else ("review" if notes else "pass")
    result.record("4-4", status, "／".join(problems + notes) or "ビルドと再解析が終わり、警告が解消した", raw=raw,
                  detail="要確認は、仕組みは動いたが修正案に問題が見つかったことを表します" if status == "review" else "")
    return {"checks": ["4-4"]}


# ---- report.md ---------------------------------------------------------------------------------


def render_report(state: dict[str, Any]) -> str:
    checks = state["checks"]
    picked = [c for c in CHECKS if c.id[0] in state["sections"]]
    counts = {k: 0 for k in STATUS_JA}
    for c in picked:
        counts[checks[c.id]["status"] if c.id in checks else "skip"] += 1
    env = state.get("environment", {})
    out = ["# coverity-selftest の結果", "",
           f"- 実施日時: {state['created_at'].replace('T', ' ')}",
           f"- 範囲: {'、'.join(f'{k} {SECTIONS[k]}' for k in state['sections'])}",
           f"- Copilot: {state.get('client') or '不明'}",
           f"- OS: {env.get('os')} ／ Python {env.get('python')} ／ プラグイン {env.get('plugin_version')}",
           "- コマンド: " + "、".join(f"{k} {v}" for k, v in (env.get("commands") or {}).items()),
           "- 共有する前に：ホスト名・ユーザ名・認証情報は伏せ字にし、社内のソースコードは記録していません。"
           "ストリーム名やファイルのパスは残るので、確認してから共有してください。", "",
           "| " + " | ".join(STATUS_JA.values()) + " |", "|" + "---|" * len(STATUS_JA),
           "| " + " | ".join(str(counts[k]) for k in STATUS_JA) + " |", ""]
    for key in state["sections"]:
        out += [f"## {key}. {SECTIONS[key]}", "",
                "| ID | 確認すること | 結果 | 期待 | 実際 |", "|---|---|---|---|---|"]
        for c in picked:
            if not c.id.startswith(f"{key}-"):
                continue
            entry = checks.get(c.id, {"status": "skip", "actual": "まだ実行していない"})
            actual = entry["actual"].replace("|", "\\|").replace("\n", " ")
            out.append(f"| {c.id} | {c.title} | {STATUS_JA[entry['status']]} | {c.expected} | {actual} |")
        out.append("")
    details = [c for c in picked if checks.get(c.id, {}).get("status") in ("fail", "review", "info")]
    if details:
        out += ["## 失敗・要確認・記録の詳細", ""]
        for c in details:
            entry = checks[c.id]
            out += [f"### {c.id} {c.title}（{STATUS_JA[entry['status']]}）", "",
                    f"- 期待: {c.expected}", f"- 実際: {entry['actual']}"]
            if entry.get("detail"):
                out.append(f"- 詳細: {entry['detail'].strip()}")
            if entry.get("raw"):
                out.append(f"- 生データ: [{entry['raw']}]({entry['raw']})")
            out.append("")
    return "\n".join(out) + "\n"
