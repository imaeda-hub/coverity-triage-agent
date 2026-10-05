# 設定の項目と変え方

設定は対象リポジトリの `.coverity-triage/` にあり、コミットしてチームで共有します。認証情報は書きません（環境変数に置きます）。

## config.yaml

| 項目 | 意味 | 決め方・既定 |
|---|---|---|
| `coverity.url` | Coverity Connect の URL | ブラウザで開く Coverity のアドレス |
| `coverity.api` | `auto`：Coverity Connect につなぐ／`fake`：偽データ（動作確認用） | 既定 `auto` |
| `coverity.user_env` / `coverity.key_env` | ユーザ名・認証キーを入れる環境変数の名前 | 既定 `COV_USER` / `COV_AUTH_KEY` |
| `coverity.triage_store` | 逸脱を書き戻すトリアージストアの名前 | 既定 `Default Triage Store` |
| `coverity.revision_field` | スナップショットで解析リビジョンを記録している項目 | 既定 `sourceVersion`。記録が無ければ、手元のコードで調べてずれを確かめる |
| `coverity.path_strip_prefixes` | Coverity のファイルパスから取り除く先頭部分 | 警告のパスが `C:/build/product/src/a.c` なら `["C:/build/product/"]`。空でも、リポジトリにある末尾の部分で自動で対応づける |
| `coverity.ca_file` | Coverity の CA 証明書のファイル | 空（OS の証明書を使う）。証明書のエラーが出るときだけ指定 |
| `vcs.type` | `git` / `svn` | 自動で判定 |
| `vcs.base_branch` | 修正を取り込む先のブランチ（git） | 自動で判定 |
| `vcs.branch_prefix` | 修正のブランチ名の先頭（git） | 既定 `coverity-fix/` |
| `output_dir` | 結果を置く場所 | 既定 `../coverity-triage-out`（リポジトリの隣）。**リポジトリの外**にする |
| `max_items` | 1 回で調べる警告の上限 | 既定 100 |
| `parallel` | 同時に調べる数 | 既定 1。速くしたいとき 2〜3 |
| `options.annotation` | 逸脱案として、ソースに Coverity の注釈を入れる差分も作る | 既定 false |
| `options.per_run_branch` | 修正を警告ごとではなく、1 つのブランチにまとめる（git） | 既定 false |
| `options.knowledge_suggestions` | 反映のあと、知識の追記の候補を示す | 既定 false |
| `options.metrics` | 推奨の採用率などを記録する | 既定 false |
| `verify.default` | ビルドでの検証：`none` / `build` / `build+analyze` | 既定 `none` |
| `verify.setup_command` | ビルドの前に実行する環境設定のコマンド。`{root}` はビルド用のフォルダに置き換わる | 例 `envset.bat "{root}" <引数>`。不要なら空 |
| `verify.build_dir` | ビルドするフォルダ（リポジトリからの相対パス） | 空なら一番上 |
| `verify.build_command` | ビルドのコマンド | 例 `make -f makefileXX` |
| `verify.cov_build_args` / `verify.cov_analyze_args` | `cov-build` / `cov-analyze` の引数（`--dir` が必要） | 既定 `--dir idir` / `--dir idir --all` |

ビルドでの検証の設定は、`/coverity-setup` の「ビルドでの検証」で、試しにビルドしてから保存するのが安全です。

## 条件ファイル（filters/*.yaml）

同じ項目の中の値は「どれか」、項目どうしは「すべて」を満たすものを選びます。書かない項目は条件にしません。

| 項目 | 意味 |
|---|---|
| `name` | 表示する名前 |
| `project` / `streams` | Coverity のプロジェクトとストリーム（ストリームは 1 つだけ） |
| `checkers` | チェッカー名（`*` が使える。例 `"MISRA C-2012 *"`） |
| `impacts` | `High` / `Medium` / `Low` |
| `triage.classification` / `triage.action` / `triage.status` | 今のトリアージの状態 |
| `limit` | この条件で調べる上限（`max_items` より多くはならない） |

例（MISRA の未分類だけ、20 件まで）：

```yaml
name: MISRA の未分類
project: MyProduct
streams: [MyProduct-main]
checkers: ["MISRA C-2012 *"]
triage:
  classification: [Unclassified]
limit: 20
```

## そのほかのファイル

- `knowledge.md`：AI が調べる前に読む、プロジェクトの知識（[usage.md](usage.md)）。
- `no-grouping.yaml`：却下されたグループの CID。次の実行から 1 件ずつ調べます（自動で追記されます）。
