---
type: change
scope: rust-p7
---
Slice 2.3 審查修正第三批（sol de7a95a_b85eec5）：新增黑箱 `test_force_recover_attached_closes_before_reopen`，以 fd 層級取樣證明 ATTACHED 起手的 force recover 實際關閉並重開恰一次、任何時刻至多一個 tty fd；出口 gate 的 results.json 與報告記錄受測 Python／pytest 版本。
