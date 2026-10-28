---
type: change
scope: rust-console
---
Slice 2.2 出口判定 `scripts/rust/s22_gate_check.py` 補強（審查 3317987_d6e3b3c-opus）：必要集合固定為 `s22_gate:{cargo_workspace,console_conformance_pty,blackbox_rust,win_test,py_parity}`，lock 少登記即 INSUFFICIENT；對照表鍵集合須等於凍結 `slice_2.2`；func-test／scenario 參照須由 `--l15-results`（`s22-l15-results.v1`）恰一列 passed 佐證；以全表模式執行 `check_p6_prereqs.py --strict`（增補 #180）；`parity-amendments-2026-10-26.md` 承接 Phase 為 P6 的承接編號逐列輸出 PASS／BLOCKED-OWNER／OWNER-DECISION-PENDING（增補 #187），未登記或待裁決者不得 PASS。黑箱 FakeTarget 的 `hold_reply` 與 `pause_responding` 改為可疊加；BC-02 不交錯黑箱改以每輪命令完成（prompt RX）為界斷言。
