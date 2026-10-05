---
name: coverity-setup
description: Coverity トリアージを使えるように準備する（必要なものの準備、設定ファイルの作成、認証情報の入力、接続確認、任意で自動検証の設定）。何度実行してもよく、足りないものだけを補う。
argument-hint: 省略可
disable-model-invocation: true
---

# Coverity トリアージの準備

利用者はこのプラグインの仕組みを知りません。専門用語を避け、**一度に 1 つずつ**、短く案内してください。
何度実行されてもよい。`doctor` で足りないものだけを補う。

## 守ること

- ターミナルでコマンドを実行する前に、**何のために何をするか**を 1 行で説明する（実行の確認は Copilot が利用者に求める）。
- git / svn / Coverity / VS Code 自体のインストールや設定変更はしない。足りない場合は社内の手順で入れてもらうよう伝える。
- **パスワード・認証キー・トークンをチャットで尋ねない。** 入力は skill の「秘密情報の入力」の方法で、利用者がターミナルの伏せ字欄に入力する。チャットに貼られた場合は、使わずに「漏えいの恐れがあるので再発行を」と伝える。
- 設定ファイルを書く前に、書く値を一覧で見せて同意を得る。
- 機械的に確かめられることは、推測せず `doctor` で確かめる。

## 準備の流れ

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

- 「調査役の AI（coverity-triage-worker）」が `ng` の場合：「警告を 1 件ずつ調べる調査役の AI を、あなたのユーザフォルダ（`%USERPROFILE%\.copilot\agents`）にコピーします。VS Code の Copilot はプラグインの中のものを使わないためです」と説明し、同意を得て `install_worker_agent()` を呼ぶ。コピーした（`installed` / `updated`）場合は、VS Code（または Copilot CLI）の再起動が必要と伝える。再起動は最後にまとめてでよい。

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
5. 接続後に警告のファイルパスが `C:/build/...` のような絶対パスなら、`path_strip_prefixes` の設定を提案する（設定の詳細は skill `coverity-help` の settings.md）。
6. 「`.coverity-triage/` をコミットしてチームで共有してください（パスワード等は含まれていません）」と伝える。あわせて、プロジェクトの知識のファイル `knowledge.md`（ひな形）もできたこと、AI が調査の前に読むので、戻らない関数や修正・逸脱の方針などを書いておけることを 1 行で伝える（詳細は skill `coverity-help` の usage.md）。コミットを頼まれたら、利用者の確認を得て実行してよい。

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

### 段階 4：確認

`doctor` を再実行し、すべて `ok`（または説明済みの `warning`）であることを確かめる。

### 段階 5：自動検証（任意）

1. 「修正案をビルド（と Coverity の再解析）で自動検証する設定もしますか？（任意。後から `/coverity-help` でも設定できます）」と聞く。
   - 検証は、1 回の実行の修正案をすべてまとめて、調査の最後に 1 回だけ行う（ビルド＋再解析なら解析 2 回分。10〜60 分程度）。
   - 不要と言われたら段階 6 へ。
2. 使う場合は、1 つずつ聞く（推測で決めない）。
   - **ビルド前の環境設定のコマンド**（例 `envset.bat "{root}" <2つ目の引数>`）：引数はいくつでも書ける。**引数の数と、それぞれに何を渡すかを利用者に聞く**（推測しない）。ビルド用にコピーしたフォルダを渡す引数は `{root}` と書く（ツールが置き換える）。それ以外の引数はそのまま渡される。不要なら空。
   - **ビルドするディレクトリ**（例 `firmware/target`）：環境設定の後に移動して、ビルドを実行する場所（makefile がある場所）。リポジトリからの相対パスで聞く。ルートでよければ空。
   - **ビルドのコマンド**（例 `make -f makefileXX`）：上のディレクトリで実行される。
   - **検証の既定**：`build`（ビルドのみ）か `build+analyze`（ビルド＋再解析）か。再解析の場合は `cov-build` / `cov-analyze` の引数（`--dir` が必須。既定 `--dir idir` / `--dir idir --all`）も確認する。
3. 「修正前のコードで試しにビルドします（10〜60 分程度かかることがあります）」と伝え、`trial_build(repo_root, 環境設定のコマンド, ビルドのコマンド, ビルドするディレクトリ)` を実行する。
   - 失敗したら、`log_tail` を見てコマンドの誤りか環境の問題かを説明し、直したコマンドで再試行する。
4. 成功したら、保存する値を見せて同意を得てから `write_verify_config` を呼ぶ。コミットしてチームで共有するよう伝える。

### 段階 6：次の案内

「準備ができました。`/coverity-run` で始められます。最初は件数の少ない条件で試すのがおすすめです」と伝える。
