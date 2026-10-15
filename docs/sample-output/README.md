# 出力の見本

`/coverity-run` が作る一覧（`summary.html`）と詳細レポート（`cid/`）、差分（`patches/`）、修正後のファイル（`fixed/`）の見本です。試す前に、読みやすさや判断の材料が足りているかを確かめるためのものです。

**AI の文章は、人が書いた仮の文です。** 見立て、確信度、逸脱コメント、修正のコードは、レポートの形を見せるために手で書いたもので、Copilot（GPT-6 Luna）の出力ではありません。実際の文章の書きぶりや判断は、実行ごとに変わります。

一方で、表や節の並び、承認欄、差分の載せ方は、プラグインのスクリプトがそのまま作ったものです。見本は、偽の Coverity データ（`plugins/coverity-triage/skills/coverity-selftest/assets/sample-target`）を使い、次のコマンドで作り直せます。

```
uv run python tests/tools/make_sample_output.py
```

一覧と詳細レポートは HTML が本体です。見本は設定 `options.markdown` を true にして作ったので、同じ内容の Markdown もあります。GitHub の上では Markdown をそのまま読めます。HTML は、ファイルをダウンロードしてブラウザで開くと、実際の見た目（色分けした差分、左右に並んだ 2 つの案など）を確かめられます。

| ファイル（HTML） | Markdown | 内容 |
|---|---|---|
| [summary.html](summary.html) | [summary.md](summary.md) | 一覧（確信度の低い順。承認欄には、はじめに入れた AI の推奨を表示） |
| [cid/20001.html](cid/20001.html) | [cid/20001.md](cid/20001.md) | 本物のバグ（ファイルハンドルの漏れ）→ 修正を推奨 |
| [cid/20002.html](cid/20002.html) | [cid/20002.md](cid/20002.md) | 誤検知（呼び出し元で NULL を除いている）→ 逸脱を推奨 |
| [cid/20003.html](cid/20003.html) | [cid/20003.md](cid/20003.md) | 本物のバグ（malloc の戻り値を確かめていない）→ 修正を推奨、確信度 中 |
| [cid/G1.html](cid/G1.html) | [cid/G1.md](cid/G1.md) | MISRA C-2012 Rule 10.3 の 3 件を 1 つのグループにまとめた → 修正を推奨 |
| [patches/](patches/) | | 修正案の差分（詳細レポートからリンクしています） |
| [fixed/](fixed/) | | 修正後のファイル（リポジトリの構成のまま。詳細レポートからリンクしています） |

どちらのファイルも表示専用です。実際の承認は、画面（`ct.py gui`）の「結果」で選び、実行フォルダの `approvals.json` に保存されます。

実際の実行フォルダには、ほかに承認（`approvals.json`）、進み具合（`run.json`、`items/<ID>/progress.json`）、検索結果（`issues.json`）、警告ごとのフォルダ（`items/`）、操作の記録（`operations.log`）などがあります。
