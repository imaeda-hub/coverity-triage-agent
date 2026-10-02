# Coverity トリアージエージェント 構成設計（たたき台）

> 本書は `docs/spec.md` の決定事項（D-xx）に基づく構成案である。
> 各項目はユーザとの対話で確認し、確定したものから「確定」と記す。現時点はすべて **案**。

## 1. プラグイン全体構成（案）

Agent Plugins 1.0 の構成に従う（D-1）。どのクライアントでも共通に使える部品（Skill・MCP）はプラグイン直下に置き、Copilot 固有の部品（エージェント・コマンド）は `com.github.copilot/` に置く。

```
coverity-triage/                         … プラグインのルート
├─ plugin.json                           … マニフェスト（$schema: agent-plugins.org 1.0.0）
├─ mcp.json                              … MCP サーバ起動設定（Python 製 MCP サーバ 1 つ・確定）
├─ skills/                               … 共通に使える知識・手順（5 つに分ける・確定）
│  ├─ triage-investigation/SKILL.md      … 調査手順：警告経路の追跡、真偽判定、確信度基準（D-64）
│  ├─ checker-knowledge/                 … チェッカー別の判断観点（D-6）
│  │  ├─ SKILL.md
│  │  └─ references/ standard.md, misra.md, cert.md
│  ├─ code-fix/SKILL.md                  … 修正方針（D-37, D-38）、文字コード保持（D-36）、社内規約（U-3）
│  ├─ deviation-comment/SKILL.md         … 逸脱コメント（D-22〜D-24）、アノテーション（D-59）
│  └─ triage-report/SKILL.md             … レポートに書く内容の基準（D-20, D-55）
├─ com.github.copilot/                   … Copilot 固有の部品
│  ├─ agents/
│  │  ├─ coverity-triage.agent.md        … 親エージェント：進捗管理・グループ化・サブエージェント起動・集約（D-39）
│  │  ├─ coverity-triage-worker.agent.md … サブエージェント：1 CID / 1 グループの調査 → 2 案作成 → 検証
│  │  └─ coverity-triage-apply.agent.md  … 承認の反映専用（apply_approvals を使えるのはこのエージェントだけ）
│  └─ commands/                          … 利用者向けの 5 つの操作（D-61）
│     ├─ init     … 初期設定
│     ├─ run      … トリアージ実行
│     ├─ resume   … 再開
│     ├─ apply    … 承認の反映
│     └─ stats    … 効果測定の集計
└─ server/                               … Python 製 MCP サーバ（D-40, D-41, D-42）
   ├─ pyproject.toml                     … Python 3.12 以上（D-46）
   └─ coverity_triage/
      ├─ mcp_server.py                   … ツールの公開窓口
      ├─ config.py                       … 設定ファイル・条件ファイルの読み込みと検証（D-49, D-50）
      ├─ coverity/                       … Coverity Connect 接続（REST / SOAP の差を吸収、U-1）
      ├─ vcs/                            … git / svn / GitHub の操作（D-25〜D-29, D-57, D-58）
      ├─ grouping.py                     … グループ候補の機械的な作成（D-53）
      ├─ verify.py                       … ビルド・再解析の実行（D-10, D-42）
      ├─ run_state.py                    … 実行フォルダ・進捗・再開（D-15, D-43, D-51）
      ├─ report.py                       … レポート生成と承認列の読み取り（D-18, D-54〜D-56, D-62）
      ├─ encoding.py                     … 文字コード・改行コードの判定と保持（D-36）
      └─ metrics.py                      … 効果測定の記録と集計（D-44）
```

### 対象リポジトリ側に置くファイル（D-50）

```
<対象リポジトリ>/.coverity-triage/
├─ config.yaml        … プロジェクト設定（init で生成）
├─ filters/*.yaml     … 絞り込み条件（複数用意可能）
└─ policy.md          … 将来：推奨方針のカスタムプロンプト（D-48）
```

## 2. 役割分担の原則（確定）

| 担当 | 担当すること | 理由 |
|---|---|---|
| AI（エージェント・Skill） | 警告経路とソースを読んで真偽を判断する、修正コードを書く、逸脱コメントを書く、確信度と推奨案を決める | 判断と文章作成は AI にしかできない |
| Python（MCP ツール） | API 通信、VCS 操作、ブランチ名・差分・出力先、ビルド・再解析の実行、進捗、レポートの体裁、承認列の読み取り、集計 | 毎回同じ結果になるべき処理。AI に任せると揺れや誤操作が起きる |

- レポートの生成（確定）：AI は判断結果を構造化データで `submit_result` に提出し、Python が CID レポート・一覧サマリの Markdown に整形する。根拠や逸脱コメントなどの文章の中身は AI が書く。

## 3. MCP ツール一覧（案）

- MCP サーバは 1 つにまとめる（確定）。実行フォルダや進捗などの状態をサーバ内で共有する。
- 外部に変更を加える `apply_approvals` は、承認の反映（apply）専用のエージェントだけに渡す（確定）。エージェント定義の `tools` で制限し、さらにツール側でも `preview_apply` で件数を提示して利用者の同意を得たことを確認できない限り実行しない（二重の防止）。
- 調査用サブエージェントにはターミナル（任意のコマンド実行）を許可しない（確定）。VCS・ビルド・再解析は MCP ツール経由でのみ行う。
- 作業領域のソースの読み取り・検索・編集は MCP ツール（`read_source` / `search_source` / `edit_source`）で行う（確定）。VS Code・CLI で同じ動きになり、ワークスペース外のファイルに対する許可確認で処理が止まらない。

| 分類 | ツール | 内容 | 関連 |
|---|---|---|---|
| 設定 | `init_project` | 設定・条件ファイルのひな形を生成 | D-61 |
| 実行管理 | `start_run` | 実行フォルダ作成、条件で CID を検索、上限件数で切り、グループ候補を作成、進捗ファイル作成 | D-11〜D-15, D-43, D-53 |
| | `next_work_item` / `get_run_status` | 未処理・エラーの CID / グループを返す | D-15, D-51 |
| | `resume_run` | 実行フォルダを指定して再開 | D-61 |
| Coverity | `get_issue_detail` | 基本情報＋イベント（警告経路）＋チェッカー説明・CWE | D-33 |
| VCS | `prepare_workspaces` | 解析リビジョンを特定し（D-17）、調査用（解析リビジョン）と修正用（最新）の作業領域を作る | D-16, D-17, D-57, D-58 |
| | `save_fix` | 修正用作業領域の変更から、差分ファイル・別領域出力・git コミット / ブランチを作る | D-4, D-25, D-26 |
| ソース操作 | `read_source` / `search_source` / `edit_source` | その CID / グループの作業領域に範囲を限定した読み取り・検索・編集。文字コード・改行コードを保持して保存する（確定） | D-36, D-57 |
| 検証 | `verify_fix` | 設定に従いビルド / 再解析を実行し、警告の消滅・新規警告を返す | D-10, D-42 |
| 結果 | `submit_result` | サブエージェントの判断結果（構造化データ）を受け取り、CID レポートを生成し、進捗を更新 | D-20 |
| | `build_summary` | 一覧サマリを生成（確信度の低い順、承認列に推奨案を下書き） | D-54〜D-56, D-62 |
| 反映 | `preview_apply` | 承認列を読み取り、反映件数を返す | D-63 |
| | `apply_approvals` | Coverity 書き戻し、push＋PR 作成、svn patch 適用 | D-27, D-29〜D-31, D-60 |
| 集計 | `get_stats` | 効果測定の集計 | D-44 |

## 4. 処理フロー（トリアージ実行・案）

- 並列度（確定）：サブエージェントは設定した数まで並列に動かす（初期値 1）。作業領域は CID / グループごとに分かれているため干渉しない。再解析（cov-build / cov-analyze）は重いため、ツール側で 1 つずつ順番に実行する。

```
利用者: run（条件ファイル＋チャットでの上書き）
  │
親エージェント
  ├─ start_run ………………… CID 検索 → 上限で切る → グループ候補 → 進捗ファイル
  └─ 繰り返し: next_work_item
        │
        └─ サブエージェント（CID / グループごとに独立したコンテキスト）
              ├─ get_issue_detail
              ├─ prepare_workspaces（調査用＝解析リビジョン / 修正用＝最新）
              ├─ ソースを読んで調査（Skill: triage-investigation, checker-knowledge）
              ├─ 修正案を作成 → save_fix（Skill: code-fix）
              ├─ 逸脱コメント案を作成（Skill: deviation-comment）
              ├─ [オプション] verify_fix
              └─ submit_result（判断結果を構造化データで提出）
  └─ build_summary → 利用者に一覧サマリの場所を報告

利用者: 一覧サマリの承認列を確認・修正 → apply
  └─ preview_apply → 件数確認（D-63）→ apply_approvals
```

## 5. 配布（確定）

- 社内の GitHub リポジトリをプラグインのマーケットプレイスとして登録し、VS Code / Copilot CLI からインストール・更新する。

## 6. 実装前に確認する事項（一次資料での確認が必要）

- `plugin.json` の必須フィールドと `$schema` の正確な URL（`https://agent-plugins.org/schemas/1.0.0/plugin.schema.json` と報じられている）
- `mcp.json` 内でプラグインのルートを参照する変数の書き方（Python サーバの起動パス指定に必要）
- `com.github.copilot/commands/` のファイル形式（VS Code と Copilot CLI の両方でコマンドとして使えるか）
- `.agent.md` のフロントマター（`model`、`tools`、`agents` など）のうち、Copilot CLI でも有効なもの（D-47 のモデル固定、D-39 のサブエージェントに関係）
- サブエージェントの起動方法が VS Code と Copilot CLI で共通に書けるか
