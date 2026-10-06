---
name: deviation-comment
description: Coverity の逸脱コメント（トリアージのコメント）の書き方と、Classification / Action / Severity の選び方。逸脱案を作るときに使う。
user-invocable: false
---

# 逸脱コメントの書き方

## 書き方

- **日本語**で書く。
- **見出しを付けない文章**で書く。
- 次をこの順に入れる。
  1. **判定区分**（必須）：「誤検知。」「意図的。」など、文の先頭に置く。
  2. **理由**（必須）：なぜ問題にならないのか。
  3. **コードの根拠の場所**：理由を裏づける関数名とファイル・行（例 `read_all()（src/io/reader.c:120）`）。
  4. **影響がないことの説明**：そのままでも動作・安全性に問題がないこと。

## 例

> 誤検知。get_buf() の戻り値は NULL になりうるが、呼び出し元 read_all()（src/io/reader.c:120）で NULL を除いてから渡しているため、この経路で NULL を参照することはなく、動作・安全性への影響はない。

> 意図的。case 2 から case 3 へのフォールスルーは、状態 2 の処理のあと状態 3 の処理を続けて行う設計（src/fsm.c:88 のコメントに記載）によるもので、break の書き忘れではないため、動作への影響はない。

## 注意

- プロジェクトの知識（`knowledge.md`）に「逸脱コメントの書き方」があれば、それに従う。
- 根拠の場所は、調べて実際に読んだ場所だけを書く。確かめていない場所を推測で書かない。
- 1〜3 文にまとめる。長い説明は結果ファイルの `verdict.rationale` に書く。
- 本物のバグ（`true_bug`）と判断した警告でも、逸脱案は必ず作る。そのときは、直さない場合のリスクを正直に書く（例「本物の不具合。ただし、この機能は今の製品では無効になっている（config.h:12）ため、今は動作に影響しない。」）。説明できるリスクが無ければそう書き、推奨は修正にする。

## Classification / Action / Severity の選び方

逸脱案（`deviation`）の目安です。値は Coverity Connect に設定されている選択肢の名前で書きます。

| 判定 | Classification | Action | Severity |
|---|---|---|---|
| 誤検知 | False Positive | Ignore | Unspecified |
| 意図的 | Intentional | Ignore | Unspecified |
| 本物のバグだが直さない | Bug | Ignore | 影響に応じて Major / Moderate / Minor |

修正案（`fix`）の目安：Classification は `Bug`、Action は `Fix Required`、Severity は影響に応じて選ぶ。

社内で使っている選択肢が違うときは、この表を社内の値に書き換えてください。
