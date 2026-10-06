---
name: coverity-triage-scripts
description: Coverity トリアージの決まった処理（実行フォルダ、グループ化、修正用のコピー、差分とブランチ、レポートと一覧、承認の読み取り、反映、準備の確認、動作確認）をするスクリプト ct.py の使い方。/coverity-setup・/coverity-run・/coverity-apply・/coverity-help・/coverity-selftest の手順で ct.py を使うときに読む。
user-invocable: false
---

# スクリプト ct.py の使い方

## 動かし方

このファイルと同じフォルダの `scripts/ct.py` を、ターミナルで uv から動かします。

```
uv run --native-tls <このスキルのフォルダ>/scripts/ct.py <コマンド> <引数>
```

- `<このスキルのフォルダ>` は、この SKILL.md があるフォルダの絶対パスです。会話の中で一度確かめたら、同じパスを使い続けます。
- uv をインストールした直後で `uv` が見つからないときは、uv のフルパスで動かします（Windows の既定は `%USERPROFILE%\.local\bin\uv.exe`）。
- 初回だけ、必要なライブラリを取ってくるため数十秒かかります。
- `--native-tls` は省かないでください。社内のネットワークでは、Windows に入っている社内の証明書を uv が使わないと、ライブラリを取れません。
- パスに空白があるときは `"..."` で囲みます。

## 結果の読み方

どのコマンドも、結果を JSON で 1 つ出します。

- `"ok": true`：できました。`next` に次にすることが書いてあります。
- `"ok": false`（終了コード 1）：できませんでした。`error` に理由、`problems` に直すところがあります。理由を読んで直すか、人に伝えます。勝手に別の方法で同じことをしようとしない（ファイルを手で書き換えるなど）。

## コマンド

引数の `--repo` は対象リポジトリの一番上のフォルダ、`--run` は `new-run` が返す実行フォルダ（`run_dir`）、`--item` は作業の ID（CID か `G1` などのグループ）です。

### 準備（/coverity-setup）

| コマンド | すること | 主な結果 |
|---|---|---|
| `prewarm` | MCP サーバの Python 環境を作る（初回の起動が時間切れにならないように） | `environment`、`seconds` |
| `doctor --repo R` | 準備の状況を調べる。Coverity への接続は調べない（MCP の `check_connection` で調べる） | `ready`、`checks`（`status` が ng のものを直す） |
| `detect --repo R` | git / svn、取り込み先のブランチ、出力先の候補を調べる | `vcs`、`base_branch`、`has_config`、`output_dir` |
| `init-config --repo R --url U --project P --stream S [--base-branch B] [--output-dir D] [--strip-prefix X]...` | 設定 `config.yaml`、最初の条件 `filters/untriaged.yaml`、知識 `knowledge.md` を作る。人に値を見せて同意を得てから実行する | `written` |
| `install-agent` | サブエージェントの定義を `~/.copilot/agents` に置く | `status`、`restart_needed` |
| `trial-build --repo R --build-command C [--setup-command S] [--build-dir D]` | （オプション）修正前の最新のコードを試しにビルドする。時間がかかる | `build_ok`、`log`、`log_tail` |
| `set-verify --repo R --build-command C [--setup-command S] [--build-dir D] [--default build]` | （オプション）ビルドでの検証の設定を書く | `written` |

### 実行（/coverity-run）

| コマンド | すること | 主な結果 |
|---|---|---|
| `runs --repo R` | 実行の一覧（新しい順） | `runs`（`unfinished` が true なら途中） |
| `resume --run D` | 止まった実行を続けられるようにする（処理中・エラーの作業を未処理に戻す） | `counts`、`next` |
| `new-run --repo R [--filter F] [--impact X]... [--checker X]... [--status X]... [--classification X]... [--action X]... [--limit N] [--verify none\|build\|build+analyze]` | 実行フォルダを作り、条件を決める。チャットで言われた条件は引数で足す（同じ項目は置き換え） | `run_dir`、`repo_root`、`filter_file`、`issues_file`、`work_dir`、`limit` |
| `plan --run D` | 検索結果（`issues.json`）から、パスの対応づけとグループ化をして作業の一覧を作る | `items`、`groups`、`analyzed_revision`、`notes` |
| `next --run D` | 次の作業を取り出し、修正用のコピーを用意する（初回は時間がかかる） | `item`（無ければ null）、`cids`、`stream`、`item_dir` |
| `brief --run D --item I` | サブエージェントへの指示ファイル `brief.md` を作る | `prompt`（サブエージェントにそのまま渡す）、`result_file` |
| `finish --run D --item I` | 結果ファイルを確かめ、差分・ブランチ・詳細レポートを作る | `recommendation`、`confidence`、`warnings`、`split_into` |
| `fail --run D --item I --reason "理由"` | 作業を処理できなかったと記録する（再開でやり直せる） | `counts` |
| `status --run D` | 進み具合 | `counts`、`errors` |
| `verify --run D` | （オプション）全部の修正案をまとめてビルドで確かめる。時間がかかる | `build_ok`、`downgraded_to_low` |
| `summary --run D` | 一覧 `summary.md` を作り、修正用のコピーを片付ける | `summary`、`recommended`、`errors` |

### 反映（/coverity-apply）

| コマンド | すること | 主な結果 |
|---|---|---|
| `preview --run D` | 一覧の承認欄と詳細レポートの逸脱の節を読み、反映の内容 `apply-plan.json` と確認用の文字列を作る | `counts`、`items`、`plan_file`、`confirmation_token` |
| `apply-code --run D --token T` | 修正を反映する（git：ブランチを push、svn：ワーキングコピーに差分を適用）。却下したグループは次から個別にする | `pull_requests`（`title`、`body`、`branch`、`compare_url`）、`svn_applied`、`coverity`、`errors` |
| `knowledge-candidates --run D` | （オプション）人が推奨を変えた・手直しした作業から、知識の候補を出す | `candidates`、`current_knowledge` |
| `add-knowledge --run D --entry "1 行の知識"...` | （オプション）人が選んだ知識を `knowledge.md` に追記する | `file` |
| `stats --repo R` | （オプション）効果測定の集計 | `recommendation_taken_rate` など |

### 動作確認（/coverity-selftest）

`selftest start | static | sample | check-run | check-apply | check-coverity | check-build | record | report`。使い方はスキル `coverity-selftest` に書いてあります。

## MCP のツールとの関係

Coverity との通信は MCP サーバ `coverity-triage` のツールが行い、結果を ct.py が示したファイルに保存します。

| MCP のツール | 渡すもの（ct.py の結果から） |
|---|---|
| `check_connection` | `repo_root` |
| `search_issues` | `repo_root`、`filter_file`、`output_file` = `issues_file`（`new-run` の結果） |
| `get_issues` | `repo_root`、`stream`、`cids`、`output_dir` = `item_dir`（`next` の結果） |
| `update_triage` | `repo_root`、`plan_file`、`confirmation_token`（`preview` の結果） |

MCP のツールが `done: false` を返したら、`done: true` になるまで同じ引数で呼び直します。
