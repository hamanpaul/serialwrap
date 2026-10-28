---
type: fix
scope: rust-refactor
---

修正 Stage 1 擁有者機台登記掃描（`scripts/rust/owner_gated.py`，sol ccf29ce）的靜默消失缺口：owner-gated 節點若同時列於 `collected` 與 `deselected`、且 `results` 無對應列，舊版 `scan_cargo_report` 仍視其為「存在」，`owner_gated_skipped` 與 `absent_expected` 皆不列，最小報告可使 `l1_judge` 判 PASS，`stage1a_gate.py` 與 `check_p4_cases.py --check` 因而不列 BLOCKED-OWNER 亦不列缺失。現改為：`present` 不再計入 deselected 且無結果者；deselected 且無結果的擁有者節點一律列入 `owner_gated_skipped`（BLOCKED-OWNER 待補），登記列另列入 `absent_expected`（收集失效，INSUFFICIENT）。`scan_cargo_report` 新增回傳鍵 `owner_gated_deselected`。本次未修改 `l1_judge.py`、`stage1a_gate.py`、`check_p4_cases.py`、凍結鎖與 selector；新增 `tests/test_rust_owner_gated.py` 覆蓋 scan 層與兩支閘門的子行程情境。
