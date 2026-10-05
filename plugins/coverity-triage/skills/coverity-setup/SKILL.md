---
name: coverity-setup
description: Coverity トリアージを使う準備をする（uv、MCP サーバの環境、サブエージェントの配置、設定ファイル、認証情報、接続の確認、オプションでビルドでの検証）。何度実行してもよく、足りないものだけを補う。
argument-hint: 省略可
disable-model-invocation: true
allowed-tools: ["coverity-triage", "shell(uv run:*)"]
---

# Coverity トリアージの準備

利用者はこのプラグインの仕組みを知りません。**一度に 1 つずつ**、短く、ふつうの言葉で案内します。
何度実行されてもかまいません。`ct.py doctor` で足りないものを調べ、それだけを補います。

最初にスキル `coverity-triage-scripts` を読み、スクリプト `ct.py` の場所と使い方を確かめます。以下の `ct.py X` は、そこに書かれた方法（`uv run <フォルダ>/scripts/ct.py X`）で実行します。

## 守ること

- ターミナルでコマンドを実行する前に、何のために実行するかを 1 行で伝える。
- git / svn / VS Code / Coverity の設定は変えない。足りないものは社内の手順で入れてもらう。
- **認証キーをチャットで聞かない。** 入力は、下の「認証情報」の方法で、利用者がターミナルの伏せ字の欄に入れる。チャットに貼られたら使わずに、「漏れたおそれがあるので、認証キーを作り直してください」と伝える。
- 設定ファイルを書く前に、書く値を表で見せて同意を得る。
- 確かめられることは推測しない。`ct.py doctor` と MCP の `check_connection` で確かめる。

## 1. 確認の画面を減らす

準備の間は、コマンドの実行ごとに確認の画面が出ます。最初に次を案内します（決定 2）。

> 準備の間だけ、チャットの許可のレベルを「すべて許可」にすると、確認の画面が出なくなります。VS Code ではチャットの入力欄の許可のメニューで **Allow all**（すべて許可）を選びます。Copilot CLI では `/allow-all` と入力します。終わったら元に戻せます。

選ばなくても準備はできます（確認の画面で、その都度「許可」を押します）。

## 2. uv

このプラグインは uv（Python とライブラリを用意する道具）で動きます。

1. ターミナルで `uv --version` を実行する。
2. 無ければ「uv を入れます」と伝え、次を実行する（PowerShell）。
   ```powershell
   powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
   ```
   - 社内のプロキシで失敗したら、環境変数 `HTTPS_PROXY` の設定が要るか、社内の担当に確かめるよう伝える。
   - 入れた直後は、この会話の中で `uv` が見つからないことがあります。そのときは `%USERPROFILE%\.local\bin\uv.exe` のフルパスで動かす。
   - **uv を入れたことを覚えておく**（最後に再起動が要る）。

## 3. 準備の状況

1. `ct.py prewarm` を実行する（MCP サーバの Python 環境を作る。初回は 1〜2 分）。
2. 対象リポジトリの一番上のフォルダ（`.git` か `.svn` があるフォルダ）を決める。開いているフォルダがそうでなければ、利用者に聞く。
3. `ct.py doctor --repo <フォルダ>` を実行し、「できていること」と「足りないこと」を短く伝える。足りないものを、上から順に次の 4〜6 で補う。

## 4. サブエージェント

`doctor` でサブエージェント `coverity-triage-worker` が ng なら、「警告を 1 件ずつ調べる AI（サブエージェント）の定義を、あなたのフォルダ `%USERPROFILE%\.copilot\agents` に置きます」と伝えて `ct.py install-agent` を実行する（決定 1）。`restart_needed` が true なら、**最後に再起動が要る**ことを覚えておく。

## 5. 設定ファイル（リポジトリにまだ無いときだけ）

チームで最初に使う人だけが作り、コミットして共有します。

1. `ct.py detect --repo <フォルダ>` を実行する。
2. 次の 3 つを、1 つずつ聞く。
   - Coverity Connect の URL（ブラウザで開く Coverity のアドレス。例 `https://coverity.example.co.jp:8443`）
   - プロジェクト名
   - ストリーム名
3. 書く値を表で見せて同意を得る。

   | 項目 | 値 |
   |---|---|
   | Coverity の URL・プロジェクト・ストリーム | 聞いた値 |
   | git / svn、取り込み先のブランチ | `detect` の値 |
   | 結果を置く場所 | リポジトリの隣のフォルダ（`../coverity-triage-out`） |
   | 最初に調べる警告 | 分類がまだの警告を 20 件まで（条件ファイル `untriaged.yaml`） |

4. 同意を得たら `ct.py init-config --repo <フォルダ> --url <URL> --project <名前> --stream <名前>` を実行する（変えたい値があれば引数で渡す）。
5. 「`.coverity-triage/` フォルダをコミットして、チームで共有してください（認証情報は入っていません）」と伝える。知識のファイル `knowledge.md` もできたこと、AI が調べる前に読むので、チームの判断の方針などを書いておけることを 1 行で伝える。

## 6. 認証情報（PC ごと）

`doctor` で ng の環境変数だけを設定します。

- **ユーザ名**：利用者に聞いて、次で保存する。
  ```powershell
  powershell -NoProfile -Command "[Environment]::SetEnvironmentVariable('COV_USER', '<ユーザ名>', 'User')"
  ```
- **認証キー**：値は聞かない。次をターミナルで実行し、表示された伏せ字の欄に利用者が入れる。
  ```powershell
  powershell -NoProfile -Command "$s = Read-Host 'COV_AUTH_KEY を入力してください（表示されません）' -AsSecureString; $v = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)); [Environment]::SetEnvironmentVariable('COV_AUTH_KEY', $v, 'User'); Write-Host '保存しました'"
  ```
  - ターミナルで入力できないときは、このコマンドを利用者に渡し、自分で PowerShell を開いて実行してもらう。
  - 認証キーの作り方が分からないと言われたら、Coverity Connect の画面のユーザのメニューから作れること（社内の案内があればそれ）を伝える。

GitHub のトークンは要りません（プルリクエストは Copilot の機能で作ります）。

## 7. 再起動（必要なときだけ、1 回）

2 で uv を入れた、または 4 で `restart_needed` が true だったときだけ、次を伝えて、ここで止まります（決定 1）。

> 準備の続きのために、VS Code（Copilot CLI の場合は Copilot CLI）を一度終了して、起動し直してください。起動したら、もう一度 `/coverity-setup` を入力してください。続きから進めます。

どちらでもなければ、そのまま 8 へ進みます。

## 8. Coverity への接続

1. MCP の `check_connection` を `repo_root` で呼ぶ。
   - MCP のツールが見つからない、または起動に失敗したときは、スキル `coverity-help` の「困ったとき」の「MCP サーバが動かない」に沿って案内する。
2. 認証できたら、かかった秒数と一緒に伝える。`warning` があればそのまま伝える。
3. `ct.py doctor` をもう一度実行し、すべて ok であることを確かめる。

## 9. トリアージ中の確認を減らす（決定 4）

トリアージは件数が多いので、このプラグインのツールだけは確認なしで動くようにします。

- Copilot CLI：`/coverity-run` と `/coverity-apply` の間は、このプラグインのツールとスクリプトが自動で許可されます。することはありません。
- VS Code：次の 2 つを案内する。
  1. コマンドパレットで **Chat: Manage Tool Approval** を開き、`coverity-triage` のツールを「確認なしで実行」にする。
  2. コマンドパレットで **Preferences: Open User Settings (JSON)** を開き、次を足す（スクリプトの実行だけを確認なしにする）。
     ```json
     "chat.tools.terminal.autoApprove": {
       "/^.*uv(\\.exe)?\"? run .*coverity-triage-scripts[\\\\/]scripts[\\\\/]ct\\.py\"? /": true
     }
     ```
  - 難しければ、トリアージの間だけ「すべて許可」にしてもよいと伝える。

## 10. ビルドでの検証（オプション）

「修正案をビルドで確かめる設定もしますか？（使わなくてもかまいません。あとからでも設定できます）」と聞く。使わないなら 11 へ。

使う場合は、1 つずつ聞く（推測で決めない）。

1. ビルドの前に実行する環境設定のコマンド（例 `envset.bat "{root}" <引数>`）。引数の数と、それぞれに何を渡すかを聞く。ビルド用のフォルダを渡す引数は `{root}` と書く。不要なら空。
2. ビルドするフォルダ（makefile がある場所。リポジトリからの相対パス）。一番上でよければ空。
3. ビルドのコマンド（例 `make -f makefileXX`）。
4. 確かめ方：ビルドだけ（`build`）か、ビルドと Coverity の再解析（`build+analyze`）か。

「修正前のコードで試しにビルドします（時間がかかることがあります）」と伝えて `ct.py trial-build` を実行する。失敗したら `log_tail` を見て、コマンドの誤りか環境の問題かを伝え、直して試す。成功したら、保存する値を見せて同意を得てから `ct.py set-verify` を実行し、コミットして共有するよう伝える。

## 11. 終わり

「準備ができました。`/coverity-run` で始められます。最初は件数の少ない条件で試すのがおすすめです」と伝える。1 で「すべて許可」にしてもらった場合は、元に戻してよいことも伝える。
