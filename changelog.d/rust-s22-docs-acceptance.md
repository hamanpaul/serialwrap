---
type: docs
scope: rust-console
---
新增 Slice 2.2 出口判定 `scripts/rust/s22_gate_check.py`（逐 `s22_gate:*` 集合委派 P2 `l1_judge` 判定、逐案要求 `AUTOMATED` 測試在報告中恰一列 passed，函式存在不等於已執行）；BLOCKED-OWNER 程序 `docs/rust-refactor/procedures/{windows-telnet-frontends,cross-reboot-console,console-echo-loopback}.md`；README 兩語段補 Rust daemon console 後端（`SERIALWRAP_CONSOLE_BACKEND`、`windows.serial_console`、OD-12 信任範圍）；`docs/regression-plugin.md` 記錄本 Phase #155 評估（不新增 `regression/` case）。
`tests/acceptance/slice22_evidence_map.yaml` 填妥凍結 `slice_2.2` 的 94 案（32 AUTOMATED、62 BLOCKED-OWNER；Slice 2.1 繼承者沿用 BO-S21-NN），新增 `docs/rust-refactor/slice22-owner-runbook.md`（BO-S22-01～07）與 `docs/rust-refactor/procedures/windows-serial-console.md`，`tests/test_acceptance_slice22.py` 驗證對照表靜態一致；`s22_gate:blackbox_rust/linux` 以 `--l1-add-only` 登記 slice22 黑箱 46 個 nodeid。
