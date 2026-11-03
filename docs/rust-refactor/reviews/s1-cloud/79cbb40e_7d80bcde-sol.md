> 審查者：pi＋gemini-4-argon（11 月 argon 已花 $39.56）（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1 | 0 | `978aa1e1` 引入 `_checkpointed_dut_tx`（bridge barrier + checkpoint 視窗），在 daemon 停止後對封閉 wire 區間做最終比對，並新增 `test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 延遲落盤回歸測試。 |
| 2 | Medium | 已修 51bd8fa5 | 0 | `51bd8fa5` 改由實際受委派之孫行程 helper 寫出 delegate ready 標記與 PID，父行程以 `win_ctrl::process_is_running(delegate_pid)` 確認存活後才發送控制事件，並由 `tests/test_rust_exec_delegate.py` 守護。 |
| 3 | Medium | 已修 ddcbdde0 | 0 | `ddcbdde0` 新增 `scripts/rust/l15_exit_driver.py` 並接通 `stage1_exit_driver.sh`，`4e3ba13f` 進一步補強逐列核對原始報告與 metadata，僅在所有非 PASS 列皆精確符合核准延後規則時才豁免非零退出碼，並補齊 driver 整合測試。 |
| 4 | High | 已修 12b3a929 | 0 | `12b3a929` 從 Dockerfile 移除 `python3-yaml`、升級測試移除 `--system-site-packages`，改由 wheelhouse 與 bundle 自帶隔離 PyYAML，並在 `tests/test_rust_mkbundle.py` 新增 `test_bundle_runtime_contains_isolated_pyyaml` 回歸。 |
| 5 | Medium | 已修 12b3a929 | 0 | `12b3a929` 新增 `--release-bundle` 建置模式，正式發佈配方不帶 `sw-cli/test-fakes`；`ac6c4e4e` 進一步在 release 流程與 Rust CLI 排除測試委派與故障注入鉤子，並由 `test_release_profile_binary_excludes_test_hooks` 驗證。 |
| 6 | Low | 已修 12b3a929 | 0 | `12b3a929` 更新 `docs/rust-refactor/release-0.4.0-plan.md`，發佈前置條件改為對齊 `stage1a_gate.py` 實際輸出的 `VERIFIED` 或符合核准延後規則的 `IMPLEMENTED`。 |

## B 新發現

NO FINDINGS

本輪範圍 `79cbb40e..7d80bcde` 僅含單一 commit `7d80bcde`（`test(sw-cli): 補齊 setup 重啟結果回歸`），僅修改 `crates/sw-cli/tests/mgmt_setup.rs`（+185 / -0）：
1. 新增 `s1m241_failure_after_mismatch_preserves_python_stderr_and_exit_zero`：驗證重啟後首次成功 `health.ping` 回傳不同指紋時，立即回報 `action: "restarted"`、`after: "mismatch"`、完整 `hint` 與逐字對齊 Python 基線的 `stderr`，不額外 sleep 等待逾時，且以 exit 0 結束。
2. 新增 `s1m241_failure_after_unreachable_preserves_python_stderr_and_exit_zero`：驗證重啟後始終不可達時，輪詢滿 10 秒（20 次 0.5 秒 sleep）後回報 `action: "restarted"`、`after: "unreachable"`、完整 `hint` 與 `stderr`，且以 exit 0 結束。
3. 新增 `s1m241_failure_restart_error_preserves_python_hint_without_postcheck`：驗證 `systemctl --user restart serialwrap` 失敗（rc=1）時，回報 `setup.daemon_restart: "failed"`、`daemon_code.action: "restart_failed"` 與 Python 原文 `hint`，不輸出 `after`，且不進入重啟後 `health.ping` 輪詢驗證。
4. 新增 `s1m241_failure_retries_unreachable_then_reports_match`：驗證重啟後先不可達、0.5 秒後重試可達且指紋匹配時，最終回報 `action: "restarted"`、`after: "match"`，無 `hint` 與 `stderr` 輸出。
5. `s1m241_failure_assert_report` 共同斷言每案 `stdout` 恰為單行 JSON 且 `daemon_code` 不外洩內部 `checked` 欄位。

實測驗證：`CARGO_TARGET_DIR=/tmp/solrev-s1-target cargo test -p sw-cli --features test-fakes --test mgmt_setup`（62 passed）、`python3 scripts/rust/check_cfg_boundary.py`、`python3 -m tools.contracts check`（`ok: true, findings: []`）、`python3 tests/acceptance/regen_lock.py --check`、`python3 -m policy_check --repo .`（25 pass, 0 fail, 2 warn）皆通過。

## 既有碼待辦（不計入結論）

- `crates/sw-cli/src/setup_cmd.rs:2094`（源自 `68282f89`）：`verify_daemon_code_after_restart` 直接沿用 `reconcile` 前解析的 `&old_endpoint`，而 Python 基線 `sw_core/cli.py:1794` 於每次輪詢時重新呼叫 `_resolve_local_endpoint_info(args)`。若執行 `setup` 前 `config.yaml` 指向舊的或自訂可達 socket，經 `reconcile` 寫回 canonical `effective_socket` 並重啟 systemd 服務後，Rust 端仍會對舊 `old_endpoint` 輪詢而非重新解析更新後的設定。

## 對裁決的異議（不計入結論）

無

結論：可放行