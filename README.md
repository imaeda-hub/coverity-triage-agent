# coverity-triage-agent

Coverity Connect の警告トリアージを代替し、人間の作業工数を削減する GitHub Copilot 用エージェントプラグイン（Agent Plugins 1.0）です。
警告ごとに「ソースコードの修正案」と「逸脱コメント案」の両方を作り、人間がどちらを採用するかを判断しやすい材料を添えます。

## はじめての方へ

| あなたは | 読む手順書 |
|---|---|
| チームで最初に設定する担当者 | [導入手順書（docs/setup-guide.md）](docs/setup-guide.md) |
| 日々トリアージに使う人 | [利用手順書（docs/user-guide.md）](docs/user-guide.md) |

出力の実物は [docs/sample-output/](docs/sample-output/) で見られます。

## 開発者向けの資料

| 資料 | 内容 |
|---|---|
| [docs/spec.md](docs/spec.md) | 要件仕様（決定事項・未決定事項） |
| [docs/design.md](docs/design.md) | 構成設計と実装の状況 |
| [docs/trial-guide.md](docs/trial-guide.md) | 開発時の試用・確認の手順（C-1〜C-8 の確認、API 調査） |

| フォルダ | 内容 |
|---|---|
| `plugins/coverity-triage/` | プラグイン本体（エージェント、Skill、コマンド、MCP サーバ） |
| `examples/sample-target/` | 試用用の対象リポジトリ（偽の Coverity データ付き） |
| `tools/coverity_api_probe.py` | Coverity Connect の API 調査スクリプト（読み取りのみ） |

## 開発

```bash
cd plugins/coverity-triage/server
uv run pytest -q
```
