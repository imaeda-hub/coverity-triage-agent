# Coverity トリアージエージェント 設計書

この文書は「どう作るか」を定めます。「何を作るか」は [spec.md](spec.md) にあります。
1 章で全体の構成、2 章で処理の流れ、3 章で部品ごとの中身、4 章で設計の根拠にした公式仕様と、利用者の PC での試しで確かめたことを書きます。

## 1. 全体の構成

### 1.1 部品と役割

公式の役割分担（VS Code「Customization concepts」、GitHub「Copilot customization cheat sheet」）に合わせて、部品を次のように分けます。

| 部品 | 公式の役割 | このプラグインでの担当 |
|---|---|---|
| スキル（入口） | 繰り返す手順をまとめる | `/coverity-setup`・`/coverity-run`・`/coverity-apply`・`/coverity-help`・`/coverity-selftest` の手順。エージェントはこの手順に沿って進める。`/coverity-run` の途中で次にすることは、スクリプト（`ct.py tick`）が決める（決定 49） |
| スキル（知識） | 必要なときに読む知識 | 調査の手順と確信度の基準、チェッカー別の観点、修正の制約、逸脱コメントの書き方、結果の書き方、同じ原因の作業のまとめ方（統合）、結果の独立した確かめ方（検証） |
| スキルに同梱するスクリプト | 決まった処理 | 実行の順番（`tick`）、初回解析（ビルド・解析・サーバへの登録を裏で動かす）、実行フォルダ、グループ化、統合の候補と反映、修正用のコピー、差分とブランチ、レポートと一覧、承認の読み書き、反映、検証、準備の確認、動作確認、画面（設定・結果・監視）。スキル `coverity-triage-scripts` の `scripts/ct.py` にまとめる |
| MCP サーバ | 外部システムとの接続 | Coverity Connect との通信だけ（接続の確認、警告の検索、警告の詳細、トリアージの書き戻し） |
| 組み込みツール | ファイルの読み書き・検索・ターミナル | ソースを読む・探す・直す、スクリプトを動かす |
| カスタムエージェント（サブエージェント） | 役割と使えるツールを決める | 4 つ（3.5）。`coverity-triage-collector`（取得）：Coverity から警告と警告経路を取るだけ。使えるのは読み取りと MCP の `search_issues`・`get_issues` だけ。`coverity-triage-worker`（作業）：警告 1 件（または 1 グループ）を調べて 2 つの案を書く。`coverity-triage-integrator`（統合。オプション）：同じ原因の作業をまとめる計画を書く。`coverity-triage-reviewer`（検証。オプション）：結果を独立に確かめる。作業・統合・検証で使えるのは読み取り・検索・編集・スキルの読み込みだけ（ターミナルと MCP のツールは使えない） |
| プラグイン | まとめて配布する | 上の部品を Agent Plugins 1.0 の形でまとめる |

- 役は、全体を進めるメイン（チャットの AI）と、取得・作業・統合・検証の 5 つ。これより足さない（決定 37）。初回解析はエージェントにせず、スクリプトの裏で動く処理にする（決まった長い処理のため。決定 13）。
- 取得・統合・検証のエージェント、`ct.py tick`、初回解析は、Copilot CLI だけを対象に作る（決定 50）。今ある VS Code 向けの部分（`${PLUGIN_DATA}` を使わないこと、モデルの表示名 など）は、そのまま残す。

### 1.2 誰が何をするか

| 担当 | すること | しないこと |
|---|---|---|
| 人 | 準備、条件の指定、画面での承認の入力、反映の同意、画面での見守り | — |
| エージェント（チャットの AI。オーケストレーション役） | 入口のスキルの手順に沿って進める。`/coverity-run` では、`ct.py tick` が返す `actions` のとおりにサブエージェントを裏で起動し、終わったら `tick` に知らせ、`say` を人に伝える | 次にすることを自分で決めること。自分でソースを調べて判断すること（サブエージェントの仕事） |
| 取得エージェント | 指示ファイルにある引数で、MCP の `search_issues`・`get_issues` を呼ぶ（保存は MCP サーバがする） | 警告の中身の判断や要約、ファイルの書き込み、ターミナル、Coverity への書き込み |
| 作業エージェント | 警告の真偽の判断、修正（修正用のコピーの上で）、逸脱コメントの作成、結果ファイルの作成。検証からの差し戻しと、統合でまとめた作業の作り直しも受け持つ | ターミナル、MCP のツール |
| 統合エージェント（オプション） | 調べ終わった作業のうち、同じ原因のものをまとめる計画（まとめた見立てと逸脱コメントを含む）を書く | コードの変更、ターミナル、MCP のツール |
| 検証エージェント（オプション） | 結果を独立に確かめ、検証の結果を書く | 結果ファイル・コード・差分の変更、推奨の変更、ターミナル、MCP のツール |
| スクリプト | 決まった処理。毎回同じ結果になる。実行の順番を決める（`tick`）。初回解析と画面のサーバも動かす | 判断や文章作成 |
| MCP サーバ | Coverity との通信（実行フォルダの `operations.log` への記録を含む） | ファイルの加工、Git の操作、`cov-commit-defects`（コマンドなのでスクリプトが動かす） |

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
│  └─ triage-investigation/ checker-knowledge/ code-fix/ deviation-comment/ triage-report/
│     triage-integration/ triage-review/       … 知識
└─ com.github.copilot/agents/                  … サブエージェント（3.5）
   └─ coverity-triage-collector.agent.md   coverity-triage-worker.agent.md
      coverity-triage-integrator.agent.md  coverity-triage-reviewer.agent.md

<対象リポジトリ>/.coverity-triage/              … チームで共有（コミットする）
├─ config.yaml   filters/*.yaml   knowledge.md   no-grouping.yaml

~/.coverity-triage/gui/<リポジトリのハッシュ>.json  … 動いている画面のアドレス（使い回すため。画面のサーバが止まると消える）

<出力先>（既定 ../coverity-triage-out）/<実行 ID>/   … リポジトリの外
├─ run.json                … 実行の設定と進み具合（作業ごとの状態、この実行で行う段、取得・統合・検証のエージェントの仕事。3.12）
├─ filter.json             … この実行で使う条件（MCP の search_issues が読む）
├─ issues.json             … Coverity から取った警告の一覧（MCP が書く。問い合わせごとの秒数も残す）
├─ issues.mapped.json      … パスをリポジトリからの相対パスに直した一覧（ct.py plan が書く）
├─ config.snapshot.yaml    … 実行を始めたときの設定の写し
├─ items/<ID>/             … 作業ごと：issue-<CID>.json（警告経路。MCP が書く）、brief.md（作業エージェントへの指示）、
│                             progress.json（作業エージェントの節目）、result.json（作業エージェントの結果）、saved.json（差分とブランチ）、
│                             review.json（検証エージェントの結果。オプション）、result.previous.json（検証で差し戻す前の結果）
├─ agents/                 … 取得・統合・検証のエージェントへの指示と進み具合：collector/search.md、collector/paths-<n>.md、
│                             integrator/brief.md、integrator/progress.json、reviewer/<ID>.md、reviewer/progress.json
├─ analysis/               … （オプション）初回解析：status.json（状況）、initial.log（コマンドの出力）、process.log（裏のプロセスの出力）
├─ integration/            … （オプション）統合：candidates.json（候補）、plan.json（統合エージェントの計画）、
│                             merged.json（まとめた作業ごとの元の作業・理由・修正の作り方・結果）
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
| 4 | `ct.py doctor` | 足りないものを調べる（リポジトリ、git / svn、設定、条件、出力先、環境変数、サブエージェント 4 つの配置。初回解析を使う設定なら、そのコマンドと登録の認証情報も） |
| 5 | `ct.py install-agent` | サブエージェント 4 つの定義をユーザのフォルダ `~/.copilot/agents`（環境変数 `COPILOT_HOME` があればその `agents`）にコピーし、プラグインに無くなった古い `coverity-triage-*.agent.md` を消す（決定 1。VS Code の Copilot でサブエージェントが見つからなかった事象への対処。古い定義が残って呼ばれないように） |
| 6 | エージェント・人 | 設定が無ければ（チームで最初の人だけ）、URL・プロジェクト・ストリームを 1 つずつ聞き、`ct.py detect` の結果と合わせて表で見せ、同意を得て `ct.py init-config` |
| 7 | 人 | 認証情報（ユーザ名・認証キー）を、ターミナルの伏せ字欄で環境変数に入れる |
| 8 | 人 | uv を入れたか、サブエージェントの定義を置いた（`restart_needed`）ときだけ、VS Code（または Copilot CLI）を再起動し、もう一度 `/coverity-setup` を入力する（再起動はこの 1 回だけ。uv を新しく入れると、起動済みの VS Code からは uv が見えず MCP サーバを起動できないため） |
| 9 | MCP `check_connection`・`ct.py doctor` | Coverity に認証できること、すべてそろったことを確かめる |
| 10 | エージェント | トリアージ中にこのプラグインのツールを確認なしで使う設定を案内する（決定 4）。取得エージェントが呼ぶ MCP のツールで確認の画面が出たら「このセッションでは許可」を選ぶよう伝える |
| 11 | （オプション） | ビルドでの検証か初回解析を使う場合だけ、`ct.py trial-build` と `ct.py set-verify`（試しのビルドではサーバに登録しない）。統合と検証を使う場合だけ、`config.yaml` の `agents` を書く |

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

始める部分（1〜4）は、入口のスキルの手順で進めます。そのあと（5〜12）は、エージェントが `ct.py tick` を呼び、返った `actions` を行い、また `tick` を呼ぶ、を `done` が返るまで繰り返します（決定 49。3.11）。どの段を行うかは `tick` が決めます。オプションの段は、その設定の実行だけで行います。

| 順 | 担当 | すること |
|---|---|---|
| 1 | `ct.py runs` | 未完了の実行（`unfinished`）があれば、再開するか人に聞く。再開は `ct.py resume`（処理中・エラーの作業を未処理に戻し、動いていたエージェントの記録を「動いていない」にし、失敗した初回解析の状況を消す）。そのあと 3・4 を行い、`tick` で途中から続ける |
| 2 | `ct.py new-run` | 実行フォルダを作り、条件を確定して `filter.json` に書く。この実行で行う段（初回解析・統合・検証。設定と `--initial` / `--no-initial` から決める）を `run.json` に残す（再開しても変わらない）。修正用のコピーを置くフォルダを返す |
| 3 | `ct.py gui --run` | 画面の「監視」を開く（動いている画面があれば使い回す。決定 32）。人の返事は待たない。開けなくても実行は続ける |
| 4 | エージェント・人 | 修正用のコピーを置くフォルダへのアクセスを 1 回許可してもらう（決定 21。再開のときも案内する） |
| 5 | 初回解析（オプション `verify.initial`） | `tick` が裏のプロセス（`ct.py initial-run`）を起動し、開いているフォルダで `cov-build` → `cov-analyze` → `cov-commit-defects` を行う。`tick` は 1 回に最長 120 秒待って返り、エージェントは終わるまで `tick` を呼び直す。登録まで終わってから 6 へ進む。失敗したら `stop` を返し、トリアージを始めずに止める（3.13） |
| 6 | 取得エージェント → MCP `search_issues` | Coverity で警告を検索し、`issues.json` に保存する（上限件数で止める） |
| 7 | `ct.py plan`（`tick` が呼ぶ） | パスの対応づけ、グループ化（1 グループ 10 件まで。同じまとまりから切り分けた作業には、同じクラスタの番号 `cluster` を残す）、解析リビジョンと取り込み先の最新リビジョンの特定をして、作業の一覧を作る |
| 8 | 取得エージェント → MCP `get_issues` | 全部の作業の警告経路を、1 回の起動でまとめて取り、`items/<ID>/` に保存する。取れなかった分は、もう 1 回だけ取り直す。それでも取れなければ、取れなかったと書いて、経路なしで調べる |
| 9 | 作業（繰り返し） | 下の 9-1〜9-3 を、作業が無くなるまで。設定 `parallel`（既定 3）の数まで同時に進める：`tick` が空いた枠の数だけ作業を取り出して `launch` を返し、1 つ終わるごとに、次の作業を返す |
| 9-1 | `ct.py next`・`ct.py brief`（`tick` が呼ぶ） | 次の作業（警告 1 件か 1 グループ）を取り、修正用のコピーを用意して最新のリビジョンにそろえる（初回はコピーを作る。差し戻しの作業は、前の修正を当てる）。利用者のリポジトリの状態も記録する。前の試みの `progress.json` は消す。パスの対応づけ・ずれの確認をして、作業エージェントへの指示 `brief.md` を作る（進み具合のファイル `progress.json` の場所と形も書く） |
| 9-2 | 作業エージェント | `brief.md` を読み、知識のスキルに沿って調べ、修正用のコピーを直し、`result.json` を書く。節目ごとに `progress.json` を上書きする（3.9） |
| 9-3 | `ct.py finish`（`tick --finished worker:<ID>` が呼ぶ） | `result.json` を確かめ、差分・ブランチ・修正後のファイル・詳細レポートを作る。不備があれば、直す点を添えて同じ作業の作業エージェントをもう一度起動する `launch` を返す（2 回直らなければ `ct.py fail`）。調べるコードや利用者のリポジトリが変わっていたら `warnings` で知らせる |
| 10 | 統合（オプション `agents.integration`） | スクリプトが候補を見つけ、統合エージェントが計画を書き、スクリプトがまとめた作業 `G<n>` を作る。作り直しが要るものは 9 に戻る（3.14） |
| 11 | 検証（オプション `agents.review`） | 検証エージェントが、終わった作業を 1 件ずつ確かめる。差し戻した作業は 9 に戻り、直したあと、もう一度確かめる（3.15） |
| 12 | （オプション）`ct.py verify` | ビルドでの検証。`tick` が `command` の action（`task` は `verify`）で返し、エージェントが裏で動かす指定で実行する（時間がかかる）。終わったら `tick --finished verify`（`ok: false` で終わったら `--failed verify`。確かめずに先へ進む） |
| 13 | `ct.py summary` | `tick` が `done` を返したら行う。一覧 `summary.html` を作り、まだ承認の無い作業に推奨を入れて `approvals.json` に保存する。全部の作業が終わっていれば修正用のコピーを消す（ブランチと差分は残る） |
| 14 | エージェント・`ct.py gui --run --view summary` | 画面の「結果」を開き、一覧の場所と件数、途中で `say` に出た大事な注意を伝え、画面で承認を確かめてから `/coverity-apply` を使うよう案内する |

- 取得エージェントが使えないときは、`tick` が MCP の呼び出し（`mcp` の action）を返し、エージェントが代わりに `search_issues`・`get_issues` を呼ぶ（3.11）。
- 統合か検証を使う実行では、一覧ができるまで画面で承認を選べない（3.8）。
- `ct.py` の各コマンドと MCP のツールは、実行フォルダの `operations.log` に 1 行ずつ記録し、画面の「監視」はそれを読んで動きを見せる（3.9）。

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
| ① 組み込み | MCP のツールが見えること、サブエージェント（作業）を起動でき、使えるツールが制限され、スキルを読めること、`/` メニュー。取得・統合・検証のエージェントも起動でき、使えるツールが決めたとおりか（取得は MCP の `search_issues`・`get_issues` があり、`update_triage` とターミナルが無い。統合・検証はターミナルと MCP のツールが無い）。4 つの定義の配置 | エージェント（観察）＋ `ct.py selftest` |
| ② 偽データ | 偽の Coverity（`coverity.api: fake`）と見本のリポジトリで、2.2（`tick` に従う）と 2.3 の流れを最後まで通す | 2.2・2.3 と同じ |
| ③ 社内 Coverity | 接続と認証、検索、警告経路の取得（読み取りだけ。初回解析もしない） | MCP ＋ `ct.py selftest` |
| ④ ビルドでの検証 | オプションを使う場合だけ。初回解析を使う設定なら、`cov-commit-defects` が見つかるか（登録そのものは試さない） | `ct.py` |

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
| | `install-agent` | サブエージェント 4 つの定義を `~/.copilot/agents`（`COPILOT_HOME` があればその `agents`）にコピーし、プラグインに無くなった古い定義を消す |
| | `trial-build` / `set-verify` | （オプション）試しのビルド／ビルドでの検証と初回解析の設定を書く（`--initial` / `--no-initial`、`--cov-commit-args`、`--timeout-minutes`） |
| | `gui` | 画面（監視・結果・設定）をブラウザで開く。`--run` で実行を、`--view`（`monitor` / `summary` / `settings`）で最初に見せる区分を選ぶ。動いていれば使い回す（3.10） |
| 実行 | `runs` / `resume` / `status` | 実行の一覧／再開（処理中・エラーの作業を未処理に戻す。失敗した初回解析はやり直す）／進み具合 |
| | `new-run` | 実行フォルダと条件と、この実行で行う段（`--initial` / `--no-initial`） |
| | `tick` | 次にすることを返す（3.11）。`/coverity-run` のエージェントは、これに従って動く |
| | `initial-status` | 初回解析の状況（3.13）。裏のプロセスの本体 `initial-run` は `tick` が起動する（ヘルプには出さない） |
| | `plan` / `next` / `brief` / `finish` / `fail` | 作業の一覧（グループ化を含む）と、1 件ずつの準備・指示・仕上げ・失敗の記録。`/coverity-run` では `tick` が呼ぶ（エージェントは直接呼ばない） |
| | `verify` / `summary` | （オプション）ビルドでの検証／一覧 `summary.html` と承認の初期値（`approvals.json`） |
| 反映 | `preview` / `apply-code` | 反映の内容（`approvals.json` から）と確認用の文字列／修正の反映 |
| | `knowledge-candidates` / `add-knowledge` / `stats` | （オプション）知識の追記の提案／効果測定の集計 |
| 動作確認 | `selftest start` / `static` / `sample` / `check-run` / `check-apply` / `check-coverity` / `check-build` / `record` / `report` | 動作確認の準備・判定・記録・結果のまとめ |

- 引数は JSON を使わず、1 つずつの引数にする（例 `--impact High --impact Medium`）。Windows の PowerShell は、外部のコマンドに渡す JSON の `"` を崩すことがあるため。
- 結果は `{"ok": true, ...}`。できなかったときは `{"ok": false, "error": ..., "problems": [...]}` と終了コード 1。
- `--run` を持つコマンド（`gui`・`selftest` を除く）が `ok: false` で終わったときは、`main` がその実行の `operations.log` に `"event": "error"`（`command`、`error`、`problems`、あれば `item`）を記録する。level は、`finish` の `problems`（サブエージェントに直させる段階）なら `warning`、それ以外は `critical`。記録の失敗は結果に影響させない。
- `run.json` の読み書きはロックファイルで順番にする（同時に進める作業があるため）。git / svn のコピーの操作も同じ。
- ファイルは一時ファイル（名前にプロセスの番号を入れる）に書いてから置き換える。Windows では、ほかのプロセス（画面など）が読んでいる間は置き換えられないので、少し待ってやり直す（`common.write_json`）。

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
| `triage-investigation`・`checker-knowledge`・`code-fix`・`deviation-comment`・`triage-report` | 作業エージェント（`checker-knowledge` は統合・検証も、`deviation-comment` は統合も読む） | `user-invocable: false` |
| `triage-integration` | 統合エージェント | `user-invocable: false`。同じ原因かの決め方と、計画 `plan.json` の形（3.14） |
| `triage-review` | 検証エージェント | `user-invocable: false`。確かめ方と、`review.json` の形（3.15） |

入口のスキル同士は互いを読まない（`disable-model-invocation: true` のスキルは AI から読めないため）。共通の手順は `coverity-triage-scripts` と知識のスキルに置く。

### 3.5 サブエージェント（取得・作業・統合・検証）

4 つとも、メインのエージェントが**平らに**起動する（サブエージェントからサブエージェントは呼ばない）。frontmatter の次の部分は 4 つで同じ（違うのは `name`・`description`・`tools`）。

```yaml
model: GPT-6 Luna          # VS Code（Local）は表示名で探す
models: [gpt-6-luna]       # Copilot の実行基盤（Copilot CLI と VS Code の Copilot）は ID。model より優先される
modelPolicy: required      # 使えないときに黙って別のモデルにしない
user-invocable: false
```

| 定義 | 役 | 同時に | `tools` | 書いてよいもの |
|---|---|---|---|---|
| `coverity-triage-collector` | 取得：警告の検索と、警告経路の取得（決定 44） | 1 つまで | `[read, "coverity-triage/search_issues", "coverity-triage/get_issues"]` | なし（保存は MCP サーバがする） |
| `coverity-triage-worker` | 作業：警告 1 件（または 1 グループ）を調べ、修正案と逸脱コメント案を作る。差し戻しと、統合でまとめた作業の作り直しも | 設定 `parallel` の数まで | `[read, search, edit, skill]` | 修正用のコピー（オプションで注釈用のコピー）、`result.json`、`progress.json` |
| `coverity-triage-integrator` | 統合：同じ原因の作業をまとめる計画を書く。コードは書かない（決定 38） | 1 つまで | `[read, search, edit, skill]` | `integration/plan.json`、`agents/integrator/progress.json` |
| `coverity-triage-reviewer` | 検証：結果を独立に確かめる（決定 45。モデルは作業と同じ） | 1 つまで | `[read, search, edit, skill]` | `items/<ID>/review.json`、`agents/reviewer/progress.json` |

- `tools` は省かずに書く。省くと、すべてのツールが使えてしまう。MCP のツールは 1 つずつ書き、`coverity-triage/*` は使わない（書き戻しの `update_triage` まで見えるため。4 章）。
- どのエージェントも、必要な情報はすべて、スクリプトが書く指示ファイルで受け取る（作業は `items/<ID>/brief.md`、ほかは `agents/` の下）。メインのエージェントは、`tick` が返す `prompt`（指示ファイルの場所）をそのまま渡し、言い換えない。汎用のサブエージェントで代わりにもしない。
- 「1 つまで」と、同じ仕事で起動する回数は、手順書だけに頼らず、スクリプトが `run.json` の `roles` で守る（3.11、3.12）。
- 作業エージェントの `brief.md` には、警告の情報と警告経路（リポジトリ内のパスに直したもの）、調査用と修正用のフォルダ、結果ファイルの場所、知識のファイルの場所、ずれの確認結果を書く。差し戻した作業には「検証からの指摘（直すこと）」と前の結果 `result.previous.json` の場所を、統合で作り直す作業には「統合の指示」（元の作業の結果と差分、`merged.json` の場所）を足す。作業エージェントは、節目ごとに進み具合を `progress.json` に上書きする（できるときだけ。3.9）。
- 取得エージェントの指示ファイル（`agents/collector/search.md`、`agents/collector/paths-<n>.md`）には、呼ぶツールと引数の表と、返す 1 行の形だけを書く。判断や要約はさせない。ファイルも書かせない。
- 編集の機能（`edit`）は、書けるフォルダを絞れない。統合と検証には、書いてよいファイルのほかを変えないよう、定義と指示ファイルで伝える。検証は、起動したときと終わったときで結果ファイルと差分の指紋（SHA-256）を比べ、変わっていたら検証の結果を使わない（3.15）。
- 作業エージェントの名前は変えない（`~/.copilot/agents` に古い定義が残って呼ばれないように）。プラグインに無くなった古い定義は、`install-agent` が消す。

### 3.6 一覧と詳細レポート（`ctlib/report.py`）

HTML が本体で、表示専用（決定 27〜29）。どちらも 1 つで完結した HTML の文書にする（`<meta charset="utf-8">`、見た目の指定 `REPORT_CSS` は `<style>` で中に書き、スクリプトは使わない）。AI と Coverity から来た文字列はすべて `html.escape` で表示用に直す。リンクは実行フォルダの中の相対パスにし、フォルダから直接（`file://`）開いても、画面の中でも動くようにする。明るい／暗いテーマ（`prefers-color-scheme`）と印刷、狭い画面（表を縦のカードに組み替える）に合わせる。

- 一覧 `summary.html`（`render_summary_html`）：上に件数のタイル（警告、作業とグループ、修正の推奨、逸脱の推奨、確信度 低、エラー、未処理）。その下に、承認は画面で入れることと、いまの承認の内訳。表は `承認 | ID | 推奨 | 確信度 | 見立て | 詳細` の 6 列で、確信度の低い順。承認欄は `approvals.json` のいまの承認を「修正／逸脱／却下／（空）」で表示し、推奨から変えた行に印を付ける。グループは「G1（3 件）」と 1 行で表す。ID と詳細は `cid/<ID>.html` へのリンク。処理できなかった作業と未処理の作業は別の表にする。確信度は、人に見せる確信度（3.15）。統合で別の作業に取り込まれた作業（`merged`）は出さない（取り込んだ作業の詳細レポートに出る）。
- 詳細レポート `cid/<ID>.html`（`render_item_html`）：見出し（ID、チェッカー、場所、推奨・確信度・Impact・見立て・いまの承認のバッジ）、結論（1 行の見立て、推奨、確信度とその理由、リビジョンのずれ、注意）、警告の概要（CID・チェッカー・ファイル・関数・行・Impact・CWE の表）、真偽の根拠（警告経路。★ のイベントを目立たせる。調査結果と根拠の場所）、対応の案（案A: 逸脱 と 案B: 修正 を、広い画面では左右に並べる。推奨の案に印。人が画面で逸脱を直したときは直した内容を出し、AI の案は折りたたみで見せる）、修正の差分（**差分をそのまま載せる**。400 行まで。`<pre class="diff">` の中で行ごとに `<span class="add|del|hunk|meta">` で色分け）、ビルドでの検証（オプション）、独立した検証（オプション。結果、見解、確信度の上限、確かめた点）、処理情報（統合でまとめた作業なら「統合した作業」も）。差分ファイル（`../patches/<name>`）と修正後のファイル（`../fixed/<ID>/fix/<path>`）へリンクする。
- Markdown（`render_summary`、`render_item`）：設定 `options.markdown` が true のときだけ、HTML と同じ内容で `summary.md`、`cid/<ID>.md` も書く（決定 28）。形はこれまでどおり（一覧は 6 列の表、詳細は 1 結論 … 6 処理情報。逸脱の節は `<!-- ct:begin deviation -->` と `<!-- ct:end deviation -->` で囲む）だが、承認欄はいまの承認の表示で、書き換えても読まない（読むのは `approvals.json` の無い古い実行だけ。3.8）。
- 書き直すとき：`finish`・ビルドでの検証・検証エージェントの結果・統合で注意を足したあと（`write_item_files`）、`summary`・承認の変更のあと（`write_summary_files` と、変えた作業の `write_item_files`）。
- ビルドでの検証で問題が出た修正案（推奨が修正のもの）は、一覧とレポートで確信度を「低」として見せる（`result.json` は変えない。3.15）。ほかの修正案と同じ箇所を変えていて、まとめて確かめられなかっただけのものは下げない。修正前のコードをビルド・解析できなかったときは、どの修正案も「確かめられなかった」として下げない。修正前の解析にも出ていなかった警告は「消えた」に数えず、「確かめられない警告」として出す。

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
  initial: false            # true なら /coverity-run の最初に解析してサーバに登録する（初回解析。3.13）
  setup_command: ""         # ここから cov_analyze_args までは、ビルドでの検証と初回解析で共有する
  build_dir: ""
  build_command: ""
  cov_build_args: "--dir idir"
  cov_analyze_args: "--dir idir --all"
  cov_commit_args: ""       # 初回解析の cov-commit-defects に足す引数（--dir・--url・--stream・--user・--version はスクリプトが付ける）
  timeout_minutes: 180      # 初回解析を止めるまでの分
agents:                     # 作業のあとのオプションの段（既定はどちらも false。決定 46）
  integration: false        # 同じ原因の作業をまとめる（3.14）
  review: false             # 結果を独立に確かめる（3.15）
  max_rework: 1             # 検証から差し戻す回数の上限（作業ごと。0〜2。決定 47）
```

- `verify.initial`・`agents.integration`・`agents.review`・`agents.max_rework` は、`new-run` のときに `run.json` に写す。実行の途中で設定を変えても、その実行の段は変わらない。`new-run --initial` / `--no-initial` は、その実行だけ `verify.initial` を変える。

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
- `update`（画面の `POST /api/runs/<run_id>/approvals`）：終わった作業だけを受け付け、値を確かめてから（`decision` が上の 4 つのどれか、逸脱の 4 項目が空でない）書く。書き込みは `common.file_lock` の中で、`common.write_json`（原子的な書き込み）で行い、同じロックの中で、その作業の詳細レポートと一覧を作り直す。実行の途中でも、終わった作業は承認できる。ただし一覧は `ct.py summary` が初めて作る（ほかの作業に AI の推奨を入れるのも、そのとき）ため、それまでは詳細レポートだけを作り直し、`preview` は「一覧がありません」で断る。統合か検証を使う実行（`run.json` の `integration` か `review` が true）では、一覧ができるまで承認を受け付けない（統合と検証で、作業の中身や確信度が変わることがあるため）。統合で取り込まれた作業（`merged`）は、承認の対象にしない。
- `decisions` / `deviation_for`（`ct.py preview`）：承認と、Coverity に書く逸脱の内容（手直しがあればそれ、無ければ AI の案）。
- 古い実行（`approvals.json` が無く `summary.md` がある）：`summary.md` の承認欄と `cid/<ID>.md` の逸脱の節を、これまでどおり読む（書き間違いは `preview` の `problems`）。画面で承認を変えるか、`ct.py summary` をもう一度実行すると、その内容で `approvals.json` を作る（決定 30）。このとき、まだ無い `cid/<ID>.html` も作り、`summary.md` は今の承認で書き直す（古い表が、もう使われない承認を見せたままにならないように。`cid/<ID>.md` は人が書いたものとして残す）。

### 3.9 動きの記録と監視（`ctlib/oplog.py`、`ctlib/activity.py`）

画面の「監視」は、実行フォルダのファイルだけを読んで、エージェントの動きを組み立てる（決定 34・35）。どのファイルも書き換えない。

- `operations.log`：1 行に 1 つの JSON。`{"time": "...", "event": "next", "level": "info", "source": "ct", "item": "20001", ...}`。`level` は `info` / `warning` / `critical`（既定 `info`）、`source` は `ct`（スクリプト）か `mcp`（MCP サーバ、3.2）。`oplog.log(run_dir, event, secrets, level=..., source=..., **fields)` で書き、`oplog.read` は壊れた行を飛ばして読む。`ct.py fail` は `critical`、`ct.py` の失敗は 3.3 のとおり。
  - `tick`（`ctlib/flow.py`）が書く記録：`launch`（取得・統合・検証のエージェントを起動した。`role`、`task`、`item`、`round`）、`role-end`（そのエージェントが終わった。`role`、`task`、`outcome`（`done` / `failed`、検証は `pass` / `flag` / `rework`）、理由など）、`integrate`（`merges`、`conflicts`。候補が無かったときは `candidates`）、`paths-missing`（警告経路を取れなかった作業。`warning`）。
  - 初回解析の記録：`initial-start`（`pid`）、`initial`（`status`、`step`、`error`、`seconds`。失敗は `critical`）。
  - 作業エージェントの起動は、これまでどおり `next`（差し戻しの作業は `rework: true`）と `brief` で分かる。取得エージェントの動きは、MCP サーバの記録（`source: mcp`）で分かる。
- `items/<ID>/progress.json`：作業エージェントが節目ごとに編集の機能で丸ごと上書きする（できるときだけ）。`{"item": "20001", "phase": "judge", "note": "呼び出し元 read_config() を確認中"}`。`phase` は `read`（警告経路とコードを読む）→ `judge`（真偽を判断する）→ `fix`（修正を作る）→ `deviation`（逸脱コメントを書く）→ `result`（結果ファイルを書く）。サブエージェントは時刻を書けない（ターミナルが無い）ので、時刻はファイルの更新時刻を使う。場所と形は `brief.md` の「フォルダとファイル」の表で伝える。
- `agents/integrator/progress.json`・`agents/reviewer/progress.json`：統合と検証のエージェントが同じように書く。`phase` は、統合が `read`（結果と差分を読む）→ `compare`（同じ原因かを比べる）→ `write`（計画を書く）、検証が `read`（警告経路と結果を読む）→ `check`（コードで確かめる）→ `write`（検証の結果を書く）。そのエージェントを起動するとき、スクリプトが前のファイルを消す。取得エージェントは書かない（ファイルを書かないため）。
- `analysis/status.json`・`analysis/initial.log`：初回解析の状況とログ（3.13）。
- `activity.board(run)`：画面が 2 秒ごとに取る状態（`GET /api/runs/<run_id>/activity`）。
  - 実行の段階（`initial` / `search` / `plan` / `paths` / `items` / `integrate` / `review` / `verify` / `summary` / `apply` / `done`）と、メインのエージェントのいまの仕事（作業を配っている、取得・統合・検証のエージェントの終わりを待っている、初回解析の終わりを待っている など）と、最後の記録からの秒数。`apply` は、最後の `apply-code` より後の `preview` か `update_triage` の記録が 10 分以内のときだけ（人が反映に同意しなかったときは、そのあと何も記録されないため）。画面の実行の一覧も同じ決め方で、10 分より前から記録の無い終わった実行は、記録を読まずに `done` にする。
  - スロット（1〜`parallel`。空きも返す）ごとの作業：CID かグループ、何回目か、段階（その作業の最後の `next` 以降の記録で決める。`mcp:get_issues` の開始 → 警告経路を取得中、`brief` → 調査中、`finish` の `problems` → 結果を直している）、`progress.json` の `phase` と `note`、経過時間、最後の動きからの秒数。
  - 停止の疑い（`stalled`）：処理中の作業で、その作業の最後の記録・`progress.json`・作業のフォルダの中のファイルの更新時刻のうち、いちばん新しいものから 10 分（`STALL_SECONDS`）以上たったとき。注意にも入れる。
  - 初回解析（`initial`。オプション）：状況（待ち・実行中・登録まで終わった・失敗した）、いまの段（環境の設定・ビルド・解析・サーバへの登録）、経過時間。動きは `initial.log` の更新時刻で見て、30 分（`INITIAL_STALL_SECONDS`。ビルドは長く何も出さないことがあるため）増えなければ停止の疑いにする。
  - 取得・統合・検証のエージェント（`roles`）：取得は起動したことがあるとき、統合と検証はオプションがオンのときだけ出す。それぞれ、状態（待ち・動いている・メインの AI が代わりにしている・終わった・うまくいかなかった）、いまの仕事（警告の検索、警告経路の取得、まとめる、`<ID>` の検証）、進み具合（取得は警告経路のそろった作業の数、統合は `phase` の位置、検証は確かめた件数）、何回目か、経過時間。`run.json` の `roles`、`tick` の記録、進み具合のファイル、書いた計画と検証の結果の更新時刻で決め、10 分動きが無ければ停止の疑いにする。
  - 最近の動き（新しい順に 40 件。記録を日本語の 1 行にしたもの）。
- `activity.alerts(run)`：重大なエラーと注意（新しい順に 100 件まで）。元は、`operations.log` の `critical` / `warning` の行、`run.json` でエラーになった作業（理由）、`items/*/issue-*.json` の取れなかった警告経路、`apply-plan.result.json` の書き込めなかった逸脱、`verify.json` のビルドの失敗、初回解析の失敗（ログの最後つき）、検証できなかった作業（`unverified`）、`run.json` の `notes`（統合できなかった など）、停止の疑い（作業・取得・統合・検証・初回解析）。
- 画面のボードは、上にメインの枠、その下に、実行の順に初回解析・取得・作業（`parallel` の数）・統合・検証の枠を、罫線の文字で描く（等幅のフォント。使っていないものは出さない）。枠には作業や仕事、段階、進み具合の棒、経過時間を出し、停止の疑いは「⚠ 停止?」と色で示す。統合で取り込まれた作業（`merged`）は、件数に数えない。

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
| `GET /api/runs/<run_id>/summary` | 一覧の行（ID、題、CID、チェッカー、場所、Impact、推奨、確信度（人に見せる確信度。3.15）、見立て、承認、変えたか、状態、反映済みか、検証の結果、詳細レポートの場所）、エラー、未処理（統合で取り込まれた作業は出さない） |
| `GET /api/runs/<run_id>/items/<ID>` | 1 つの作業の行、いまの逸脱と AI の逸脱、差分ファイルと修正後のファイルの一覧、詳細レポートのアドレス |
| `POST /api/runs/<run_id>/approvals` | `{"item", "decision", "deviation"}` を 3.8 の `update` で保存する。反映済みの作業を変えたときは注意も返す（変更はできる）。統合か検証を使う実行では、一覧ができるまで断る |
| `GET /api/runs/<run_id>/activity` | 3.9 の `board` |
| `GET /files/<run_id>/<パス>` | 実行フォルダの中の `summary.html`、`cid/*.html`、`patches/*.patch`、`fixed/**` だけ（`..` やシンボリックリンクで外に出るものは 404）。`summary.html` と `cid/*.html` だけを HTML として返し、それ以外（修正後のファイルの中の HTML も）は `text/plain`（UTF-8 で読めず cp932 で読めるファイルは `charset=shift_jis`） |
| `GET /view/<run_id>/<パス>` | 差分は行ごとに色分け、修正後のファイルは行番号つきの表示用の HTML（サーバでエスケープして作る。スクリプトなし）。上に元のファイルへのリンクとパス |
| そのほか | 設定の `/api/state`・`/api/save`・`/api/credentials`・`/api/trial-build`、`/api/ping`、`/api/close`（「サーバを止める」） |

- AI の文章を含む HTML（`/files/` と `/view/`）には `Content-Security-Policy: sandbox allow-same-origin; default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'` を付け、スクリプトを動かさない。`allow-same-origin` は、レポートの中のリンク（`../patches/...` など）を開くときに Cookie を送るため（`allow-scripts` は付けないので、スクリプトは動かない）。
- 画面の本体（`/`）は、ほかのページの枠の中に出さない（`frame-ancestors 'none'`）。`/files/` と `/view/` は画面の枠の中に出すため、同じアドレスの中だけ許す（`X-Frame-Options: SAMEORIGIN`、`frame-ancestors 'self'`）。本体の CSP は `script-src 'unsafe-inline'`、`connect-src 'self'`、`frame-src 'self'`。

### 3.11 次にすることの決め方（`ct.py tick`、`ctlib/flow.py`）

`/coverity-run` の順番は、エージェントではなくスクリプトが決めます（決定 49）。エージェントは `tick` を呼び、返った `actions` を順に行い、サブエージェントが終わるたびに、その `task` を知らせて `tick` を呼び直します。

- 呼び方：`ct.py tick --run D`。サブエージェントが終わった（返事が返った）ら `--finished <task>`、途中で止まった・エラーになった・起動できなかったら `--failed <task> --reason "<理由>"`。複数が終わっていたら、1 つずつ知らせる。`--wait`（既定 120 秒）は、初回解析の間に 1 回で待つ最長の秒数。
- `task`：`collector:search`、`collector:paths`、`worker:<ID>`、`integrator`、`reviewer:<ID>`、`verify`。同じ仕事でもう一度起動したときは、回数を付ける（`worker:20001#2`）。エージェントは、actions の `task` をそのまま知らせる。同じ知らせが 2 回来ても、前の起動の知らせが遅れて来ても、何もしない（二重に起動しないため）。

結果：

| 項目 | 内容 |
|---|---|
| `stage` / `stage_label` | いまの段：`initial` / `search` / `paths` / `items` / `integrate` / `review` / `verify` / `done` |
| `actions` | いまからすること（下の表）。順に全部行う |
| `wait` | true なら、裏で動いているサブエージェント（`running_items`、`running_roles`）が終わるのを待つ。初回解析の間（`stage` が `initial`）は、そのまま `tick` を呼び直す |
| `done` | true なら、`ct.py summary` と結果の画面へ進む（`next` に書いてある） |
| `stop` | true なら、`say` と `error` を人に伝えて止める（初回解析の失敗。`initial` に状況） |
| `say` | 人に伝えること（件数、差し戻し、統合の結果、検証できなかった作業 など） |
| `counts`・`running_items`・`running_roles`・`next` | 進み具合、動いている作業とエージェント、次にすること |

| `actions` の `type` | 中身 | エージェントがすること |
|---|---|---|
| `launch` | `agent`（カスタムエージェントの名前）、`task`、`item`、`prompt`、`background: true` | その名前のカスタムエージェントを、`prompt` をそのまま渡して、裏で動かす指定で起動する（1 つ終わるごとに次へ進めるように。4 章） |
| `command` | `task`（`verify`）、`command`（`ct.py verify --run "D"`）、`note` | `ct.py` で実行し（裏で動かす指定で）、終わったら `tick --finished verify`。動いている間に `tick` を呼んでも、もう一度は返さない |
| `mcp` | `tool`、`args`、`until_done` | 取得エージェントが使えないときに、エージェントが代わりに MCP のツールを呼ぶ（`done` が true になるまで同じ引数で）。終わったら `tick` |

1 回の `tick` は、次の段を前から順に見て、まだ済んでいない最初の段で返ります。

1. 初回解析（`run.json` の `initial`）：3.13。
2. 検索：`issues.json` ができていなければ、取得エージェントに検索させる（指示ファイル `agents/collector/search.md`）。できていれば `ct.py plan` を呼んで作業の一覧を作り、件数を `say` に入れる。作業が 0 件なら `done`。
3. 警告経路：警告経路のファイルが無い未処理の作業があれば、取得エージェントに全部まとめて取らせる（`agents/collector/paths-<n>.md`）。取りに行くのは 2 回まで（`fetch_rounds`）。2 回目の前に、1 回目で取れなかった（`error` だけの）`issue-<CID>.json` を消して、取り直す。2 回目のあとも無い CID は、取れなかったことを `issue-<CID>.json` に書き（記録 `paths-missing`）、経路なしで進む。
4. 作業：空いた枠の数だけ `ct.py next` と `ct.py brief` を呼び、作業エージェントの `launch` を返す。途中の作業で修正用のコピーを用意できなかったときは、それまでに起動した分を返し、次の `tick` でもう一度ぶつかったら `ok: false` で止まる。`--finished worker:<ID>` では `ct.py finish` を呼び、`problems` があれば、直す点を添えて同じ作業でもう一度 `launch` を返す（2 回まで。`fix_rounds`。そのあとは `ct.py fail`）。`--failed worker:<ID>`（と、`problems` 以外の理由で `finish` できないとき）は `ct.py fail`（再開でやり直せる）。統合でまとめた作業が処理できなかったときは、その作業を消して、元の作業を `done` に戻す（記録 `integrate-undo`。`notes` に書く）。未処理か処理中の作業があるうちは、ここで返す。
5. 統合（`integration`）：3.14。
6. 検証（`review`）：検証の済んでいない、終わった作業を 1 件ずつ（3.15）。
7. ビルドでの検証（`verify_mode` が `none` 以外で、まだ済んでいない）：`ct.py verify` の `command` を返す。`verify` が終わると（確かめる修正案が無かったときも）`run.json` の `verified` が true になる。`--failed verify` のときも true にして、`notes` に「できなかった」と書く。検証エージェントがビルドの結果を読み直す段（叩き台にあった最後の確認）は作っていない。ビルドで問題が出た修正の推奨は、人に見せる確信度を「低」にするだけ（3.15）。
8. 全部済んだら `done`。

- 取得・統合・検証のエージェントは、それぞれ同時に 1 つまで（`roles` の `active`）。同じ仕事で起動するのは 2 回まで（`rounds`）。
- 取得エージェントが起動できない・止まった（`--failed collector:...`）、または 2 回起動しても検索が終わらないときは、`roles.collector.fallback` を true にし、そのあとは `mcp` の actions を返す（エージェントが代わりに取る）。
- 統合エージェントの計画と、検証エージェントの結果に直すところがあれば、直す点を添えてもう一度起動する。2 回とも通らないとき、止まったときは、そのエージェントの仕事を飛ばして進める（統合はまとめずに進め、`notes` に書く。検証はその作業を `unverified` にする）。夜間に人が見ていなくても、最後まで進めるため。
- `tick` が `ok: false` を返したとき（修正用のコピーを用意できないなど）は、エージェントは人に伝えて止める（動いているサブエージェントの終わりは待って知らせる）。

### 3.12 実行の状態 `run.json`（`ctlib/run.py`）

実行全体に足した項目：

| 項目 | 内容 |
|---|---|
| `initial`・`integration`・`review`・`max_rework` | この実行で行う段と、差し戻しの上限。`new-run` が設定（と `--initial` / `--no-initial`）から決める。再開しても変わらない |
| `fetch_rounds` | 取得エージェントに警告経路を取らせた回数（2 回まで） |
| `integrated` | 統合が済んだ（候補が無かった、統合できなかったときも true） |
| `verified` | ビルドでの検証が済んだ |
| `roles` | 取得・統合・検証のエージェントの、いまの仕事。`{役: {task, active, started, ended, rounds, fallback, item}}`。検証は `result_sha`・`patch_sha`（起動したときの結果ファイルと差分の指紋）も持つ |
| `notes` | 実行全体の注意（統合できなかった など。画面の「監視」に注意として出る） |

作業ごと（`items[]`）に足した項目：

| 項目 | 内容 |
|---|---|
| `status` | `pending` / `in_progress` / `done` / `error` に、`merged`（統合で別の作業に取り込まれた）を足した |
| `cluster` | グループ化で同じまとまりだった作業に同じ番号（10 件で切り分けた G1・G2 を、統合の候補にするため） |
| `fix_rounds` | この試みで、作業エージェントに結果ファイルを直させた回数（2 回まで。`next` で 0 に戻す） |
| `merged_from` / `merged_into` | 統合でできた作業の元の作業／取り込まれた先の作業 |
| `review` | 検証の結果：`pass` / `flag` / `rework` / `unverified`（null はまだ検証していない） |
| `review_rounds` | 検証から差し戻した回数 |
| `rework` | 差し戻しで直すこと（次の試みの `brief.md` に書く。`finish` で空にする） |

- `counts()` は、`merged` の作業があるときだけ `"merged"` のキーを持つ。
- `unfinished(state)`：`/coverity-run` に、まだすることがあるか（作業の一覧が無い、未処理・処理中・エラーの作業がある、統合が済んでいない、検証の済んでいない終わった作業がある）。`ct.py runs` と画面の実行の一覧が使う。
- `ct.py resume` は、`roles` の `active` をすべて false にする（止まったセッションのサブエージェントは、もういないため）。
- `finish` は、作業を `done` にするときに `review` を null に戻す（新しい結果は、もう一度検証する）。

### 3.13 初回解析（`ctlib/initial.py`。オプション `verify.initial`）

今まで人が手で行っていた「Coverity で解析してサーバに登録する」を、`/coverity-run` の最初に行います（決定 40〜43）。手元の解析の結果は、サーバに登録するだけで、トリアージには使いません。調べる警告、CID、警告経路は、これまでどおりサーバから取ります。

- 動かすコマンド（2 つのシェルの行。それぞれ `&&` でつなぐ）：

  ```
  1. <setup_command>（{root} は開いているフォルダ） → cd <build_dir>
       → cov-build <cov_build_args> <build_command> → cov-analyze <cov_analyze_args>
  2. <setup_command> → cd <build_dir>
       → cov-commit-defects --dir <中間ディレクトリ> --url <coverity.url> --stream <条件ファイルのストリーム>
                            --user <ユーザ名> --version <開いているフォルダの今のコミット> <cov_commit_args>
  ```

  - 解析するのは、開いているフォルダ（`repo_root`）をそのまま（決定 41）。ビルドの生成物と中間ディレクトリも、その中にできる（手で行っていたときと同じ）。初回解析は作業より前に終わるので、作業ごとに比べる「利用者のリポジトリの状態」には影響しない。
  - 中間ディレクトリは、`cov_analyze_args` の `--dir`。
  - ストリームは、条件ファイルのもの（そのあと警告を読むのと同じ）。別のストリームに登録すると、そのあとの検索で見えないため。
  - `--version` は、開いているフォルダの今のコミット（svn はリビジョン）。スナップショットの `sourceVersion`（設定 `revision_field`）に入り、そのあと `search_issues` が読む解析リビジョンと合う。
  - 認証キーは、2 行目（登録）にだけ、環境変数 `COVERITY_PASSPHRASE` で渡す（`cov-commit-defects` は `--password` が無いとき、これを読む。4 章）。1 行目（ビルドと解析）には、認証キーの環境変数を渡さない（ビルドの道具が環境変数をログに出すことがあるため）。コマンドラインには書かず、ログに残すコマンドの行は、認証情報の値を伏せる。ユーザ名は `coverity.user_env` の環境変数。どちらかが無ければ、ビルドの前に失敗にする。認証キーのファイルを使うサイトは、`cov_commit_args` で指定する。
  - ビルドには、スクリプトのために uv が作った Python の環境（`VIRTUAL_ENV` と、PATH のその `Scripts`）を渡さない（`common.build_env`。ビルドの中の `python` が、その環境を使わないように）。最後の検証（`ct.py verify`）も同じ。
  - 開いているフォルダに、コミットしていない変更（追跡しているファイル）があったかを `status.json` の `dirty` に残す。あったときは、`plan` が解析リビジョンを使わずに、手元のコードで調べてずれを確かめる（登録したリビジョンと、解析したコードが同じではないため）。
  - `coverity.api: fake` のときは、登録の代わりに `echo` で記録だけする（動作確認と自動テスト用）。
- 動かし方：`tick` が `initial.start` で、切り離したプロセス（`ct.py initial-run --run D`。画面のサーバと同じ `spawn_detached`。Windows では `CREATE_NO_WINDOW` で、窓を出さない）を起動する。プロセスは、段の区切りの印（`__CT_STEP__build` など）をログに出し、ログを少しずつ読み進めて、段が変わったときだけ `analysis/status.json` を書く。見張りのプロセスが例外で終わるときは、先にビルドを止める。シェル 1 回で数十分待てる保証が無いため、エージェントは短い `tick` を繰り返して待つ（1 回最長 120 秒）。
- `analysis/status.json`：`status`（`running` / `done` / `failed`。ファイルが無ければ `none`）、`step`（`setup` / `build` / `analyze` / `commit`）、`started`、`finished`、`pid`、`error`、登録できたときは `revision` と `stream`。`ct.py initial-status` は、これに経過秒数、段の名前、失敗したときはログの最後を足して返す。`running` なのにプロセスがいない（始めて 30 秒より後）とき、または時間の上限を 10 分過ぎても `running` のとき（プロセスの番号が別のプログラムに使い回された場合）は、失敗とみなす。ログの最後は、UTF-8 で読めなければ cp932 で読む（日本語の Windows のビルドの道具の出力）。
- 時間切れ：`verify.timeout_minutes`（既定 180 分）を過ぎたら、プロセスのまとまりごと止めて失敗にする（人が見ていない間に、固まったビルドを止めるため）。
- 待つ：登録まで終わってから、警告の検索を始める（決定 42）。
- 失敗したら：`tick` は `stop: true` を返し、トリアージを始めない（古い警告を調べないため。決定 43）。失敗した段とログの最後を人に伝える。設定を直して `/coverity-run` で再開すると、`resume` が失敗した状況を消し、初回解析からやり直す。登録まで済んでいれば、再開しても繰り返さない。
- 準備：`ct.py doctor` は、`verify.initial` が true のとき、ビルドのコマンド、`cov-build`・`cov-analyze`・`cov-commit-defects` が PATH にあるか、登録の認証情報があるかも確かめる。試しのビルド（`trial-build`）では登録しない。
- 最後の検証（`ct.py verify`）は、これまでどおりで、サーバには登録しない。

### 3.14 統合（オプション `agents.integration`）

作業の前のグループ化（`grouping.py`。機械的）は、これまでどおりです。作業の中では、作業エージェントが原因の違う CID をグループから外します（`group_excluded_cids`）。そのうえで、作業が全部終わったあとに、同じ原因の作業をまとめ直します（決定 38）。まとめすぎは作業エージェントが分け、まとめ不足は統合エージェントがまとめる、という両方向の補正です。

1. 候補を見つける（`flow.candidates`）：終わった作業（グループ化しない CID を含むものを除く）の組のうち、次のどれかに当たるもの。つながった組を 1 つの候補にし、理由と一緒に `integration/candidates.json` に書く。候補が無ければ、統合エージェントは起動せず、統合は済んだことにする。
   - グループ化で同じまとまりだった（`cluster` が同じ。10 件で切り分けた）
   - グループから外された CID と、元のグループ（またはその仲間）（`split_from`）
   - 修正の差分が、同じファイルの同じ行を変えている（差分の `@@` の範囲が重なる）
   - 根拠の場所（`verdict.evidence` のファイルと行）が同じ
2. 計画を書く（統合エージェント）：指示ファイル `agents/integrator/brief.md`（候補と理由、調べ終わった作業の表、結果ファイルと差分の場所、決まり）を読み、スキル `triage-integration` に沿って `integration/plan.json` を書く。候補に無い組でも、根拠を書いてまとめてよい。迷うときはまとめない。コードは変えない。

   ```json
   {"merges": [{"members": ["20011", "20012"], "reason": "...", "fix_source": "item:20011",
                "result": {"verdict": {"judgement": "true_bug", "summary": "..."}, "recommendation": "fix", "confidence": "high"}}],
    "conflicts": [{"items": ["20021", "20030"], "note": "..."}]}
   ```

   （`result` は一部だけ。全体の形はスキル `triage-integration`）

   - `result`：まとめた作業の見立て・推奨・確信度・逸脱案・修正案・ずれ（`result.json` と同じ形。`item` はスクリプトが足す）。逸脱コメントは、すべての CID に当てはまる 1 つの文にする。
   - `fix_source`：`item:<ID>`（その作業の修正だけで全部に効く）、`combine`（元の作業の差分を全部当てる）、`rework`（作業エージェントが作り直す）。
   - `conflicts`：原因は違うのに、修正が同じ箇所を変えている作業。まとめずに、それぞれの作業の注意（`warnings`）に書き、詳細レポートを作り直す。
3. 確かめてまとめる（`flow.integrate`。`tick --finished integrator` が呼ぶ）：
   - 決まり：まとめる作業は 2 つ以上、調べ終わった作業だけ、グループ化しない CID を含まない、1 つの作業は 1 つのまとまりだけ、CID は合わせて 10 件まで、`fix_source` は 3 つのどれか。外れていれば、直す点を添えて統合エージェントをもう一度起動する（3.11）。
   - 計画の全部が決まりに合うときだけ、まとめる。新しい作業と、元の作業の `merged` は 1 回でまとめて記録する（途中で失敗して、半分だけまとまることが無いように）。修正はそのあとで作り、作れないものは作業エージェントに任せる。
   - まとめるごとに、新しい作業 `G<次の番号>`（`merged_from` 付き）を作る。番号を G の続きにするので、ブランチ名（`coverity-fix/<実行 ID>-G<n>`）、レポート、グループの扱いがそのまま使える。元の作業の `issue-<CID>.json` を写し、元の作業は `merged`（`merged_into` 付き）にして、一覧と反映から外す（元の差分とレポートは記録として残る）。
   - 確信度は、計画の確信度と、元の作業のうち いちばん低い確信度の、低いほう。
   - `integration/merged.json` に、まとめた作業ごとの `members`・`reason`・`fix_source`・`result` を残す。
   - 修正：`item:<ID>` と `combine` は、スクリプトが元の差分を修正用のコピーに当てて、差分・ブランチ・修正後のファイル・詳細レポートを作り、作業を `done` にする。差分が当たらない、または何も変わらないときと、`rework`、オプション `annotation`（注釈を、まとめた逸脱コメントで書き直すため）のときは、作業を未処理のままにする。作業エージェントが `brief.md` の「統合の指示」（元の作業の結果と差分、`merged.json`）を読んで作り直す（2.2 の 9 に戻る）。
   - 記録：`integrate`（`merges`、`conflicts`）。

### 3.15 検証（オプション `agents.review`）と人に見せる確信度（`ctlib/assess.py`）

作業と統合が全部終わってから、終わった作業を 1 件ずつ、検証エージェントが独立に確かめます（決定 39）。作った者は、自分を検証しません。

- 指示ファイル `agents/reviewer/<ID>.md`（スクリプトが書く）：警告と警告経路、チェッカーの説明、調べるコード、確かめる結果（見立ての 1 行、推奨、確信度、根拠の場所、逸脱案、修正案、ずれ）、差分と修正後のファイルの場所。作業エージェントの考えた過程（`rationale`、`confidence_reason`）は渡さない（独立に判断させるため）。スクリプトが決まった方法で気づいたこと（根拠のファイルや行が無い、判定と推奨・確信度が合わない など。`flow._lint`）も書き、重みは検証エージェントが決める。統合でまとめた作業とグループには、すべての CID が同じ原因かも確かめるよう書く。
- 結果 `items/<ID>/review.json`（スキル `triage-review`）：

  ```json
  {"item": "20001", "outcome": "pass",
   "findings": [{"check": "根拠の場所", "ok": true, "detail": "...", "file": "src/reader.c", "line": 20}],
   "confidence_cap": null, "rework": [], "note": "人向けの 1〜2 文"}
  ```

  - `outcome` は、`pass`（合格）、`flag`（要確認。`confidence_cap` に確信度の上限）、`rework`（差し戻し。`rework` に直す点）の 3 つだけ。推奨は変えられず、結果ファイルとコードも変えない。
- 確かめ方（`tick --finished reviewer:<ID>` が行う）：
  - 形が正しくない、`item` が違う、`rework` なのに直す点が無いときは、直す点を添えてもう一度起動する（3.11）。
  - 起動したときと、結果ファイルか差分の指紋が違えば、検証の結果は使わない（検証の間に変わったため）。
  - 起動できない・止まった・2 回とも形が通らないときは、その作業を `unverified`（未検証）にして先へ進む。確信度は作業エージェントのまま。画面の「監視」に注意として出す。
- 差し戻し（決定 47）：`rework` で、差し戻した回数（`review_rounds`）が `max_rework`（既定 1）より少なければ、作業を未処理に戻す。次の `next` は、前の修正を修正用のコピーに当て、前の結果を `result.previous.json` に移す。`brief.md` に「検証からの指摘（直すこと）」を書く。作業エージェントが直して `finish` すると、もう一度検証する。上限に達したあとの `rework` は `flag` として扱い、「差し戻して直したあとも、指摘が残っています」と添えて人に任せる。差し戻しで直した作業は、前のブランチを消して、同じ名前で作り直す。
- 差し戻しで、前の修正を最新のコードに当てられないときは、止めずに最新のコードから作り直してもらう（`brief.md` にそう書く）。再開で同じ作業をやり直しても、検証が確かめた前の結果（`result.previous.json`）は上書きしない。
- 統合でまとめた作業から、作業エージェントが元の作業の CID を外したときは、その元の作業を未処理に戻して、個別に調べ直す。
- 統合と検証のエージェントは、コードや利用者のファイルを変えない決まり。起動したときと終わったときの、利用者のリポジトリの指紋を比べ、変わっていれば `say` で知らせる。
- ビルドでの検証の後は差し戻さない（直すと、ビルドと解析をやり直すことになり、朝までに終わらなくなるため）。

人に見せる確信度（`assess.effective`。決定 48）：

- 作業エージェントの確信度、検証の上限（`confidence_cap`。`flag` と `rework` で上限が書かれていなければ「低」）、ビルドでの検証（推奨が修正で `problems` があれば「低」）のうち、いちばん低いもの。下げるだけで、上げない。下げた理由は、確信度の理由の後ろに足す。
- 推奨が逸脱の作業は、ビルドでの検証の問題では下げない（修正は、もう一方の案にすぎないため）。
- `result.json` は、作業エージェントが書いたまま変えない（人に見せる値と、AI の記録を混ぜない）。一覧、詳細レポート、画面の「結果」、`ct.py verify` の `downgraded_to_low` は、この確信度を使う。

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

### 利用者の PC での試しで確かめたこと（Copilot CLI）

エージェントの構成を作り変える前に、利用者の PC の Copilot CLI で、試し用のエージェントを使って確かめました。公式の文書だけでは決められなかった点です。

| 項目 | 確かめたこと | 設計への反映 |
|---|---|---|
| 子のエージェントの MCP のツール | 子のエージェントの `tools` に `coverity-triage/check_connection` のように 1 つずつ書くと、そのツールだけが見える（中では `coverity-triage-check_connection` という名前になる）。`coverity-triage/*` と書くと、書き戻しの `update_triage` まで見える | 取得エージェントの `tools` は `coverity-triage/search_issues` と `coverity-triage/get_issues` を 1 つずつ書く。`*` は使わない（書き戻しを持たせないため。3.5） |
| 子のエージェントの確認の画面 | 子のエージェントが MCP のツールを呼んでも、確認の画面は出なかった（前に許可していた可能性はある） | 取得エージェントで止まる心配は小さい。新しい PC では、`/coverity-setup` で「このセッションでは許可」を案内する（2.1 の 10） |
| 同時に動かせる数 | 子のエージェントが 6 つ同時に動き、断られなかった | 既定の `parallel: 3` のままで足りる |
| 待ち方 | ふつうに起動すると、同時に起動した分が全部終わるまで次へ進めない。裏で動かす指定（background）なら、1 つ終わった時点で次へ進め、`read_agent` で様子も見られた | `tick` の `launch` は `background: true`。エージェントは裏で起動し、1 つ終わるごとに `tick` に知らせて次を受け取る（3.11） |
| `cov-commit-defects` の認証と版 | `--password` を省くと、環境変数 `COVERITY_PASSPHRASE` を読む。今の認証情報で、`--version` に解析したコミットを入れて、問題なく登録できた | 初回解析は、認証キーを `COVERITY_PASSPHRASE` で渡し、`--version` に今のコミットを入れる（3.13） |

## 5. 開発

開発の進め方（テスト、見本の作り直し、版数）は [development.md](development.md) にまとめる。
