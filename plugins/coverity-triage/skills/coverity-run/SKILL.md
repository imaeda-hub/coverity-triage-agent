---
name: coverity-run
description: Coverity のトリアージを実行する。条件に合う警告を取り出し、警告ごとにサブエージェント coverity-triage-worker に調べさせて修正案と逸脱コメント案を作り、一覧 summary.md を作る。途中で止まった実行は続きから再開する。
argument-hint: 省略可。条件ファイルの名前や追加の条件（例 misra.yaml / High だけ / 10 件だけ）
disable-model-invocation: true
allowed-tools: ["coverity-triage", "shell(uv run:*)"]
---

# Coverity トリアージの実行

あなたの仕事は、全体を順に進めることです。**ソースを読んで判断したり、直したりはしません。** 警告の調査と修正は、作業ごとにカスタムエージェント `coverity-triage-worker` をサブエージェントとして起動して任せます。

- 必ずこの名前のカスタムエージェントを起動します。汎用のサブエージェントで代わりにしたり、自分で調べたりしません（使えるツールとモデルが、この定義で決まっているため）。
- 起動できないときは、理由を利用者に伝えて止め、`/coverity-setup` を実行して、VS Code（または Copilot CLI）を再起動するよう案内します。
- 人への質問は、始める前の確認だけにします。始めたら、終わるまで質問しません（夜間に人がいなくても進められるように）。

最初にスキル `coverity-triage-scripts` を読み、`ct.py` の場所と使い方、MCP のツールとの関係を確かめます。以下の `ct.py X` はそこに書かれた方法で実行します。

## 1. 始める

1. 対象リポジトリの一番上のフォルダ（`repo_root`）を決める（ふつうは開いているフォルダ）。
2. `ct.py runs --repo <repo_root>` で前回の実行を見る。
   - 設定ファイルが無いなどで失敗したら、「先に `/coverity-setup` で準備してください」と伝えて終わる。
   - いちばん新しい実行が途中（`unfinished: true`）なら、「前回の実行（日時・条件）が途中です。続きから再開しますか？」と聞く。再開するなら `ct.py resume --run <run_dir>` を実行する。結果の `run_dir`、`repo_root`、`filter_file`、`issues_file`、`work_dir`、`verify` をこの後ずっと使い、`planned` が true なら 2 へ、false なら 3 の 3 から進める。
3. 新しく始めるとき：
   1. 利用者の言葉から条件を決める。条件ファイルの名前があれば `--filter`、「High だけ」などは `--impact High` のように引数で足す。件数の指定は `--limit`。ビルドでの検証の指定があれば `--verify build` など。
   2. `ct.py new-run --repo <repo_root> ...` を実行する。結果の `run_dir`、`repo_root`、`filter_file`、`issues_file`、`work_dir`、`verify` をこの後ずっと使う。
   3. 修正用のコピーを置くフォルダ（`work_dir`）は、開いているフォルダの外にあります。利用者に次を伝える（決定 21）。
      > 修正案は、あなたの作業中のファイルではなく、`<work_dir>` に用意するコピーの上で作ります。このフォルダの読み書きの確認が出たら、「このセッションでは許可」を選んでください（Copilot CLI では、いま `/add-dir <work_dir>` と入力しても許可できます）。
   4. MCP の `search_issues` を `repo_root`、`filter_file`、`output_file` = `issues_file` で、`done` が true になるまで呼ぶ。
   5. 見つかった件数（`found`）を伝える。`limited` が true なら「上限の `limit` 件で止めました」と添える。0 件なら、条件を伝えて終わる。
   6. `ct.py plan --run <run_dir>` を実行し、作業の数とグループの数を伝える。`notes` があれば短く伝える。

## 2. 作業を 1 つずつ進める

`ct.py next` の結果の `item` が null になるまで、次を繰り返します。

1. `ct.py next --run <run_dir>` で次の作業を取る。`ok: false` のとき（修正用のコピーを用意できないなど）は、ほかの作業でも同じことが起きるので、`error` を利用者に伝えて止める。
2. MCP の `get_issues` を `repo_root`、`stream`、`cids`、`output_dir` = `item_dir` で、`done` が true になるまで呼ぶ。
3. `ct.py brief --run <run_dir> --item <item>` を実行する。
4. サブエージェント `coverity-triage-worker` を起動し、結果の `prompt` をそのまま渡す。
5. サブエージェントが終わったら `ct.py finish --run <run_dir> --item <item>` を実行する。
   - `ok: false` で `problems` があるときは、`coverity-triage-worker` をもう一度起動し、`prompt` に続けて「結果ファイルの次の点を直してください：」と `problems` を渡す。2 回直しても通らなければ、`ct.py fail --run <run_dir> --item <item> --reason "<理由>"` を実行して次へ進む。
   - サブエージェントが途中で止まった、エラーになったときも `ct.py fail` で記録して次へ進む。
   - `warnings` があれば覚えておき、最後にまとめて伝える。
6. 5 件ごとくらいに、進み具合（`counts`）を 1 行で伝える。

`plan` の結果の `parallel` が 2 以上のときは、`next` をその数まで続けて呼び、サブエージェントを同時に起動してかまいません（作業ごとに別の修正用のコピーが用意されます）。

## 3. 終える

1. ビルドでの検証を使う実行（`verify` が none 以外）なら、「修正案をまとめてビルドで確かめます。時間がかかります」と伝えて `ct.py verify --run <run_dir>` を実行する。終わったら、ビルドできたかと、確信度を「低」に下げた作業（`downgraded_to_low`）を伝える。
2. `ct.py summary --run <run_dir>` を実行する。
3. 次を短く伝える。
   - 一覧のファイル（`summary`）の場所、作業の数、修正と逸脱の推奨の数、処理できなかった作業の数
   - 2 で覚えておいた `warnings`
   - 次にすること：「`summary.md` の承認欄を確かめてください。AI の推奨を入れてあるので、変えたい行だけ『修正』『逸脱』『却下』に書き換えます。逸脱コメントは各詳細レポートの『案A: 逸脱』で直せます。終わったら `/coverity-apply` で反映します」

## してはいけないこと

- ソースを読んで判断する、直す（サブエージェントの仕事）。
- `run_dir` の中のファイル（`run.json`、`result.json` など）を手で書き換える。
- `ct.py preview`・`ct.py apply-code`・MCP の `update_triage` を使う（反映は、人が承認欄を書いた後に `/coverity-apply` で行う）。
