---
type: change
scope: rust-p7
---
新增 Slice 2.3 出口 gate 編排 `scripts/rust/p7_gate.sh`（P7 Task 19）：依序執行並記錄 cfg 邊界、cargo L1、Windows 原生（能力缺失記 BLOCKED-OWNER）、黑箱 A/B 與先前切片回歸、func-test 兩案、案例鎖、`slice_2.3` 涵蓋（`scripts/rust/p7_case_coverage.py`）、全套 pytest 與 policy check，再以 `p7_gate_report.py build`／`verify` 產出綁定 run_id、HEAD 與 artifact sha256 的報告；`p7_gate_report.py` 新增只記錄不判定的 `init`／`record`。`scripts/rust/run_p7_ab.sh` 併入 slice21／slice22 黑箱、l15 slice21／slice23 與 func-test `brcm-01-login-flow`、`ev-01-spawn-handler`，`p7_ab_report.py` 逐階段判定（缺證據、skip、BLOCKED-UPSTREAM 皆不得 PASS）。新增 `crates/sw-daemon/tests/cap_windows_com_p7.rs`（三案 `cap:windows_com`，peer 端腳本化 DUT `tests/support/com_peer.rs`）與 B-01 擁有者程序 `docs/rust-refactor/evidence/p7-b01-procedure.md`。
