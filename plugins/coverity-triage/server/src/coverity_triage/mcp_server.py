"""MCP server of the Coverity triage agent (spec D-40 to D-42; design 3).

Tools are grouped by who may use them. The agent definitions restrict each agent's tools:
the apply tools (``preview_apply`` / ``apply_approvals``) are given only to the apply agent.
"""

from __future__ import annotations

import functools
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import service
from .config import ConfigError
from .coverity import CoverityError
from .encoding import EncodingError
from .models import TriageResult
from .report import ReportError
from .run_state import RunError
from .vcs import VcsError
from .verify import VerifyError
from .workspace import WorkspaceError

EXPECTED_ERRORS = (service.ServiceError, ConfigError, CoverityError, EncodingError, ReportError,
                   RunError, VcsError, VerifyError, WorkspaceError, ValueError)

mcp = MCPServer(
    name="coverity-triage",
    instructions=(
        "Coverity 警告のトリアージ用ツール。start_run（または resume_run）が返す run_dir を以降の"
        "すべてのツールに渡す。ソースの参照・編集は必ず read_source / search_source / edit_source を使う。"
    ),
)


def tool(func):
    """Register a tool and turn expected errors into messages the agent can act on."""
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from exc
    return mcp.tool()(wrapper)


# ---- setup / run management (parent agent, commands) ---------------------------------------


@tool
def init_project(repo_root: str, vcs_type: str = "git") -> dict:
    """対象リポジトリに .coverity-triage/（設定・条件ファイル・偽データのひな形）を作る。既存ファイルは上書きしない。"""
    return service.init_project(repo_root, vcs_type)


@tool
def start_run(repo_root: str, filter_file: str, overrides: dict | None = None,
              verify_mode: str | None = None) -> dict:
    """トリアージを開始する。条件ファイルで CID を検索し、上限件数で切り、グループ候補を作り、実行フォルダを作る。

    overrides: チャットで指定された条件の上書き（例: {"impacts": ["High"], "triage": {"status": ["New"]}}）。
    verify_mode: none / build / build+analyze。省略時は設定ファイルの値。
    """
    return service.start_run(repo_root, filter_file, overrides, verify_mode)


@tool
def resume_run(run_dir: str) -> dict:
    """中断した実行を再開する。処理中だった項目とエラーの項目を未処理に戻す。"""
    return service.resume_run(run_dir)


@tool
def get_run_status(run_dir: str) -> dict:
    """実行の進捗（未処理・処理中・完了・エラーの件数とエラー内容）を返す。"""
    return service.get_run_status(run_dir)


@tool
def next_work_item(run_dir: str) -> dict:
    """次の作業項目（CID またはグループ）を処理中にして返す。item が null なら残りなし。"""
    return service.next_work_item(run_dir)


@tool
def build_summary(run_dir: str) -> dict:
    """一覧サマリ summary.md を作る（確信度の低い順、承認列に推奨案を下書き。人が記入済みの承認は保持）。"""
    return service.build_summary(run_dir)


# ---- worker subagent ------------------------------------------------------------------------


@tool
def get_issue_detail(run_dir: str, item_id: str) -> dict:
    """作業項目の各 CID の基本情報・イベント（警告経路）・チェッカー説明を返す。"""
    return service.get_issue_detail(run_dir, item_id)


@tool
def prepare_workspaces(run_dir: str, item_id: str) -> dict:
    """調査用（analyzed: 解析リビジョン）と修正用（fix: 最新リビジョン）の作業領域を用意し、リビジョン情報とずれの確認結果を返す。"""
    return service.prepare_workspaces(run_dir, item_id)


@tool
def read_source(run_dir: str, item_id: str, workspace: str, path: str,
                start_line: int = 1, end_line: int | None = None) -> dict:
    """作業領域（analyzed / fix / annotation）のファイルを行番号付きで読む。path はリポジトリからの相対パス。"""
    return service.read_source(run_dir, item_id, workspace, path, start_line, end_line)


@tool
def search_source(run_dir: str, item_id: str, workspace: str, pattern: str,
                  glob: str | None = None) -> dict:
    """作業領域を正規表現で検索する。glob でファイルを絞り込める（例: "*.c", "src/*.h"）。"""
    return service.search_source(run_dir, item_id, workspace, pattern, glob)


@tool
def edit_source(run_dir: str, item_id: str, workspace: str, path: str,
                old_text: str, new_text: str) -> dict:
    """fix / annotation の作業領域のファイルで、old_text（一意に一致すること）を new_text に置き換える。文字コード・改行コードは保持される。"""
    return service.edit_source(run_dir, item_id, workspace, path, old_text, new_text)


@tool
def save_fix(run_dir: str, item_id: str, kind: str, message: str) -> dict:
    """fix / annotation の変更を保存し、差分ファイル・修正後ファイルを出力する（git はコミットとブランチも作る）。"""
    return service.save_fix(run_dir, item_id, kind, message)


@tool
def verify_fix(run_dir: str, item_id: str, kind: str = "fix", mode: str | None = None) -> dict:
    """設定ファイルのコマンドでビルド / 再解析し、警告が消えたか・新しい警告が出たかを返す。mode 省略時は実行時の指定。"""
    return service.verify_fix(run_dir, item_id, kind, mode)


@tool
def submit_result(run_dir: str, item_id: str, result: TriageResult) -> dict:
    """判断結果を提出する（各項目の書き方は skill triage-report を参照）。不備があればエラーになるので直して再提出する。"""
    return service.submit_result(run_dir, item_id, result)


@tool
def report_error(run_dir: str, item_id: str, message: str) -> dict:
    """作業項目を処理できなかったことを記録する（その項目はスキップされ、再開時にやり直せる）。"""
    return service.report_error(run_dir, item_id, message)


# ---- apply agent only -------------------------------------------------------------------------


@tool
def preview_apply(run_dir: str) -> dict:
    """一覧サマリの承認列を読み、反映する件数と confirmation_token を返す。結果を人に見せて同意を得ること。"""
    return service.preview_apply(run_dir)


@tool
def apply_approvals(run_dir: str, confirmation_token: str) -> dict:
    """人の同意を得た後に反映する（Coverity 書き戻し、push とプルリクエスト作成、svn patch 適用）。"""
    return service.apply_approvals(run_dir, confirmation_token)


# ---- stats ------------------------------------------------------------------------------------


@tool
def get_stats(repo_root: str) -> dict:
    """出力先フォルダのすべての実行について、採用状況・手直し・処理時間を集計する。"""
    return service.get_stats(service.output_dir_of(repo_root))


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
