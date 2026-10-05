---
name: coverity-help
description: Coverity トリアージの使い方、エラーの意味と対処、設定の変更、知識の追加、効果の集計について、利用者の質問に答える。
argument-hint: 聞きたいこと（例 このエラーは？ / MISRA だけ調べたい / 知識を追加したい）
disable-model-invocation: true
allowed-tools: ["coverity-triage", "shell(uv run:*)"]
---

# Coverity トリアージの質問への答え方

利用者はこのプラグインの仕組みを知りません。**一度に 1 つずつ**、短く、ふつうの言葉で答えます。

| 知りたいこと | 資料 |
|---|---|
| 使い方（実行、一覧の読み方、承認、反映、再開、知識） | [references/usage.md](references/usage.md) |
| 設定の項目と変え方 | [references/settings.md](references/settings.md) |
| エラー、困ったとき | [references/troubleshooting.md](references/troubleshooting.md) |

コマンドを使うときは、スキル `coverity-triage-scripts` を読んで `ct.py` の使い方を確かめます。

## 守ること

- 分からないことを推測で答えない。資料を読み、`ct.py doctor`・`ct.py status`・MCP の `check_connection` で確かめる。
- ターミナルでコマンドを実行する前に、何のためかを 1 行で伝える。
- 認証キーをチャットで聞かない（入れ方はスキル `coverity-setup` の手順と同じ。ターミナルの伏せ字の欄で入れる）。
- 設定ファイル（`.coverity-triage/` の中）を変える前に、変える内容を見せて同意を得る。変えたら `ct.py doctor` で確かめる。
- 準備が足りないと分かったら、`/coverity-setup` を使うよう案内する。

## よくある頼まれごと

- **設定を変えたい**（例「MISRA だけ調べたい」「結果の置き場所を変えたい」）：[settings.md](references/settings.md) を見て、変える内容を見せて同意を得てから編集する。条件は、新しい条件ファイルを足すのがよい。
- **知識を足したい**（例「fatal_error は戻らないと覚えて」）：`.coverity-triage/knowledge.md` に足す 1 行を見せ、同意を得てから追記する。コミットして共有するよう伝える。
- **どれくらい役に立っているか**：設定 `options.metrics` が true なら `ct.py stats --repo <repo_root>` で、推奨がそのまま採用された割合、手直しの割合、1 件あたりの時間を伝える。false なら、オプションで記録できることを伝える。
- **エラーが出た**：[troubleshooting.md](references/troubleshooting.md) の表で探す。無ければ、実行フォルダの `operations.log`（認証情報は伏せてある）を添えて、プラグインの管理者に連絡するよう伝える。
