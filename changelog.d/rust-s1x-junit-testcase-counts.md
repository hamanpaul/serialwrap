---
type: fix
scope: rust-cli
---

修正 Stage 1 出口 gate 與 P4 完成度檢查的 junit 計數：pytest 9 的 subtests 會計入 testsuite 的 `tests` 屬性，但不產生對應的 testcase 元素，致使 `s1_pytest_full` 的 junit 與 l1-results 列數永遠不符（5024 對 4932，差值 92）而判為證據不足。`scripts/rust/stage1a_gate.py`（黑箱分支與 `_junit_totals`）與 `scripts/rust/check_p4_cases.py`（`_junit_counts`）改以 testcase 元素逐一計數，計數實作依路徑載入 P2 `tests/acceptance/check_evidence_index.py::_junit_counts`，不另寫一份；P2 模組缺席或載入失敗時 fail-closed（閘門記入 errors 而整體 FAIL，P4 檢查列為 BLOCKED-PREREQ）。l1_judge 的逐項嚴格相等比對不變，pytest JUnit 含失敗或錯誤的整份失敗判定照舊；真正不一致（testcase 數與報告不等）仍判為證據不足。

補充（S1X 出口收尾）：`register_p4_evidence.py` 之 junit 計數改委派 `check_p4_cases`（再委派 P2 `check_evidence_index._junit_counts`），登記與檢查路徑採同一 testcase 層計數。
