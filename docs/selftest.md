# テスト手順書（社内の環境での動作確認）

社内の環境でプラグインが動くかを確かめる手順です。確認のほとんどは Skill `/coverity-selftest` が行い、結果を 1 つのフォルダにまとめます（仕様 D-81）。所要時間の目安は 1〜2 時間です（④ のビルドと再解析の時間を含む）。

確認は VS Code と Copilot CLI の**両方**で行ってください（仕様 D-2）。コマンドの例は Windows のコマンドプロンプト（cmd）の書き方です。

---

## 1. 必要なもの

| 必要なもの | 確認方法 |
|---|---|
| VS Code ＋ GitHub Copilot（エージェントモードが使えること） | Copilot Chat でエージェントを選べる |
| GitHub Copilot CLI | `copilot --version` |
| git | `git --version` |
| このリポジトリを読める GitHub アカウント | ブラウザで https://github.com/imaeda-hub/coverity-triage-agent を開ける |

## 2. リポジトリをクローンする

```bat
cd C:\work
git clone https://github.com/imaeda-hub/coverity-triage-agent.git
```

以降、`C:\work\coverity-triage-agent` を「このリポジトリ」と書きます。

## 3. プラグインを入れる

- **Copilot CLI**

  ```bat
  copilot plugin marketplace add C:\work\coverity-triage-agent
  copilot plugin install coverity-triage@coverity-triage-agent
  ```

- **VS Code**：設定 `chat.plugins.marketplaces` に `C:\work\coverity-triage-agent` を追加し、拡張機能ビューで `@agentPlugins` を検索して `coverity-triage` をインストールします。
- VS Code では、次の 2 つを有効にしておきます（サブエージェント `coverity-triage-worker` の起動に必要）。
  - チャットのツールで「Run Subagent」（`agent/runSubagent`）
  - 設定 `chat.customAgentInSubagent.enabled`（サブエージェントにカスタムエージェントを使わせる。実験的な設定）

参考：[Agent plugins in VS Code](https://code.visualstudio.com/docs/agent-customization/agent-plugins)、[GitHub Copilot CLI plugin reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference)

> 記録：実際に使った操作・コマンドと、成功したか（エラーが出た場合はその表示をそのまま）

## 4. `/coverity-selftest` を実行する

| 範囲 | 確かめること | 開いておくフォルダ |
|---|---|---|
| ① プラグインの組み込み | MCP サーバとツール、Python 環境の場所、サブエージェントの起動・ツールの制限・Skill の読み込み、`/` メニュー、モデル | どこでもよい |
| ② 偽データでの一連の流れ | 実行 → 結論の照合 → 反映の安全策 → 反映 → 知識の追記 → 効果測定。偽データは結果フォルダの中に作るので、開いているリポジトリには触れません | どこでもよい |
| ③ 社内 Coverity 接続（読み取りのみ） | doctor、REST / SOAP の接続と認証、列キー、警告の検索、警告経路、`sourceCodeInfo` の応答の形、スナップショットのリビジョン、実際の警告 1 件の調査 | `/coverity-setup` 済みの社内リポジトリ |
| ④ 実ビルドでの自動検証 | 試しのビルド、`cov-build` / `cov-analyze` / `cov-format-errors --json-output-v7`、修正案を当てたビルド＋再解析 | ③ と同じ（`/coverity-setup` の段階 5 で検証を設定済み） |

1. ③④ を行う場合は、社内リポジトリを開いて `/coverity-setup` を済ませる。
2. 社内リポジトリ（①② だけなら任意のフォルダ）を開き、`/coverity-selftest` を実行する。範囲を絞るときは `/coverity-selftest ①②` のように指定する。
3. 途中で、`/` メニューの表示やサブエージェントのモデル名を 1 つずつ聞かれるので答える。
4. 終わると結果フォルダ（既定は `%USERPROFILE%\coverity-triage-selftest\<日時>\`）の場所と、成功・失敗などの件数が表示される。

③ でも Coverity への書き込みは行いません。④ は 10〜60 分程度かかります。

## 5. 結果を共有する

結果フォルダには次の 2 つがあります。

- `report.md`：確認項目ごとの結果（成功・失敗・要確認・記録・未実施）、期待、実際。失敗・要確認は詳細と生データへのリンク付き
- `raw/`：原因分析のための生データ

フォルダごと共有してください。ホスト名・ユーザ名・認証情報は伏せ字にし、社内のソースコードは記録していませんが、ストリーム名やファイルのパスは残るので、確認してから共有してください。

## 6. 手作業で確かめること

`/coverity-selftest` では確かめられないものです。

| 確認すること | 確認方法 |
|---|---|
| AI による準備の案内 | 新しい PC（または uv を消した状態）で `/coverity-setup` を実行し、uv のインストール → MCP サーバの再起動 → 設定ファイルの作成 → 認証情報の伏せ字入力 → `doctor` がすべて ok、まで案内されるか |
| 伏せ字入力 | 認証キーの入力時に、Copilot のターミナルで伏せ字の入力欄に入力できるか（できない場合は、コマンドを渡されて自分の PowerShell で実行する流れになるか） |
| 入口の Skill の流れと使い勝手 | 社内リポジトリで `/coverity-run` → `summary.md` の確認 → `/coverity-apply` を一度通し、気になった点（遅い、質問が多い、レポートが読みにくい等）を記録する |

> 記録：各項目の結果（○ / ×）と、エラーの表示（そのまま）

## 7. あわせて共有してほしいもの

- **U-3**：社内コーディング規約の資料（`skills/code-fix` に取り込みます）
- 社内 Coverity Connect の **Classification / Action / Severity の選択肢**（`skills/deviation-comment` の表を社内の値に合わせます）
