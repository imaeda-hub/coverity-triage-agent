# トリアージ結果 2026-10-02 15:15
条件: 試用（全件） ／ 対象 6 件（グループ 1）／ エラー 0 件

承認列には「修正 / 逸脱 / 却下」のいずれかを書きます（推奨案を下書き済み）。空欄の行は反映しません。逸脱コメントの手直しは各詳細レポートで行います。

| 承認 | CID | 推奨 | 確信度 | チェッカー | Impact | 場所 | 見立て | グループ | 検証 | ずれ | 詳細 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 修正 | 20003 | 修正 | 中 | NULL_RETURNS | High | src/reader.c / first_char() | get_buf()（malloc）の戻り値を確認せずに参照 | - | - | なし | [→](cid/20003.md) |
| 修正 | 20001 | 修正 | 高 | RESOURCE_LEAK | High | src/reader.c / open_and_read() | fgets() 失敗時に fp を閉じずに return している | - | - | なし | [→](cid/20001.md) |
| 逸脱 | 20002 | 逸脱 | 高 | FORWARD_NULL | Medium | src/reader.c / open_and_read() | 唯一の呼び出し元 read_config() で path の NULL を除外済み | - | - | なし | [→](cid/20002.md) |
| 修正 | G1（3 件） | 修正 | 高 | MISRA C-2012 Rule 10.3 | Low | src/reader.c / to_u8() | int を unsigned char に暗黙変換して代入（Rule 10.3 違反） | G1 | - | なし | [→](cid/G1.md) |
