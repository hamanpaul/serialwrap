> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

我已啟動釋出 profile 測試鉤子排除的測試驗證任務，稍後完成將取得結果。
## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1 | 0 | `978aa1e1` 引進 `_checkpointed_dut_tx`（bridge barrier + checkpoint 視窗機制），在 daemon stop 後比對封閉 wire 區間，並補 `test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 延遲落盤回歸測試。 |
| 2 | Medium | 已修 51bd8fa5 | 0 | `51bd8fa5` 改由實際受委派之孫行程 helper 寫出 delegate ready 標記與 PID，父行程以 `win_ctrl::process_is_running(delegate_pid)` 確認存活後才發送控制事件，移除脆弱的固定 2 秒 sleep，並以 `tests/test_rust_exec_delegate.py` 守護。 |
| 3 | Medium | 已修 ddcbdde0 | 0 | `ddcbdde0` 新增 `scripts/rust/l15_exit_driver.py` 並接通 `stage1_exit_driver.sh`，`4e3ba13f` 進一步補強逐列比對原始報告與 metadata，僅在精確符合核准延後規則時才豁免 exit 1，並補齊 driver 整合測試。 |
| 4 | High | 已修 12b3a929 | 0 | `12b3a929` 從 Dockerfile 移除 `python3-yaml`、升級測試移除 `--system-site-packages`，改由 wheelhouse/bundle 自帶隔離 PyYAML，並在 `tests/test_rust_mkbundle.py` 新增 `test_bundle_runtime_contains_isolated_pyyaml` 回歸。 |
| 5 | Medium | 已修 12b3a929 | 0 | `12b3a929` 提供 `--release-bundle` 建置模式，正式配方不帶 `sw-cli/test-fakes`；`ac6c4e4e` 進一步於 release.yml 與 Rust CLI 排除測試委派與故障注入鉤子，並由 `test_release_profile_binary_excludes_test_hooks` 實證。 |
| 6 | Low | 已修 12b3a929 | 0 | `12b3a929` 更新 `docs/rust-refactor/release-0.4.0-plan.md`，發佈前置條件改為對齊 gate 實際產出的 `VERIFIED` 或符合條件之 `IMPLEMENTED`（僅限核准之 `BLOCKED-OWNER`）。 |

## B 新發現

NO FINDINGS

本輪範圍 `dff65c08..79cbb40e` 僅包含單一 commit `79cbb40e`（`test(sw-cli): 補強 setup daemon restart 回歸`），變更僅限 `crates/sw-cli/tests/mgmt_setup.rs`（+160 / -6）：
1. 抽取共用測試函式 `s1m241_setup_stale_service_is_restarted_and_verified_case`，強化斷言 `daemon_code` 的 `daemon`、`installed` 指紋欄位，以及執行順序（`daemon-reload` < `enable` < `restart`），並斷言 `restart` 只呼叫恰好一次。
2. 新增 `s1m241_setup_stale_daemon_without_fingerprint_is_restarted`，覆蓋舊 daemon 未回傳指紋時觸發重啟並在重啟後驗證指紋 match。
3. 新增 `s1m241_setup_system_scope_with_sudo_restarts_after_enable`，鎖定 `--system --with-sudo` 模式下的重啟順序與單次執行。
4. 強化 `s1m241_assessment_stale_systemd_pending_sudo_is_reported` 對 `pending_sudo` 內容及無 sudo 執行路徑之斷言。

所有改動皆為加嚴測試與補強覆蓋，未變動產品程式碼，未放寬或削弱任何斷言。`cargo test -p sw-cli --test mgmt_setup` 58 案全數通過，`python3 -m tools.contracts check`（findings: []）與 `python3 -m policy_check --repo .`（25 pass、0 fail、2 warn）皆全綠。

## 既有碼待辦（不計入結論）

無

## 對裁決的異議（不計入結論）

無

結論：可放行
