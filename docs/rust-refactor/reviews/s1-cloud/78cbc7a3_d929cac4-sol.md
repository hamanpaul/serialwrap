> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1 | 0 | `tests/contract_blackbox/test_cli_mgmt_daemon.py` 以 `_checkpointed_dut_tx` 等待 bridge barrier 與 checkpoint SHA-256 雜湊確認封閉視窗，並於 `finally` 停止 daemon 後再行比對 DUT TX；補入 `test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 延遲落盤測試。 |
| 2 | Medium | 已修 51bd8fa5 | 0 | `crates/sw-pal/tests/exec_delegate.rs` 改由受委派孫行程寫出帶 PID 的 `delegated-ready` 標記（`DELEGATE_READY_ENV`），父測試透過 `process_is_running` 確認孫行程存活後才發送 Ctrl 事件，並以 `tests/test_rust_exec_delegate.py` 確保關鍵檢查未被削弱。 |
| 3 | Medium | 已修 4e3ba13f | 0 | `scripts/rust/stage1_exit_driver.sh` 引入 `scripts/rust/l15_exit_driver.py`（前序 4e3ba13f 與 ddcbdde0），對照原始 Windows L1.5 測試報告、metadata 與 gate 延後列，僅在所有非 PASS 列精確符合 OD-30～OD-32 延後項且無 harness 異常時才放行退出碼，並補齊 `tests/test_rust_stage1_exit_driver.py`。 |
| 4 | High | 已修 12b3a929 | 0 | `scripts/docker/run_upgrade_container.py` 移除 `--system-site-packages`，改由 wheelhouse 離線安裝 0.3.2 依賴；`tools/docker/upgrade.Dockerfile` 移除系統 `python3-yaml`；`scripts/rust/mkbundle.sh` 提供 `--release-bundle` 將 PyYAML 封入 bundle runtime；`tests/test_docker_upgrade.py` 補 `test_upgrade_environment_does_not_hide_missing_pyyaml`。 |
| 5 | Medium | 已修 12b3a929 | 0 | `scripts/rust/mkbundle.sh` 提供不含 `--features sw-cli/test-fakes` 的 `--release-bundle` 模式；`docs/rust-refactor/release-0.4.0-plan.md` 更新發布配方為 `--release-bundle --profile release`；`tests/test_rust_mkbundle.py` 補齊防禦測試。 |
| 6 | Low | 已修 12b3a929 | 0 | `docs/rust-refactor/release-0.4.0-plan.md` 前置條件更新為接受真實的 `VERIFIED` 或無未核准缺口的 `IMPLEMENTED`，並以 `test_release_plan_accepts_only_real_stage1_gate_outcomes` 驗證。 |

---

## B 新發現

NO FINDINGS

---

## 既有碼待辦（不計入結論）

- `crates/sw-cli/tests/support/mod.rs`：存在 4 處未使用的 import 警告（`sw_pal::fakes::service::ServiceModelState`、`ServiceAction`、`ServiceCtl`、`super::*`、`crate::json_val as json`），建議在後續收尾輪清理。

---

## 對裁決的異議（不計入結論）

無

---

結論：可放行
