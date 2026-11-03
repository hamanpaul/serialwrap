> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

I have launched `python3 -m tools.contracts check` to verify contract compliance. Waiting for the check to complete.
I have launched the pytest run on `tests/contract_blackbox/test_bb_cli_help.py`, `tests/test_contracts_cli_schema.py`, `tests/test_contracts_keep_baseline.py`, `tests/test_contracts_catalog.py`, `tests/test_setup_cli.py`, and `tests/test_setup_reconcile.py`. Waiting for it to finish.
I am waiting for the pytest execution to finish.
Waiting for the background task to complete.
Waiting for pytest execution to complete.
Waiting for background task e6b652fa-85d3-4b47-a0f6-847fa4943fc2/task-79 to finish.
Waiting for background task e6b652fa-85d3-4b47-a0f6-847fa4943fc2/task-79 to finish.
Waiting for pytest task-79 to complete.
Task is running. I will wait for notification.
The background task is making progress. I am ending my turn to wait for completion.
Waiting for task-79 to finish.
Pytest is at 84%. Waiting for completion notification.
## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `978aa1e1` | 0 | `978aa1e1` 於 [tests/contract_blackbox/test_cli_mgmt_daemon.py](file://tests/contract_blackbox/test_cli_mgmt_daemon.py#L141-L191) 新增 `test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 測試，以 `_checkpointed_dut_tx` 透過 bridge barrier 確保 UART bridge 排空落盤後再讀取 checkpoint 封閉 TX 視窗，徹底取代固定等待 settle 的 `_wait_dut_tx`。 |
| 2 | Medium | 已修 `51bd8fa5` | 0 | `51bd8fa5` 重構 [crates/sw-pal/tests/exec_delegate.rs](file://crates/sw-pal/tests/exec_delegate.rs#L168-L230)，改由受委派的孫行程寫出 `DELEGATE_READY_ENV` 標記與 PID，父行程以 `process_is_running(delegate_pid)` 確認孫行程存活後才發送控制事件，並在 [tests/test_rust_exec_delegate.py](file://tests/test_rust_exec_delegate.py) 補入靜態斷言守護，避免子行程空跑通過。 |
| 3 | Medium | 已修 `ddcbdde0` | 0 | `ddcbdde0` 實作 [scripts/rust/l15_exit_driver.py](file://scripts/rust/l15_exit_driver.py) 並接通 [scripts/rust/stage1_exit_driver.sh](file://scripts/rust/stage1_exit_driver.sh#L419)，精確判定僅在完全符合 OD-30～OD-32 擁有者核准延後規則時才豁免 Windows L1.5 非零退出碼；並在 [tests/test_rust_stage1_exit_driver.py](file://tests/test_rust_stage1_exit_driver.py#L258-L326) 新增 5 個測試案例確認非延後項或 gate 失敗仍 fail-closed 阻擋。 |
| 4 | High | 已修 `12b3a929` | 0 | `12b3a929` 從 [tools/docker/upgrade.Dockerfile](file://tools/docker/upgrade.Dockerfile#L12) 移除系統 `python3-yaml`，[scripts/docker/run_upgrade_container.py](file://scripts/docker/run_upgrade_container.py) 移除 `--system-site-packages` 改由 wheelhouse 離線提供 pipx 依賴，[scripts/rust/mkbundle.sh](file://scripts/rust/mkbundle.sh) 建置自帶 PyYAML 的隔離 runtime（`include-system-site-packages = false`），並由 [tests/test_rust_mkbundle.py](file://tests/test_rust_mkbundle.py#L176-L192)（`test_bundle_runtime_contains_isolated_pyyaml`）斷言隔離環境下的依賴可用性。 |
| 5 | Medium | 已修 `12b3a929` | 0 | `12b3a929` 於 [scripts/rust/mkbundle.sh](file://scripts/rust/mkbundle.sh) 提供 `--release-bundle` 模式排除 `sw-cli/test-fakes`，後續 `ac6c4e4e` 進一步確保 release profile 完全排除 `SERIALWRAP_TEST_` 與 `SW_TEST_UPDATE_*` 測試鉤子，並於 [tests/test_release_workflow.py](file://tests/test_release_workflow.py#L216-L248)（`test_release_profile_binary_excludes_test_hooks`）與 [tests/test_rust_mkbundle.py](file://tests/test_rust_mkbundle.py#L271-L298)（`test_release_bundle_build_does_not_enable_test_fakes`）加設守護。 |
| 6 | Low | 已修 `12b3a929` | 0 | `12b3a929` 修訂了 [docs/rust-refactor/release-0.4.0-plan.md](file://docs/rust-refactor/release-0.4.0-plan.md#L10) 的發佈前置條件，改為明確接受 `VERIFIED` 或僅含 OD-27～OD-32 允許之 `BLOCKED-OWNER` 的 `IMPLEMENTED`，對齊 gate 的實際狀態產出。 |

---

## B 新發現

NO FINDINGS

---

## 既有碼待辦（不計入結論）

1. [crates/sw-cli/tests/cli_spec_schema.rs:40](file://crates/sw-cli/tests/cli_spec_schema.rs#L40) 與 [contracts/cli_schema.json:25](file://contracts/cli_schema.json#L25) — `4f27ea12` 合入 main 時，`contracts/cli_schema.json` 中的 `x-global-options[0]`（`--version`）其 `action` 欄位被寫入 `"_VersionAction"`，使得 `cli_spec_matches_schema` 測試執行時因 `action_kind` 僅匹配 `"version"` 而觸發 panic（`schema action 不在 argparse 子集內：_VersionAction`）。建議將 `contracts/cli_schema.json` 中的 `"_VersionAction"` 正規化回 `"version"`，或在 `crates/sw-cli/tests/cli_spec_schema.rs` 的 `action_kind` 補入對 `"_VersionAction"` 的映射。

---

## 對裁決的異議（不計入結論）

無

---

結論：可放行
