---
name: checker-knowledge
description: Coverity のチェッカー（標準チェッカー、MISRA C/C++、CERT C/C++）ごとの判断観点。C / C++ の Coverity 警告を調査するときに、チェッカー名に応じて参照する。
user-invocable: false
---

# チェッカー別の判断観点

チェッカー名に応じて、次の資料を読んでから調査してください。

| チェッカー名 | 資料 |
|---|---|
| `MISRA C-2012 ...`、`MISRA C++-...` など MISRA で始まるもの | [references/misra.md](references/misra.md) |
| `CERT ...` で始まるもの | [references/cert.md](references/cert.md) |
| 上記以外（`NULL_RETURNS`、`RESOURCE_LEAK`、`OVERRUN` などの標準チェッカー） | [references/standard.md](references/standard.md) |

資料にないチェッカーは、指示ファイルの「チェッカーの説明（Coverity）」を読み、スキル `triage-investigation` の手順で調べます。
