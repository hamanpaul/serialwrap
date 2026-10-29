---
type: change
scope: rust-p7
---
新增 Slice 2.3 出口 gate 報告產生器與驗證器 `scripts/rust/p7_gate_report.py`（P7 Task 19）：測試列以 P2 `l1_judge` 依凍結 nodeid 逐案判定（缺 l1_results／0 測試／必要案例 skip 或重複一律 INCOMPLETE）、能力缺失列為 BLOCKED-OWNER＋B 編號、擁有者項目需核對證據 sha256、`verify` 重算比對防竄改。
