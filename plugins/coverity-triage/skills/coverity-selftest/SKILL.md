---
name: coverity-selftest
description: このプラグインが利用者の環境（VS Code / Copilot CLI、社内 Coverity、社内のビルド）で動くかを実際に動かして確かめ、原因分析に使える結果フォルダ（report.md と生データ）を作る。
argument-hint: 省略可。範囲 ①〜④（①組み込み ②偽データの流れ ③社内 Coverity ④実ビルドの検証）と結果の出力先（例：② / ③④ / ① D:\selftest）
disable-model-invocation: true
---

# プラグインの動作確認（動的テスト）

利用者の環境でプラグインを実際に動かし、確認項目ごとに結果を記録します（仕様 D-81）。判定と記録は `selftest_*` ツールが行います。あなたは、手順を進めることと、ツールでは確かめられないこと（サブエージェントの起動、利用者への確認）を担います。確認項目の一覧と期待する結果は、結果フォルダの `report.md` に出ます。

## 守ること

- **Coverity に書き込まない。** 社内のリポジトリで `preview_apply` / `apply_approvals` / `add_knowledge` を呼ばない（反映を試すのは ② の偽データだけで、`selftest_step` が行う）。
- 社内のリポジトリのファイルを編集しない。パスワード・認証キー・トークンをチャットで尋ねない。
- 途中でツールがエラーを返したら、止まらずに、関係する確認項目を `selftest_record(result_dir, 確認項目, "fail", エラーの内容)` で記録して次へ進む。
- 利用者への質問は**一度に 1 つ**。答えはそのまま記録する。

## 開始

1. 引数から範囲（①〜④、省略時はすべて）と出力先（省略時は既定）を読み取る。
2. `repo_root` は開いているワークスペースのフォルダ（無ければ省略）。使っている Copilot（`VS Code` か `Copilot CLI`）が分からなければ利用者に尋ねる。
3. `selftest_start(sections, repo_root, client, out_dir)` を呼ぶ。返ってきた `result_dir` を以降すべてで使う。`skipped` にある範囲は、理由を伝えて飛ばす。
4. 「結果は `<result_dir>` に保存します」と伝え、選ばれた範囲を ①→②→③→④ の順に進める。

## ① プラグインの組み込み

1. **ツール**：あなたに見えている `coverity-triage` の MCP ツールの名前をすべて、カンマ区切りで `selftest_step("plugin", result_dir, answer=名前の一覧)` に渡す（1-1、1-2）。
2. **サブエージェント**：カスタムエージェント `coverity-triage-worker` を名前で指定してサブエージェントとして起動し、次の指示を渡す（汎用のサブエージェントで代用しない）。
   > これは動作確認です。作業項目の処理はしません。次の 2 行だけを返して終了してください。
   > 1 行目：`TOOLS: ` に続けて、あなたが使えるツールの名前をすべてカンマ区切りで（推測で足さない）
   > 2 行目：Skill `triage-investigation` を読み込み、`SKILL: ` に続けて、その本文の最初の見出し（`# ` の行）をそのまま
   - 返答をそのまま `selftest_step("worker", result_dir, answer=返答)` に渡す（1-5〜1-7）。
   - 起動できなかった場合は、理由（例：サブエージェントを起動するツールが無効、カスタムエージェントを名前で指定できない）を `selftest_record(result_dir, "1-5", "fail", 理由)` で記録する。1-6・1-7・1-8 はツールが「未実施」と記録するので、1-8 の質問はしない。
3. **利用者への確認**（1 つずつ尋ね、`selftest_record` で記録する。分からないという答えは `review`）
   - 1-3：「チャット欄に `/` を入力してください。`coverity-setup`・`coverity-run`・`coverity-apply`・`coverity-help`・`coverity-selftest` の 5 つは出ていますか？ また、`triage-investigation`・`checker-knowledge`・`code-fix`・`deviation-comment`・`triage-report` が出ていないか確認してください」。VS Code ではプラグイン名が前に付く（`/coverity-triage:coverity-run` など）。表示された呼び方も `actual` に書く。
   - 1-4：「エージェントを選ぶ一覧に `coverity-triage-worker` が出ていないか確認してください」
   - 1-8：「さきほどのサブエージェントの実行の表示（またはログ）に、モデル名は出ていますか？ 出ていれば教えてください」。`gpt-6 luna` なら `pass`、別のモデルなら `fail`、表示が無ければ `review`。

## ② 偽データでの一連の流れ

1. `selftest_step("sample", result_dir)` で偽データの作業リポジトリを作る（2-1）。返ってきた `repo_root`（以下「偽データのリポジトリ」）と `filter_file` を使う。
2. `start_run(偽データのリポジトリ, filter_file)` を呼び、`/coverity-run` と同じ処理ループで全件を処理する：`next_work_item` で作業項目を取り、項目ごとにサブエージェント `coverity-triage-worker` を起動して「run_dir: `<run_dir>` / item_id: `<item>` を処理してください。」と指示する。`item` が `null` になったら `build_summary(run_dir)` を呼ぶ。サブエージェントが提出せずに終わった項目は `report_error` で記録する。
3. `selftest_step("flow_run", result_dir, run_dir)`（2-2〜2-5）。
4. `selftest_step("flow_apply", result_dir, run_dir)`（2-6、2-7）。このツールが利用者の代わりに承認列と逸脱コメントを変えて反映する。
5. 返ってきた `knowledge_candidates` から、`/coverity-apply` の「知識の追記の提案」と同じ考え方で、次回の調査に使える一般的な知識を 1 行ずつ下書きし、`add_knowledge(run_dir, 下書き)` で追記する。**偽データのリポジトリなので、利用者の承認は不要。**
6. `selftest_step("flow_knowledge", result_dir, run_dir)`（2-8、2-9）。

## ③ 社内 Coverity 接続（読み取りのみ）

1. `selftest_step("coverity", result_dir, repo_root=repo_root)`（3-1〜3-8）。
2. 返ってきた `filter_file` が `null` なら、`selftest_record(result_dir, "3-9", "skip", "検索結果が 0 件のため")` を記録して ④ へ。
3. そうでなければ、実際の警告 1 件で試す：`start_run(repo_root, filter_file, overrides={"max_items": 1}, verify_mode="none")` を呼び、② の 2 と同じ処理ループで 1 件を処理して `build_summary` を呼ぶ。続けて `selftest_step("real_run", result_dir, run_dir)`（3-9）。この `run_dir` は ④ で使う。

## ④ 実ビルドでの自動検証

1. ③ を行っていない場合は、③ の 3 と同じ手順で 1 件のトリアージを行う（`selftest_step("real_run")` は呼ばない）。
2. `selftest_step("build", result_dir, repo_root=repo_root)`（4-1〜4-3）。
3. 返ってきた `ready` が `false` なら、`selftest_record(result_dir, "4-4", "skip", "試しのビルドまたは Coverity のコマンドが使えないため")` を記録して終了へ。
4. 3 の `run_dir` の修正案が無い場合（調査がエラーになった等）は、`4-4` を `skip` で記録して終了へ。
5. 「修正案を当ててビルドと再解析をします。10〜60 分程度かかります」と伝え、`verify_run(run_dir, "build+analyze")` を呼ぶ。続けて `selftest_step("verify", result_dir, run_dir)`（4-4）。

## 終了

次を短く報告する。

- 結果フォルダ `<result_dir>` と `report.md` のパス
- 成功・失敗・要確認・記録・未実施の件数と、失敗・要確認の項目（ID と 1 行の内容）
- 「ホスト名・ユーザ名・認証情報は伏せ字にし、社内のソースコードは記録していません。ストリーム名やファイルのパスは残るので、確認してからフォルダごと共有してください」
