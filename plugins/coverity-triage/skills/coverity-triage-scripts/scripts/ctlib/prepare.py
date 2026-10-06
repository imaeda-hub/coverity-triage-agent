"""Commands of /coverity-setup: prewarm, doctor, detect, init-config, install-agent, trial-build, set-verify."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import yaml

from .common import PLUGIN_ROOT, CtError, out_text, run_cmd
from .config import (CONFIG_DIR, CONFIG_FILE, DEFAULT_FILTER, DEFAULT_OUTPUT_DIR, FILTERS_DIR, KNOWLEDGE_TEMPLATE,
                     FilterSpec, ProjectConfig, VerifyConfig, knowledge_path, load_config, output_dir)
from .envvars import get_env
from .vcs import detect as detect_vcs
from .vcs import make_vcs
from .verify import trial_build as run_trial_build

SERVER_DIR = PLUGIN_ROOT / "server"
WORKER_AGENT = PLUGIN_ROOT / "com.github.copilot" / "agents" / "coverity-triage-worker.agent.md"
CONFIG_HEADER = ("# Coverity トリアージの設定（/coverity-setup で作成）。チームで共有するためコミットします。\n"
                 "# 認証情報はここに書きません（環境変数に置きます）。\n")


def _uv() -> str:
    """uv itself; ``uv run`` tells the script where it is."""
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        raise CtError("uv が見つかりません（/coverity-setup の手順で入れます）")
    return uv


def user_agents_dir() -> Path:
    """Folder of the person's own custom agents, read by GitHub Copilot."""
    return Path.home() / ".copilot" / "agents"


# ---- prewarm / install-agent -----------------------------------------------------------------


def prewarm() -> dict[str, Any]:
    """Create the MCP server's Python environment now, so its first start is quick."""
    started = time.monotonic()
    uv = _uv()
    version = out_text(run_cmd([uv, "--version"]))
    run_cmd([uv, "sync", "--native-tls", "--frozen", "--quiet", "--directory", str(SERVER_DIR)])
    return {"ok": True, "uv": version, "environment": str(SERVER_DIR / ".venv"),
            "seconds": round(time.monotonic() - started, 1)}


def agent_state() -> dict[str, Any]:
    target = user_agents_dir() / WORKER_AGENT.name
    if not target.is_file():
        return {"status": "missing", "path": str(target)}
    same = target.read_bytes() == WORKER_AGENT.read_bytes()
    return {"status": "current" if same else "outdated", "path": str(target)}


def install_agent() -> dict[str, Any]:
    """Copy the worker agent to the person's agents folder (where Copilot finds it in every harness)."""
    before = agent_state()["status"]
    target = user_agents_dir() / WORKER_AGENT.name
    if before != "current":
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(WORKER_AGENT, target)
    return {"ok": True, "status": {"missing": "installed", "outdated": "updated", "current": "unchanged"}[before],
            "path": str(target), "restart_needed": before != "current"}


# ---- detect / init-config --------------------------------------------------------------------


def _git_ref(repo: Path, ref: str) -> str | None:
    proc = run_cmd(["git", "symbolic-ref", "--short", ref], repo, check=False)
    if proc.returncode != 0:
        return None
    return out_text(proc) or None


def detect(repo_root: str) -> dict[str, Any]:
    """Values decided from the repository, for the person to confirm."""
    repo = Path(repo_root).resolve()
    if not repo.is_dir():
        raise CtError(f"フォルダがありません: {repo}")
    vcs = detect_vcs(repo)
    base = None
    if vcs == "git":
        head = _git_ref(repo, "refs/remotes/origin/HEAD")
        base = head.split("/", 1)[1] if head and "/" in head else (_git_ref(repo, "HEAD") or "main")
    return {"ok": True, "repo_root": str(repo), "vcs": vcs,
            "vcs_note": None if vcs else "git / svn のリポジトリの一番上のフォルダ（.git か .svn があるフォルダ）を開いてください",
            "base_branch": base, "has_config": (repo / CONFIG_DIR / CONFIG_FILE).is_file(),
            "output_dir": DEFAULT_OUTPUT_DIR, "output_dir_resolved": str((repo / DEFAULT_OUTPUT_DIR).resolve())}


def init_config(repo_root: str, url: str, project: str, stream: str, base_branch: str | None,
                out_dir: str | None, strip_prefixes: list[str], overwrite: bool) -> dict[str, Any]:
    """Write config.yaml, the first filter and knowledge.md, after the person confirmed the values."""
    found = detect(repo_root)
    repo = Path(found["repo_root"])
    if not found["vcs"]:
        raise CtError(found["vcs_note"])
    target = repo / CONFIG_DIR
    config_path = target / CONFIG_FILE
    if config_path.exists() and not overwrite:
        raise CtError("設定ファイルがもうあります。作り直すときは --overwrite を付けます", path=str(config_path))
    config = {
        "coverity": {"url": url.rstrip("/"), "api": "auto", "user_env": "COV_USER", "key_env": "COV_AUTH_KEY",
                     "triage_store": "Default Triage Store", "revision_field": "sourceVersion",
                     "path_strip_prefixes": strip_prefixes},
        "vcs": {"type": found["vcs"], "base_branch": base_branch or found["base_branch"] or "main",
                "branch_prefix": "coverity-fix/"},
        "output_dir": out_dir or DEFAULT_OUTPUT_DIR,
        "max_items": 100,
        "parallel": 1,
        "options": {"annotation": False, "per_run_branch": False, "knowledge_suggestions": False, "metrics": False},
        "verify": VerifyConfig().model_dump(),
    }
    if found["vcs"] == "svn":
        del config["vcs"]["base_branch"], config["vcs"]["branch_prefix"]
    ProjectConfig.model_validate(config)
    out = output_dir(repo, ProjectConfig.model_validate(config))
    if out == repo or repo in out.parents:
        raise CtError(f"出力先はリポジトリの外にしてください: {out}")
    first_filter = {"name": "未トリアージ（20 件まで）", "project": project, "streams": [stream],
                    "triage": {"classification": ["Unclassified"]}, "limit": 20}
    FilterSpec.model_validate(first_filter)
    (target / FILTERS_DIR).mkdir(parents=True, exist_ok=True)
    config_path.write_text(CONFIG_HEADER + yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                           encoding="utf-8")
    written = [str(config_path)]
    filter_path = target / FILTERS_DIR / DEFAULT_FILTER
    if overwrite or not filter_path.exists():
        filter_path.write_text(yaml.safe_dump(first_filter, allow_unicode=True, sort_keys=False), encoding="utf-8")
        written.append(str(filter_path))
    if not knowledge_path(repo).exists():  # never overwrite what the team wrote
        knowledge_path(repo).write_text(KNOWLEDGE_TEMPLATE, encoding="utf-8")
        written.append(str(knowledge_path(repo)))
    return {"ok": True, "written": written,
            "next": "内容を確かめてコミットし、チームで共有するよう人に伝える（認証情報は入っていません）"}


# ---- doctor ----------------------------------------------------------------------------------


def _check(name: str, status: str, detail: str, fix: str = "") -> dict[str, str]:
    return {"check": name, "status": status, "detail": detail, "how_to_fix": fix}


def doctor(repo_root: str) -> dict[str, Any]:
    """What is still missing on this PC and in this repository. Coverity itself is checked by the MCP
    tool check_connection."""
    repo = Path(repo_root).resolve()
    checks = []
    vcs = detect_vcs(repo)
    checks.append(_check("リポジトリ", "ok" if vcs else "ng", f"{vcs or '不明'}（{repo}）",
                         "" if vcs else ".git か .svn があるフォルダを開いてください"))
    if vcs:
        found = shutil.which(vcs)
        checks.append(_check(f"{vcs} コマンド", "ok" if found else "ng", found or "見つかりません",
                             "" if found else f"{vcs} を入れて PATH を通してください"))
    venv = (SERVER_DIR / ".venv").is_dir()
    checks.append(_check("MCP サーバの Python 環境", "ok" if venv else "ng", str(SERVER_DIR / ".venv"),
                         "" if venv else "ct.py prewarm を実行します"))
    agent = agent_state()
    checks.append(_check("サブエージェント coverity-triage-worker", "ok" if agent["status"] == "current" else "ng",
                         {"current": "入っています", "missing": "まだ入っていません",
                          "outdated": "プラグインの更新前のものです"}[agent["status"]] + f"（{agent['path']}）",
                         "" if agent["status"] == "current" else "ct.py install-agent を実行し、VS Code（Copilot CLI）を再起動します"))

    config_path = repo / CONFIG_DIR / CONFIG_FILE
    config = None
    if not config_path.is_file():
        checks.append(_check("設定ファイル", "ng", "まだありません", "/coverity-setup で作ります"))
    else:
        try:
            config = load_config(repo)
            checks.append(_check("設定ファイル", "ok", str(config_path)))
        except CtError as exc:
            checks.append(_check("設定ファイル", "ng", str(exc), "表示された項目を直します"))
    if config is not None:
        filters = []
        for path in sorted((repo / CONFIG_DIR / FILTERS_DIR).glob("*.yaml")):
            try:
                spec = FilterSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
                filters.append(path.name if len(spec.streams) == 1 else f"{path.name}（ストリームが 1 つでない）")
            except Exception as exc:  # noqa: BLE001 - report every broken file
                filters.append(f"{path.name}（読めません: {str(exc)[:100]}）")
        bad = [f for f in filters if "（" in f]
        checks.append(_check("条件ファイル", "ng" if bad or not filters else "ok", "、".join(filters) or "ありません",
                             "条件ファイルを直します" if bad else ("" if filters else "/coverity-setup で作ります")))
        out = output_dir(repo, config)
        inside = out == repo or repo in out.parents
        checks.append(_check("出力先", "ng" if inside else "ok", str(out),
                             "リポジトリの外にしてください（誤ってコミットしないため）" if inside else ""))
        if config.coverity.api == "fake":
            checks.append(_check("Coverity", "warning", "偽データを使う設定です（coverity.api: fake）",
                                 "社内の Coverity を使うときは coverity.api を auto にします"))
        else:
            for label, name in (("Coverity のユーザ名", config.coverity.user_env),
                                ("Coverity の認証キー", config.coverity.key_env)):
                ok = bool(get_env(name))
                checks.append(_check(label, "ok" if ok else "ng", f"環境変数 {name}: {'あり' if ok else 'なし'}",
                                     "" if ok else "/coverity-setup の手順で、ターミナルの伏せ字の欄から入れます"))
            if config.coverity.url.lower().startswith("http://"):
                checks.append(_check("Coverity の URL", "warning", "http（認証キーが暗号化されずに送られます）",
                                     "https が使えるか Coverity の管理者に確かめます"))
        if config.vcs.type == "git" and vcs == "git":
            vcs_ops = make_vcs(repo, config.vcs)
            has_base = vcs_ops.has_revision(config.vcs.base_branch) or vcs_ops.has_revision(f"origin/{config.vcs.base_branch}")
            checks.append(_check("取り込み先のブランチ", "ok" if has_base else "ng", config.vcs.base_branch,
                                 "" if has_base else "設定 vcs.base_branch を直します"))
            remote = out_text(run_cmd(["git", "remote", "get-url", "origin"], repo, check=False))
            checks.append(_check("git のリモート origin", "ok" if remote else "warning", remote or "ありません",
                                 "" if remote else "修正を push できません（反映のときに必要）"))
    ready = all(c["status"] != "ng" for c in checks)
    return {"ok": True, "ready": ready, "checks": checks,
            "next": "Coverity への接続は MCP の check_connection で確かめる" if ready else "status が ng の項目を直す"}


# ---- build verification settings (option) ---------------------------------------------------


def trial_build(repo_root: str, setup_command: str, build_command: str, build_dir: str) -> dict[str, Any]:
    """Build the latest code once with the given commands, before saving them."""
    repo = Path(repo_root).resolve()
    config = load_config(repo)
    settings = config.verify.model_copy(update={"setup_command": setup_command, "build_command": build_command,
                                                "build_dir": build_dir})
    work = output_dir(repo, config) / "_trial-build"
    if work.exists():
        shutil.rmtree(work)
    vcs = make_vcs(repo, config.vcs)
    latest, note = vcs.resolve_latest()
    vcs.export(latest, work / "src")
    result = run_trial_build(settings, work / "src", work / "trial-build.log")
    return {"ok": True, **result, "revision": latest, "note": note, "built_in": str(work / "src")}


def set_verify(repo_root: str, default: str, setup_command: str, build_command: str, build_dir: str,
               cov_build_args: str | None, cov_analyze_args: str | None) -> dict[str, Any]:
    """Save the build verification settings the person agreed to."""
    repo = Path(repo_root).resolve()
    path = repo / CONFIG_DIR / CONFIG_FILE
    text = path.read_text(encoding="utf-8")
    header = "".join(line + "\n" for line in text.splitlines() if line.startswith("#"))
    data = yaml.safe_load(text) or {}
    verify = {**(data.get("verify") or {}), "default": default, "setup_command": setup_command,
              "build_dir": build_dir, "build_command": build_command}
    if cov_build_args is not None:
        verify["cov_build_args"] = cov_build_args
    if cov_analyze_args is not None:
        verify["cov_analyze_args"] = cov_analyze_args
    data["verify"] = verify
    ProjectConfig.model_validate(data)
    path.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"ok": True, "written": str(path), "verify": verify}
