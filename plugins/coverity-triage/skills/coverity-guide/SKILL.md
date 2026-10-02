---
name: coverity-guide
description: Coverity トリアージエージェントの準備（環境の準備、設定ファイルの作成、認証情報の入力、接続確認）と、利用者からの質問（使い方、エラーの意味、設定の変更、効果測定）に対応するための手順と知識。
---

# 案内役の手順と知識

| 知りたいこと | 資料 |
|---|---|
| 設定項目の意味と決め方 | [references/settings.md](references/settings.md) |
| 使い方（実行・サマリの読み方・承認・反映・再開） | [references/usage.md](references/usage.md) |
| エラー・困ったときの対処 | [references/troubleshooting.md](references/troubleshooting.md) |

## 準備の流れ（/coverity-setup）

対象リポジトリのルート（`.git` または `.svn` があるフォルダ）を `repo_root` とする。開いているワークスペースがそうでなければ、利用者に確認する。

### 段階 0：uv（プラグインの動作に必要な道具）

このプラグインの道具（MCP ツール）は uv で動く。`doctor` などのツールが使えない場合は、まずここから行う。

1. ターミナルで `uv --version` を実行する。
2. 無ければ「プラグインが使う Python の準備ツール uv を入れます」と説明し、次を実行する（PowerShell）：
   ```powershell
   powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
   ```
   - 社内プロキシでダウンロードに失敗した場合は、プロキシの設定（環境変数 `HTTPS_PROXY`）が必要か、社内の担当に確認するよう伝える。
3. 入ったら、プラグインの道具を起動し直してもらう：VS Code のコマンドパレットで「MCP: サーバーの一覧表示」→ `coverity-triage` →「再起動」。分からなければ「ウィンドウの再読み込み」でもよい。
   - Python 本体とライブラリは、この起動時に uv が自動で用意する（初回は 1〜2 分）。利用者が入れる必要はない。
4. `doctor` が使えるようになったら段階 1 へ。

### 段階 1：診断

`doctor(repo_root)` を実行し、結果を「できていること / 足りないこと」に分けて短く伝える。`ng` の項目を上から順に解決する。

### 段階 2：設定ファイル（リポジトリにまだ無い場合だけ）

チームで最初に使う人が作り、コミットして共有する。すでにあれば何もしない。

1. `detect_project(repo_root)` で自動判定する。
2. 利用者に聞くのは次の 3 つだけ。1 つずつ聞く。
   - Coverity Connect の URL（ブラウザで開いている Coverity のアドレス。例 `https://coverity.example.co.jp:8443`）
   - プロジェクト名
   - ストリーム名
3. 書く値を表で見せて同意を得る（自動で決めた値には判定の根拠を添える。例：文字コードは「Shift_JIS のファイルが 120 個、UTF-8 が 3 個」）。
   | 項目 | 値 |
   |---|---|
   | Coverity の URL / プロジェクト / ストリーム | 聞いた値 |
   | git / svn、取り込み先ブランチ | `detect_project` の値 |
   | 日本語を新しく入れるときの文字コード | `detect_project` の値 |
   | 結果の出力先 | リポジトリの隣のフォルダ（`../coverity-triage-out`） |
   | 調べる警告 | 未トリアージ・1 回 20 件まで（`untriaged.yaml`） |
4. 同意を得たら `write_project_config` を呼ぶ。変更したい値があれば反映してから呼ぶ。
5. 接続後に警告のファイルパスが `C:/build/...` のような絶対パスなら、`path_strip_prefixes` の設定を提案する（[settings.md](references/settings.md)）。
6. 「`.coverity-triage/` をコミットしてチームで共有してください（パスワード等は含まれていません）」と伝える。コミットを頼まれたら、利用者の確認を得て実行してよい。

### 段階 3：認証情報（個人の PC ごと）

`doctor` で `ng` / `warning` になっている環境変数だけを設定する。

- **ユーザ名**（秘密ではない）：利用者に聞き、次で保存する。
  ```powershell
  powershell -NoProfile -Command "[Environment]::SetEnvironmentVariable('COV_USER', '<聞いたユーザ名>', 'User')"
  ```
- **秘密情報の入力**（`COV_AUTH_KEY`、`GITHUB_TOKEN`）：**値をチャットで聞かない。** 次をターミナルで実行し、表示された伏せ字の入力欄に利用者が入力する。
  ```powershell
  powershell -NoProfile -Command "$s = Read-Host '<環境変数名> を入力してください（表示されません）' -AsSecureString; $v = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)); [Environment]::SetEnvironmentVariable('<環境変数名>', $v, 'User'); Write-Host '保存しました'"
  ```
  - ターミナルで入力できない環境では、このコマンドを利用者に渡し、自分で PowerShell を開いて実行してもらう。
  - 認証キーの発行場所が分からないと言われたら：Coverity Connect の画面のユーザメニューから発行する（社内の案内があればそれに従う）。
  - GitHub のトークンは、git でプルリクエストを作るときだけ必要。権限は対象リポジトリの Contents と Pull requests の読み書き。
- 保存した値は、ツールがすぐに読める（VS Code の再起動は不要）。

### 段階 4：確認と次の案内

1. `doctor` を再実行し、すべて `ok`（または説明済みの `warning`）なら完了。
2. 「準備ができました。`/coverity-run` で始められます。最初は件数の少ない条件で試すのがおすすめです」と伝える。

## 質問への対応（/coverity-help）

- 使い方は [usage.md](references/usage.md)、エラーは [troubleshooting.md](references/troubleshooting.md) を見て答える。分からないことは推測で答えず、`doctor` や `get_run_status` で確かめる。
- 設定の変更（例：「MISRA だけ調べたい」「出力先を変えたい」「Shift_JIS にしたい」）は、[settings.md](references/settings.md) を見て、変更内容を見せて同意を得てから `.coverity-triage/` のファイルを編集し、`doctor` で確かめる。条件ファイルは新しいファイルとして追加するのがよい。
- 効果測定（「どれくらい役立っている？」）は `get_stats(repo_root)` を使い、承認の内訳、推奨どおりに採用された割合（全体・確信度別）、手直しの割合、1 件あたりの処理時間を伝える。
