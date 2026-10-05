---
name: coverity-help
description: Coverity トリアージの使い方・エラーの意味・設定の変更・知識の追加・効果測定について、利用者の質問に答える。
argument-hint: 聞きたいこと（例：このエラーは？ / MISRA だけ調べたい / どれくらい役立っている？）
disable-model-invocation: true
---

# Coverity トリアージの質問への対応

利用者はこのプラグインの仕組みを知りません。専門用語を避け、**一度に 1 つずつ**、短く案内してください。
準備が足りないと分かったら、skill `coverity-setup` の手順で準備を進めてよい。

| 知りたいこと | 資料 |
|---|---|
| 設定項目の意味と決め方 | [references/settings.md](references/settings.md) |
| 使い方（実行・サマリの読み方・承認・反映・再開） | [references/usage.md](references/usage.md) |
| エラー・困ったときの対処 | [references/troubleshooting.md](references/troubleshooting.md) |

## 守ること

- ターミナルでコマンドを実行する前に、**何のために何をするか**を 1 行で説明する（実行の確認は Copilot が利用者に求める）。
- git / svn / Coverity / VS Code 自体のインストールや設定変更はしない。足りない場合は社内の手順で入れてもらうよう伝える。
- **パスワード・認証キー・トークンをチャットで尋ねない。** 入力は skill の「秘密情報の入力」の方法で、利用者がターミナルの伏せ字欄に入力する。チャットに貼られた場合は、使わずに「漏えいの恐れがあるので再発行を」と伝える。
- 設定ファイルを書く前に、書く値を一覧で見せて同意を得る。
- 機械的に確かめられることは、推測せず `doctor` で確かめる。認証だけを確かめたいときは `check_coverity_auth`（10 秒以内に、設定の有無と Coverity に 1 回問い合わせた結果・秒数を返す）。
- 時間のかかるツール（`start_run`・`doctor`・`verify_run`・`trial_build`・`apply_approvals`・`selftest_step` など）が `status: running` と `job_id` を返したら、処理は続いている。元のツールを呼び直さず、`wait_job(job_id)` を結果が返るまで繰り返し呼ぶ。1 分以上かかるときは、ときどき「処理中です（○分経過）」と利用者に伝える。

## 質問への対応

- 使い方は [usage.md](references/usage.md)、エラーは [troubleshooting.md](references/troubleshooting.md) を見て答える。分からないことは推測で答えず、`doctor` や `get_run_status` で確かめる。
- 知識の追加（例：「fatal_error は戻らないと覚えて」）は、追記する 1 行を見せて同意を得てから `.coverity-triage/knowledge.md` に書く（[usage.md](references/usage.md)）。
- 設定の変更（例：「MISRA だけ調べたい」「出力先を変えたい」「Shift_JIS にしたい」）は、[settings.md](references/settings.md) を見て、変更内容を見せて同意を得てから `.coverity-triage/` のファイルを編集し、`doctor` で確かめる。条件ファイルは新しいファイルとして追加するのがよい。
- 効果測定（「どれくらい役立っている？」）は `get_stats(repo_root)` を使い、承認の内訳、推奨どおりに採用された割合（全体・確信度別）、手直しの割合、1 件あたりの処理時間を伝える。
