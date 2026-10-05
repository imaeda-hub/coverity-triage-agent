# 出力サンプル

`/coverity-selftest` の偽データ（`plugins/coverity-triage/skills/coverity-selftest/assets/sample-target`）を、実際の MCP サーバ（stdio 接続）で処理した結果です。会社での試用の前に、一覧サマリと詳細レポートの読みやすさ・判断材料の過不足を確認するためのものです。

- 調査・判断（見立て、確信度、逸脱コメント、修正コード）は、開発時に Claude がサブエージェント役として行いました。実際の運用では Copilot（gpt-6 luna）が行うため、文章の書きぶりは変わります。
- レポートの体裁（章立て、一覧サマリ、承認列）は MCP サーバが生成したものそのままです。
- 実際の実行フォルダには、ほかに `fixed/`（修正後ファイル）、`results/`、`work/`、`run.json`、`operations.log` などがあります（docs/design.md 8.3 I-5）。

| ファイル | 内容 |
|---|---|
| [summary.md](summary.md) | 一覧サマリ（確信度の低い順、承認列に推奨案を下書き） |
| [cid/20001.md](cid/20001.md) | 本物のバグ（ファイルハンドルの漏れ）→ 修正推奨 |
| [cid/20002.md](cid/20002.md) | 誤検知（呼び出し元で NULL を除外済み）→ 逸脱推奨 |
| [cid/20003.md](cid/20003.md) | 本物のバグ（malloc の戻り値未確認）→ 修正推奨・確信度 中 |
| [cid/G1.md](cid/G1.md) | MISRA C-2012 Rule 10.3 の 3 件をグループ化 → 修正推奨 |
| [patches/](patches/) | 修正案の差分ファイル |
