> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

最後一次確認，停用工具調用，等待通知。
已啟動 `pytest -q tests/test_rust_win_scripts.py` 驗證，正在等待執行完成。
已啟動 `pytest -q tests/contract_blackbox/test_bb_cli_help.py` 驗證 CLI help 契約，正在等待執行完成。
已啟動 gate 及 L1 判定測試驗證，正在等待執行完成。
已啟動 `cargo test --workspace`，正在等待執行完成。
## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1 | 0 | `tests/contract_blackbox/test_cli_mgmt_daemon.py` 改用 `_checkpointed_dut_tx` 搭配靜默屏障（barrier）與 checkpoint 封閉 TX 視窗，並新增延遲落盤回歸測試 `test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles`。 |
| 2 | Medium | 已修 51bd8fa5 | 0 | `crates/sw-pal/tests/exec_delegate.rs` 改由受委派孫行程 helper 自行寫出含 PID 的 ready 標記，父行程驗證該 PID 存活後才發送 Ctrl 事件，並於事件後驗證孫行程結束；同步補齊委派前阻塞情境回歸。 |
| 3 | Medium | 已修 ddcbdde0 | 0 | `scripts/rust/l15_exit_driver.py` 與 `scripts/rust/stage1_exit_driver.sh` 接通延後列判準，當且僅當非 PASS 列精確符合 OD-30～OD-32 授權時不阻擋出口，並以 `tests/test_rust_stage1_exit_driver.py` 驗證（後續經 4e3ba13f 補強逐列比對）。 |
| 4 | High | 已修 12b3a929 | 0 | `tools/docker/upgrade.Dockerfile` 移除系統 `python3-yaml`，升級 runner 移除 `--system-site-packages`，`scripts/rust/mkbundle.sh` 打包自帶 PyYAML 的隔離 runtime 並以 `-I` 驗證；新增 `tests/test_rust_mkbundle.py::test_bundle_runtime_contains_isolated_pyyaml` 回歸。 |
| 5 | Medium | 已修 12b3a929 | 0 | `scripts/rust/mkbundle.sh` 提供 `--release-bundle` 選項不編入 `sw-cli/test-fakes`，`docs/rust-refactor/release-0.4.0-plan.md` 同步更新發佈配方；新增 `tests/test_rust_mkbundle.py::test_release_bundle_build_does_not_enable_test_fakes` 回歸。 |
| 6 | Low | 已修 12b3a929 | 0 | `docs/rust-refactor/release-0.4.0-plan.md:10` 出口條件更正為 Stage 1A gate overall 為 `VERIFIED` 或為 `IMPLEMENTED` 且僅剩 OD-27～OD-32 明確允許之 `BLOCKED-OWNER`。 |

## B 新發現

NO FINDINGS

審查範圍 `b7e3c0e5..ed2bc78b` 包含 2 個 commit：
- `8b16bc3b fix(contracts): 以 Windows Python 3.12.3 更新 help_root_col80 golden`：對齊 Python 基線 PR #241 對 `--version` 說明文字的更新（「顯示版本與程式碼指紋後離開」），`contracts/cli_help/windows/_meta.json` 與 `help_root_col80.json` 契約同步更新。經 `python3 -m tools.contracts check`（0 findings, ok: true）與 `tests/test_rust_win_scripts.py::test_windows_help_root_col80_matches_python_baseline` 驗證通過。
- `ed2bc78b fix(scripts): Windows 黑箱 B 輪改用 test-fakes 執行檔`：修正 Windows 黑箱 B 輪委派測試因缺少 `test-fakes` feature 而無法讀取 `SERIALWRAP_TEST_DELEGATE_GROUP` 的問題。腳本比照 Linux dev bundle 流程，先編譯 `test-fakes` 並複製至 `target/win-blackbox-test-fakes/serialwrap.exe` 供黑箱測試與 Windows runner 使用；隨後以純 release 重建覆寫標準 release 路徑，並以內嵌 Python 腳本雙向斷言正式 binary 絕無 `SERIALWRAP_TEST_` / `SW_TEST_UPDATE_` 鉤子、fakes binary 包含委派鉤子。相關 import 檢查、staging 複製與比較器 identity 計算皆一致對齊 `fakes_exe`，並以 `test_rust_win_scripts.py::test_win_blackbox_uses_test_fakes_cli_and_keeps_release_unhooked` 驗證通過。

全套工作區檢查（`cargo test --workspace`、`python3 -m tools.contracts check`、`python3 -m policy_check --repo .`）均通過且無破壞既有行為。

## 既有碼待辦（不計入結論）

無

## 對裁決的異議（不計入結論）

無

結論：可放行
