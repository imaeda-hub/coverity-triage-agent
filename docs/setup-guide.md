# 導入手順書（チームの設定担当者向け）

Coverity トリアージエージェントを、チームで使えるようにするための手順書です。
**チームで最初に 1 回だけ**、設定担当の 1 人が行います。日々の使い方は [利用手順書](user-guide.md) を見てください。

- 所要時間の目安：1〜2 時間（自動検証を使う場合はさらに 1 時間程度）
- 前提知識：git または svn の基本操作、Coverity Connect の画面操作。プラグインの仕組みを知っている必要はありません。

> 【試用後に確定】と書かれた箇所は、試用（[docs/trial-guide.md](trial-guide.md)）で確認した後に正式な手順へ書き換えます。

---

## 1. 全体像

```
 利用者の PC（Windows）
 ┌──────────────────────────────────────────────┐
 │ VS Code / Copilot CLI                         │
 │   └ GitHub Copilot ── プラグイン「coverity-triage」 │
 │                        ├ エージェント（AI の指示書）    │
 │                        └ MCP サーバ（Python。AI の道具）│
 │ 対象リポジトリ（git / svn のワーキングコピー）       │
 │   └ .coverity-triage/  ← この手順書で作る設定      │
 └──────────────────────────────────────────────┘
        │ 警告の取得・書き戻し          │ push・プルリクエスト（git のみ）
        ▼                              ▼
   社内 Coverity Connect            社内 GitHub / GitHub Enterprise
```

- プラグインは各利用者の PC にインストールします（利用手順書の「最初に 1 回だけ」）。
- **設定ファイルは対象リポジトリに置き、コミットしてチームで共有します**（この手順書で作るもの）。利用者は設定を作る必要がありません。
- パスワードやトークンは設定ファイルに書かず、各利用者の PC の環境変数に設定します。

## 2. 事前に用意するもの

| No | 用意するもの | 確認方法・入手先 |
|---|---|---|
| 1 | 対象リポジトリのワーキングコピー（git / svn） | 普段使っているもので可 |
| 2 | Coverity Connect の URL | ブラウザで開いている Coverity Connect のアドレス（例: `https://coverity.example.co.jp:8443`） |
| 3 | Coverity のプロジェクト名・ストリーム名 | Coverity Connect の画面 |
| 4 | Coverity のユーザ名と認証キー | Coverity Connect の画面右上のユーザメニュー →「認証キー」で発行（パスワードで接続する場合は不要）【試用後に確定】 |
| 5 | GitHub のトークン（git の場合のみ） | プルリクエストを作るために使う。GitHub の Settings → Developer settings → Personal access tokens で発行。権限：対象リポジトリの Contents（読み書き）と Pull requests（読み書き） |
| 6 | 自分の PC の準備 | [利用手順書の「1. 最初に 1 回だけ」](user-guide.md#1-最初に-1-回だけパソコンの準備) を先に済ませておく |

## 3. 設定ファイルのひな形を作る

対象リポジトリを VS Code で開き、Copilot Chat で次のコマンドを実行します。

```
/coverity-init
```

対象リポジトリに次のファイルが作られます。

```
<対象リポジトリ>/.coverity-triage/
├─ config.yaml                   … プロジェクト設定（4 章で編集）
├─ filters/untriaged-high.yaml   … 絞り込み条件の例（5 章で編集）
└─ fake-issues.yaml              … 試し用の偽データ（7 章で使う。本番では不要）
```

> コマンドが使えない場合は、このリポジトリの `plugins/coverity-triage/server/src/coverity_triage/templates/` の中身を、対象リポジトリの `.coverity-triage/` に手でコピーしても同じです。

## 4. プロジェクト設定（config.yaml）を編集する

`.coverity-triage/config.yaml` を開き、上から順に決めていきます。**迷ったら「既定値のままでよいか」の列を見てください。**

### 4.1 Coverity の接続

| 項目 | 意味 | 決め方 | 既定値のままでよいか |
|---|---|---|---|
| `coverity.url` | Coverity Connect の URL | 2 章の No.2 | 要変更 |
| `coverity.api` | 接続方式 | 試すときは `fake`。本番は【試用後に確定】（`rest` / `soap` / `auto`） | 7 章で切り替える |
| `coverity.fake_data` | 試し用の偽データ | `fake-issues.yaml` のまま | ○ |
| `coverity.user_env` | ユーザ名を入れる環境変数の名前 | 通常は `COV_USER` のまま | ○ |
| `coverity.key_env` | 認証キー（またはパスワード）を入れる環境変数の名前 | 通常は `COV_AUTH_KEY` のまま | ○ |
| `coverity.revision_field` | Coverity のスナップショットに解析したリビジョン（git のコミット / svn のリビジョン番号）を記録している項目 | 記録していなければそのままで可（その場合は手元のコードで調査し、ずれを報告します）。記録する運用にすると調査の精度が上がります。項目名は【試用後に確定】 | ○ |
| `coverity.path_strip_prefixes` | Coverity が表示するファイルパスから取り除く先頭部分 | Coverity Connect でファイルパスが `C:/build/product/src/a.c` のように表示される場合、`["C:/build/product/"]` と書く。`src/a.c` のように表示されるなら空のまま | 表示を見て判断 |

### 4.2 バージョン管理

| 項目 | 意味 | 決め方 | 既定値のままでよいか |
|---|---|---|---|
| `vcs.type` | `git` か `svn` | 対象リポジトリに合わせる | `/coverity-init` で自動設定 |
| `vcs.base_branch` | 修正を取り込む先のブランチ（git のみ） | 通常は `main` や `develop` など、プルリクエストのマージ先 | 要確認 |
| `vcs.branch_mode` | 修正ブランチの単位（git のみ） | `per_cid`：CID ごとに 1 つのブランチ・プルリクエスト（レビューしやすい）／ `per_run`：1 回の実行で承認した修正をまとめて 1 つ（プルリクエストが少ない） | ○（`per_cid`） |
| `vcs.branch_prefix` | 修正ブランチ名の先頭 | 社内のブランチ命名規則があれば合わせる | ○ |
| `vcs.github_token_env` | GitHub のトークンを入れる環境変数の名前 | 通常は `GITHUB_TOKEN` のまま | ○ |

### 4.3 動作

| 項目 | 意味 | 決め方 | 既定値のままでよいか |
|---|---|---|---|
| `output_dir` | 結果（レポート・差分など）の出力先 | **リポジトリの外**を指定する（誤ってコミットしないため）。相対パスはリポジトリのルートから見た位置。既定の `../coverity-triage-out` はリポジトリの隣のフォルダ | ○ |
| `max_items` | 1 回の実行で処理する件数の上限 | 最初は 10〜20 程度で試し、慣れたら増やす | 最初は小さく |
| `parallel` | 同時に調査する数 | 最初は `1`。速くしたい場合に 2〜3 | ○ |
| `deviation_target` | 逸脱コメントの記録先 | `coverity`：Coverity にだけ登録／`coverity+annotation`：ソースコードにも注釈コメントを入れる | ○ |
| `ascii_file_encoding` | 英数字だけのファイルに日本語を追加するときの文字コード | **プロジェクトが Shift_JIS なら `cp932`**、UTF-8 なら `utf-8` | 要確認 |
| `model` | 参考表示のみ（実際の AI モデルはプラグイン側で固定） | 変更不要 | ○ |

### 4.4 自動検証（任意）

修正案をビルド・再解析して、警告が消えたかを自動で確かめる機能です。最初は `default: none`（使わない）で始め、慣れてから設定することをおすすめします。6 章を参照してください。

## 5. 絞り込み条件ファイルを作る

`.coverity-triage/filters/` に、「どの警告をトリアージするか」を書いたファイルを置きます。用途ごとに複数作れます。利用者は実行時にファイル名を指定します。

**書き方の決まり**：同じ項目の中に複数の値を書くと「どれか 1 つに当てはまる」、項目を並べると「すべてに当てはまる」になります。書かなかった項目は条件になりません。

### 例 1：未トリアージで重要度の高いもの（`untriaged-high.yaml`）

```yaml
name: 未トリアージの High / Medium
project: MyProduct
streams:
  - MyProduct-main
impacts: [High, Medium]
triage:
  classification: [Unclassified]
  action: [Undecided]
max_items: 20
```

### 例 2：MISRA の警告だけ（`misra.yaml`）

```yaml
name: MISRA の未トリアージ
project: MyProduct
streams:
  - MyProduct-main
checkers:
  - "MISRA C-2012 *"     # * は「任意の文字列」
triage:
  classification: [Unclassified]
```

| 項目 | 意味 |
|---|---|
| `name` | 表示名（一覧サマリに出ます） |
| `project` | Coverity のプロジェクト名 |
| `streams` | ストリーム名 |
| `checkers` | チェッカー名（`*` が使えます） |
| `impacts` | `High` / `Medium` / `Low` |
| `triage.classification` / `triage.action` / `triage.status` | 現在のトリアージ状態 |
| `max_items` | この条件での上限件数（省略時は config.yaml の値） |
| `revision` | 解析したリビジョンを手で指定する場合のみ（通常は空） |

## 6. 自動検証を設定する（任意）

| 項目 | 意味 | 例 |
|---|---|---|
| `verify.default` | 既定の検証方法：`none`（しない）／`build`（ビルドのみ）／`build+analyze`（ビルド＋再解析） | `none` |
| `verify.build_command` | ビルドのコマンド（リポジトリのルートで実行されます） | `build.bat Release` |
| `verify.cov_build_args` | `cov-build` に渡す引数 | `--dir idir` |
| `verify.cov_analyze_args` | `cov-analyze` に渡す引数（`--dir` は必須） | `--dir idir --all` |

- 利用者の PC で、上記のビルドと Coverity の解析ツール（`cov-build` など）が動く必要があります。
- 再解析は時間がかかるため、1 件ずつ順番に実行されます。初回は比較用に修正前のコードも解析するため、解析 2 回分の時間がかかります。

## 7. 動作を確認する

### 7.1 偽データで試す（社内 Coverity に接続しない）

1. `config.yaml` の `coverity.api` を `fake` にする（ひな形の状態）。
2. `fake-issues.yaml` の `file`・`function`・`line` を、**対象リポジトリに実在する**ファイル・関数・行に書き換える（1〜2 件で十分）。
3. 絞り込み条件ファイルの `project` を、`fake-issues.yaml` の `project` と同じにする。
4. [利用手順書の 2〜4 章](user-guide.md#2-トリアージを実行する) のとおり実行し、一覧サマリができることを確認する。

### 7.2 社内 Coverity に接続する【試用後に確定】

1. `coverity.api` を本番の接続方式に変更する。
2. 自分の PC に環境変数（`COV_USER`、`COV_AUTH_KEY`）を設定する（[利用手順書 1.4](user-guide.md#14-環境変数を設定する)）。
3. `max_items: 3` 程度の条件ファイルで実行し、結果を確認する。

## 8. チームに展開する

1. `.coverity-triage/` をコミットする（`fake-issues.yaml` は本番で使わないなら削除してよい）。
   - 出力先（`output_dir`）がリポジトリの中になっていないことを確認してください。
2. 利用者に次を案内します（そのまま使える文例）。

```
Coverity トリアージエージェントを導入しました。
1. 利用手順書（docs/user-guide.md）の「1. 最初に 1 回だけ」で PC を準備してください。
2. 使える条件ファイル：
   - untriaged-high.yaml … 未トリアージの High / Medium
   - misra.yaml … MISRA の未トリアージ
3. 最初は件数の少ない条件で試してください。
問い合わせ先：<設定担当者の名前>
```

## 9. 設定を変更するとき

- `config.yaml` と条件ファイルを編集してコミットするだけです。利用者は最新を取り込めば反映されます。
- 却下されたグループの記録 `.coverity-triage/no-grouping.yaml` は自動で追記されます。これもコミットして共有してください（次回から同じ CID が個別に調査されます）。

## 10. 確定待ちの事項

| 内容 | 確定の時期 |
|---|---|
| プラグインのインストール方法（社内マーケットプレイスの登録手順を含む） | 試用（trial-guide.md の A）の後 |
| Coverity への接続方式・認証方式（`coverity.api`、認証キーかパスワードか） | API 調査（trial-guide.md の B）の後 |
| スナップショットに解析リビジョンを記録する項目名（`coverity.revision_field`） | API 調査の後 |
