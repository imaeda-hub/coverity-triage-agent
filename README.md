# Coverity トリアージエージェント

Coverity の警告を AI が調査し、警告ごとに **「修正案」と「逸脱コメント案」** を作ります。
あなたは一覧を見て、承認するだけです。

## はじめに（初回だけ・約 15 分）

1. **プラグインを入れる**：VS Code の GitHub Copilot に、社内配布の `coverity-triage` プラグインをインストールします。（正確な操作は試用後に記載します）
2. **対象リポジトリを VS Code で開き、Copilot Chat で次を入力します。**

   ```
   /coverity-setup
   ```

   AI が、必要なものの準備から設定まで対話で案内します。あなたが答えるのは **Coverity の URL・プロジェクト名・ストリーム名** だけです。
   （git / svn / Coverity はすでに使える前提です）

## 毎回の使い方（3 ステップ）

| | やること | 何が起きるか |
|---|---|---|
| 1 | `/coverity-run` | AI が警告を調査し、一覧 `summary.md` を作ります（1 件数分） |
| 2 | `summary.md` の **「承認」列** を確認 | 推奨案が下書き済みです。変えたい行だけ `修正` / `逸脱` / `却下` に書き換えます |
| 3 | `/coverity-apply` | 件数を確認して「はい」。**修正**はプルリクエスト作成（svn はワーキングコピーに適用。コミットはご自身で）、**逸脱**は Coverity に登録されます |

一覧は確信度の低い順に並んでいます。確信度「高」は 1 行の見立てに納得できれば、そのまま承認して構いません。
出力の実物：[docs/sample-output/summary.md](docs/sample-output/summary.md)

## 困ったら

`/coverity-help` に日本語で聞いてください。例：

- `/coverity-help このエラーは何？`
- `/coverity-help MISRA の警告だけ調べたい`
- `/coverity-help どれくらい役立っている？`

## 安心して使うために

- 承認して反映するまで、あなたのコードも Coverity も **変更されません**。
- パスワードやトークンを **チャットに入力することはありません**（伏せ字の入力欄に入力します）。

---

<details>
<summary>開発者向け</summary>

| 資料・フォルダ | 内容 |
|---|---|
| [docs/spec.md](docs/spec.md) | 要件仕様 |
| [docs/design.md](docs/design.md) | 構成設計と実装の状況 |
| [docs/trial-guide.md](docs/trial-guide.md) | 開発時の試用・確認の手順 |
| `plugins/coverity-triage/` | プラグイン本体（エージェント、Skill、コマンド、MCP サーバ） |
| `examples/sample-target/` | 試用用の対象リポジトリ（偽の Coverity データ付き） |
| `tools/coverity_api_probe.py` | Coverity Connect の API 調査スクリプト（読み取りのみ） |

```bash
cd plugins/coverity-triage/server
uv run pytest -q
```

</details>
