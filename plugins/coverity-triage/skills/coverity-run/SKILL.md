---
name: coverity-run
description: Coverity トリアージを実行する。条件に合う警告を取り出し、作業項目ごとにサブエージェント coverity-triage-worker に調査させ、一覧サマリを作る。未完了の実行があれば再開を提案する。
argument-hint: 省略可。条件ファイル名や追加条件（例：misra.yaml / High だけ / ビルドで検証して）
disable-model-invocation: true
---

# Coverity トリアージの実行

あなたの役割は、トリアージ全体の進行管理です。**ソースコードの調査・修正は自分で行わず**、作業項目ごとに、カスタムエージェント `coverity-triage-worker` をサブエージェントとして起動して任せます（作業項目ごとに新しいコンテキストで調査させるため。仕様 D-39）。
目的は人間のトリアージ工数の削減です。人間への質問は、開始前の確認と最後の報告に絞ってください。

## 開始

1. 対象リポジトリのルート（`repo_root`。通常は開いているワークスペースのフォルダ）を決める。
2. `list_runs(repo_root)` で最近の実行を確認する。
   - 設定ファイルが無いなどのエラーなら、「先に `/coverity-setup` で準備してください」と伝えて終了する。
   - 最新の実行が未完了（`unfinished: true`）なら、「前回の実行（日時・条件）が途中です。続きから再開しますか？」と尋ねる。再開なら `resume_run(run_dir)` を呼び、処理ループへ進む。
3. 新しく始める場合：
   - 条件ファイル：指定が無ければ `untriaged.yaml`。指定がファイル名でなければ（例「High だけ」）、既定の条件ファイルに `overrides` として加える（例 `{"impacts": ["High"]}`）。
   - 自動検証の指定（なし / ビルドのみ / ビルド＋再解析）があれば `verify_mode` に `none` / `build` / `build+analyze` で渡す。
   - `start_run` を呼ぶ。返ってきた `run_dir` は以降すべてで使う。
   - `found` と `processing` が違う場合は「上限件数で切った」ことを伝える。`note` があればそのまま伝える。
4. 処理ループへ進む。

## 処理ループ

1. `next_work_item` を、`parallel`（start_run / resume_run の戻り値）の数まで呼んで作業項目を取得する。
2. 取得した作業項目ごとに、サブエージェント `coverity-triage-worker` を起動する。渡す指示は次の形にする：
   > run_dir: `<run_dir>` / item_id: `<item>` を処理してください。
   - `parallel` が 2 以上なら、サブエージェントを並列に起動してよい。
3. サブエージェントが完了したら、次の作業項目を取得して 2 を繰り返す。`item` が `null` になったら終了。
4. サブエージェントが `submit_result` も `report_error` もせずに終わった場合は、`report_error` でその項目をエラーにする（理由を簡潔に書く）。

## 終了

1. 自動検証（`verify_mode` が `none` 以外。start_run / resume_run の戻り値で分かる）の場合：
   - 「全件の修正案をまとめてビルド（＋再解析）します。10〜60 分程度かかります」と伝え、`verify_run(run_dir)` を呼ぶ。
   - 終わったら、ビルドの成否と、問題が出て確信度を「低」に下げた作業項目（`downgraded_to_low`）を短く伝える。`error` があればそのまま伝える。
2. `build_summary` を呼ぶ。
3. 次の内容を短く報告する：
   - 一覧サマリのパス（`summary.md`）、処理件数、エラー件数、未処理があればその旨
   - 次の作業：「`summary.md` の承認列を確認してください（推奨案を下書き済み。変えたい行だけ 修正 / 逸脱 / 却下 に書き換え）。逸脱コメントの手直しは各詳細レポートで行えます。終わったら `/coverity-apply` で反映します」

## 禁止事項

- ソースコードを読んで判断したり、修正したりしない（サブエージェントの仕事）。
- `preview_apply` / `apply_approvals` は使わない（反映は、利用者が承認を記入した後に `/coverity-apply` で行う）。
