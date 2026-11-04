> 審查者：pi＋gemini-4-argon（11 月 argon 已花 $39.94）（代理 sol 單審位，擁有者 2026-11-02 指定）

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

- **S1-ca19dd1-1**: [Medium] `tests/test_s1m241_cli_fingerprint_parity.py:227`（並由 `ca19dd18` 登記於 `tests/acceptance/l1_required.lock.json:9554` 與 `docs/rust-refactor/s1m-241-parity.md:50`）— **`_write_systemctl_shim` 在 `daemon.stop` 後僅等待 `health.ping` 斷線即啟動新 daemon，未等舊 daemon 釋放 `serialwrapd.lock` 的 `flock`，且跨 PID namespace 誤寫局部 PID，導致本輪登記之 L1 必跑測試 `test_s1m241_setup_stale_systemd_restarts_and_verifies` 實跑紅燈。**
  - **具體失敗情境**：
    1. 本輪 `ca19dd18` 將 `tests/test_s1m241_cli_fingerprint_parity.py::test_s1m241_setup_stale_systemd_restarts_and_verifies` 正式納入 `l1_required.lock.json` 的 `s1m241_pytest` L1 必跑鎖，並於 `docs/rust-refactor/s1m-241-parity.md:50` 宣稱作為 S1M-241 驗收證據；`1c083db2` 亦在同檔擴充呼叫 `_write_systemctl_shim`。
    2. 依 `sw_core/daemon.py:310-313`，Python daemon 收到 `daemon.stop` 後的 `finally` 順序為：`await server.stop()`（先關閉 Unix socket，此時 `health.ping` 立即回傳 `None`）→ `service.stop()` → `lock.release()`（最後才釋放 `SERIALWRAP_TEST_LOCK` 的 `flock`）。
    3. `_write_systemctl_shim`（`tests/test_s1m241_cli_fingerprint_parity.py:227-235`）送出 `rpc("daemon.stop")` 後，只要看到 `rpc("health.ping") is None` 就立刻 `break` 並 `subprocess.Popen` 啟動新 `serialwrapd.py`，完全未確認 `SERIALWRAP_TEST_LOCK` 的 `flock` 已釋放。當舊 daemon 仍在執行 `service.stop()` 尚未執行 `lock.release()` 時，新 daemon 在 `SingletonLock.acquire()`（`sw_core/lock_posix.py:21`）呼叫 `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` 遭遇 `BlockingIOError`，印出 `serialwrapd: DAEMON_ALREADY_RUNNING` 並以 `rc=2` 結束，致使 `systemctl --user restart serialwrap` 失敗。
    4. 實測執行 `ca19dd18` 登記的 `s1m241_pytest` selector（`CARGO_TARGET_DIR=/tmp/solrev-s1-target python3 -m pytest -q tests/test_s1m241_cli_fingerprint_parity.py tests/test_build_fingerprint.py`）以及單獨連跑該測試 4 次，皆重現失敗：
       - 預期輸出：`daemon_code["action"] == "restarted"`、`payload["setup"]["daemon_restart"] == "ran"`。
       - 實際輸出：`AssertionError: daemon_code={'action': 'restart_failed', 'daemon': 'cab4809c72db', 'hint': 'systemctl restart 失敗；請手動執行 `serialwrap service restart` 並查 journalctl', 'installed': '347ea96fa479', 'reason': 'fingerprint_mismatch', 'stale': True}, setup={'applied': True, 'daemon_restart': 'failed', ...}, daemon='serialwrapd: no profiles loaded\nserialwrapd: DAEMON_ALREADY_RUNNING\n'`。
    5. 此外，`cli setup --user` 是在 `_isolated_pidns_prefix()`（`unshare --user --map-root-user --pid --fork --mount-proc`）建立的子 PID namespace 內執行，`_write_systemctl_shim`（lines `259-268`）在子 `/proc` 讀到的 `proc.pid` 與 `NSpid` 是**子 PID namespace 內的局部小整數 PID**（且子 PID namespace 的 PID 1 即 `serialwrap setup` 結束時，kernel 已自動終止該子 namespace 內的新 daemon）；但 `_write_systemctl_shim` 將此局部 PID 寫入 `SERIALWRAP_TEST_PID_FILE`，導致外層 `finally`（lines `456-467`）在 host PID namespace 讀取該小整數 PID 並對外層 `/proc/<pid>/stat` 檢查與呼叫 `os.kill(current_pid, signal.SIGTERM)`，既有跨 namespace 誤殺外層同 UID 行程的風險，也覆蓋了原本外層舊 daemon 的 PID。
  - **確切修法**：
    1. 在 `_write_systemctl_shim`（`tests/test_s1m241_cli_fingerprint_parity.py:227-235`）中，`rpc("daemon.stop")` 後除了等待 `rpc("health.ping") is None` 與 socket 消失外，必須一併輪詢確認 `os.environ["SERIALWRAP_TEST_LOCK"]` 可成功取得非阻塞排他鎖 `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)`（取得後立即 `LOCK_UN` 並 `os.close(fd)`），確認舊 daemon 已完成 `finally: ... lock.release()` 後，才複製 `sw_core` 並啟動新 `serialwrapd.py`。
    2. 移除 `_write_systemctl_shim` 將子 PID namespace 局部 PID 寫入外層 `SERIALWRAP_TEST_PID_FILE` 的邏輯（或區分外層舊 daemon `daemon` handle 與子 namespace 內行程，外層 `finally` 只以 `_send_rpc(str(endpoint), "daemon.stop")` 及外層 `daemon` `Popen` 物件回收，不拿子 PID namespace 的局部 PID 在外層呼叫 `os.kill`）。
    3. 修正後連跑 `test_s1m241_setup_stale_systemd_restarts_and_verifies` 至少 5 次及完整 `s1m241_pytest` selector 確認全綠。

## 既有碼待辦（不計入結論）

- `crates/sw-cli/src/setup_cmd.rs:2094`（源自 `68282f89`）：`verify_daemon_code_after_restart` 直接沿用 `reconcile` 前解析的 `&old_endpoint`，而 Python 基線 `sw_core/cli.py:1794` 於每次輪詢時重新呼叫 `_resolve_local_endpoint_info(args)`。若執行 `setup` 前 `config.yaml` 指向舊的或自訂可達 socket，經 `reconcile` 寫回 canonical `effective_socket` 並重啟 systemd 服務後，Rust 端仍會對舊 `old_endpoint` 輪詢而非重新解析更新後的設定。

## 對裁決的異議（不計入結論）

無

結論：需修正