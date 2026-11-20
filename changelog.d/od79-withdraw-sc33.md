---
type: change
scope: rust-refactor
---

- contracts：依擁有者裁決 OD-79 撤回 SC-33（傳輸中途被拒回 AUTOBOOT_QUIET／COMMAND_ABORTED_IN_FLIGHT），編號保留不重用，亦不新增 pending owner item 8；檔頭註解與調和表同步撤回說明。
- tests/contracts：新增 `test_sc33_withdrawn_per_od79` 斷言 SC-33 不得登記、pending_owner_confirmations 無 item 8。
- tests/sw-daemon：新增 ServiceRig 端到端回歸測試 `file_rpc_rejected_during_boot_quiet_returns_autoboot_quiet_zero_tx`，驗證 `file.push` 與 `file.pull` 於 boot quiet 期間依 SC-03 拒絕並回傳 `AUTOBOOT_QUIET`、帶 `retry_after_s` 與 `hint`，DUT 零 TX 且無 WAL／state 副作用。
- acceptance：以 add-only 登記 `file_rpc_rejected_during_boot_quiet_returns_autoboot_quiet_zero_tx` 至 `p7_gate:cargo-test-workspace.linux` L1 必要集合。
