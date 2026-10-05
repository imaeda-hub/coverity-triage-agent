---
name: coverity-apply
description: 一覧 summary.md の承認欄に従って反映する。逸脱は Coverity にトリアージを書き戻し、修正は git ならブランチを push してプルリクエストを作り、svn ならワーキングコピーに差分を当てる。
argument-hint: 省略可。実行フォルダ（省略時はいちばん新しい実行）
disable-model-invocation: true
allowed-tools: ["coverity-triage", "shell(uv run:*)"]
---

# 承認の反映

最初にスキル `coverity-triage-scripts` を読み、`ct.py` の場所と使い方を確かめます。以下の `ct.py X` はそこに書かれた方法で実行します。

## 1. 反映する内容を確かめる

1. 対象リポジトリの一番上のフォルダ（`repo_root`）を決める。
2. 実行フォルダ（`run_dir`）を決める。利用者が指定しなければ、`ct.py runs --repo <repo_root>` で一覧を見て、`summary` が true のうちいちばん新しいものにする。
3. `ct.py preview --run <run_dir>` を実行する。
   - `problems` があれば（承認欄の書き間違いなど）、そのまま伝えて、直してからもう一度 `/coverity-apply` を使うよう案内して終わる。
   - 件数がすべて 0 なら、「反映するものはありません（承認欄が空か、もう反映済みです）」と伝えて終わる。
4. 件数（修正・逸脱・却下）と、作業の ID を見せて、「この内容で反映しますか？」と聞く。逸脱は Coverity の分類とコメントが書き換わること、修正は git ならブランチを push すること（svn ならワーキングコピーのファイルが変わること）を添える。
5. 同意が得られなければ終わる。

## 2. 反映する

1. 逸脱が 1 件以上あれば、MCP の `update_triage` を `repo_root`、`plan_file`、`confirmation_token`（`preview` の結果）で、`done` が true になるまで呼ぶ。`failed` があれば、ID と理由を覚えておく。
2. `ct.py apply-code --run <run_dir> --token <confirmation_token>` を実行する。

## 3. プルリクエスト（git のとき）

`apply-code` の結果の `pull_requests` の項目ごとに、`branch` から `base` へのプルリクエストを、`title` と `body` で作ります。

- あなたが GitHub でプルリクエストを作るツールを使えるなら、それで作る（決定 3）。
- 使えないときは、`compare_url` を一覧で示し、「リンクを開くと、題名と本文が入った作成画面が出ます。内容を確かめて作成してください」と伝える。`compare_url` が null のとき（GitHub 以外のリモート）は、ブランチ名を伝える。

GitHub のトークンを利用者に聞いてはいけません。

## 4. 結果を伝える

次を短く伝えます。

- Coverity に書き戻した件数（`coverity.registered`）と、書き戻せなかったもの（`update_triage` の `failed`、`coverity.not_registered`）
- 作ったプルリクエスト（または作成用のリンク）
- svn のとき：`svn_applied` の差分をワーキングコピーに当てたこと。「内容を確かめてから、ご自身でコミットしてください」
- `errors` があればそのまま。反映できなかったものは、もう一度 `/coverity-apply` を使うと、残りだけを反映できること

## 5. オプション

設定 `options.knowledge_suggestions` が true のとき（`.coverity-triage/config.yaml`）：

1. `ct.py knowledge-candidates --run <run_dir>` を実行する。`candidates` が空なら何もしない。
2. 人が推奨を変えた、または逸脱コメントを直した作業から、ほかの警告にも使える短い知識（1 項目 1 行。例「read_config() は path を必ず検証してから渡す」）を考え、`current_knowledge` と重ならないものを示す。
3. 人が選んだものだけ、`ct.py add-knowledge --run <run_dir> --entry "<知識>"` で追記する（`--entry` は 1 項目ごと）。コミットして共有するよう伝える。

設定 `options.metrics` が true のときは、反映のたびに記録されます。集計は `ct.py stats --repo <repo_root>` で見られます。
