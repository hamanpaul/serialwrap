> 審查者：grok-4.7（代理 sol 單審位，擁有者 2026-11-02 指定）

我會以唯讀方式做增量審查：先核對前輪六項 finding 的現況，再只看 `ca19dd18..25ceaff5` 是否引入新缺陷。這輪只有三個 commit，都集中在指紋與 flock。我接著對照前輪六項的程式位置，並逐一看這三個 diff。三個 commit 的 diff 已取得。接著核對前輪六項是否仍在，並把指紋錯誤鍵、flock 等待與 L1 鎖的呼叫路徑讀完。前輪六項的程式已對過，接著只跑本輪三個 commit 直接鎖住的測試，確認沒有新的失敗路徑。## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1 | 0 | `_wait_dut_tx` 已不在 `tests/contract_blackbox/test_cli_mgmt_daemon.py`。現行 `_checkpointed_dut_tx` 先要求 barrier `settled`，再以 checkpoint 的 `wire_len` 與 SHA-256 封住前綴。SC-18 在 `daemon stop` 之後才做最終比對，斷言 `final_tx == b"echo X\n"`。`test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 在 barrier handler 內寫入第二筆 `echo X\n`，要求視窗為 `b"echo X\necho X\n"`。 |
| 2 | Medium | 已修 51bd8fa5 | 0 | `crates/sw-pal/tests/exec_delegate.rs` 的 ready 由 `exec_or_wait` 拉起的孫行程寫入 `delegated grandchild pid=<pid>`。父行程最多等 10 秒，逾時或子行程先退出則 panic；確認該 PID 仍存活後才送 Ctrl 事件，並要求結束碼 `0xC000013A` 且該 PID 已退出。非 Windows 組態直接 panic。 |
| 3 | Medium | 已修 4e3ba13f | 0 | `ddcbdde0` 已把出口接到 `scripts/rust/l15_exit_driver.py`，`4e3ba13f` 補上逐列核對。`stage1_exit_driver.sh` 保留各 step 原始退出碼，最後只看摘要的 `failed_steps`。豁免限於 gate overall 為 `IMPLEMENTED` 或 `VERIFIED`、`errors` 為空，且每筆非 PASS 的 L1.5 列都符合核准延後規則。`harness_stop_failed`、缺報告與其他非零 step 仍使 `failed_steps` 非 0。整合回歸含 `test_exit_driver_cli_reads_both_raw_reports_and_execution_metadata` 與 `test_driver_filters_windows_l15_rc_only_through_owner_deferred_gate_summary`。 |
| 4 | High | 已修 12b3a929 | 0 | 升級容器已無 `--system-site-packages`，`tools/docker/upgrade.Dockerfile` 也無 `python3-yaml`。舊版走 `pipx install --no-index --find-links`。正式配方為 `mkbundle.sh --release-bundle`，runtime 要求 `include-system-site-packages = false` 並自帶 PyYAML。`test_upgrade_environment_does_not_hide_missing_pyyaml` 鎖住上述來源；release 計畫的容器 smoke 在只裝 `python3.12` 的環境以 bundle runtime 匯入 `yaml`。本輪未重跑 Docker。 |
| 5 | Medium | 已修 12b3a929 | 0 | `docs/rust-refactor/release-0.4.0-plan.md` 第 20 列為 `mkbundle.sh --release-bundle --profile release`，內容寫明不含 `sw-cli/test-fakes`。`mkbundle.sh` 只在 `--dev` 傳 `--features test-fakes`，且與 `--release-bundle` 互斥。`crates/sw-cli/src/rollback.rs` 對 `SW_TEST_UPDATE_PAUSE_AFTER`、`SW_TEST_UPDATE_FORCE_VERIFY_FAIL` 的讀取與 panic 都在 `#[cfg(feature = "test-fakes")]` 內；未啟用該 feature 時 `test_pause_after` 為空函式。`ac6c4e4e` 另以同一 feature 閘住 `SERIALWRAP_TEST_DELEGATE_GROUP`。 |
| 6 | Low | 已修 12b3a929 | 0 | 計畫第 10 列改為接受 `VERIFIED`，或 `IMPLEMENTED` 且只剩 OD-27～OD-32 的 `BLOCKED-OWNER`、同時沒有 FAIL／INSUFFICIENT。`overall: PASS` 已不在該條件。 |

## B 新發現

本輪 `ca19dd18..25ceaff5` 為 3 個非 merge commit：`28317950`、`10a67591`、`25ceaff5`。

`10a67591` 讓結構合法、指紋非 12 位小寫十六進位的 manifest，以 `components.daemon.code_fingerprint` 作為 `version_pairing` 錯誤鍵；缺欄仍略過。判定順序與 `parse_manifest` 相同。`CARGO_TARGET_DIR=/tmp/solrev-s1-target cargo test -p sw-cli --test mgmt_doctor version_pairing_invalid_code_fingerprint_uses_its_own_key -- --exact` 為 1 passed。

`28317950` 的 systemctl 替身在 `health.ping` 失敗、socket 不存在、且 flock 可取得後才啟動新 daemon，並停止把 namespace 內 PID 寫回外層 pid 檔。`python3 -m pytest -p no:cacheprovider -q tests/test_s1m241_cli_fingerprint_parity.py::test_s1m241_systemctl_shim_waits_for_lock_and_keeps_outer_pid` 為 1 passed（0.84s）。doctor 共用檢查數 18 改為 19，與 Python、Rust 的 Linux 清單一致（含 `daemon_code_current`）；檢查名稱與逐項欄位比對仍在。

`25ceaff5` 的 `tests/acceptance/l1_required.lock.json` 只新增上述 cargo 節點（5 個既有鄰接集合各一筆）與 pytest 節點（1 筆，緊接既有 `test_s1m241_setup_stale_systemd_restarts_and_verifies`）。

NO FINDINGS

## 既有碼待辦（不計入結論）

`crates/sw-pal/tests/exec_delegate.rs` 尚未另備「子行程在 `exec_or_wait` 前停住、不建立孫行程」的突變案例。現行 Windows 測試要等孫行程寫出的 PID 標記，逾時會 panic。

## 對裁決的異議（不計入結論）

無

結論：可放行