---
name: coverity-selftest
description: このプラグインが利用者の PC（VS Code / Copilot CLI、社内の Coverity、社内のビルド）で動くかを確かめ、結果を report.md にまとめる。
argument-hint: 省略可。範囲 ①〜④（① 組み込み ② 偽データでの一連の流れ ③ 社内の Coverity ④ ビルドでの検証）。例 ①② / ③
disable-model-invocation: true
allowed-tools: ["coverity-triage", "shell(uv run:*)"]
---

# 動作確認

確認項目ごとに結果を記録し、最後に `report.md` を作ります。決まった判定は `ct.py selftest` が行い、あなたは手順を進めることと、スクリプトでは確かめられないこと（MCP のツール、サブエージェント、利用者への質問）を担います。

最初にスキル `coverity-triage-scripts` を読み、`ct.py` の場所と使い方を確かめます。以下の `ct.py X` はそこに書かれた方法で実行します。

## 守ること

- **社内の Coverity に書き込まない。** ③ では `ct.py preview`・`ct.py apply-code`・MCP の `update_triage` を使わない（反映を試すのは ② の偽データだけ）。
- 社内のリポジトリのファイルを変えない。認証キーをチャットで聞かない。
- エラーが出ても止まらずに、関係する項目を `ct.py selftest record --dir <dir> --id <項目> --status fail --actual "<エラーの内容>"` で記録して次へ進む。
- 利用者への質問は一度に 1 つ。答えはそのまま記録する。

## 始める

1. 範囲を決める（指定が無ければ ①〜④ すべて）。③④ は対象のリポジトリ（`/coverity-setup` 済み）を開いているときだけ。
2. 使っているもの（VS Code か Copilot CLI）が分からなければ、利用者に聞く。
3. `ct.py selftest start --sections <1,2,3,4 のうち> --client "<VS Code か Copilot CLI>" [--repo <リポジトリ>]` を実行する。結果の `dir` をこの後ずっと使う。
4. 「結果は `<dir>` にまとめます」と伝え、①→②→③→④ の順に進める。

## ① 組み込み

1. `ct.py selftest static --dir <dir>`（1-7：サブエージェントの定義と MCP サーバの Python 環境）。
2. ② で作る見本のリポジトリを先に作る：`ct.py selftest sample --dir <dir>`（2-1）。結果の `repo_root` を「見本のリポジトリ」とする。
3. 1-1：MCP の `check_connection` を見本のリポジトリで呼ぶ。`ok` が true なら pass、ツールが無い・失敗なら fail（エラーの内容を記録）。
4. サブエージェント `coverity-triage-worker` を名前を指定して起動し、次を渡す（汎用のサブエージェントで代わりにしない）。
   > これは動作確認です。作業はしません。次の 3 行だけを返してください。
   > 1 行目：`TOOLS: ` に続けて、あなたが使えるツールの名前をすべてカンマ区切りで（推測で足さない）
   > 2 行目：スキル `triage-investigation` を読み、`SKILL: ` に続けて、その本文の最初の見出し（`# ` の行）をそのまま
   > 3 行目：`MODEL: ` に続けて、あなたが動いているモデルの名前
   - 1-3：起動できて返事が返れば pass。起動できなければ fail（理由を記録）し、1-4〜1-6 は skip にする。
   - 1-4：`TOOLS` に、ターミナル（`execute`、`shell`、`run_in_terminal`、`bash`、`powershell` など）と `coverity-triage` のツールが無ければ pass、あれば fail。返事をそのまま `--actual` に書く。
   - 1-5：`SKILL` が `# 警告の真偽の調べ方` なら pass。
   - 1-6：`MODEL` が GPT-6 Luna なら pass、ほかのモデルなら fail、分からなければ review。
5. 1-2：利用者に「チャット欄に `/` を入力してください。`coverity-setup`・`coverity-run`・`coverity-apply`・`coverity-help`・`coverity-selftest` の 5 つは出ていますか？」と聞く（VS Code では `/coverity-triage:coverity-run` のようにプラグイン名が前に付く）。出ていれば pass、分からなければ review。表示された呼び方も記録する。

それぞれ `ct.py selftest record --dir <dir> --id <項目> --status <pass|fail|review|skip> --actual "<見たこと>"` で記録する。

## ② 偽データでの一連の流れ

見本のリポジトリ（① の 2 で作ったもの。① をしないときはここで `ct.py selftest sample` を実行）で、`/coverity-run` と同じ手順を進めます。条件ファイルは `sample` の結果の `filter`（`all.yaml`）です。

1. `ct.py new-run --repo <見本のリポジトリ> --filter all.yaml` → MCP の `search_issues` → `ct.py plan` → 作業ごとに `next` → `get_issues` → `brief` → サブエージェント `coverity-triage-worker` → `finish` → 最後に `ct.py summary`。
2. `ct.py selftest check-run --dir <dir> --run <run_dir>`（2-2〜2-6）。
3. 2-7：MCP の `update_triage` を、`ct.py preview --run <run_dir>` の `plan_file` と、わざと違う確認用の文字列 `000000000000` で呼ぶ。書き込みを断られれば pass、書き込んでしまったら fail。記録する。
4. 見本のリポジトリなので、利用者に聞かずに反映まで進める：`preview` の `confirmation_token` で MCP の `update_triage` を `done` まで呼び、`ct.py apply-code --run <run_dir> --token <confirmation_token>`。プルリクエストは作らない。
5. `ct.py selftest check-apply --dir <dir> --run <run_dir>`（2-8）。

## ③ 社内の Coverity（読み取りだけ）

対象のリポジトリで、警告 1 件だけを調べます。

1. 3-2：MCP の `check_connection` を `repo_root` で呼ぶ。`ok` が true で `seconds` が 10 以下なら pass。結果をそのまま記録する。失敗したら 3-3〜3-5 を skip にして ④ へ。
2. `ct.py new-run --repo <repo_root> --limit 1` → MCP の `search_issues` を `done` まで → `ct.py plan` → `ct.py next` → MCP の `get_issues` を `done` まで → `ct.py brief` → サブエージェント → `ct.py finish` → `ct.py summary`。検索が 0 件なら、そこで止めてよい。
3. `ct.py selftest check-coverity --dir <dir> --run <run_dir>`（3-1、3-3〜3-5）。

## ④ ビルドでの検証（オプション）

設定 `verify.build_command` があるときだけ行います。無ければ 4-1・4-2 を skip にします。

1. `ct.py selftest check-build --dir <dir>`（4-2：Coverity のコマンドが見つかるか）。
2. 4-1：「修正前のコードを試しにビルドします（時間がかかることがあります）」と伝え、`ct.py trial-build --repo <repo_root> --build-command "<設定の値>" [--setup-command ...] [--build-dir ...]` を実行する。`build_ok` が true なら pass。失敗したら `log_tail` の要点を記録する。

## 終わる

1. `ct.py selftest report --dir <dir>` を実行する。`not_recorded` に項目が残っていれば、記録し忘れていないか確かめる。
2. 次を短く伝える。
   - `report.md` の場所
   - 成功・失敗・要確認・未実施の件数と、失敗・要確認の項目（ID と 1 行）
   - 「ホスト名・ユーザ名・認証情報は伏せてあり、社内のソースコードは記録していません。ストリーム名やファイルのパスは残るので、確かめてからフォルダごと共有してください」
