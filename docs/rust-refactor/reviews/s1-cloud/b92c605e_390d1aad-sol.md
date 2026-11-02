> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

I have launched `cargo test -p sw-cli --features test-fakes` in the background and will wait for it to complete.
I have launched the regression guard pytest suite in the background and will wait for it to complete.
## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `978aa1e1` | 0 | [test_cli_mgmt_daemon.py:143](file://tests/contract_blackbox/test_cli_mgmt_daemon.py#L143) 移除單純時間輪詢的 `_wait_dut_tx`，改以 `_checkpointed_dut_tx` 呼叫 bridge 的 barrier／checkpoint 排空非同步 wire 紀錄並封鎖視窗；在 `test_autostart_l15_tx_once` 停止 daemon 後取得封閉視窗比對 `final_tx == b"echo X\n"`，並補上 `test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 測試。 |
| 2 | Medium | 已修 `51bd8fa5` | 0 | [exec_delegate.rs:168](file://crates/sw-pal/tests/exec_delegate.rs#L168) 改由實際受委派執行的孫行程寫出 `DELEGATE_READY_ENV` 與 PID；父行程透過 `win_ctrl::process_is_running(delegate_pid)` 確認孫行程存活後才發送控制事件，並於事件後確認其終止；新增 [test_rust_exec_delegate.py](file://tests/test_rust_exec_delegate.py) 作為回歸守衛。 |
| 3 | Medium | 已修 `ddcbdde0` | 0 | 新增 [l15_exit_driver.py](file://scripts/rust/l15_exit_driver.py) 串接 [stage1_exit_driver.sh](file://scripts/rust/stage1_exit_driver.sh)；保留原始退出碼，僅在非零步驟僅為 `win_l15_func_test 1` 且經 gate 核准為 OD-30～OD-32 允許之 `BLOCKED-OWNER` 時才予以豁免，其餘皆維持 fail-closed，並由 `4e3ba13f` 補強逐列證據比對與 [test_rust_stage1_exit_driver.py](file://tests/test_rust_stage1_exit_driver.py) 整合測試。 |
| 4 | High | 已修 `12b3a929` | 0 | [upgrade.Dockerfile](file://tools/docker/upgrade.Dockerfile) 移除預裝 `python3-yaml`，[run_upgrade_container.py](file://scripts/docker/run_upgrade_container.py) 移除 `--system-site-packages`，改用 wheelhouse 離線提供；bundle 組裝自帶隔離 Python runtime 與 PyYAML，並以 [test_rust_mkbundle.py](file://tests/test_rust_mkbundle.py) 的 `test_bundle_runtime_contains_isolated_pyyaml` 斷言隔離環境可用。 |
| 5 | Medium | 已修 `12b3a929` | 0 | [mkbundle.sh](file://scripts/rust/mkbundle.sh) 新增 `--release-bundle` 選項，正式建置不啟用 `sw-cli/test-fakes` feature；[release-0.4.0-plan.md](file://docs/rust-refactor/release-0.4.0-plan.md) 發佈配方更新為 `--release-bundle`，並於 [test_rust_mkbundle.py](file://tests/test_rust_mkbundle.py) 新增 `test_release_bundle_build_does_not_enable_test_fakes` 守衛。 |
| 6 | Low | 已修 `12b3a929` | 0 | [release-0.4.0-plan.md](file://docs/rust-refactor/release-0.4.0-plan.md) 前置條件已修正為實際 gate 狀態：接受 `VERIFIED`，或 `IMPLEMENTED` 且僅剩 OD-27～OD-32 明確允許之 `BLOCKED-OWNER` 列。 |

---

## B 新發現

NO FINDINGS

本輪審查範圍 `b92c605e..390d1aad` 包含 1 個非 merge commit（無 merge commit）：
- `390d1aad` `fix(sw-pal): 修正 fake ProcScan 重拉 socket 路徑`：
  - [crates/sw-pal/src/fakes/proc.rs](file://crates/sw-pal/src/fakes/proc.rs#L272) 對重拉 daemon 填入的 socket 路徑加上 `.strip_prefix("unix://").unwrap_or(endpoint)`，將 URL 形式還原為與真實行程 `--socket` 參數一致的裸路徑。
  - 此改動使 `TestEnv` 下 relaunched daemon 與初始 daemon 的 `DaemonProc.socket` 欄位語意對齊，消除 Windows 下因 `unix://C:\...` 包含磁碟代號而被 `parse_endpoint()` 誤判為非法 netloc 之假性失敗，且在 POSIX / Linux 上解析仍為 `Endpoint::Unix`，未引入任何破壞性變更或新缺陷。
  - 新增 changelog fragment [changelog.d/s1-win-w2b-fake-proc-socket.md](file://changelog.d/s1-win-w2b-fake-proc-socket.md) 符合命名與內容規範。

---

## 既有碼待辦（不計入結論）

無

---

## 對裁決的異議（不計入結論）

無

---

結論：可放行
