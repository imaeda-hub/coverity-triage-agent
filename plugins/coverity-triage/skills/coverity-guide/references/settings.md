# 設定項目の意味と決め方

設定は対象リポジトリの `.coverity-triage/` にあり、コミットしてチームで共有する。パスワード等は書かない（環境変数）。

## config.yaml

| 項目 | 意味 | 決め方・既定値 |
|---|---|---|
| `coverity.url` | Coverity Connect の URL | ブラウザで開いている Coverity のアドレス |
| `coverity.api` | 接続方式 `auto` / `rest` / `soap` / `fake` | 通常は `auto`。`fake` は偽データでの試用（`fake_data` に偽データのファイル） |
| `coverity.user_env` / `coverity.key_env` | ユーザ名・認証キーを入れる環境変数の名前 | 既定 `COV_USER` / `COV_AUTH_KEY` のまま |
| `coverity.revision_field` | スナップショットに解析リビジョンを記録している項目 | 記録していなければそのまま（手元のコードで調査し、ずれを報告する） |
| `coverity.path_strip_prefixes` | Coverity のファイルパスから取り除く先頭部分 | 警告のパスが `C:/build/product/src/a.c` なら `["C:/build/product/"]`。空でも、リポジトリに実在する末尾部分で自動対応づけを試みる |
| `vcs.type` | `git` / `svn` | 自動判定 |
| `vcs.base_branch` | 修正を取り込む先のブランチ（git） | 自動判定（リモートの既定ブランチ） |
| `vcs.branch_mode` | `per_cid`：CID ごとに 1 プルリクエスト／`per_run`：承認した修正をまとめて 1 つ | 既定 `per_cid` |
| `vcs.branch_prefix` | 修正ブランチ名の先頭 | 既定 `coverity-fix/`。社内の命名規則があれば合わせる |
| `vcs.github_token_env` | GitHub トークンの環境変数名 | 既定 `GITHUB_TOKEN` |
| `output_dir` | 結果の出力先 | **リポジトリの外**。既定 `../coverity-triage-out`（リポジトリの隣） |
| `max_items` | 1 回の上限件数（条件ファイルで上書き可） | 既定 100 |
| `parallel` | 同時に調査する数 | 既定 1。速くしたいとき 2〜3 |
| `deviation_target` | `coverity`：逸脱は Coverity にだけ登録／`coverity+annotation`：ソースに注釈コメントも入れる | 既定 `coverity` |
| `ascii_file_encoding` | 英数字だけのファイルに日本語を入れるときの文字コード `utf-8` / `cp932` | 自動判定（Shift_JIS のプロジェクトなら `cp932`） |
| `verify.default` | 自動検証 `none` / `build` / `build+analyze` | 既定 `none` |
| `verify.setup_command` | ビルド前の環境設定のコマンド（同じコマンドプロンプトで先に実行）。`{root}` はビルド用にコピーしたフォルダに置き換わる | 例 `envset.bat "{root}"`。不要なら空 |
| `verify.build_command` | ビルドのコマンド（リポジトリのルートで実行） | 例 `make -f makefileXX` |
| `verify.cov_build_args` / `verify.cov_analyze_args` | `cov-build` / `cov-analyze` の引数（`--dir` 必須） | 例 `--dir idir` / `--dir idir --all` |

自動検証の仕組み：全件の調査が終わった後に、その実行の修正案を**すべてまとめて適用したコピー**で 1 回だけビルド（＋解析）する。再解析では修正前のコードも 1 回解析して比べるため、1 回の実行で解析 2 回分の時間がかかる。利用者の PC でビルドと Coverity の解析ツールが動く必要がある。設定を変えたら `trial_build` で試しにビルドしてから `write_verify_config` で保存する。

## 条件ファイル（filters/*.yaml）

同じ項目の中の複数の値は「どれか」、項目同士は「すべて」。書かない項目は条件にしない。

| 項目 | 意味 |
|---|---|
| `name` | 表示名 |
| `project` / `streams` | Coverity のプロジェクト・ストリーム |
| `checkers` | チェッカー名（`*` 可。例 `"MISRA C-2012 *"`） |
| `impacts` | `High` / `Medium` / `Low` |
| `triage.classification` / `triage.action` / `triage.status` | 現在のトリアージ状態 |
| `max_items` | この条件での上限件数 |
| `revision` | 解析リビジョンの手動指定（通常は空） |

例（MISRA の未トリアージだけ）：

```yaml
name: MISRA の未トリアージ
project: MyProduct
streams: [MyProduct-main]
checkers: ["MISRA C-2012 *"]
triage:
  classification: [Unclassified]
max_items: 20
```

## no-grouping.yaml

却下されたグループの CID が自動で追記される（次回から 1 件ずつ調査する）。コミットして共有する。
