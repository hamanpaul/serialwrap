---
type: fix
scope: acceptance
---
Slice 2.2 出口判定依 rulings/p6.md「OD-53 selector 記錄 caps」：不改 L1 鎖 schema，`s22_gate_check.py` 由現有 selector 推導收集器 `cargo_l1_results.py --caps`（test-binary 參數恰為 `--ignored cap_<cap>__` → `--caps <cap>`）；推導不出的集合（如 `--include-ignored`）記 EVIDENCE_MISSING，其 passed 列不充當逐案證據、failed 列仍判 FAIL。各集合輸出推導的 `caps` 與收集命令，`--plan` 只依鎖印出前置收集命令。
