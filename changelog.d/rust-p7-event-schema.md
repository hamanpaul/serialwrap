---
type: change
scope: rust-p7
---
Rust `sw_core::event_engine::schema` 承接 Python `validate_rule_dict`：欄位檢查順序、錯誤訊息、`int()`／`str()`／truthiness 語意與大整數逐列對齊 `contracts/golden/p7/event_schema.ndjson`；`sw_proto::pyre::redos` 以 P3 regex AST 重現 `sre_parse` 的 alternation 正規化，計算 ReDoS 歧義分數並對可疑樣式 fail-closed 拒絕（EVT-D-S15，P7 Task 11）。
