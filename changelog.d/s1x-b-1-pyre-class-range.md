---
type: fix
scope: rust-cli
---

修正 Rust CLI 的 `pyre` 字元類別解析：範圍 `a-b` 改在讀到當下即檢查（對照 CPython `sre_parse`），`bad character range` 的報錯優先序先於其後的 `unterminated character set` 與類別內跳脫錯誤；`-` 後直接結束仍回 unterminated。跳脫 token 的範圍訊息與位置亦對齊 CPython（例如 `[\x7a-a]` 回 `bad character range \x-a at position 3`）。`profile test` 的 `checks.<regex>.error` 因此與 Python 3.12.3 逐字一致（S1X-b-1）。`pyre.tsv` golden 新增 14 筆語料（add-only）。
