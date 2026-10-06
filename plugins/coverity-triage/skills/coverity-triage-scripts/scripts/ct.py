# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0", "pydantic>=2.7"]
# ///
"""Scripts of the Coverity triage plugin: the fixed steps that need no judgement.

Run with ``uv run <this folder>/ct.py <command> ...``. Every command prints one JSON object.
``"ok": false`` (exit code 1) means the command could not do its work; ``error`` says why and
``problems`` lists what to fix. Usage of each command: skill coverity-triage-scripts (SKILL.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ctlib import apply, knowledge, metrics, prepare, selftest, triage
from ctlib.common import CtError
from ctlib.config import load_config, output_dir


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("checkers", "impacts"):
        if getattr(args, key):
            out[key] = getattr(args, key)
    triage_filter = {k: getattr(args, k) for k in ("status", "classification", "action") if getattr(args, k)}
    if triage_filter:
        out["triage"] = triage_filter
    return out


def _stats(repo_root: str) -> dict[str, Any]:
    from pathlib import Path
    repo = Path(repo_root).resolve()
    return {"ok": True, **metrics.aggregate(output_dir(repo, load_config(repo)))}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ct.py", description="Coverity トリアージのスクリプト")
    sub = p.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, *options: tuple[str, dict[str, Any]]) -> argparse.ArgumentParser:
        cmd = sub.add_parser(name, help=help_text)
        for flag, kwargs in options:
            cmd.add_argument(flag, **kwargs)
        return cmd

    repo = ("--repo", {"required": True, "help": "対象リポジトリの一番上のフォルダ"})
    run = ("--run", {"required": True, "help": "実行フォルダ（new-run が返す run_dir）"})
    item = ("--item", {"required": True, "help": "作業の ID（CID か G1 など）"})

    # setup
    command("prewarm", "MCP サーバの Python 環境を作る")
    command("doctor", "準備の状況を調べる", repo)
    command("detect", "リポジトリから決められる値を調べる", repo)
    command("init-config", "設定・条件・知識のファイルを作る", repo,
            ("--url", {"required": True}), ("--project", {"required": True}), ("--stream", {"required": True}),
            ("--base-branch", {}), ("--output-dir", {}),
            ("--strip-prefix", {"action": "append", "default": [], "dest": "strip_prefixes"}),
            ("--overwrite", {"action": "store_true"}))
    command("install-agent", "サブエージェントの定義を ~/.copilot/agents に置く")
    command("trial-build", "（オプション）修正前の最新のコードを試しにビルドする", repo,
            ("--build-command", {"required": True}), ("--setup-command", {"default": ""}),
            ("--build-dir", {"default": ""}))
    command("set-verify", "（オプション）ビルドでの検証の設定を書く", repo,
            ("--build-command", {"required": True}), ("--setup-command", {"default": ""}),
            ("--build-dir", {"default": ""}),
            ("--default", {"default": "build", "choices": ["none", "build", "build+analyze"]}),
            ("--cov-build-args", {}), ("--cov-analyze-args", {}))

    # run
    command("runs", "実行の一覧（新しい順）", repo, ("--limit", {"type": int, "default": 10}))
    command("new-run", "実行フォルダを作り、条件を決める", repo,
            ("--filter", {"help": "条件ファイル（filters/ の名前かパス）"}),
            ("--checker", {"action": "append", "dest": "checkers"}),
            ("--impact", {"action": "append", "dest": "impacts"}),
            ("--status", {"action": "append"}), ("--classification", {"action": "append"}),
            ("--action", {"action": "append"}), ("--limit", {"type": int}),
            ("--verify", {"choices": ["none", "build", "build+analyze"]}))
    command("plan", "検索結果から作業の一覧を作る", run)
    command("next", "次の作業を取り出し、修正用のコピーを用意する", run)
    command("brief", "サブエージェントへの指示ファイルを作る", run, item)
    command("finish", "結果を確かめ、差分・ブランチ・詳細レポートを作る", run, item)
    command("fail", "作業を処理できなかったと記録する", run, item, ("--reason", {"required": True}))
    command("resume", "止まった実行を再開できるようにする", run)
    command("status", "実行の進み具合", run)
    command("verify", "（オプション）修正案をまとめてビルドで確かめる", run,
            ("--mode", {"choices": ["none", "build", "build+analyze"]}))
    command("summary", "一覧 summary.md を作る", run)

    # apply
    command("preview", "反映の内容と確認用の文字列を作る", run)
    command("apply-code", "修正を反映する（git: push、svn: 差分の適用）", run, ("--token", {"required": True}))
    command("knowledge-candidates", "（オプション）知識の追記の候補", run)
    command("add-knowledge", "（オプション）知識を追記する", run,
            ("--entry", {"action": "append", "required": True, "dest": "entries"}))
    command("stats", "（オプション）効果測定の集計", repo)

    # self-test
    selftest.add_commands(sub)
    return p


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    c = args.command
    if c == "prewarm":
        return prepare.prewarm()
    if c == "doctor":
        return prepare.doctor(args.repo)
    if c == "detect":
        return prepare.detect(args.repo)
    if c == "init-config":
        return prepare.init_config(args.repo, args.url, args.project, args.stream, args.base_branch,
                                   args.output_dir, args.strip_prefixes, args.overwrite)
    if c == "install-agent":
        return prepare.install_agent()
    if c == "trial-build":
        return prepare.trial_build(args.repo, args.setup_command, args.build_command, args.build_dir)
    if c == "set-verify":
        return prepare.set_verify(args.repo, args.default, args.setup_command, args.build_command, args.build_dir,
                                  args.cov_build_args, args.cov_analyze_args)
    if c == "runs":
        return triage.runs(args.repo, args.limit)
    if c == "new-run":
        return triage.new_run(args.repo, args.filter, _overrides(args), args.limit, args.verify)
    if c == "plan":
        return triage.plan(args.run)
    if c == "next":
        return triage.next_item(args.run)
    if c == "brief":
        return triage.brief(args.run, args.item)
    if c == "finish":
        return triage.finish(args.run, args.item)
    if c == "fail":
        return triage.fail(args.run, args.item, args.reason)
    if c == "resume":
        return triage.resume(args.run)
    if c == "status":
        return triage.status(args.run)
    if c == "verify":
        return triage.verify(args.run, args.mode)
    if c == "summary":
        return triage.summary(args.run)
    if c == "preview":
        return apply.preview(args.run)
    if c == "apply-code":
        return apply.apply_code(args.run, args.token)
    if c == "knowledge-candidates":
        return knowledge.candidates(args.run)
    if c == "add-knowledge":
        return knowledge.add(args.run, args.entries)
    if c == "stats":
        return _stats(args.repo)
    return selftest.dispatch(args)


def main(argv: list[str] | None = None) -> int:
    # Never stop on a character the console cannot show.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
        code = 0 if result.get("ok", True) else 1
    except CtError as exc:
        result, code = {"ok": False, "error": str(exc), **exc.details}, 1
    except OSError as exc:
        result, code = {"ok": False, "error": f"ファイルやコマンドの操作に失敗しました: {exc}"}, 1
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
