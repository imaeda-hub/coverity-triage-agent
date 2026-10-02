# 試用・確認の手順書（会社に戻ってから実施）

この手順書では、次の 2 つを行います。所要時間の目安は合わせて 1〜2 時間です。

- **A. 最小プラグインの試用**（段階 1）：偽の Coverity データで、プラグインが VS Code と Copilot CLI で動くかを確認し、`docs/design.md` 7 章の確認事項を埋めます。社内 Coverity には接続しません。
- **B. API 調査スクリプトの実行**（段階 1'）：社内 Coverity Connect に読み取りのみで接続し、未決定事項 U-1（REST / SOAP）と U-2（認証方式）を決める材料を集めます。

結果は、各節の「記録」欄を埋めて共有してください。

---

## 0. 準備

コマンドの例は Windows のコマンドプロンプト（cmd）の書き方です。

| 必要なもの | 確認方法 |
|---|---|
| VS Code ＋ GitHub Copilot（エージェントモードが使えること） | Copilot Chat でエージェントを選べる |
| GitHub Copilot CLI | `copilot --version` |
| uv（Python 環境を自動で用意するツール） | `uv --version`。無ければ `powershell -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| git | `git --version` |
| このリポジトリ | `git clone` 済み |

## A. 最小プラグインの試用

### A-1. MCP サーバ単体の動作確認

プラグインに組み込む前に、サーバが起動できることを確認します。

```bat
cd <このリポジトリ>\plugins\coverity-triage\server
uv run pytest -q          # 24 passed になること（初回は依存パッケージを自動で入れます）
```

> 記録：`pytest` の結果（○ / ×、× ならエラーの末尾）

### A-2. 試用用の対象リポジトリを作る

```bat
xcopy /E /I <このリポジトリ>\examples\sample-target C:\work\sample-target
cd C:\work\sample-target
git init -b main
git add .
git commit -m "init"
```

`src/reader.c` には次の偽の警告が設定されています（`.coverity-triage/fake-issues.yaml`）。

| CID | チェッカー | 想定される結論 |
|---|---|---|
| 20001 | RESOURCE_LEAK | 本物のバグ（fgets 失敗時に fp を閉じていない）→ 修正推奨 |
| 20002 | FORWARD_NULL | 誤検知の可能性（呼び出し元で path を検証済み）→ 逸脱推奨 |
| 20003 | NULL_RETURNS | 本物のバグ（malloc の戻り値を確認していない）→ 修正推奨 |
| 20004〜20006 | MISRA C-2012 Rule 10.3 | 同じ関数の同じ違反 → グループ G1 にまとまる |

### A-3. プラグインをインストールする

`plugins/coverity-triage` フォルダをローカルのプラグインとしてインストールします。

- **VS Code**：コマンドパレットからエージェントプラグインのインストール（ローカルフォルダ指定）を実行する。
- **Copilot CLI**：プラグインのインストールコマンドでローカルパスを指定する。

正確な操作は一次資料（「Agent plugins in VS Code」「Creating a plugin for GitHub Copilot CLI」）に従ってください。

> 記録：インストールの手順（実際に使った操作・コマンド）、成功したか

### A-4. 確認事項（docs/design.md 7 章）

インストール後、次を 1 つずつ確認し、結果を記録してください。うまくいかない場合は、エラーメッセージと、一次資料に書かれている正しい書き方を記録してもらえれば、こちらで修正します。

| No | 確認すること | 確認方法 | 関係するファイル |
|---|---|---|---|
| C-1 | `plugin.json` が受け付けられるか（必須項目の不足がないか） | インストール時にエラーが出ないか | `plugin.json` |
| C-2 | MCP サーバが起動するか（`mcp.json` のプラグインルートの変数 `${PLUGIN_ROOT}` の書き方が正しいか） | Copilot の MCP サーバ一覧に `coverity-triage` が表示され、ツールが 18 個見えるか | `mcp.json` |
| C-3 | エージェントが選べるか | エージェントの一覧に `coverity-guide`、`coverity-triage`、`coverity-triage-apply` が出るか（`coverity-triage-worker` は一覧に出ない想定） | `com.github.copilot/agents/*.agent.md` |
| C-4 | モデルの固定が効くか | エージェント選択時のモデルが `gpt-6 luna` になるか。モデル名の正しい書き方も確認 | 各 `.agent.md` の `model:` |
| C-5 | ツールの制限が効くか | `coverity-triage` エージェントから `apply_approvals` が使えないこと | 各 `.agent.md` の `tools:` |
| C-6 | コマンドが使えるか | `/coverity-setup`・`/coverity-run`・`/coverity-apply`・`/coverity-help` の 4 つが表示されるか | `com.github.copilot/commands/*.md` |
| C-7 | サブエージェントが起動するか | 実行時に、作業項目ごとに `coverity-triage-worker` が別のコンテキストで動くか | `coverity-triage.agent.md` の `agents:`、`tools: [agent]` |
| C-8 | Skill が読み込まれるか | サブエージェントが `triage-investigation` などを参照しているか | `skills/*/SKILL.md` |
| C-9 | AI による準備の案内が動くか | 新しい PC（または uv を消した状態）で `/coverity-setup` を実行し、uv のインストール → MCP サーバの再起動 → 設定ファイルの作成 → 認証情報の伏せ字入力 → `doctor` がすべて ok、まで案内されるか | `agents/coverity-guide.agent.md`、`skills/coverity-guide/` |
| C-10 | 伏せ字入力がターミナルで使えるか | 認証キーの入力時に、Copilot のターミナルで伏せ字の入力欄に入力できるか（できない場合はコマンドを渡されて自分の PowerShell で実行する流れになるか） | `skills/coverity-guide/SKILL.md` 段階 3 |
| C-11 | 案内役のツール名 | `coverity-guide.agent.md` の `tools:` の `runCommands`・`editFiles`・`search` が、ターミナル実行・ファイル編集・検索の正しい名前か | `agents/coverity-guide.agent.md` |
| C-12 | 実際のビルドで自動検証が動くか | `/coverity-setup` の段階 5 で `envset.bat` の 2 つの引数（例 `envset.bat "{root}" <2つ目>`）と `make -f makefileXX` を設定し、試しのビルドが通るか。その後 `/coverity-run ビルドで検証して`（できれば再解析も）で、まとめた検証の結果がレポートに載るか。`cov-format-errors --json-output-v7` が社内のバージョンで使えるか | `server/src/coverity_triage/verify.py` |

確認は VS Code と Copilot CLI の**両方**で行ってください（仕様 D-2）。

### A-5. 一連の流れを試す

対象リポジトリ（`C:\work\sample-target`）を開いた状態で、次を実行します。

1. **トリアージ実行**：`/coverity-run all.yaml`
   - 期待：4 つの作業項目（20001、20002、20003、G1）が処理され、`C:\work\coverity-triage-out\<日時>\summary.md` ができる。
2. **結果の確認**：`summary.md` と `cid\*.md` を開く。
   - 期待：確信度の低い順に並び、承認列に推奨案が下書きされている。各レポートに修正案と逸脱コメント案の両方がある。
   - 確認：A-2 の表の「想定される結論」と合っているか。
3. **承認の記入**：`summary.md` の承認列を 1 つ「却下」に変え、どれか 1 つの詳細レポートで逸脱コメントを手直しする。
4. **承認の反映**：`/coverity-apply <実行フォルダ>`
   - 期待：反映前に件数が表示され、同意を求められる。同意すると、逸脱は `.coverity-triage\fake-issues.yaml.writes.jsonl` に書き戻しが記録される（偽データのため）。
   - 修正の反映は push とプルリクエスト作成になるため、リモートが無い試用環境ではエラーになります（想定どおり）。
5. **効果測定の集計**：`/coverity-help どれくらい役立っている？`
   - 期待：承認の内訳、採用率、手直しの割合が表示される。

> 記録：各手順の結果（○ / ×）、AI の判断が想定と違った CID とその内容、気になった点（遅い、質問が多い、レポートが読みにくい等）

---

## B. API 調査スクリプトの実行（読み取りのみ）

社内 Coverity Connect に接続します。**書き込みは一切行いません**。実行してよいかは、社内のルールに従って判断してください。

```bat
set COV_USER=<あなたのユーザ名>
set COV_AUTH_KEY=<認証キー（無ければパスワード）>
python <このリポジトリ>\tools\coverity_api_probe.py ^
    --url https://<Coverity サーバ>:<ポート> ^
    --project <プロジェクト名> --stream <ストリーム名> --cid <実在する CID を 1 つ> ^
    --out probe-report.md
```

- 社内 CA の証明書が必要な場合は `--ca-file <証明書ファイル>` を付けます。
- レポートではサーバのホスト名を `<coverity-host>` に置き換えています。認証キーも伏せています。
- 認証キーで失敗する場合は、パスワードでも試し、両方の結果を記録してください（U-2 の判断材料になります）。

> 記録：`probe-report.md` の内容（共有して問題ない範囲で）

### 調べたいこと（結果からこちらで判断します）

| 未決定事項 | 判断材料 |
|---|---|
| U-1 REST / SOAP | REST の列一覧と検索結果、SOAP の操作の有無（特に `getStreamDefects` のイベント、`updateTriageForCIDsInTriageStore`） |
| U-2 認証方式 | REST（Basic 認証）と SOAP（WS-Security）のそれぞれで、認証キー / パスワードで成功したか |
| D-17 リビジョンの記録先 | 最新スナップショットの項目（`description`、`sourceVersion` など）のうち、運用で使えそうなもの |
| アノテーションの書式（design 5.6） | Coverity のヘルプにあるコード注釈（`coverity[...]`）の書式。バージョンは `getVersion` の結果 |

---

## C. あわせて共有してほしいもの

- **U-3**：社内コーディング規約の資料（`skills/code-fix` に取り込みます）
- 社内 Coverity Connect の **Classification / Action / Severity の選択肢**（`skills/deviation-comment` の表を社内の値に合わせます）
