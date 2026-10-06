---
name: triage-report
description: Coverity トリアージの結果ファイル（result.json）の形と、各項目に書く内容の基準。調べ終わって結果を書くときに使う。
user-invocable: false
---

# 結果ファイルの書き方

レポートと一覧の体裁はスクリプトが整えます。あなたは中身（判断と文章）だけを、指示ファイル（`brief.md`）にある結果ファイルの場所に、次の JSON で書きます。
人はまず一覧の「見立て」と、詳細レポートの「結論」だけを読みます。そこだけで判断できるように書きます。

```json
{
  "item": "20001 または G1（指示ファイルの見出しの ID）",
  "verdict": {
    "judgement": "false_positive | true_bug | intentional | undetermined",
    "summary": "一覧に載る 1 行（40 字くらい）",
    "rationale": "警告経路を追った結果。どの条件がなぜ成り立つ／成り立たないか",
    "evidence": [{"file": "src/a.c", "line": 120, "note": "ここで NULL を除いている"}]
  },
  "recommendation": "fix | deviation",
  "confidence": "high | medium | low",
  "confidence_reason": "確信度の基準のどれに当たるか（具体的に）",
  "deviation": {
    "classification": "False Positive",
    "action": "Ignore",
    "severity": "Unspecified",
    "comment": "逸脱コメント（スキル deviation-comment の書き方）"
  },
  "fix": {
    "classification": "Bug",
    "action": "Fix Required",
    "severity": "Moderate",
    "summary": "直した内容を 1 行で",
    "impact": "動きの変化と影響の範囲",
    "exceeded_constraints": [],
    "already_fixed_on_latest": false
  },
  "revision_drift": {"status": "none | detected | unknown", "detail": ""},
  "group_excluded_cids": []
}
```

## 各項目の基準

- `verdict.summary`：判断の決め手を 1 行で。例「呼び出し元 3 箇所で NULL を確かめている」「エラーのとき fd を閉じていない」。
- `verdict.rationale`：人が追い直さなくて済むよう、経路のどの段階をどう確かめたかを順に書く。
- `verdict.evidence`：根拠のコードの場所。調べて実際に読んだ場所だけ。パスはリポジトリの一番上からの相対パス。
- `confidence` / `confidence_reason`：スキル `triage-investigation` の基準で。
- `fix.summary` / `fix.impact` / `fix.exceeded_constraints`：スキル `code-fix` に従う。
- `deviation`：スキル `deviation-comment` に従う。
- `group_excluded_cids`：グループのうち、原因が違うので個別の作業に戻す CID。グループでなければ空。
- 文字列は空にしない。JSON として正しい形にする（ダブルクォート、カンマ）。

## 書く前の確かめ

- 修正するコードのフォルダのファイルを直したか（最新のリビジョンでもう直っているときを除く）。
- アノテーションのフォルダがあるときは、注釈を入れたか。
- 調べるコードのフォルダ、利用者の作業中のファイルを変えていないか。

書き終えたら、呼び出し元に 1 行だけ返します（例「20001: 修正を推奨（確信度 高）— fgets が失敗したとき fp を閉じていない」）。結果ファイルに不備があると、呼び出し元から直す点が伝えられます。そのときは結果ファイルだけを直します。
