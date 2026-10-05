# 試用・確認の手順書（会社で実施）

社内の環境でプラグインが動くかを確かめます。確認のほとんどは Skill `/coverity-selftest` が行い、結果を 1 つのフォルダにまとめます（仕様 D-81）。所要時間の目安は 1〜2 時間です（④ のビルドと再解析の時間を含む）。

確認は VS Code と Copilot CLI の**両方**で行ってください（仕様 D-2）。

---

## 0. 準備

| 必要なもの | 確認方法 |
|---|---|
| VS Code ＋ GitHub Copilot（エージェントモードが使えること） | Copilot Chat でエージェントを選べる |
| GitHub Copilot CLI | `copilot --version` |
| git | `git --version` |

VS Code では、チャットのツールで「Run Subagent」（`agent/runSubagent`）を有効にしておきます（サブエージェントの起動に必要）。

## 1. プラグインを入れる

- **Copilot CLI**：`copilot plugin marketplace add <このリポジトリ>`（clone したフォルダ、または GitHub の `<組織>/<リポジトリ>`）の後に `copilot plugin install coverity-triage@coverity-triage-agent`。
- **VS Code**：設定 `chat.plugins.marketplaces` にこのリポジトリを追加し、拡張機能ビューで `@agentPlugins` を検索してインストール。

参考：[Agent plugins in VS Code](https://code.visualstudio.com/docs/agent-customization/agent-plugins)、[GitHub Copilot CLI plugin reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference)

> 記録：実際に使った操作・コマンドと、成功したか（インストール時のエラーは C-1 の判断材料）

## 2. `/coverity-selftest` を実行する

| 範囲 | 確かめること | 開いておくフォルダ |
|---|---|---|
| ① プラグインの組み込み | MCP サーバとツール、Python 環境の場所、サブエージェントの起動・ツールの制限・Skill の読み込み、`/` メニュー、モデル（C-1〜C-8） | どこでもよい |
| ② 偽データでの一連の流れ | 実行 → 結論の照合 → 反映の安全策 → 反映 → 知識の追記 → 効果測定（A-5、C-11）。偽データは結果フォルダの中に作るので、開いているリポジトリには触れません | どこでもよい |
| ③ 社内 Coverity 接続（読み取りのみ） | doctor、REST / SOAP の接続と認証、列キー、警告の検索、警告経路、`sourceCodeInfo` の応答の形、スナップショットのリビジョン、実際の警告 1 件の調査（B、B-2、U-1、U-2、D-17） | `/coverity-setup` 済みの社内リポジトリ |
| ④ 実ビルドでの自動検証 | 試しのビルド、`cov-build` / `cov-analyze` / `cov-format-errors --json-output-v7`、③ の修正案を当てたビルド＋再解析（C-12） | ③ と同じ（`/coverity-setup` の段階 5 で検証を設定済み） |

1. 社内リポジトリで `/coverity-setup` を済ませる（③④ を行う場合）。
2. そのリポジトリを開き、`/coverity-selftest` を実行する（範囲を絞るときは `/coverity-selftest ①②` のように指定）。
3. 途中で、`/` メニューの表示やサブエージェントのモデル名を 1 つずつ聞かれるので答える。
4. 終わると結果フォルダ（既定は `~/coverity-triage-selftest/<日時>/`）の場所が表示される。

③ でも Coverity への書き込みは行いません。④ は 10〜60 分程度かかります。

> 共有：結果フォルダ（`report.md` と `raw/`）をフォルダごと共有してください。ホスト名・ユーザ名・認証情報は伏せ字にし、社内のソースコードは記録していませんが、ストリーム名やファイルのパスは残るので、確認してから共有してください。

## 3. 手作業で確かめること

`/coverity-selftest` では確かめられないものです。

| No | 確認すること | 確認方法 |
|---|---|---|
| C-9 | AI による準備の案内 | 新しい PC（または uv を消した状態）で `/coverity-setup` を実行し、uv のインストール → MCP サーバの再起動 → 設定ファイルの作成 → 認証情報の伏せ字入力 → `doctor` がすべて ok、まで案内されるか |
| C-10 | 伏せ字入力 | 認証キーの入力時に、Copilot のターミナルで伏せ字の入力欄に入力できるか（できない場合は、コマンドを渡されて自分の PowerShell で実行する流れになるか） |
| ― | 実際の使い勝手 | 社内リポジトリで `/coverity-run` → `summary.md` の確認 → `/coverity-apply` を一度通し、気になった点（遅い、質問が多い、レポートが読みにくい等）を記録する |

> 記録：各項目の結果（○ / ×）と、エラーの表示（そのまま）

---

## 4. あわせて共有してほしいもの

- **U-3**：社内コーディング規約の資料（`skills/code-fix` に取り込みます）
- 社内 Coverity Connect の **Classification / Action / Severity の選択肢**（`skills/deviation-comment` の表を社内の値に合わせます）
