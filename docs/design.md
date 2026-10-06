# Coverity トリアージエージェント 設計書

この文書は「どう作るか」を定めます。「何を作るか」は [spec.md](spec.md) にあります。
1 章で全体の構成、2 章で処理の流れ、3 章で部品ごとの中身、4 章で設計の根拠にした公式仕様を書きます。

## 1. 全体の構成

### 1.1 部品と役割

公式の役割分担（VS Code「Customization concepts」、GitHub「Copilot customization cheat sheet」）に合わせて、部品を次のように分けます。

| 部品 | 公式の役割 | このプラグインでの担当 |
|---|---|---|
| スキル（入口） | 繰り返す手順をまとめる | `/coverity-setup`・`/coverity-run`・`/coverity-apply`・`/coverity-help`・`/coverity-selftest` の手順。エージェントはこの手順に沿って進める |
| スキル（知識） | 必要なときに読む知識 | 調査の手順と確信度の基準、チェッカー別の観点、修正の制約、逸脱コメントの書き方、結果の書き方 |
| スキルに同梱するスクリプト | 決まった処理 | 実行フォルダ、グループ化、修正用のコピー、差分とブランチ、レポートと一覧、承認の読み取り、反映、検証、準備の確認、動作確認。スキル `coverity-triage-scripts` の `scripts/ct.py` にまとめる |
| MCP サーバ | 外部システムとの接続 | Coverity Connect との通信だけ（接続の確認、警告の検索、警告の詳細、トリアージの書き戻し） |
| 組み込みツール | ファイルの読み書き・検索・ターミナル | ソースを読む・探す・直す、スクリプトを動かす |
| カスタムエージェント（サブエージェント） | 役割と使えるツールを決める | `coverity-triage-worker`：警告 1 件（または 1 グループ）を調べて 2 つの案を書く。使えるのは読み取り・検索・編集・スキルの読み込みだけ（ターミナルは使えない） |
| プラグイン | まとめて配布する | 上の部品を Agent Plugins 1.0 の形でまとめる |

### 1.2 誰が何をするか

| 担当 | すること | しないこと |
|---|---|---|
| 人 | 準備、条件の指定、一覧の承認欄の記入、反映の同意 | — |
| エージェント（チャットの AI） | 入口のスキルの手順に沿って、スクリプトと MCP のツールを順に呼び、サブエージェントに調査を任せ、結果を人に伝える | 自分でソースを調べて判断すること（サブエージェントの仕事） |
| サブエージェント | 警告の真偽の判断、修正（修正用のコピーの上で）、逸脱コメントの作成、結果ファイルの作成 | ターミナル、Coverity への書き込み |
| スクリプト | 決まった処理。毎回同じ結果になる | 判断や文章作成 |
| MCP サーバ | Coverity との通信 | ファイルの加工、Git の操作 |

### 1.3 ファイルの配置

```
plugins/coverity-triage/                       … プラグイン（Agent Plugins 1.0）
├─ plugin.json
├─ mcp.json                                    … MCP サーバ coverity-triage の起動設定
├─ server/                                     … MCP サーバ（Python。uv のプロジェクト）
│  └─ src/coverity_triage/                     … Coverity Connect との通信だけ
├─ skills/
│  ├─ coverity-setup/   coverity-run/   coverity-apply/   coverity-help/   coverity-selftest/   … 入口
│  ├─ coverity-triage-scripts/                 … 決まった処理のスクリプト（エージェントが読む）
│  │  ├─ SKILL.md                              … コマンドの使い方
│  │  └─ scripts/ct.py, scripts/ctlib/          … uv run で動く Python
│  └─ triage-investigation/ checker-knowledge/ code-fix/ deviation-comment/ triage-report/   … 知識
└─ com.github.copilot/agents/coverity-triage-worker.agent.md   … サブエージェント

<対象リポジトリ>/.coverity-triage/              … チームで共有（コミットする）
├─ config.yaml   filters/*.yaml   knowledge.md   no-grouping.yaml

<出力先>（既定 ../coverity-triage-out）/<実行 ID>/   … リポジトリの外
├─ run.json                … 実行の設定と進み具合（作業ごとの状態）
├─ filter.json             … この実行で使う条件（MCP の search_issues が読む）
├─ issues.json             … Coverity から取った警告の一覧（MCP が書く。問い合わせごとの秒数も残す）
├─ issues.mapped.json      … パスをリポジトリからの相対パスに直した一覧（ct.py plan が書く）
├─ config.snapshot.yaml    … 実行を始めたときの設定の写し
├─ items/<ID>/             … 作業ごと：issue-<CID>.json（警告経路。MCP が書く）、brief.md（サブエージェントへの指示）、
│                             result.json（サブエージェントの結果）、saved.json（差分とブランチ）
├─ work/                   … 修正用のコピー：fix-<n>、annotation-<n>（オプション）、analyzed（解析リビジョン）。ct.py summary で消す
├─ cid/<ID>.md             … 詳細レポート
├─ patches/<ID>-fix.patch  … 差分（git / svn が出したバイト列のまま）
├─ fixed/<ID>/fix/         … 修正後のファイル（リポジトリの構成のまま）
├─ summary.md              … 一覧（承認欄つき）
├─ verify.json             … （オプション）ビルドでの検証の結果
├─ apply-plan.json         … 反映の内容。apply-plan.result.json は MCP の update_triage の結果
├─ metrics.json            … （オプション）効果測定の記録
└─ operations.log          … 操作の記録（認証情報は伏せる）
```

## 2. 処理の流れ

以下の「エージェント」はチャットの AI、「`ct.py X`」はスクリプトのコマンド、「MCP `X`」は MCP のツールです。

### 2.1 準備（`/coverity-setup`）

| 順 | 担当 | すること |
|---|---|---|
| 1 | エージェント | 「すべて許可」にする手順を案内する（決定 2） |
| 2 | エージェント | ターミナルで `uv --version`。無ければ uv をインストールし、この会話の間は uv をフルパスで呼ぶ |
| 3 | `ct.py prewarm` | MCP サーバ用の Python 環境を作る（`uv sync`）。初回起動で時間切れにならないよう、ここで済ませる |
| 4 | `ct.py doctor` | 足りないものを調べる（リポジトリ、git / svn、設定、条件、出力先、環境変数、サブエージェントの配置） |
| 5 | `ct.py install-agent` | サブエージェントの定義をユーザのフォルダ `~/.copilot/agents` にコピーする（決定 1。VS Code の Copilot でサブエージェントが見つからなかった事象への対処） |
| 6 | エージェント・人 | 設定が無ければ（チームで最初の人だけ）、URL・プロジェクト・ストリームを 1 つずつ聞き、`ct.py detect` の結果と合わせて表で見せ、同意を得て `ct.py init-config` |
| 7 | 人 | 認証情報（ユーザ名・認証キー）を、ターミナルの伏せ字欄で環境変数に入れる |
| 8 | 人 | uv を入れたか、サブエージェントの定義を置いた（`restart_needed`）ときだけ、VS Code（または Copilot CLI）を再起動し、もう一度 `/coverity-setup` を入力する（再起動はこの 1 回だけ。uv を新しく入れると、起動済みの VS Code からは uv が見えず MCP サーバを起動できないため） |
| 9 | MCP `check_connection`・`ct.py doctor` | Coverity に認証できること、すべてそろったことを確かめる |
| 10 | エージェント | トリアージ中にこのプラグインのツールを確認なしで使う設定を案内する（決定 4） |
| 11 | （オプション） | ビルドでの検証を使う場合だけ、`ct.py trial-build` と `ct.py set-verify` |

### 2.2 実行（`/coverity-run`）

| 順 | 担当 | すること |
|---|---|---|
| 1 | `ct.py runs` | 未完了の実行があれば、再開するか人に聞く（再開は `ct.py resume`） |
| 2 | `ct.py new-run` | 実行フォルダを作り、条件を確定して `filter.json` に書く。修正用のコピーを置くフォルダを返す |
| 3 | エージェント・人 | 修正用のコピーを置くフォルダへのアクセスを 1 回許可してもらう（決定 21） |
| 4 | MCP `search_issues` | Coverity で警告を検索し、`issues.json` に保存する（上限件数で止める） |
| 5 | `ct.py plan` | パスの対応づけ、グループ化（1 グループ 10 件まで）、解析リビジョンと取り込み先の最新リビジョンの特定をして、作業の一覧を作る |
| 6 | 繰り返し | 下の 6-1〜6-5 を、作業が無くなるまで（設定 `parallel` が 2 以上なら、その数まで同時に） |
| 6-1 | `ct.py next` | 次の作業（警告 1 件か 1 グループ）を取り、修正用のコピーを用意して最新のリビジョンにそろえる（初回はコピーを作る）。利用者のリポジトリの状態も記録する |
| 6-2 | MCP `get_issues` | 警告経路を Coverity から取り、`items/<ID>/` に保存する |
| 6-3 | `ct.py brief` | パスの対応づけ・ずれの確認をして、サブエージェントへの指示 `brief.md` を作る |
| 6-4 | サブエージェント | `brief.md` を読み、知識のスキルに沿って調べ、修正用のコピーを直し、`result.json` を書く |
| 6-5 | `ct.py finish` | `result.json` を確かめ、差分・ブランチ・修正後のファイル・詳細レポートを作る。不備があれば `problems` を返す（エージェントがサブエージェントに直させる。2 回直らなければ `ct.py fail`）。調べるコードや利用者のリポジトリが変わっていたら `warnings` で知らせる |
| 7 | （オプション）`ct.py verify` | ビルドでの検証。時間がかかるので、ターミナルで裏で動かす |
| 8 | `ct.py summary` | 一覧 `summary.md` を作り、全部の作業が終わっていれば修正用のコピーを消す（ブランチと差分は残る） |
| 9 | エージェント | 一覧の場所と件数を伝え、承認欄を確認してから `/coverity-apply` を使うよう案内する |

### 2.3 反映（`/coverity-apply`）

| 順 | 担当 | すること |
|---|---|---|
| 1 | `ct.py preview` | 一覧の承認欄と詳細レポートの逸脱の節を読み、件数と反映内容 `apply-plan.json`、確認用の文字列を作る |
| 2 | エージェント・人 | 件数を見せて同意を得る |
| 3 | MCP `update_triage` | 逸脱を Coverity に書き戻す。確認用の文字列と `apply-plan.json` の中身が一致しないと書き込まない。結果は `apply-plan.result.json` に残す |
| 4 | `ct.py apply-code` | 確認用の文字列を確かめてから、修正を反映する（git：ブランチを push。svn：ワーキングコピーに差分を適用）。却下したグループは `no-grouping.yaml` に足す。プルリクエストの題名・本文と、作成用の URL を返す。`update_triage` の結果も読み、反映済みの部分を記録する（次の preview で繰り返さない） |
| 5 | エージェント | プルリクエストを Copilot の GitHub の機能で作る。使えないときは、作成用の URL を人に示す（決定 3） |
| 6 | （オプション）`ct.py knowledge-candidates` / `add-knowledge` | 知識の追記の提案 |

### 2.4 動作確認（`/coverity-selftest`）

| 範囲 | 確かめること | 主な担当 |
|---|---|---|
| ① 組み込み | MCP のツールが見えること、サブエージェントを起動でき、使えるツールが制限され、スキルを読めること、`/` メニュー | エージェント（観察）＋ `ct.py selftest` |
| ② 偽データ | 偽の Coverity（`coverity.api: fake`）と見本のリポジトリで、2.2 と 2.3 の流れを最後まで通す | 2.2・2.3 と同じ |
| ③ 社内 Coverity | 接続と認証、検索、警告経路の取得（読み取りだけ） | MCP ＋ `ct.py selftest` |
| ④ ビルドでの検証 | オプションを使う場合だけ | `ct.py` |

結果は `~/coverity-triage-selftest/<日時>/report.md` にまとめ、ホスト名・ユーザ名・認証情報は伏せる。

## 3. 部品ごとの中身

### 3.1 plugin.json・mcp.json

- `plugin.json`：Agent Plugins 1.0 の必須項目（`$schema`、`name`）と説明・版数。
- `mcp.json`：

  ```json
  {
    "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
    "mcpServers": {
      "coverity-triage": {
        "type": "stdio",
        "command": "uv",
        "args": ["run", "--native-tls", "--frozen", "--quiet", "--directory", "${PLUGIN_ROOT}/server", "coverity-triage-mcp"]
      }
    }
  }
  ```

  `${PLUGIN_DATA}` は使わない。VS Code（Local）は `${PLUGIN_DATA}` を置き換えないため（4 章）。Python の環境は uv の既定どおり `server/.venv` にでき、準備の手順 3 で作っておく。

### 3.2 MCP サーバ `coverity-triage`

- どのツールも 20 秒以内に返す（Copilot の実行基盤はツール 1 回を既定 30 秒で打ち切るため、4 章）。次の問い合わせが 20 秒を超えそうなら、そこで止めて `done: false` を返す。エージェントは `done` が `true` になるまで同じ引数で呼び直す。保存済みの分は繰り返さない。
- 結果は `ct.py` が示したファイルに保存し、ツールは件数などの要約だけを返す（大きなデータを会話に流さない）。
- stdio では `initialize` で始まる版（2025-11-25 まで）だけで応える（SDK の `serve_loop`）。SDK の既定（`MCPServer.run`）は最初の要求で版を決めるため、Copilot が先に `server/discover`（2026-07-28）で問い合わせ、そのあと同じ接続で `initialize` に切り替えると -32022 で断ってしまう（初回の起動が遅いときに起きた）。
- パスは絶対パスだけ受け付ける（サーバは `uv run --directory` で自分のフォルダで動くため）。保存先のフォルダが無ければエラーにする（`ct.py` が作る）。
- ツールの注釈（MCP の `annotations`）：`check_connection` は読み取りだけ（`readOnlyHint`）。書き戻しは `destructiveHint`。どれも社内の Coverity だけを相手にするので `openWorldHint: false`。

| ツール | 入力 | すること | 返すもの |
|---|---|---|---|
| `check_connection` | `repo_root` | 設定の Coverity に、環境変数の認証情報で 1 回だけ読み取りの問い合わせをする（10 秒以内） | 認証できたか、かかった秒数、URL が http のときの注意 |
| `search_issues` | `repo_root`、`filter_file`（`ct.py new-run` が作る `filter.json`）、`output_file`（`issues.json`） | REST で検索し、条件の `limit` 件で止めて保存する。続けて最新スナップショットの解析リビジョンを記録する。問い合わせごとの時間も残す | 見つかった件数、上限で止めたか、解析リビジョン、`done` |
| `get_issues` | `repo_root`、`stream`、`cids`（50 件まで）、`output_dir`（`items/<ID>/`） | SOAP で警告経路とチェッカーの説明を取り、`issue-<CID>.json` に保存する。取れなかった CID は理由を `error` に書いて保存する | 保存した CID、取れなかった CID、残り、`done` |
| `update_triage` | `repo_root`、`plan_file`（`apply-plan.json`）、`confirmation_token` | `apply-plan.json` の `triage` の各項目を REST で書き込む。確認用の文字列（ファイルの SHA-256 の先頭 12 文字）が合わなければ何も書かない。結果は `apply-plan.result.json` に残す。書き込み済みの項目（内容が同じもの）は書き直さない | 書き込んだ項目、失敗した項目と理由、残り、`done` |

`apply-plan.json` の `triage` の 1 項目：`{"id": "C002", "cids": [20002], "classification": "False Positive", "action": "Ignore", "severity": "Unspecified", "comment": "..."}`。

`coverity.api: fake` のときは、偽データのファイルを読み、書き戻しは `<偽データ>.writes.jsonl` に記録する（動作確認と自動テスト用）。

### 3.3 スクリプト（スキル `coverity-triage-scripts` の `scripts/ct.py`）

エージェントは `uv run --native-tls <このスキルのフォルダ>/scripts/ct.py <コマンド>` で動かす。結果は JSON で標準出力に出す。依存するライブラリはスクリプトの先頭に書く（PEP 723）。

| 区分 | コマンド | すること |
|---|---|---|
| 準備 | `prewarm` | MCP サーバの Python 環境を作る |
| | `doctor` | 準備の状況を調べる（Coverity への接続は MCP `check_connection`） |
| | `detect` / `init-config` | 自動で決められる値を調べる／設定・条件・知識のひな形を書く |
| | `install-agent` | サブエージェントの定義を `~/.copilot/agents` にコピーする |
| | `trial-build` / `set-verify` | （オプション）試しのビルド／検証の設定を書く |
| 実行 | `runs` / `resume` / `status` | 実行の一覧／再開（処理中・エラーの作業を未処理に戻す）／進み具合 |
| | `new-run` / `plan` | 実行フォルダと条件／作業の一覧（グループ化を含む） |
| | `next` / `brief` / `finish` / `fail` | 1 件ずつの準備・指示・仕上げ・失敗の記録 |
| | `verify` / `summary` | （オプション）ビルドでの検証／一覧 |
| 反映 | `preview` / `apply-code` | 反映の内容と確認用の文字列／修正の反映 |
| | `knowledge-candidates` / `add-knowledge` / `stats` | （オプション）知識の追記の提案／効果測定の集計 |
| 動作確認 | `selftest start` / `static` / `sample` / `check-run` / `check-apply` / `check-coverity` / `check-build` / `record` / `report` | 動作確認の準備・判定・記録・結果のまとめ |

- 引数は JSON を使わず、1 つずつの引数にする（例 `--impact High --impact Medium`）。Windows の PowerShell は、外部のコマンドに渡す JSON の `"` を崩すことがあるため。
- 結果は `{"ok": true, ...}`。できなかったときは `{"ok": false, "error": ..., "problems": [...]}` と終了コード 1。
- `run.json` の読み書きはロックファイルで順番にする（同時に進める作業があるため）。git / svn のコピーの操作も同じ。

修正用のコピー：

- git：出力先の下に worktree を作り、取り込み先の最新のコミットにそろえる（`git worktree add --detach`）。作業ごとに元に戻して使い回す（`reset --hard` と `clean -fdx`）。仕上げでコミットし、警告ごとのブランチ（`coverity-fix/cid-<CID>`、グループは `coverity-fix/<実行 ID>-G1`）を作る。オプション `per_run_branch` のときはブランチを作らず、反映のときに承認した分だけを 1 つのブランチにまとめる（`cherry-pick`）。どちらの場合も `refs/coverity-triage/<実行 ID>/<ID>-fix` でコミットを残す。実行の最後（`ct.py summary`）に worktree を消す。
- svn：出力先の下に最新のリビジョンをチェックアウトし、作業ごとに `svn revert` と、管理外のファイルの削除で戻して使い回す。差分は `svn add` のあと `svn diff` で作る。
- 解析リビジョンが分かり、リポジトリにある場合は、調査用にそのリビジョンのコピー（`analyzed`）も作る。分からない場合は、手元のコード（開いているリポジトリ）を読んで調べ、ずれを確かめる（`ct.py brief` が、ファイルの有無・行数・関数名で機械的にも確かめる）。
- 利用者のリポジトリの状態（`git status` と `git diff` の要約値）を `next` で記録し、`finish` で比べる。変わっていれば警告として知らせ、レポートの注意に書く。
- 差分のファイルは git / svn が出したバイト列のまま保存する（文字コードを変えない）。詳細レポートに載せるときだけ、表示用に読み替える。

### 3.4 スキル

| スキル | 呼ぶ人 | frontmatter の要点 |
|---|---|---|
| `coverity-setup`・`coverity-run`・`coverity-apply`・`coverity-help`・`coverity-selftest` | 人（`/`） | `disable-model-invocation: true`。`coverity-run` / `coverity-apply` には、このプラグインのツールとスクリプトの実行を確認なしにする `allowed-tools` を書く（決定 4） |
| `coverity-triage-scripts` | エージェント | `user-invocable: false`。コマンドの使い方を書く |
| `triage-investigation`・`checker-knowledge`・`code-fix`・`deviation-comment`・`triage-report` | サブエージェント | `user-invocable: false` |

入口のスキル同士は互いを読まない（`disable-model-invocation: true` のスキルは AI から読めないため）。共通の手順は `coverity-triage-scripts` と知識のスキルに置く。

### 3.5 サブエージェント `coverity-triage-worker`

```yaml
name: coverity-triage-worker
description: Coverity の警告 1 件（または 1 グループ）を調べ、修正案と逸脱コメント案を作る
model: GPT-6 Luna          # VS Code（Local）は表示名で探す
models: [gpt-6-luna]       # Copilot の実行基盤（Copilot CLI と VS Code の Copilot）は ID。model より優先される
modelPolicy: required      # 使えないときに黙って別のモデルにしない
user-invocable: false
tools: [read, search, edit, skill]
```

- ターミナルと MCP のツールは使わない。必要な情報はすべて `brief.md` で受け取り、結果は `result.json` に書く。
- `brief.md` には、警告の情報と警告経路（リポジトリ内のパスに直したもの）、調査用と修正用のフォルダ、結果ファイルの場所、知識のファイルの場所、ずれの確認結果を書く。

### 3.6 一覧と詳細レポート

- 一覧 `summary.md`：`| 承認 | ID | 推奨 | 確信度 | 見立て | 詳細 |`。確信度の低い順。承認欄に推奨案を入れておく。グループは「G1（3 件）」と 1 行で表す。
- 詳細レポート `cid/<ID>.md`：1 結論、2 警告の概要（チェッカー・Impact・場所・CWE を含む）、3 真偽の根拠（警告経路と調査結果）、4 案A 逸脱（手直しできる節。`<!-- ct:begin deviation -->` と `<!-- ct:end deviation -->` で囲む）、5 案B 修正（**差分をそのまま載せる**。400 行まで）、6 処理情報。ビルドでの検証（オプション）を使ったときは 6 に検証の結果が入り、処理情報は 7 になる。
- ビルドでの検証で問題が出た修正案は、確信度を「低」に下げる。ほかの修正案と同じ箇所を変えていて、まとめて確かめられなかっただけのものは下げない。

### 3.7 設定ファイル `config.yaml`

```yaml
coverity:
  url: https://coverity.example.co.jp:8443
  api: auto                 # auto / fake
  user_env: COV_USER
  key_env: COV_AUTH_KEY
  triage_store: Default Triage Store
  revision_field: sourceVersion
  path_strip_prefixes: []
  ca_file: ""
vcs:
  type: git                 # git / svn
  base_branch: main
  branch_prefix: coverity-fix/
output_dir: ../coverity-triage-out
max_items: 100
parallel: 1
options:                    # 既定はすべて false
  annotation: false
  per_run_branch: false
  knowledge_suggestions: false
  metrics: false
verify:                     # options の代わりに、default が none 以外ならビルドでの検証を使う
  default: none             # none / build / build+analyze
  setup_command: ""
  build_dir: ""
  build_command: ""
  cov_build_args: "--dir idir"
  cov_analyze_args: "--dir idir --all"
```

## 4. 設計の根拠にした公式仕様

| 項目 | 内容 | 出典 |
|---|---|---|
| 部品の役割 | MCP は外部システムとの接続、スキルは手順とスクリプト、カスタムエージェントは役割とツールの制限 | VS Code「Customization concepts」、GitHub「Copilot customization cheat sheet」 |
| スキルのスクリプト | スキルのフォルダに `scripts/` を置き、エージェントがスキルのフォルダからの相対パスで実行する。依存は PEP 723 で書き `uv run` で動かす | Agent Skills「Using scripts in skills」「Adding skills support」 |
| ハーネス | VS Code の Session Target「Copilot」は Copilot CLI と同じ実行基盤、「Local」は VS Code 内蔵 | VS Code「Agent harnesses」 |
| ツールの時間 | Copilot の実行基盤は MCP のツール 1 回を既定 30 秒で打ち切る。Agent Plugins の `mcp.json` には時間を書けない | Copilot CLI リファレンス、Agent Plugins 1.0 仕様 7.2 |
| ツールの承認 | Copilot の実行基盤では、MCP のツールは呼ぶたびに承認が要る。スキルの `allowed-tools`、`--allow-tool` で事前に許可できる。VS Code では「Chat: Manage Tool Approval」でサーバ単位に許可できる | Copilot CLI リファレンス、VS Code「Approvals」 |
| ワークスペースの外 | 開いているフォルダの外のファイルの読み書きには許可が要る | Copilot CLI リファレンス（`/add-dir`）、VS Code「Approvals」 |
| `${PLUGIN_DATA}` | VS Code は Agent Plugins の `mcp.json` で `${PLUGIN_ROOT}` だけを置き換え、`${PLUGIN_DATA}` は置き換えない | VS Code「Agent plugins」、VS Code のソース `pluginParsers.ts` |
| モデル名 | VS Code（Local）は表示名（例 `GPT-6 Luna`）で探す。Copilot の実行基盤は ID（例 `gpt-6-luna`）で、解決できないと親のモデルで動く（`modelPolicy: required` を除く） | VS Code のソース `languageModels.ts`、Copilot CLI リファレンス |
| プルリクエスト | Copilot CLI が標準で AI に渡す GitHub のツールにプルリクエストの作成は含まれない（人が使う `/pr create` はある） | Copilot CLI リファレンス |
| uv の証明書 | uv は既定では自分が持つ証明書だけを信頼する。社内のプロキシが証明書を差し替える環境では、`--native-tls`（OS の証明書を使う。uv 0.11 からは `--system-certs` が新しい名前で、`--native-tls` も同じ動きで使える）が要る | uv の changelog（0.11.0）、uv の環境変数の説明（`UV_NATIVE_TLS`、`UV_SYSTEM_CERTS`） |
| MCP の版の決め方 | MCP の Python SDK 2.2 の `MCPServer.run("stdio")` は、最初の要求で版（2025 年までの handshake か 2026-07-28）を決め、あとから別の版の要求が来ると断る。`serve_loop` は handshake の版だけで応える | MCP Python SDK のソース `mcp/server/runner.py`（`serve_dual_era_loop`、`serve_loop`）、`mcp/client/_probe.py` |
| スキルの `allowed-tools` | Copilot CLI はスキルが使われている間、書いたツールを確認なしで使う（文字列か配列。MCP は `サーバ名` か `サーバ名(ツール名)`、ターミナルは `shell(コマンド:*)`）。VS Code はこの項目を使わず、エディタでヒントを出すだけ（読み込みは妨げない） | Copilot CLI リファレンス（Skills reference、Tool permission patterns）、VS Code のソース `promptValidator.ts` |
| VS Code の確認の設定 | MCP のツールは「Chat: Manage Tool Approval」でサーバごとに確認なしにできる。ターミナルのコマンドは `chat.tools.terminal.autoApprove` に `/正規表現/` で書ける | VS Code「Approvals」 |
| MCP のツールの注釈 | VS Code は `readOnlyHint` が無いツールの実行前に確認を出し、`openWorldHint` が true のツールは結果も確認させる。このため Coverity だけを相手にするツールは `openWorldHint: false` にする | VS Code のソース `mcpLanguageModelToolContribution.ts`、MCP 仕様（Tool annotations） |
| プラグインのサブエージェント | Copilot はプラグインの `com.github.copilot/agents/` からカスタムエージェントを読む。ユーザのフォルダ `~/.copilot/agents/` がいちばん優先される | GitHub「About plugins」、Copilot CLI リファレンス（Custom agent locations） |
| スキルの名前 | VS Code は、名前が小文字・数字・ハイフンだけで、フォルダ名と同じでないと読み込まない。プラグインのスキルは `/プラグイン名:スキル名` で出る | VS Code「Agent skills」 |

## 5. 開発

開発の進め方（テスト、見本の作り直し、版数）は [development.md](development.md) にまとめる。
