"""Setup support for /coverity-setup and /coverity-help (spec D-66 to D-70).

The guide agent asks the person only for the Coverity URL, project and stream; the rest
is detected here, shown for confirmation, and written by :func:`write_project_config`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from . import config as cfg
from .coverity import make_client
from .encoding import decode
from .envvars import get_env
from .run_state import RUN_FILE

SOURCE_SUFFIXES = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".inl"}
MAX_SCANNED_FILES = 3000
DEFAULT_OUTPUT_DIR = "../coverity-triage-out"
DEFAULT_FILTER = "untriaged.yaml"


def _run(args: list[str], cwd: Path) -> str | None:
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, check=False)
    except FileNotFoundError:
        return None
    return proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else None


# ---- detection -------------------------------------------------------------------------------


def detect_vcs(root: Path) -> str | None:
    if (root / ".git").exists():
        return "git"
    if (root / ".svn").exists():
        return "svn"
    return None


def detect_base_branch(root: Path) -> str:
    head = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], root)
    if head and "/" in head:
        return head.split("/", 1)[1]
    current = _run(["git", "symbolic-ref", "--short", "HEAD"], root)  # also on a branch without commits
    return current or "main"


def detect_encoding(root: Path) -> dict[str, Any]:
    """Count C/C++ sources by encoding among files that contain non-ASCII text."""
    counts = {"utf-8": 0, "cp932": 0}
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() not in SOURCE_SUFFIXES:
                continue
            scanned += 1
            if scanned > MAX_SCANNED_FILES:
                break
            try:
                source = decode((Path(dirpath) / name).read_bytes())
            except Exception:
                continue
            if not source.ascii_only:
                counts[source.encoding] += 1
        if scanned > MAX_SCANNED_FILES:
            break
    encoding = "cp932" if counts["cp932"] > counts["utf-8"] else "utf-8"
    return {"encoding": encoding, "files_with_utf8": counts["utf-8"],
            "files_with_cp932": counts["cp932"], "scanned": min(scanned, MAX_SCANNED_FILES)}


def detect_project(repo_root: str) -> dict[str, Any]:
    """Values decided automatically, for the person to confirm (spec D-70)."""
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ValueError(f"フォルダが見つかりません: {repo_root}")
    vcs = detect_vcs(root)
    encoding = detect_encoding(root)
    return {
        "repo_root": str(root),
        "has_config": (cfg.config_dir(root) / cfg.CONFIG_FILE_NAME).is_file(),
        "vcs_type": vcs,
        "vcs_note": None if vcs else "リポジトリのルート（.git または .svn があるフォルダ）を開いてください",
        "base_branch": detect_base_branch(root) if vcs == "git" else None,
        "ascii_file_encoding": encoding["encoding"],
        "encoding_evidence": encoding,
        "output_dir": DEFAULT_OUTPUT_DIR,
        "output_dir_resolved": str((root / DEFAULT_OUTPUT_DIR).resolve()),
        "filter_file": DEFAULT_FILTER,
    }


# ---- writing ---------------------------------------------------------------------------------

CONFIG_HEADER = (
    "# Coverity トリアージエージェントの設定（/coverity-setup で作成）\n"
    "# 変更したいときは /coverity-help に頼めます。パスワード等はここに書きません（環境変数）。\n"
)


def write_project_config(repo_root: str, coverity_url: str, project: str, stream: str,
                         vcs_type: str, base_branch: str | None = None,
                         ascii_file_encoding: str = "utf-8",
                         path_strip_prefixes: list[str] | None = None,
                         output_dir: str = DEFAULT_OUTPUT_DIR,
                         api: str = "auto", overwrite: bool = False) -> dict[str, Any]:
    """Write ``config.yaml`` and the default filter after the person confirmed the values."""
    root = Path(repo_root).resolve()
    target = cfg.config_dir(root)
    config_path = target / cfg.CONFIG_FILE_NAME
    if config_path.exists() and not overwrite:
        raise ValueError("設定ファイルがすでにあります。作り直す場合は overwrite=true を指定してください")
    data = {
        "coverity": {
            "url": coverity_url.rstrip("/"),
            "api": api,
            "user_env": "COV_USER",
            "key_env": "COV_AUTH_KEY",
            "revision_field": "version",
            "path_strip_prefixes": path_strip_prefixes or [],
        },
        "vcs": {
            "type": vcs_type,
            "base_branch": base_branch or "main",
            "branch_mode": "per_cid",
            "branch_prefix": "coverity-fix/",
            "github_token_env": "GITHUB_TOKEN",
        },
        "output_dir": output_dir,
        "max_items": 100,
        "parallel": 1,
        "deviation_target": "coverity",
        "ascii_file_encoding": ascii_file_encoding,
        "verify": {"default": "none", "build_command": "", "cov_build_args": "--dir idir",
                   "cov_analyze_args": "--dir idir --all"},
    }
    cfg.ProjectConfig.model_validate(data)  # same validation as at run time
    filter_data = {
        "name": "未トリアージ（上限 20 件）",
        "project": project,
        "streams": [stream],
        "triage": {"classification": ["Unclassified"]},
        "max_items": 20,
    }
    cfg.FilterSpec.model_validate(filter_data)
    (target / cfg.FILTERS_DIR_NAME).mkdir(parents=True, exist_ok=True)
    config_path.write_text(CONFIG_HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                           encoding="utf-8")
    filter_path = target / cfg.FILTERS_DIR_NAME / DEFAULT_FILTER
    if overwrite or not filter_path.exists():
        filter_path.write_text(yaml.safe_dump(filter_data, allow_unicode=True, sort_keys=False),
                               encoding="utf-8")
    return {"written": [str(config_path), str(filter_path)],
            "next": "内容を確認してコミットし、チームで共有してください（パスワード等は含まれていません）"}


# ---- diagnosis -------------------------------------------------------------------------------


def _check(name: str, ok: bool | None, detail: str, fix: str = "") -> dict[str, Any]:
    status = "ok" if ok else ("warning" if ok is None else "ng")
    return {"check": name, "status": status, "detail": detail, "how_to_fix": fix}


def doctor(repo_root: str) -> dict[str, Any]:
    """Machine checks of the PC and the repository; the agent explains the results."""
    root = Path(repo_root).resolve()
    checks: list[dict[str, Any]] = []
    vcs = detect_vcs(root)
    checks.append(_check("リポジトリ", bool(vcs), f"{vcs or '不明'}（{root}）",
                         "" if vcs else ".git または .svn があるフォルダを開いてください"))
    if vcs:
        found = shutil.which(vcs)
        checks.append(_check(f"{vcs} コマンド", bool(found), found or "見つかりません",
                             "" if found else f"{vcs} をインストールして PATH を通してください"))

    config_path = cfg.config_dir(root) / cfg.CONFIG_FILE_NAME
    config = None
    if not config_path.is_file():
        checks.append(_check("設定ファイル", False, "まだありません", "/coverity-setup で作成します"))
    else:
        try:
            config = cfg.load_project_config(root)
            checks.append(_check("設定ファイル", True, str(config_path)))
        except cfg.ConfigError as exc:
            checks.append(_check("設定ファイル", False, str(exc), "/coverity-help で修正できます"))

    if config is not None:
        filters = sorted(p.name for p in (cfg.config_dir(root) / cfg.FILTERS_DIR_NAME).glob("*.yaml"))
        checks.append(_check("条件ファイル", bool(filters), ", ".join(filters) or "ありません",
                             "" if filters else "/coverity-setup で作成します"))
        output = (root / config.output_dir).resolve() if not Path(config.output_dir).is_absolute() \
            else Path(config.output_dir)
        inside = output == root or root in output.parents
        checks.append(_check("出力先", not inside, str(output),
                             "リポジトリの外を指定してください（誤ってコミットしないため）" if inside else ""))
        if config.coverity.api == "fake":
            checks.append(_check("Coverity 接続", None, "偽データで動作中（coverity.api: fake）",
                                 "社内 Coverity に接続する準備ができたら /coverity-help に頼んでください"))
        else:
            for label, env in (("Coverity ユーザ名", config.coverity.user_env),
                               ("Coverity 認証キー", config.coverity.key_env)):
                checks.append(_check(label, bool(get_env(env)), f"環境変数 {env}",
                                     "" if get_env(env) else "/coverity-setup で入力します（伏せ字で入力）"))
        if config.vcs.type == "git":
            has_token = bool(get_env(config.vcs.github_token_env))
            checks.append(_check("GitHub トークン", True if has_token else None,
                                 f"環境変数 {config.vcs.github_token_env}",
                                 "" if has_token else "プルリクエストを作るときに必要です（/coverity-setup で入力）"))
        try:
            client = make_client(config.coverity)
            name = DEFAULT_FILTER if DEFAULT_FILTER in filters else (filters[0] if filters else None)
            spec = cfg.load_filter(root, name) if name else cfg.FilterSpec()
            found = len(client.search_issues(spec))
            checks.append(_check("Coverity から警告を取得", True, f"{found} 件（条件ファイル {spec.name}）"))
        except Exception as exc:
            checks.append(_check("Coverity から警告を取得", False, str(exc), "/coverity-help に表示内容を伝えてください"))

    ready = all(c["status"] != "ng" for c in checks)
    return {"ready": ready, "checks": checks}


# ---- runs ------------------------------------------------------------------------------------


def list_runs(repo_root: str, limit: int = 10) -> dict[str, Any]:
    """Recent runs, newest first, with whether each is unfinished (spec D-69)."""
    config = cfg.load_project_config(repo_root)
    output = Path(config.output_dir)
    output = output if output.is_absolute() else (Path(repo_root) / output).resolve()
    runs = []
    for run_file in sorted(output.glob(f"*/{RUN_FILE}"), reverse=True)[:limit]:
        meta = json.loads(run_file.read_text(encoding="utf-8"))
        counts = {"pending": 0, "in_progress": 0, "done": 0, "error": 0}
        for item in meta.get("items", []):
            counts[item.get("status", "pending")] += 1
        applied = sum(1 for i in meta.get("items", []) if i.get("applied"))
        runs.append({"run_dir": str(run_file.parent), "created_at": meta.get("created_at"),
                     "filter_name": meta.get("filter_name"), "status": counts,
                     "unfinished": counts["pending"] + counts["in_progress"] + counts["error"] > 0,
                     "applied_items": applied,
                     "summary_exists": (run_file.parent / "summary.md").is_file()})
    return {"output_dir": str(output), "runs": runs}
