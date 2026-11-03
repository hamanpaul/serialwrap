> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

I will wait for the contract test execution to complete.
I am waiting for the background tests to complete.
Waiting for the test execution to complete.
I will wait for task-124 to complete.
Waiting for the test execution to complete.
I will pause here and wait for the test task to complete.
I will wait for the unit tests to finish.
Waiting for task-160 to finish.
Waiting for completion notification.
Waiting for task-160 to finish.
I will wait for the cargo test run to complete.
I will wait for the resource markers test to complete.
Waiting for task-200 to complete.
Waiting for task-200.
Waiting for task-200.
## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1 | 0 | [test_cli_mgmt_daemon.py](file://tests/contract_blackbox/test_cli_mgmt_daemon.py#L143) 移除容易競態的 `_wait_dut_tx`，改以 bridge passive barrier 等待佇列排空，並以 bridge control checkpoint 取得完整封閉 TX 視窗；新增單元測試 [`test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles`](file://tests/contract_blackbox/test_cli_mgmt_daemon.py#L145) 驗證延遲落盤。 |
| 2 | Medium | 已修 51bd8fa5 | 0 | [exec_delegate.rs](file://crates/sw-pal/tests/exec_delegate.rs#L168) 改由受委派之孫行程 helper 親自寫出含其 PID 的 delegate ready 檔案，父行程確認該 PID 存活後才發送控制事件，並於事件後驗證孫行程終止；新增 [test_rust_exec_delegate.py](file://tests/test_rust_exec_delegate.py) 防護斷言。 |
| 3 | Medium | 已修 ddcbdde0 | 0 | 新增 [l15_exit_driver.py](file://scripts/rust/l15_exit_driver.py) 接通 [stage1_exit_driver.sh](file://scripts/rust/stage1_exit_driver.sh#L419)，精確在 gate 整體為 `IMPLEMENTED`/`VERIFIED` 且合法延後列為 `BLOCKED-OWNER` 時豁免 Windows 測試端 exit 1；新增 [test_rust_stage1_exit_driver.py](file://tests/test_rust_stage1_exit_driver.py#L281) 多組正反向情境回歸。 |
| 4 | High | 已修 12b3a929 | 0 | [upgrade.Dockerfile](file://tools/docker/upgrade.Dockerfile#L12) 移除系統 `python3-yaml`，升級測試腳本移除 `--system-site-packages`；[mkbundle.sh](file://scripts/rust/mkbundle.sh) 封裝自帶 PyYAML 的隔離 Python runtime，並補齊 [`test_bundle_runtime_contains_isolated_pyyaml`](file://tests/test_rust_mkbundle.py#L176) 測試。 |
| 5 | Medium | 已修 12b3a929 | 0 | [mkbundle.sh](file://scripts/rust/mkbundle.sh#L147) 新增 `--release-bundle` 模式（不啟用 `sw-cli/test-fakes` feature），[release-0.4.0-plan.md](file://docs/rust-refactor/release-0.4.0-plan.md#L20) 全面更新發佈命令；補齊 [`test_release_bundle_build_does_not_enable_test_fakes`](file://tests/test_rust_mkbundle.py#L271) 靜態檢查。 |
| 6 | Low | 已修 12b3a929 | 0 | [release-0.4.0-plan.md](file://docs/rust-refactor/release-0.4.0-plan.md#L10) 依 `stage1a_gate.py` 實際輸出狀態改寫條件，接受 `VERIFIED` 或僅剩 OD-27～OD-32 允許之 `BLOCKED-OWNER` 的 `IMPLEMENTED`。 |

---

## B 新發現

1. **[Medium] [tests/test_contracts_keep_baseline.py:225](file://tests/test_contracts_keep_baseline.py#L225)** — 同步 Python 基線更新 logger source 行號時漏更測試 pin，導致合規測試紅燈。
   - **問題：** commit [4f27ea12](file://contracts/daemon_logging.yaml#L104) 合入 main 時，因 [session_manager.py](file://sw_core/session_manager.py) 增修程式碼，將 [contracts/daemon_logging.yaml](file://contracts/daemon_logging.yaml) 的 source 行號更新（`CREDENTIALS_UNRESOLVED` 由 `:1915` 變更為 `:1991`；`TRANSPORT_STALL` 由 `:2015` 變更為 `:2091`），但漏了同步更新 [tests/test_contracts_keep_baseline.py:225-226](file://tests/test_contracts_keep_baseline.py#L225-L226) 中硬編碼的 `pairs`（仍寫死 `1915` 與 `2015`），使既有基線測試失敗。
   - **具體失敗情境：**
     - 輸入：執行 `python3 -m pytest -q tests/test_contracts_keep_baseline.py::test_last_error_logging_templates_match`
     - 預期：1 passed
     - 實際：FAILED，`AssertionError: daemon_logging.yaml 缺少 sw_core/session_manager.py:1915 的訊息（CREDENTIALS_UNRESOLVED 的 WARNING）`
   - **確切修法：** 將 [`test_last_error_logging_templates_match`](file://tests/test_contracts_keep_baseline.py#L217) 內的 `pairs` 行號更新為 `"sw_core/session_manager.py:1991"` 與 `"sw_core/session_manager.py:2091"`，或改由 [contracts/session_last_errors.yaml](file://contracts/session_last_errors.yaml) 依 key 動態比對，消除行號硬編碼。

2. **[Medium] [contracts/cli_help/linux/help_sub_cmd_submit_col80.json:1](file://contracts/cli_help/linux/help_sub_cmd_submit_col80.json#L1)** — CLI help golden 檔提前合入 Python 端新參數與格式，導致 Rust CLI 測試出現 34 筆斷言失敗。
   - **問題：** commit [4f27ea12](file://contracts/cli_help/linux/) 合入 main 時，以 Python 最新 help 輸出全面覆寫了 [contracts/cli_help/linux/](file://contracts/cli_help/linux/) 下的 40 餘個 JSON 檔（包含 `cmd submit` 新增 `--expect-reboot`、`--source` 與 `--owner` 說明更新、version 加入 code fingerprint 等）。然而 `crates/sw-cli`（Rust 原生 parser）尚未實作 `--expect-reboot` 及相關說明文字，且該批 golden 仍標記為 `native: true`，導致 Rust 端既有回歸測試 [`native_paths_match_python_3_12_golden`](file://crates/sw-cli/tests/argparse_golden.rs#L104) 產生 34 筆 diff 失敗。
   - **具體失敗情境：**
     - 輸入：執行 `CARGO_TARGET_DIR=/tmp/solrev-s1-target cargo test -p sw-cli --test argparse_golden`
     - 預期：test result: ok. 3 passed; 0 failed
     - 實際：FAILED，`thread 'native_paths_match_python_3_12_golden' panicked at crates/sw-cli/tests/argparse_golden.rs:131:5: 34 筆不符`（`help_sub_cmd_submit_col*.json` 缺少 `--expect-reboot` 與 `--source` 更新說明等）
   - **確切修法：** 在 [crates/sw-cli/src/cli_spec.rs](file://crates/sw-cli/src/cli_spec.rs) 為原生 parser 補上 `--expect-reboot` 選項與 `--source` 說明文字對齊；或若對齊排定於下一專屬任務，則應先將尚未對齊的子命令 golden 條目標記為 `native: false`（或保留過渡狀態），不可在 Rust 端未實作前直接破壞 workspace cargo test 綠燈基線。

---

## 既有碼待辦（不計入結論）

- [crates/sw-cli/src/commands/rpc_cmds.rs](file://crates/sw-cli/src/commands/rpc_cmds.rs#L139) 目前未實作 `cmd submit` 的 `--expect-reboot` 參數轉發組包與保留前綴本地預檢（已於 [s1-main-sync-observable-changes.md](file://docs/rust-refactor/reviews/s1-main-sync-observable-changes.md) 列為後續對齊工作）。

---

## 對裁決的異議（不計入結論）

無

---

結論：需修正
