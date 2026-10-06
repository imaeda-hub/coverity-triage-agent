# Coverity トリアージ

Coverity の警告を AI が 1 件ずつ調べ、警告ごとに **修正案** と **逸脱コメント案** の両方を作ります。
あなたは一覧を見て、どちらにするかを決めるだけです。決めたものだけが反映されます。

## 必要なもの

| もの | 確かめ方 |
|---|---|
| VS Code と GitHub Copilot（チャットでエージェントが使えること）、または GitHub Copilot CLI | Copilot のチャットが開ける |
| git か svn（いつも使っているもの） | `git --version` / `svn --version` |
| 社内の Coverity Connect のアカウントと認証キー | ブラウザで Coverity に入れる |
| Copilot で GPT-6 Luna が使えること | チャットのモデルの一覧に出る |

uv（このプラグインが使う Python の道具）は、無ければ準備のときに入ります。

## はじめての準備（20 分ほど）

1. **プラグインを入れる**
   - VS Code：設定 `chat.plugins.marketplaces` に `imaeda-hub/coverity-triage-agent` を足します。拡張機能のビューで `@agentPlugins` を検索し、`coverity-triage` を **Install** します。
   - Copilot CLI：次を実行します。
     ```
     copilot plugin marketplace add imaeda-hub/coverity-triage-agent
     copilot plugin install coverity-triage@coverity-triage-agent
     ```
2. **調べたいリポジトリを開き、チャットで `/coverity-setup` と入力します。** VS Code では `/coverity-triage:coverity-setup` と表示されます。
3. **AI の案内に答えます。** 聞かれるのは次のことだけです。
   - Coverity の URL、プロジェクト名、ストリーム名（チームで最初に使う人だけ。設定はコミットして共有します）
   - Coverity のユーザ名と認証キー（認証キーは、ターミナルに出る伏せ字の欄に入れます。チャットには書きません）
4. **再起動を頼まれたら**、VS Code（または Copilot CLI）を起動し直して、もう一度 `/coverity-setup` と入力します。続きから進みます。

「準備ができました」と出たら終わりです。

## 毎回の使い方

| | すること | 起きること |
|---|---|---|
| 1 | `/coverity-run` と入力する。例 `/coverity-run High だけ`、`/coverity-run 10 件だけ` | AI が警告を 1 件ずつ調べ、一覧 `summary.md` を作ります。最初に 1 回だけ、修正用のフォルダの読み書きを許可します。始めたら、終わるまで質問はしません |
| 2 | `summary.md` の **承認** 欄を見る | AI の推奨が入っています。変えたい行だけ「修正」「逸脱」「却下」に書き換えます。逸脱コメントは、各詳細レポートで直せます |
| 3 | `/coverity-apply` と入力する | 件数を確かめて同意すると、**逸脱** は Coverity に書き込まれ、**修正** はプルリクエストになります（svn は作業中のフォルダに入るので、確かめてコミットします） |

一覧は確信度の低い順に並びます。確信度が「高」で、1 行の見立てに納得できれば、そのまま承認してかまいません。
一覧とレポートの見本：[docs/sample-output](docs/sample-output/README.md)

途中で止まっても、もう一度 `/coverity-run` と入力すると続きから再開できます。
承認して反映するまで、あなたの作業中のファイルも Coverity も変わりません。

## 困ったとき

- `/coverity-help` に聞いてください。例：`/coverity-help このエラーは何？`、`/coverity-help MISRA だけ調べたい`
- この PC で正しく動くかは `/coverity-selftest` で確かめられます。結果は `report.md` にまとまるので、うまく動かないときは、その結果のフォルダをプラグインの管理者に渡してください（認証情報などは伏せてあります）。

---

開発する人向けの資料は [docs/](docs/development.md) にあります。
