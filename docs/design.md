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
| スキルに同梱するスクリプト | 決まった処理 | 実行フォルダ、グループ化、修正用のコピー、差分とブランチ、レポートと一覧、承認の読み書き、反映、検証、準備の確認、動作確認、画面（設定・結果・監視）。スキル `coverity-triage-scripts` の `scripts/ct.py` にまとめる |
| MCP サーバ | 外部システムとの接続 | Coverity Connect との通信だけ（接続の確認、警告の検索、警告の詳細、トリアージの書き戻し） |
| 組み込みツール | ファイルの読み書き・検索・ターミナル | ソースを読む・探す・直す、スクリプトを動かす |
| カスタムエージェント（サブエージェント） | 役割と使えるツールを決める | `coverity-triage-worker`：警告 1 件（または 1 グループ）を調べて 2 つの案を書く。使えるのは読み取り・検索・編集・スキルの読み込みだけ（ターミナルは使えない） |
| プラグイン | まとめて配布する | 上の部品を Agent Plugins 1.0 の形でまとめる |

### 1.2 誰が何をするか

| 担当 | すること | しないこと |
|---|---|---|
| 人 | 準備、条件の指定、画面での承認の入力、反映の同意、画面での見守り | — |
| エージェント（チャットの AI） | 入口のスキルの手順に沿って、スクリプトと MCP のツールを順に呼び、サブエージェントに調査を任せ、結果を人に伝える | 自分でソースを調べて判断すること（サブエージェントの仕事） |
| サブエージェント | 警告の真偽の判断、修正（修正用のコピーの上で）、逸脱コメントの作成、結果ファイルの作成 | ターミナル、Coverity への書き込み |
| スクリプト | 決まった処理。毎回同じ結果になる。画面のサーバも動かす | 判断や文章作成 |
| MCP サーバ | Coverity との通信（実行フォルダの `operations.log` への記録を含む） | ファイルの加工、Git の操作 |

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
│  │  └─ scripts/ct.py, scripts/ctlib/          … uv run で動く Python（画面は ctlib/gui.py と ctlib/gui.html）
│  └─ triage-investigation/ checker-knowledge/ code-fix/ deviation-comment/ triage-report/   … 知識
└─ com.github.copilot/agents/coverity-triage-worker.agent.md   … サブエージェント

<対象リポジトリ>/.coverity-triage/              … チームで共有（コミットする）
├─ config.yaml   filters/*.yaml   knowledge.md   no-grouping.yaml

~/.coverity-triage/gui/<リポジトリのハッシュ>.json  … 動いている画面のアドレス（使い回すため。画面のサーバが止まると消える）

<出力先>（既定 ../coverity-triage-out）/<実行 ID>/   … リポジトリの外
├─ run.json                … 実行の設定と進み具合（作業ごとの状態）
├─ filter.json             … この実行で使う条件（MCP の search_issues が読む）
├─ issues.json             … Coverity から取った警告の一覧（MCP が書く。問い合わせごとの秒数も残す）
├─ issues.mapped.json      … パスをリポジトリからの相対パスに直した一覧（ct.py plan が書く）
├─ config.snapshot.yaml    … 実行を始めたときの設定の写し
├─ items/<ID>/             … 作業ごと：issue-<CID>.json（警告経路。MCP が書く）、brief.md（サブエージェントへの指示）、
│                             progress.json（サブエージェントの節目）、result.json（サブエージェントの結果）、saved.json（差分とブランチ）
├─ work/                   … 修正用のコピー：fix-<n>、annotation-<n>（オプション）、analyzed（解析リビジョン）。ct.py summary で消す
├─ cid/<ID>.html           … 詳細レポート（表示専用。cid/<ID>.md はオプション markdown のときだけ）
├─ patches/<ID>-fix.patch  … 差分（git / svn が出したバイト列のまま）
├─ fixed/<ID>/fix/         … 修正後のファイル（リポジトリの構成のまま）
├─ summary.html            … 一覧（表示専用。承認欄はいまの承認。summary.md はオプション markdown のときだけ）
├─ approvals.json          … 承認と逸脱の手直し（画面が書く。ct.py summary が推奨を入れておく）
├─ verify.json             … （オプション）ビルドでの検証の結果
├─ apply-plan.json         … 反映の内容。apply-plan.result.json は MCP の update_triage の結果
├─ metrics.json            … （オプション）効果測定の記録
└─ operations.log          … 操作の記録（ct.py と MCP サーバが 1 行ずつ足す。各行に level と source。認証情報は伏せる）
```

## 2. 処理の流れ

以下の「エージェント」はチャットの AI、「`ct.py X`」はスクリプトのコマンド、「MCP `X`」は MCP のツールです。

### 2.1 準備（`/coverity-setup`）

| 順 | 担当 | すること |
|---|---|---|
| 0 | エージェント・人 | 対話で準備するか、画面で準備するかを聞く（決定 22）。画面のときは、6・7・11 の代わりに `ct.py gui` で画面の「設定」を開く（下の表） |
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

画面で準備するとき（決定 22〜26、31〜33）：

| 順 | 担当 | すること |
|---|---|---|
| 1 | エージェント・`ct.py` | 上の 1〜5 と同じ |
| 2 | `ct.py gui` | 画面のサーバを裏で起動し（127.0.0.1 の空いているポート、URL に毎回違う確認用の文字列）、既定のブラウザで「設定」を開いて、すぐ戻る。同じリポジトリの画面が動いていれば、起動せずに使い回す |
| 3 | 人 | 画面で、基本設定・オプション設定・条件・知識・グループ化しない CID・認証情報を入れ、保存の前に表で内容を確かめて保存する。ビルドでの検証は、画面の値で試しのビルド（`trial-build` と同じ処理）ができる。保存したらチャットで知らせる。画面は閉じずに残し、あとで監視と結果の確認に使う |
| 4 | `ct.py doctor` | 足りないものが残っていないかを確かめる（残っていれば、画面をもう一度開いて直してもらう） |
| 5 | エージェント・人・MCP | 上の 8〜10（再起動、接続の確認、確認を減らす案内）と同じ |

画面のサーバ（`ctlib/gui.py`、画面は `ctlib/gui.html`。Python の標準ライブラリだけで動く。結果と監視を含めた全体は 3.10）：

- 受け付けるのは `127.0.0.1` / `localhost` の Host と、URL の確認用の文字列（API は `X-CT-Token` ヘッダ）が合う要求だけ。ほかのウェブページやプログラムからは使えない。
- 保存はすべての値を確かめてから書く（1 つでも直すところがあれば何も書かない）。確かめ方は `init-config` と同じ（設定のモデル、出力先がリポジトリの外、条件ファイルのストリームが 1 つ）。`config.yaml` の先頭のコメントは残す。
- 認証情報は Windows のユーザの環境変数に保存する（`[Environment]::SetEnvironmentVariable(..., 'User')` と同じ。レジストリに書いて、変わったことを知らせる）。画面には認証キーが設定済みかどうかだけを返す。
- 保存しても、タブを閉じても、サーバは止まらない。止まるのは画面の「サーバを止める」（確認つき）だけ（決定 31）。アクセスが無くても自動では止まらない。ただし、同じリポジトリで新しいサーバが起動して置き換わったときは、古いほうが止まる（3.10）。
- Windows では、ターミナルを閉じても止まらないように、ターミナルから切り離して起動する（`DETACHED_PROCESS`、できれば `CREATE_BREAKAWAY_FROM_JOB`）。

### 2.2 実行（`/coverity-run`）

| 順 | 担当 | すること |
|---|---|---|
| 1 | `ct.py runs` | 未完了の実行があれば、再開するか人に聞く（再開は `ct.py resume`。そのあと 3 で画面を開き、途中から続ける） |
| 2 | `ct.py new-run` | 実行フォルダを作り、条件を確定して `filter.json` に書く。修正用のコピーを置くフォルダを返す |
| 3 | `ct.py gui --run` | 画面の「監視」を開く（動いている画面があれば使い回す。決定 32）。人の返事は待たない。開けなくても実行は続ける |
| 4 | エージェント・人 | 修正用のコピーを置くフォルダへのアクセスを 1 回許可してもらう（決定 21） |
| 5 | MCP `search_issues` | Coverity で警告を検索し、`issues.json` に保存する（上限件数で止める） |
| 6 | `ct.py plan` | パスの対応づけ、グループ化（1 グループ 10 件まで）、解析リビジョンと取り込み先の最新リビジョンの特定をして、作業の一覧を作る |
| 7 | 繰り返し | 下の 7-1〜7-5 を、作業が無くなるまで。設定 `parallel`（既定 3）の数まで同時に進める：`next` を続けて呼んで作業を取り（`can_start_more` が 0 になるまで）、サブエージェントを同時に起動し、1 つ終わるごとに `finish` して次を取る |
| 7-1 | `ct.py next` | 次の作業（警告 1 件か 1 グループ）を取り、修正用のコピーを用意して最新のリビジョンにそろえる（初回はコピーを作る）。利用者のリポジトリの状態も記録する。前の試みの `progress.json` は消す |
| 7-2 | MCP `get_issues` | 警告経路を Coverity から取り、`items/<ID>/` に保存する |
| 7-3 | `ct.py brief` | パスの対応づけ・ずれの確認をして、サブエージェントへの指示 `brief.md` を作る（進み具合のファイル `progress.json` の場所と形も書く） |
| 7-4 | サブエージェント | `brief.md` を読み、知識のスキルに沿って調べ、修正用のコピーを直し、`result.json` を書く。節目ごとに `progress.json` を上書きする（3.9） |
| 7-5 | `ct.py finish` | `result.json` を確かめ、差分・ブランチ・修正後のファイル・詳細レポートを作る。不備があれば `problems` を返す（エージェントがサブエージェントに直させる。2 回直らなければ `ct.py fail`）。調べるコードや利用者のリポジトリが変わっていたら `warnings` で知らせる |
| 8 | （オプション）`ct.py verify` | ビルドでの検証。時間がかかるので、ターミナルで裏で動かす |
| 9 | `ct.py summary` | 一覧 `summary.html` を作り、まだ承認の無い作業に推奨を入れて `approvals.json` に保存する。全部の作業が終わっていれば修正用のコピーを消す（ブランチと差分は残る） |
| 10 | エージェント・`ct.py gui --run --view summary` | 画面の「結果」を開き、一覧の場所と件数を伝え、画面で承認を確かめてから `/coverity-apply` を使うよう案内する |

`ct.py` の各コマンドと MCP のツールは、実行フォルダの `operations.log` に 1 行ずつ記録し、画面の「監視」はそれを読んで動きを見せる（3.9）。

### 2.3 反映（`/coverity-apply`）

| 順 | 担当 | すること |
|---|---|---|
| 0 | 人 | 画面の「結果」で承認を選び、必要なら逸脱の内容を手直しする（`approvals.json` に保存。3.8） |
| 1 | `ct.py preview` | `approvals.json` の承認と逸脱の手直し（古い実行は `summary.md` の承認欄と `cid/<ID>.md` の逸脱の節）を読み、件数と反映内容 `apply-plan.json`、確認用の文字列を作る。承認が 1 件も無ければ、画面で承認するよう案内を返す |
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
- 実行フォルダのファイルを扱う呼び出しは、その `operations.log` に記録する（決定 36。画面の「監視」が読む）。実行フォルダかどうかは、`search_issues` の `output_file` の親、`get_issues` の `output_dir` の 2 つ上、`update_triage` の `plan_file` の親に `run.json` があるかで決める。1 回の呼び出しで `"source": "mcp"`、`"event": "mcp:<ツール名>"` の行を、`"phase": "start"` と `"end"`（例外のときは `"error"`）で書く。例外は level `critical`（`error` に例外の文字列）、取れなかった CID などの一部の失敗は `warning`。認証情報の値は伏せ、記録に失敗してもツールの動きは変えない。

| ツール | 入力 | すること | 返すもの |
|---|---|---|---|
| `check_connection` | `repo_root` | 設定の Coverity に、環境変数の認証情報で 1 回だけ読み取りの問い合わせをする（10 秒以内） | 認証できたか、かかった秒数、URL が http のときの注意 |
| `search_issues` | `repo_root`、`filter_file`（`ct.py new-run` が作る `filter.json`）、`output_file`（`issues.json`） | REST で検索し、条件の `limit` 件で止めて保存する。続けて最新スナップショットの解析リビジョンを記録する。問い合わせごとの時間も残す | 見つかった件数（`found`）、Coverity が返した件数（`coverity_total`）、検索のあとの条件で外した件数（`excluded`。外した警告と合わなかった項目の値は `issues.json` の `excluded`）、上限で止めたか、解析リビジョン、`done` |
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
| | `gui` | 画面（監視・結果・設定）をブラウザで開く。`--run` で実行を、`--view`（`monitor` / `summary` / `settings`）で最初に見せる区分を選ぶ。動いていれば使い回す（3.10） |
| 実行 | `runs` / `resume` / `status` | 実行の一覧／再開（処理中・エラーの作業を未処理に戻す）／進み具合 |
| | `new-run` / `plan` | 実行フォルダと条件／作業の一覧（グループ化を含む） |
| | `next` / `brief` / `finish` / `fail` | 1 件ずつの準備・指示・仕上げ・失敗の記録 |
| | `verify` / `summary` | （オプション）ビルドでの検証／一覧 `summary.html` と承認の初期値（`approvals.json`） |
| 反映 | `preview` / `apply-code` | 反映の内容（`approvals.json` から）と確認用の文字列／修正の反映 |
| | `knowledge-candidates` / `add-knowledge` / `stats` | （オプション）知識の追記の提案／効果測定の集計 |
| 動作確認 | `selftest start` / `static` / `sample` / `check-run` / `check-apply` / `check-coverity` / `check-build` / `record` / `report` | 動作確認の準備・判定・記録・結果のまとめ |

- 引数は JSON を使わず、1 つずつの引数にする（例 `--impact High --impact Medium`）。Windows の PowerShell は、外部のコマンドに渡す JSON の `"` を崩すことがあるため。
- 結果は `{"ok": true, ...}`。できなかったときは `{"ok": false, "error": ..., "problems": [...]}` と終了コード 1。
- `--run` を持つコマンド（`gui`・`selftest` を除く）が `ok: false` で終わったときは、`main` がその実行の `operations.log` に `"event": "error"`（`command`、`error`、`problems`、あれば `item`）を記録する。level は、`finish` の `problems`（サブエージェントに直させる段階）なら `warning`、それ以外は `critical`。記録の失敗は結果に影響させない。
- `run.json` の読み書きはロックファイルで順番にする（同時に進める作業があるため）。git / svn のコピーの操作も同じ。

修正用のコピー：

- git：出力先の下に worktree を作り、取り込み先の最新のコミットにそろえる（`git worktree add --detach`）。作業ごとに元に戻して使い回す（`reset --hard` と `clean -fdx`）。仕上げでコミットし、警告ごとのブランチ（`coverity-fix/cid-<CID>`、グループは `coverity-fix/<実行 ID>-G1`）を作る。オプション `per_run_branch` のときはブランチを作らず、反映のときに承認した分だけを 1 つのブランチにまとめる（`cherry-pick`）。どちらの場合も `refs/coverity-triage/<実行 ID>/<ID>-fix` でコミットを残す。実行の最後（`ct.py summary`）に worktree を消す。
- svn：出力先の下に最新のリビジョンをチェックアウトし（2 つ目からのスロットは、同じ実行のコピーを PC の中で複製してから `svn update` でそろえる。大きなリポジトリをサーバから取り直すと数十分かかるため）、作業ごとに `svn revert` と、管理外のファイルの削除で戻して使い回す。差分は `svn add` のあと `svn diff` で作る。
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

- ターミナルと MCP のツールは使わない。必要な情報はすべて `brief.md` で受け取り、結果は `result.json` に書く。節目ごとに、進み具合を `progress.json` に上書きする（できるときだけ。3.9）。
- `brief.md` には、警告の情報と警告経路（リポジトリ内のパスに直したもの）、調査用と修正用のフォルダ、結果ファイルの場所、知識のファイルの場所、ずれの確認結果を書く。

### 3.6 一覧と詳細レポート（`ctlib/report.py`）

HTML が本体で、表示専用（決定 27〜29）。どちらも 1 つで完結した HTML の文書にする（`<meta charset="utf-8">`、見た目の指定 `REPORT_CSS` は `<style>` で中に書き、スクリプトは使わない）。AI と Coverity から来た文字列はすべて `html.escape` で表示用に直す。リンクは実行フォルダの中の相対パスにし、フォルダから直接（`file://`）開いても、画面の中でも動くようにする。明るい／暗いテーマ（`prefers-color-scheme`）と印刷、狭い画面（表を縦のカードに組み替える）に合わせる。

- 一覧 `summary.html`（`render_summary_html`）：上に件数のタイル（警告、作業とグループ、修正の推奨、逸脱の推奨、確信度 低、エラー、未処理）。その下に、承認は画面で入れることと、いまの承認の内訳。表は `承認 | ID | 推奨 | 確信度 | 見立て | 詳細` の 6 列で、確信度の低い順。承認欄は `approvals.json` のいまの承認を「修正／逸脱／却下／（空）」で表示し、推奨から変えた行に印を付ける。グループは「G1（3 件）」と 1 行で表す。ID と詳細は `cid/<ID>.html` へのリンク。処理できなかった作業と未処理の作業は別の表にする。
- 詳細レポート `cid/<ID>.html`（`render_item_html`）：見出し（ID、チェッカー、場所、推奨・確信度・Impact・見立て・いまの承認のバッジ）、結論（1 行の見立て、推奨、確信度とその理由、リビジョンのずれ、注意）、警告の概要（CID・チェッカー・ファイル・関数・行・Impact・CWE の表）、真偽の根拠（警告経路。★ のイベントを目立たせる。調査結果と根拠の場所）、対応の案（案A: 逸脱 と 案B: 修正 を、広い画面では左右に並べる。推奨の案に印。人が画面で逸脱を直したときは直した内容を出し、AI の案は折りたたみで見せる）、修正の差分（**差分をそのまま載せる**。400 行まで。`<pre class="diff">` の中で行ごとに `<span class="add|del|hunk|meta">` で色分け）、ビルドでの検証（オプション）、処理情報。差分ファイル（`../patches/<name>`）と修正後のファイル（`../fixed/<ID>/fix/<path>`）へリンクする。
- Markdown（`render_summary`、`render_item`）：設定 `options.markdown` が true のときだけ、HTML と同じ内容で `summary.md`、`cid/<ID>.md` も書く（決定 28）。形はこれまでどおり（一覧は 6 列の表、詳細は 1 結論 … 6 処理情報。逸脱の節は `<!-- ct:begin deviation -->` と `<!-- ct:end deviation -->` で囲む）だが、承認欄はいまの承認の表示で、書き換えても読まない（読むのは `approvals.json` の無い古い実行だけ。3.8）。
- 書き直すとき：`finish`・ビルドでの検証のあと（`write_item_files`）、`summary`・承認の変更のあと（`write_summary_files` と、変えた作業の `write_item_files`）。
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
parallel: 3
options:                    # 既定はすべて false
  annotation: false
  per_run_branch: false
  knowledge_suggestions: false
  metrics: false
  markdown: false           # true なら一覧と詳細レポートの Markdown も出す
verify:                     # options の代わりに、default が none 以外ならビルドでの検証を使う
  default: none             # none / build / build+analyze
  setup_command: ""
  build_dir: ""
  build_command: ""
  cov_build_args: "--dir idir"
  cov_analyze_args: "--dir idir --all"
```

### 3.8 承認 `approvals.json`（`ctlib/approvals.py`）

人が画面で入れる承認と、逸脱の手直しを、実行フォルダの `approvals.json` に置く（決定 29）。

```json
{
  "version": 1,
  "updated_at": "2026-10-15T10:00:00",
  "items": {
    "20001": {"decision": "fix", "edited": false, "updated_at": "..."},
    "20002": {"decision": "deviation", "edited": true, "updated_at": "...",
              "deviation": {"classification": "False Positive", "action": "Ignore",
                            "severity": "Unspecified", "comment": "人が直した逸脱コメント"}},
    "G1":    {"decision": null, "edited": false, "updated_at": "..."}
  }
}
```

- `decision`：`fix` / `deviation` / `reject` / `null`（反映しない。これまでの空欄）。
- `deviation`：人が逸脱の内容を直したときだけ持つ。無ければ `result.json` の逸脱案を使う。AI の案と同じ値に戻すと消える。
- `edited`：`decision` が AI の推奨と違うか、`deviation` を持つとき true（一覧と画面で印を付けるための表示用）。
- `ensure_defaults`（`ct.py summary`）：まだ項目の無い、終わった作業に、AI の推奨を入れる（決定 7）。ある項目は変えない。一覧ができたあとに終わった作業（再開した実行）には、`ct.py finish` が同じように推奨を入れる（画面に出る承認と `preview` が反映するものをそろえるため。画面は、一覧ができたあとに項目の無い作業を「反映しない」と表示する）。
- `update`（画面の `POST /api/runs/<run_id>/approvals`）：終わった作業だけを受け付け、値を確かめてから（`decision` が上の 4 つのどれか、逸脱の 4 項目が空でない）書く。書き込みは `common.file_lock` の中で、`common.write_json`（原子的な書き込み）で行い、同じロックの中で、その作業の詳細レポートと一覧を作り直す。実行の途中でも、終わった作業は承認できる。ただし一覧は `ct.py summary` が初めて作る（ほかの作業に AI の推奨を入れるのも、そのとき）ため、それまでは詳細レポートだけを作り直し、`preview` は「一覧がありません」で断る。
- `decisions` / `deviation_for`（`ct.py preview`）：承認と、Coverity に書く逸脱の内容（手直しがあればそれ、無ければ AI の案）。
- 古い実行（`approvals.json` が無く `summary.md` がある）：`summary.md` の承認欄と `cid/<ID>.md` の逸脱の節を、これまでどおり読む（書き間違いは `preview` の `problems`）。画面で承認を変えるか、`ct.py summary` をもう一度実行すると、その内容で `approvals.json` を作る（決定 30）。このとき、まだ無い `cid/<ID>.html` も作り、`summary.md` は今の承認で書き直す（古い表が、もう使われない承認を見せたままにならないように。`cid/<ID>.md` は人が書いたものとして残す）。

### 3.9 動きの記録と監視（`ctlib/oplog.py`、`ctlib/activity.py`）

画面の「監視」は、実行フォルダのファイルだけを読んで、エージェントの動きを組み立てる（決定 34・35）。どのファイルも書き換えない。

- `operations.log`：1 行に 1 つの JSON。`{"time": "...", "event": "next", "level": "info", "source": "ct", "item": "20001", ...}`。`level` は `info` / `warning` / `critical`（既定 `info`）、`source` は `ct`（スクリプト）か `mcp`（MCP サーバ、3.2）。`oplog.log(run_dir, event, secrets, level=..., source=..., **fields)` で書き、`oplog.read` は壊れた行を飛ばして読む。`ct.py fail` は `critical`、`ct.py` の失敗は 3.3 のとおり。
- `items/<ID>/progress.json`：サブエージェントが節目ごとに編集の機能で丸ごと上書きする（できるときだけ）。`{"item": "20001", "phase": "judge", "note": "呼び出し元 read_config() を確認中"}`。`phase` は `read`（警告経路とコードを読む）→ `judge`（真偽を判断する）→ `fix`（修正を作る）→ `deviation`（逸脱コメントを書く）→ `result`（結果ファイルを書く）。サブエージェントは時刻を書けない（ターミナルが無い）ので、時刻はファイルの更新時刻を使う。場所と形は `brief.md` の「フォルダとファイル」の表で伝える。
- `activity.board(run)`：画面が 2 秒ごとに取る状態（`GET /api/runs/<run_id>/activity`）。
  - 実行の段階（`search` / `plan` / `items` / `verify` / `summary` / `apply` / `done`）と、メインのエージェントのいまの仕事（作業を配っている、サブエージェントの終わりを待っている など）と、最後の記録からの秒数。`apply` は、最後の `apply-code` より後の `preview` か `update_triage` の記録が 10 分以内のときだけ（人が反映に同意しなかったときは、そのあと何も記録されないため）。画面の実行の一覧も同じ決め方で、10 分より前から記録の無い終わった実行は、記録を読まずに `done` にする。
  - スロット（1〜`parallel`。空きも返す）ごとの作業：CID かグループ、何回目か、段階（その作業の最後の `copy` か `next` 以降の記録で決める。`next` がコピーを用意し始めた `copy` だけ → 修正用のコピーを用意中、`mcp:get_issues` の開始 → 警告経路を取得中、`brief` → 調査中、`finish` の `problems` → 結果を直している）、`progress.json` の `phase` と `note`、経過時間、最後の動きからの秒数。
  - 停止の疑い（`stalled`）：修正用のコピーを用意中のものを除く処理中の作業で、その作業の最後の記録・`progress.json`・作業のフォルダの中のファイルの更新時刻のうち、いちばん新しいものから 10 分（`STALL_SECONDS`）以上たったとき。注意にも入れる。
  - 最近の動き（新しい順に 40 件。記録を日本語の 1 行にしたもの）。
- `activity.alerts(run)`：重大なエラーと注意（新しい順に 100 件まで）。元は、`operations.log` の `critical` / `warning` の行、`run.json` でエラーになった作業（理由）、`items/*/issue-*.json` の取れなかった警告経路、`apply-plan.result.json` の書き込めなかった逸脱、`verify.json` のビルドの失敗、停止の疑い。
- 画面のボードは、上にメインの枠、その下に `parallel` の数の枠を、罫線の文字で描く（等幅のフォント）。枠には作業、段階、進み具合の棒（`phase` の位置）、経過時間を出し、停止の疑いは「⚠ 停止?」と色で示す。

### 3.10 画面（`ctlib/gui.py`、`ctlib/gui.html`）

設定（2.1）に加えて、結果と監視を 1 つの画面で扱う（決定 31〜34）。Python の標準ライブラリだけで動き、画面の HTML・CSS・JavaScript はすべて `gui.html` の中に書く（外から取り込まない）。

- 区分：上のナビの「監視」「結果」「設定」。設定の中は、基本設定／オプション設定／条件／知識／グループ化しない CID のタブ。URL の `#run=<実行 ID>&view=monitor|summary|settings` で最初の画面を決め、画面を変えると書き換える。
  - 監視：3.9 のボード、重大なエラー（`critical` は赤、`warning` は黄。選ぶと詳しい内容を開く）、最近の動き。実行を選んでいなければ、未完了のいちばん新しい実行を選ぶ。タブが隠れている間は取りに行かない。
  - 結果：実行の一覧 → 一覧（確信度の低い順。承認を選ぶとすぐ保存して知らせる。推奨から変えた行に印。件数のタイル）→ 詳細（左に詳細レポート `cid/<ID>.html` を枠の中に出し、右に承認、逸脱の手直し、「保存」「AI の案に戻す」、差分ファイルと修正後のファイルへのリンク。狭い画面では縦に並べる）。
  - 設定：2.1 のとおり。保存しても止まらない。右上のメニューの「サーバを止める」（確認つき）で止める。
- 起動と使い回し（`launch`）：`~/.coverity-triage/gui/<リポジトリの絶対パスの SHA-256 の先頭 16 文字>.json` に `url`・`pid`・`port`・`token`・`repo`・`version`（スクリプトの指紋）を書く（POSIX では、ほかのユーザが読めないよう 0600 にする。置き場所は環境変数 `CT_GUI_STATE_DIR` で変えられる）。次の `ct.py gui` は、まずこのファイルを読み、その確認用の文字列で `/api/ping` が通れば、新しく起動せずにそのアドレスを返す（`reused: true`）。答えが無ければ新しく起動し、古いスクリプトで動いているサーバ（`version` が違う）は止めてから起動し直す。サーバが止まるときにファイルを消す。サーバは 3 秒ごとにこのファイルを読み、別のサーバの記録に変わっていたら（`--foreground` で起動した、ファイルが壊れていて新しく起動した など）自分で止まる。`ct.py gui` から見つけられないサーバを残さないため（使われていないことでは止まらない。ファイルが消えた・読めないときも止まらない）。
- 確認用の文字列：`/` を正しい `?t=` で開いたときに Cookie（`ct_token_<ポート>`。`HttpOnly`、`SameSite=Strict`、`Path=/`）を付ける。Cookie は同じ PC のすべてのポートで共有されるため、名前にポートを入れて、別のリポジトリの画面と混ざらないようにする。API は `X-CT-Token` ヘッダ、`/files/` と `/view/` は Cookie かヘッダで確かめる。
- 実行は `^[0-9]{8}-[0-9]{6}(-[0-9]+)?$` の ID だけを受け付け、設定の出力先のすぐ下のフォルダだけを探す。

| 要求 | 返すもの |
|---|---|
| `GET /api/runs` | 実行の一覧（ID、フォルダ、日時、条件、件数、一覧の有無、未完了か、段階） |
| `GET /api/runs/<run_id>/summary` | 一覧の行（ID、題、CID、チェッカー、場所、Impact、推奨、確信度、見立て、承認、変えたか、状態、反映済みか、詳細レポートの場所）、エラー、未処理 |
| `GET /api/runs/<run_id>/items/<ID>` | 1 つの作業の行、いまの逸脱と AI の逸脱、差分ファイルと修正後のファイルの一覧、詳細レポートのアドレス |
| `POST /api/runs/<run_id>/approvals` | `{"item", "decision", "deviation"}` を 3.8 の `update` で保存する。反映済みの作業を変えたときは注意も返す（変更はできる） |
| `GET /api/runs/<run_id>/activity` | 3.9 の `board` |
| `GET /files/<run_id>/<パス>` | 実行フォルダの中の `summary.html`、`cid/*.html`、`patches/*.patch`、`fixed/**` だけ（`..` やシンボリックリンクで外に出るものは 404）。`summary.html` と `cid/*.html` だけを HTML として返し、それ以外（修正後のファイルの中の HTML も）は `text/plain`（UTF-8 で読めず cp932 で読めるファイルは `charset=shift_jis`） |
| `GET /view/<run_id>/<パス>` | 差分は行ごとに色分け、修正後のファイルは行番号つきの表示用の HTML（サーバでエスケープして作る。スクリプトなし）。上に元のファイルへのリンクとパス |
| そのほか | 設定の `/api/state`・`/api/save`・`/api/credentials`・`/api/trial-build`、`/api/ping`、`/api/close`（「サーバを止める」） |

- AI の文章を含む HTML（`/files/` と `/view/`）には `Content-Security-Policy: sandbox allow-same-origin; default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'` を付け、スクリプトを動かさない。`allow-same-origin` は、レポートの中のリンク（`../patches/...` など）を開くときに Cookie を送るため（`allow-scripts` は付けないので、スクリプトは動かない）。
- 画面の本体（`/`）は、ほかのページの枠の中に出さない（`frame-ancestors 'none'`）。`/files/` と `/view/` は画面の枠の中に出すため、同じアドレスの中だけ許す（`X-Frame-Options: SAMEORIGIN`、`frame-ancestors 'self'`）。本体の CSP は `script-src 'unsafe-inline'`、`connect-src 'self'`、`frame-src 'self'`。

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
