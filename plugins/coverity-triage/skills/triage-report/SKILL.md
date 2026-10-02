---
name: triage-report
description: Coverity トリアージの判断結果を submit_result で提出するときの形式と、各項目に書く内容の基準。
---

# 判断結果の提出（submit_result）

レポートの体裁はツールが整えます。あなたは中身（判断と文章）だけを、次の形式で提出します。
人間はまず一覧サマリの「見立て」と各レポートの「結論」だけを読みます。そこだけで判断できるように書いてください。

```json
{
  "work_item": "12345 または G1",
  "verdict": {
    "judgement": "false_positive | true_bug | intentional | undetermined",
    "summary": "一覧サマリに載る 1 行要約（40 字程度）",
    "rationale": "警告経路を追跡した結果。どの条件がなぜ成立する／しないか",
    "evidence": [{"file": "src/a.c", "line": 120, "note": "ここで NULL を除外"}]
  },
  "recommendation": "fix | deviation",
  "confidence": "high | medium | low",
  "confidence_reason": "確信度の基準のどれに当たるか（具体的に）",
  "deviation": {
    "classification": "False Positive",
    "action": "Ignore",
    "severity": "Unspecified",
    "comment": "逸脱コメント（skill deviation-comment の書式）"
  },
  "fix": {
    "classification": "Bug",
    "action": "Fix Required",
    "severity": "Moderate",
    "summary": "修正内容の要約",
    "impact": "振る舞いの変化と影響範囲",
    "exceeded_constraints": [],
    "already_fixed_on_latest": false
  },
  "revision_drift": {"status": "none | detected | unknown", "detail": ""},
  "group_excluded_cids": []
}
```

## 各項目の基準

- `verdict.summary`：判定の決め手を 1 行で。例：「呼び出し元 3 箇所で NULL チェック済み」「エラー経路で fd が閉じられない」。
- `verdict.rationale`：人が追跡をやり直さなくて済むよう、経路のどの段階を確認したかを順に書く。
- `verdict.evidence`：根拠のコード箇所。調査で実際に読んだ場所だけ。
- `confidence` / `confidence_reason`：skill `triage-investigation` の基準に従う。
- `fix.summary` / `fix.impact`：skill `code-fix` に従う。
- `group_excluded_cids`：グループのうち、原因が違うため個別処理に戻す CID。

## 提出前の確認

- 修正案を `save_fix`（kind=`fix`）で保存したか（最新で解消済みの場合を除く）。
- アノテーション方式の場合、`save_fix`（kind=`annotation`）で保存したか。
