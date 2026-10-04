# Coverity トリアージエージェント 構成設計（たたき台）

> 本書は `docs/spec.md` の決定事項（D-xx）に基づく構成案である。
> 各項目はユーザとの対話で確認し、確定したものには「確定」と記す。「案」のままの項目（1 章の構成図の細部、3 章のツール名、4 章のフロー）は実装時に調整し得る。

## 1. プラグイン全体構成（案）

Agent Plugins 1.0 の構成に従う（D-1）。どのクライアントでも共通に使える部品（Skill・MCP）はプラグイン直下に置き、Copilot 固有の部品（エージェント・コマンド）は `com.github.copilot/` に置く。

```
coverity-triage/                         … プラグインのルート
├─ plugin.json                           … マニフェスト（$schema: agent-plugins.org 1.0.0）
├─ mcp.json                              … MCP サーバ起動設定（Python 製 MCP サーバ 1 つ・確定）
├─ skills/                               … 共通に使える知識・手順（トリアージ用 5 つ・確定、案内役 1 つ）
│  ├─ triage-investigation/SKILL.md      … 調査手順：警告経路の追跡、真偽判定、確信度基準（D-64）
│  ├─ checker-knowledge/                 … チェッカー別の判断観点（D-6）
│  │  ├─ SKILL.md
│  │  └─ references/ standard.md, misra.md, cert.md
│  ├─ code-fix/SKILL.md                  … 修正方針（D-37, D-38）、文字コード保持（D-36）、社内規約（U-3）
│  ├─ deviation-comment/SKILL.md         … 逸脱コメント（D-22〜D-24）、アノテーション（D-59）
│  ├─ triage-report/SKILL.md             … レポートに書く内容の基準（D-20, D-55）
│  └─ coverity-guide/                    … 案内役の手順と知識（設定・使い方・困ったとき、D-65）
├─ com.github.copilot/                   … Copilot 固有の部品
│  ├─ agents/
│  │  ├─ coverity-triage.agent.md        … 親エージェント：進捗管理・グループ化・サブエージェント起動・集約（D-39）
│  │  ├─ coverity-triage-worker.agent.md … サブエージェント：1 CID / 1 グループの調査 → 2 案作成 → 検証
│  │  ├─ coverity-triage-apply.agent.md  … 承認の反映専用（apply_approvals を使えるのはこのエージェントだけ）
│  │  └─ coverity-guide.agent.md         … 案内役（準備と質問、D-66）
│  └─ commands/                          … 利用者向けの 4 つのコマンド（D-69）
│     ├─ coverity-setup   … 準備（案内役）
│     ├─ coverity-run     … トリアージ実行（未完了なら再開を提案）
│     ├─ coverity-apply   … 承認の反映
│     └─ coverity-help    … 質問・設定変更・効果測定（案内役）
└─ server/                               … Python 製 MCP サーバ（D-40, D-41, D-42）
   ├─ pyproject.toml                     … Python 3.12 以上（D-46）
   └─ coverity_triage/
      ├─ mcp_server.py                   … ツールの公開窓口
      ├─ config.py                       … 設定ファイル・条件ファイルの読み込みと検証（D-49, D-50）
      ├─ coverity/                       … Coverity Connect 接続（REST：検索・書き戻し／SOAP：警告経路・スナップショット、D-77）
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
├─ config.yaml        … プロジェクト設定（/coverity-setup で生成）
├─ filters/*.yaml     … 絞り込み条件（複数用意可能）
├─ no-grouping.yaml   … グループ化しない CID（却下されたグループから自動追記）
└─ knowledge.md       … プロジェクトの知識（調査の前に AI が読む。反映後に人が選んだ知識を追記。推奨の方針（D-48）もここに書く、D-78）
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
| 準備 | `detect_project` / `write_project_config` / `doctor` / `list_runs` | 自動判定、確認済みの値で設定を書き出し、準備状況の診断、最近の実行の一覧（再開・反映の対象を決める） | D-66〜D-70 |
| 実行管理 | `start_run` | 実行フォルダ作成、条件で CID を検索、上限件数で切り、グループ候補を作成、進捗ファイル作成 | D-11〜D-15, D-43, D-53 |
| | `next_work_item` / `get_run_status` | 未処理・エラーの CID / グループを返す | D-15, D-51 |
| | `resume_run` | 実行フォルダを指定して再開 | D-61 |
| Coverity | `get_issue_detail` | 基本情報＋イベント（警告経路）＋チェッカー説明・CWE | D-33 |
| VCS | `prepare_workspaces` | 解析リビジョンを特定し（D-17）、調査用（解析リビジョン）と修正用（最新）の作業領域を作る | D-16, D-17, D-57, D-58 |
| | `save_fix` | 修正用作業領域の変更から、差分ファイル・別領域出力・git コミット / ブランチを作る | D-4, D-25, D-26 |
| ソース操作 | `read_source` / `search_source` / `edit_source` | その CID / グループの作業領域に範囲を限定した読み取り・検索・編集。文字コード・改行コードを保持して保存する（確定） | D-36, D-57 |
| 検証 | `verify_run`（全件の調査後に 1 回、D-72） / `trial_build` / `write_verify_config` | 設定に従いビルド / 再解析を実行し、警告の消滅・新規警告を返す | D-10, D-42 |
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
              └─ submit_result（判断結果を構造化データで提出）
  ├─ [オプション] verify_run …… 全修正案をまとめて 1 回ビルド（＋再解析）、問題があれば確信度を「低」に（D-72〜D-74）
  └─ build_summary → 利用者に一覧サマリの場所を報告

利用者: 一覧サマリの承認列を確認・修正 → apply
  └─ preview_apply → 件数確認（D-63）→ apply_approvals
```

## 5. 設定ファイルの書式

### 5.1 絞り込み条件ファイル（確定）

`.coverity-triage/filters/*.yaml`。同じ項目内の複数値は OR、項目同士は AND。省略した項目は条件にしない。

```yaml
# .coverity-triage/filters/untriaged-high.yaml
name: 未トリアージの High Impact
project: MyProduct
streams:
  - MyProduct-main
checkers:            # 省略時は全チェッカー
  - NULL_RETURNS
  - "MISRA C-2012 *"  # ワイルドカード可
impacts: [High, Medium]
triage:
  classification: [Unclassified]
  action: [Undecided]
  status: [New, Triaged]
max_items: 50        # 上限件数（省略時は設定ファイルの値）
revision: ""         # 解析リビジョンの手動指定（D-17 の (2)。通常は空）
```

### 5.2 プロジェクト設定ファイル（確定）

`.coverity-triage/config.yaml`。認証情報は値を書かず、環境変数名だけを書く（D-34）。

```yaml
coverity:
  url: https://coverity.example.co.jp:8443
  api: auto            # auto：Coverity Connect に接続（D-77）／ fake：偽データ（試用・テスト用、I-3）
  fake_data: ""        # api: fake のときの偽データのファイル（I-3）
  user_env: COV_USER
  key_env: COV_AUTH_KEY
  revision_field: sourceVersion   # 解析リビジョンの記録項目（D-17。SOAP のスナップショット情報の項目名）
  triage_store: Default Triage Store   # 書き戻し先のトリアージストア
  ca_file: ""         # サーバの CA 証明書。空なら OS の証明書ストアを使う
  path_strip_prefixes: []   # Coverity のファイルパスから取り除く接頭辞（I-9）
vcs:
  type: git              # git / svn
  base_branch: main      # 修正の起点（D-58）
  branch_mode: per_cid   # per_cid / per_run（D-25）
  branch_prefix: coverity-fix/
  github_token_env: GITHUB_TOKEN
output_dir: D:/coverity-triage-out
max_items: 100
parallel: 1
deviation_target: coverity   # coverity / coverity+annotation（D-59）
ascii_file_encoding: utf-8   # 英数字だけのファイルに日本語を追加するときの文字コード（I-10）
verify:
  default: none          # none / build / build+analyze（D-10）
  setup_command: 'envset.bat "{root}" <2つ目の引数>'   # ビルド前に実行。{root} は検証用コピーのフォルダ、他の引数はそのまま（D-75）
  build_dir: 'firmware/target'   # setup_command の後に移動するディレクトリ（リポジトリからの相対パス、D-76）
  build_command: "make -f makefileXX"
  cov_build_args: "--dir idir"
  cov_analyze_args: "--dir idir --all"
model: gpt-6 luna       # 参考表示（実際の固定は D-47 の方式）
```

### 5.3 CID（グループ）ごとのレポートの章立て（確定）

結論を先に置き、確信度の高いものは冒頭だけ読めば判断できる構成とする。

```markdown
# CID 12345 — NULL_RETURNS（High）

## 1. 結論
- 推奨: 逸脱 ／ 確信度: 高
- 見立て: 誤検知（呼び出し元で NULL チェック済み）
- 注意: リビジョンのずれなし ／ 制約超過なし

## 2. 警告の概要
ファイル・関数・チェッカー説明・CWE・グループの CID 一覧

## 3. 真偽の根拠
警告経路（イベント）とコード上の追跡結果

## 4. 案A: 逸脱
- Classification / Action / Severity 案
- 逸脱コメント案（← ここを手直し可）
- アノテーション差分（設定時）

## 5. 案B: 修正
- 差分（ブランチ名 / .patch へのリンク）
- 影響範囲とリスク、超えた制約
- Classification / Action / Severity 案

## 6. 自動検証の結果（実施時）

## 7. 処理情報
解析 / 修正の起点リビジョン、処理時間、エラー
```

### 5.4 一覧サマリの書式（確定）

`<実行フォルダ>/summary.md`。承認列には「修正 / 逸脱 / 却下」のいずれかを書く（推奨案を下書き済み、D-62）。並びは確信度の低い順（D-54）。

```markdown
# トリアージ結果 2026-10-02 15:30
条件: untriaged-high.yaml ／ 対象 42 件（グループ 5）／ エラー 1 件

| 承認 | CID | 推奨 | 確信度 | チェッカー | Impact | 場所 | 見立て | グループ | 検証 | ずれ | 詳細 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 修正 | 12001 | 修正 | 低 | OVERRUN | High | buf.c / copy() | 配列境界を超え得る | - | 成功 | なし | [→](cid/12001.md) |
| 逸脱 | 12345 | 逸脱 | 高 | NULL_RETURNS | High | io.c / read_all() | 呼び出し元で NULL チェック済み | G1 | - | なし | [→](cid/12345.md) |

## エラー
| CID | 内容 |
|---|---|
| 12999 | ソースが見つからない |
```

- グループの扱い（確定）：グループは 1 行にまとめ（CID 列に「G1（20 件）」のように表示）、承認も 1 つとする。CID 一覧は詳細レポートに載せる。一部だけ別扱いにしたい場合は「却下」とし、次回の実行で個別に処理する。
- 却下されたグループ（確定）：その CID を `.coverity-triage/no-grouping.yaml` に記録し（コミットしてチームで共有）、次回以降はグループ化せず個別に処理する。

### 5.5 サブエージェントの提出データ（`submit_result`・確定）

レポート・一覧サマリ・効果測定の元データ。必須項目が欠けている場合、ツールは受け付けず再提出を求める。
検証結果・差分の場所・処理時間などはツール側が記録しているため、AI は提出しない。

```yaml
work_item: 12345 または G1
verdict:            # 真偽の見立て
  judgement: false_positive | true_bug | intentional | undetermined
  summary: "呼び出し元で NULL チェック済み"   # 1行要約
  rationale: "..."          # 根拠（経路の追跡結果）
  evidence: [{file, line, note}]
recommendation: fix | deviation
confidence: high | medium | low
confidence_reason: "..."
deviation:
  classification / action / severity
  comment: "誤検知。..."
fix:
  summary: "..."
  impact: "..."            # 影響範囲とリスク
  exceeded_constraints: []  # 超えた制約（D-38）
  already_fixed_on_latest: false  # D-58
  classification / action / severity
revision_drift: none | detected (内容)
group_excluded_cids: []     # 原因が異なり個別処理に戻す CID（D-53）
```

### 5.6 アノテーションの書式（D-59 (b) 選択時・確定）

警告行の直前の行に、ブロックコメントで埋め込む（C90 でも使え、C / C++ 共通で安全）。理由の文章は逸脱コメント（D-22〜D-24）と同じものを使う。

```c
/* coverity[misra_c_2012_rule_10_4_violation:FALSE] 誤検知。... */
/* coverity[misra_c_2012_rule_15_5_violation] 意図的。... */
```

- `[...]` の中は main イベントのタグ。誤検知は `:FALSE` を付ける。付けない注釈を Coverity は「意図的（Intentional）」として扱う（Black Duck コミュニティ「Suppressing False Positive/Intentional defects」）。
- Coverity が認識する正確な書式（タグ名、理由の書き方）は、社内の Coverity バージョンで実装前に確認する（7 章）。

### 5.7 ログ（確定）

実行フォルダにツール操作ログを残す。内容は MCP ツールの呼び出し、API 通信の概要、VCS 操作、ビルド・解析の出力、反映で外部に加えた変更（書き戻し内容・PR URL）。認証情報は伏せて記録する。障害調査と監査に使う。

## 6. 配布（確定）

- 社内の GitHub リポジトリをプラグインのマーケットプレイスとして登録し、VS Code / Copilot CLI からインストール・更新する。
- MCP サーバの Python 環境は uv で自動構築する（`mcp.json` で uv 経由で起動）。利用者は uv を入れるだけでよい。

## 7. 実装前に確認する事項（一次資料での確認が必要）

- `plugin.json` の必須フィールドと `$schema` の正確な URL（`https://agent-plugins.org/schemas/1.0.0/plugin.schema.json` と報じられている）
- `mcp.json` 内でプラグインのルートを参照する変数の書き方（Python サーバの起動パス指定に必要）
- `com.github.copilot/commands/` のファイル形式（VS Code と Copilot CLI の両方でコマンドとして使えるか）
- `.agent.md` のフロントマター（`model`、`tools`、`agents` など）のうち、Copilot CLI でも有効なもの（D-47 のモデル固定、D-39 のサブエージェントに関係）
- サブエージェントの起動方法が VS Code と Copilot CLI で共通に書けるか
- アノテーション（5.6）の正確な書式が社内の Coverity バージョンで認識されるか

## 8. 実装の状況（段階 2 完了時点）

### 8.1 リポジトリ内の配置

```
plugins/coverity-triage/      … プラグイン本体（1 章の構成）
  └─ server/                  … MCP サーバ（Python 3.12、uv）。tests/ に自動テスト
examples/sample-target/       … 試用用の対象リポジトリ（偽の Coverity データ付き）
tools/coverity_api_probe.py   … API 調査スクリプト（読み取りのみ、U-1・U-2 用）
docs/trial-guide.md           … 試用・確認の手順書
```

### 8.2 実装済み・未実装

| 部品 | 状態 |
|---|---|
| 設定・条件ファイル、進捗・再開、グループ化、文字コード保持、git / svn 操作、検証、レポート・サマリ、承認の反映、効果測定、MCP サーバ（25 ツール） | 実装済み・自動テスト済み（git / svn の実リポジトリで確認） |
| エージェント 3 つ、Skill 5 つ、コマンド 5 つ | 初版を作成。動作は未確認（試用手順書 A で確認） |
| Coverity Connect への接続（REST / SOAP、D-77） | 実装済み・偽サーバでの自動テスト済み（`server/src/coverity_triage/connect.py`）。社内サーバでの動作は試用手順書 B で確認。偽データで動く `coverity.api: fake` も残す |

### 8.3 実装時の判断（要確認）

実装中に決めた細部です。すべてユーザの確認を経て確定した。

| No | 内容 | 理由 |
|---|---|---|
| I-1（確定） | 作業領域は、git の worktree / svn の checkout ではなく、**エクスポート（`git archive` / `svn export`）したスナップショット＋変更を重ねる層**で実現した。git のコミットとブランチは一時インデックスで作る（D-57 の実現方法の変更） | 利用者のリポジトリの作業ツリー・`.git` の作業領域情報に一切触れない。並列処理で作業領域が干渉しない。svn で CID ごとに checkout するより大幅に軽い |
| I-2（確定） | `branch_mode: per_run` のとき、実行ごとのブランチは**承認の反映時に、承認された修正だけを集めて**作る（トリアージ中は CID ごとのコミットを非公開の参照に保存） | 却下された修正をあとから取り除く手間（revert）が不要になる |
| I-3（確定） | 設定に `coverity.api: fake` と `coverity.fake_data`（偽データのファイル）を追加した | 社内 Coverity に接続せずに試用・自動テストするため |
| I-4（確定） | ブランチ名：単一 CID は `<prefix>cid-<CID>`、グループは `<prefix><実行ID>-G<n>`、アノテーションは末尾に `-annotation`。同名のブランチがあれば末尾に `-<実行ID>` | CID 番号で探しやすくするため |
| I-5（確定） | 差分ファイル名：`patches/<作業項目>-fix.patch`、`patches/<作業項目>-annotation.patch`。修正後ファイルは `fixed/<作業項目>/<fix または annotation>/` | 修正案とアノテーション案を区別するため |
| I-6（確定、出力形式は要確認） | 再解析による検証では、実行ごとに 1 回、修正前の最新コードも解析し（ベースライン）、新規の警告を判定する。結果の読み取りは `cov-format-errors --json-output-v7` を使う | 修正で新たに出た警告と、元からある警告を区別するため。出力形式は社内のバージョンで要確認 |
| I-7（確定） | グループを「逸脱」で承認した場合、グループ内のすべての CID に同じ属性・逸脱コメントを書き戻す | グループは同一原因で 1 つの案を出す仕様（D-52）のため |
| I-8（確定） | 承認の反映の同意確認は、`preview_apply` が返す確認用の文字列を `apply_approvals` に渡す方式。確認後にサマリやレポートが変更されたら反映を拒否する | 確認した内容と実際に反映する内容が食い違うことを防ぐため（D-63） |
| I-9（確定） | Coverity が返すファイルパス（ビルド環境の絶対パスの場合など）は、設定 `coverity.path_strip_prefixes` の接頭辞を取り除いてリポジトリ内のパスに対応づける。設定で対応づけられない場合は、リポジトリに実在する最も長い末尾部分で自動的に対応づけ、その旨をレポートに明記する。候補が複数あり決められない場合は対応づけず、その旨を明記する | 解析環境とリポジトリでパスが違っても調査を止めないため（実装の見直しで判明、ユーザ確認済み） |
| I-10（確定） | 英数字だけのファイル（UTF-8 か Shift_JIS か判別できない）に日本語などを追加する場合は、設定 `ascii_file_encoding`（`utf-8` / `cp932`、既定 `utf-8`）の文字コードで保存する | Shift_JIS のプロジェクトで文字コードが混在しないようにするため（実装の見直しで判明、ユーザ確認済み） |
| I-11（確定） | Coverity Connect への接続の細部：(1) 条件のうちワイルドカードを含むチェッカー名と Status は、検索後に手元で絞り込む（サーバ側の絞り込み方が公開例で確認できないため）(2) 証明書は OS の証明書ストアを使い（社内 CA を入れた会社の PC でそのまま動くように）、`coverity.ca_file` で個別に指定もできる (3) `revision_field` の既定を、SOAP のスナップショット情報に実在する項目名 `sourceVersion` に変えた（旧既定 `version` は該当する項目が無かった。リビジョンを記録する運用は今後の拡張 E-2） (4) 書き戻し先のトリアージストアを `coverity.triage_store`（既定 `Default Triage Store`）で指定する | 推測で書かず、公開されている形だけで動くようにするため |

## 9. 使いやすさの改善（D-65〜D-70）

| 変更 | 内容 |
|---|---|
| 資料 | 人が読むのは README だけ（5 分以内）。設定項目・使い方・困ったときの対処は Skill `coverity-guide`（references/settings.md・usage.md・troubleshooting.md）に移し、利用者は `/coverity-help` で AI に聞く。導入手順書・利用手順書は廃止 |
| 案内役エージェント | `coverity-guide`：`/coverity-setup`（準備）と `/coverity-help`（質問）を担当。ターミナル実行（毎回利用者が確認）とファイル編集ができる |
| 準備の流れ | 段階 0：uv（無ければ AI がインストール → MCP サーバを再起動。Python とライブラリは uv が自動で用意）→ 段階 1：`doctor` で診断 → 段階 2：設定が無ければ URL・プロジェクト・ストリームだけ聞き、残りは `detect_project` で自動判定して確認 → `write_project_config` → 段階 3：認証情報を伏せ字で入力（値はチャットに出ない）→ 段階 4：`doctor` で確認 |
| 環境変数 | Windows では利用者の環境変数をレジストリから直接読むため、準備中に保存した値が VS Code の再起動なしで有効になる（`envvars.get_env`） |
| コマンド | `/coverity-setup`・`/coverity-run`（未完了なら再開を提案）・`/coverity-apply`（省略時は最新の実行）・`/coverity-help` の 4 つ |

## 10. 自動検証のまとめ実行（D-71〜D-75）

| 項目 | 内容 |
|---|---|
| タイミング | 全件の調査が終わった後、親エージェントが `verify_run` を 1 回呼ぶ。サブエージェントは検証しない |
| 修正案の合成 | 最新コードのコピーに、各修正案の差分ファイルを順に適用する（git は `-p1`、svn は `-p0`）。同じ箇所を変更していて適用できない修正案は「適用不可」とし、まとめた検証から外す |
| 実行するコマンド | 1 つのシェルで `setup_command`（`{root}` をコピー先に置換、Windows では `call`）→ `build_dir` へ絶対パスで移動（Windows では `cd /d`、D-76）→ ビルドのみ：`build_command`／再解析：`cov-build <引数> <build_command>` → `cov-analyze` → `cov-format-errors --json-output-v7` |
| 比較の基準 | 再解析の場合は、修正前の最新コードも 1 回ビルド・解析する（1 回の実行で解析 2 回分） |
| 結果の割り当て | CID ごとに警告が消えたか（mergeKey、無ければチェッカー・ファイル・関数で照合）。新しい警告は、そのファイルを変更した修正案へ。ビルドエラーは、ログのエラー行に変更ファイル名が出ている修正案へ（特定できなければ全修正案、他の修正案が原因なら「未確認」） |
| 確信度 | 問題が出た修正案は確信度を「低」に下げ、理由に追記する（元の値は `confidence_before_verify` に保存。再実行しても二重に追記しない）。推奨案は変えない |
| 設定 | `/coverity-setup` の段階 5（任意）で聞き、`trial_build` で修正前のコードを試しにビルドしてから `write_verify_config` で保存する |
