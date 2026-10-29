---
type: change
scope: rust-s21
---
OD-55：`s21_core.linux` 中從不執行的 `#[ignore]` 列移出——24 個 `cap_<cap>__*` 能力測試改登記於新增的 `s21_gate:cap_{posix_pty,sys_admin,windows_native,interactive_desktop,supervisor_runtime}`（selector `--ignored cap_<cap>__`，`linux_cap_posix_pty` section 以 `--caps posix_pty` 實跑 `s21_gate:cap_posix_pty`），非能力列 `plain_holder_child`、`real_mkbundle_dev_output_verifies`、`windows_switch_running_exe_rename` 直接移除；`test_l1_lock_cargo_sets_exclude_ignored_tests` 僅依 OD-53 對能力集合條件豁免。`s21_core.windows` 待 Windows 原生收集後處理。
