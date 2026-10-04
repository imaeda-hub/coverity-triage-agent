---
name: deviation-comment
description: Coverity の逸脱コメント（トリアージコメント）の書き方と、Classification / Action / Severity の選び方。逸脱案を作るときに使う。
user-invocable: false
---

# 逸脱コメントの書き方（仕様 D-22〜D-24）

## 書式

- **日本語**で書く。
- **見出しを付けない文章形式**で書く。
- 次の要素をこの順で含める。
  1. **判定区分**（必須）：「誤検知。」「意図的。」など、文の先頭に置く。
  2. **理由**（必須）：なぜ問題にならないのか。
  3. **コード上の根拠箇所**：理由を裏付ける関数名とファイル・行（例：`read_all()（src/io/reader.c:120）`）。
  4. **影響がないことの説明**：そのままにしても動作・安全性に問題がないこと。

## 例

> 誤検知。get_buf() の戻り値は NULL になり得るが、呼び出し元 read_all()（src/io/reader.c:120）で NULL を除外してから渡しているため、当該経路で NULL 参照は発生せず動作・安全性への影響はない。

> 意図的。case 2 から case 3 へのフォールスルーは、状態 2 の処理後に状態 3 の処理を続けて行う設計（src/fsm.c:88 のコメントに記載）によるもので、break の欠落ではないため動作への影響はない。

## 書き方の注意

- プロジェクトの知識（`project_knowledge`）の「逸脱コメントの書き方」に書かれていることがあれば、それに従う。
- 根拠箇所は、調査で実際に読んだ場所だけを書く。確認していない場所を推測で書かない。
- 1〜3 文程度にまとめる。長い説明は詳細レポート（`verdict.rationale`）に書く。
- `true_bug`（本物のバグ）と判断した警告でも、逸脱案は必ず作る。その場合は、修正しない場合のリスクを正直に書く（例：「本物の不具合。ただし該当機能は現行製品で無効化されている（config.h:12）ため、現時点で動作への影響はない。」）。説明できるリスクがなければ、その旨を書き、推奨は修正にする。

## Classification / Action / Severity の選び方

逸脱案（`deviation`）の目安です。値は Coverity Connect に設定されている選択肢の名前で書きます。

| 判定 | Classification | Action | Severity |
|---|---|---|---|
| 誤検知 | False Positive | Ignore | Unspecified |
| 意図的 | Intentional | Ignore | Unspecified |
| 本物のバグだが修正しない | Bug | Ignore | 影響に応じて Major / Moderate / Minor |

修正案（`fix`）の目安：Classification は `Bug`、Action は `Fix Required`、Severity は影響に応じて選ぶ。
社内で使っている選択肢が違う場合は、この表を社内の値に合わせて書き換えてください。
