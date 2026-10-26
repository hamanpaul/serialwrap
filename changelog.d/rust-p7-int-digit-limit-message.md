---
type: fix
scope: rust-p7
---
事件規則 schema 的 `int(str)`：超過 4300 位數的數字字串改回 CPython 3.12 位數上限 `ValueError` 訊息（`Exceeds the limit (4300 digits) …`），invalid literal 訊息的 repr 依 `%.200R` 截斷至 200 字元；`sw_proto::pycompat` 新增 `py_int_parse_checked`／`PyIntParseError`（審查 L1）。
