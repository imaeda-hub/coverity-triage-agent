---
description: Coverity トリアージの初期設定（設定ファイル・条件ファイルのひな形を対象リポジトリに作る）
agent: coverity-triage
---

対象リポジトリに Coverity トリアージの設定ファイルのひな形を作ってください。

1. 対象リポジトリのルートと、VCS の種類（git / svn）を確認する（ワークスペースのフォルダに `.git` / `.svn` があればそれを使う）。
2. `init_project` を呼ぶ。
3. 作成されたファイルと、次に編集すべき項目を案内する：
   - `.coverity-triage/config.yaml`：Coverity サーバの URL、出力先（リポジトリの外を推奨）、ブランチの単位、検証コマンド
   - `.coverity-triage/filters/*.yaml`：絞り込み条件
   - 認証情報は環境変数（既定: `COV_USER`、`COV_AUTH_KEY`、`GITHUB_TOKEN`）に設定する
   - 試用時は `coverity.api: fake` のまま、`fake-issues.yaml` を実際のファイル・関数・行に合わせて書き換える
