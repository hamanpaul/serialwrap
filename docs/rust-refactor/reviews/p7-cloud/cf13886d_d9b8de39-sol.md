> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

The background task running `CARGO_TARGET_DIR=/tmp/solrev-p7-target cargo test -p sw-pal --features test-fakes` is currently executing. Waiting for it to finish.
The task running the `sw-core` tests is executing. Waiting for completion.
The task running `test_slice21_gate_judge.py`, `test_l1_judge.py`, and `test_rust_cargo_l1_results.py` is executing. Waiting for completion.
The test runner is executing. Waiting for completion.
## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `f150a246` | 0 | [`crates/sw-core/src/event_engine/dispatcher.rs`](file://crates/sw-core/src/event_engine/dispatcher.rs#L188)：在 [`sw-pal`](file://crates/sw-pal/src/process.rs) 新增 `py_communicate_timeout_overflows`，期限溢位時回報非 OSError 之 `SpawnFailed(InvalidInput)`；`start + timeout` 改採 `checked_add`；dispatcher 名額改採 RAII guard（`Release`）；worker 迴圈加上 `catch_unwind` 防止 panic 造成 worker 終止（後續亦於 `030d294f` 確保溢位時維持管線開啟直到子行程結束）。回歸測試 [`huge_timeout_native_handler_then_normal_rule_completes`](file://crates/sw-core/tests/event_dispatcher.rs) 與 [`handler_panic_releases_slot_and_worker_survives`](file://crates/sw-core/tests/event_dispatcher.rs) 均已建立且通過。 |
| 2 | Medium | 已修 `f150a246` | 0 | [`crates/sw-core/src/event_engine/mod.rs`](file://crates/sw-core/src/event_engine/mod.rs#L269)：新增 [`Dispatcher::start_fallible`](file://crates/sw-core/src/event_engine/dispatcher.rs#L340)（`FallibleEmit`），`EventEngine` 改用可傳遞寫檔失敗的 emit 閉包；完成類事件寫檔失敗時立即返回，不更新 counter 與 cooldown（對齊 Python `_emit` 例外穿出 `_run_handler` 行為），併發名額由 RAII 正常釋放。回歸測試 [`completion_log_write_failure_skips_counter_and_cooldown`](file://crates/sw-core/tests/event_engine.rs) 已建立且通過。 |
| 3 | Medium | 已修 `f150a246` | 0 | [`crates/sw-core/src/session_core.rs`](file://crates/sw-core/src/session_core.rs#L765)：發布至 `deps.fence` 的 `foreground_busy` 改為呼叫 `self.foreground_busy()`（只計命令，與 capture gate 同源），不再誤將 interactive open/send 的 `self.foreground` 視為前景命令。回歸測試 [`interactive_send_tx_in_flight_is_not_foreground_for_events`](file://crates/sw-core/tests/event_scope_p7.rs) 與 [`foreground_command_in_flight_is_foreground_for_events`](file://crates/sw-core/tests/event_scope_p7.rs) 均已建立且通過。 |
| 4 | Medium | 已修 `f150a246` | 0 | [`crates/sw-core/src/session_core.rs`](file://crates/sw-core/src/session_core.rs#L4505)：`log_start` 改為先安裝 `self.log_capture` 與 observer，再呼叫 `self.session_dict(now)` 組裝回應，確保首次回應中的巢狀 `session.capture` 帶有正確的 active capture 物件與零計數。回歸測試 [`first_log_start_reply_session_has_active_capture`](file://crates/sw-core/tests/capture_p7.rs) 已建立且通過。 |

---

## B 新發現

NO FINDINGS

### 審查範圍與核對要點（`cf13886d..d9b8de39`）

本輪審查範圍共包含 7 個 commit（6 個非 merge commit 及 1 個 merge commit）：
1. `8d824b3a` fix(test,gate): 修正 `sw-pal` 測試的 fd 重用競態（`invalid_serial_fd_is_a_poll_wait_error` 改用 `c_int::MAX`）與 fork 時窗競態（[`bind_stale_unix_socket`](file://crates/sw-pal/src/conformance/lock_suite.rs) 取代 bind+listen；PTY master link 比對防止誤判）；`slice21_gate.sh` `--caps` 依 OD-53 改由 L1 鎖 selector 推導。
2. `cc25b01d` fix(gate): `cargo_l1_results.py` 增設 `--caps-only` 選項，在執行能力測試時跳過預設 run，避免 root 環境下因非 root 權限測試造成誤判；[`slice21_gate.sh`](file://scripts/rust/slice21_gate.sh) 配合以 `nonroot_cmd` 降權執行。
3. `8c0c942a` docs(gate): 記錄 T22 gate 第七輪實跑證據與斷路器快照，純文件更新。
4. `200c10fd` fix: 對齊 OD-69，在 release／shutdown 回條前透過 `DrainDataBarrier` 排空讀取器已入列的同 epoch RX，並在回條前完成 capture 與 WAL；[`crates/sw-pal/src/proc_holders/posix.rs`](file://crates/sw-pal/src/proc_holders/posix.rs) 重掃 holder 時透過 `process_tree_pids` 排除自身行程樹，避免 daemon 在 fork→exec 窗口中繼承 tty 的子行程被誤判為外部 holder；`event_latency` 依裁決在 [`contracts/resource_limits.yaml`](file://contracts/resource_limits.yaml) 改為 `freeze_status: frozen`。
5. `5d732744` test: [`tests/acceptance/regen_lock.py`](file://tests/acceptance/regen_lock.py) 支援集合名稱含冒號（如 `s21_gate:*`）的 `--allow-removal` 解析，並在平台全數移除時清理空鍵（落實編排者裁決：P5 交付 regen_lock 修正以解除 P7 的 OD-70/OD-72 阻擋）；新增 `s21_gate:py_parity_76_80` 集合。
6. `bedc8deb` test: 修正 [`crates/sw-core/tests/lease_selftest.rs`](file://crates/sw-core/tests/lease_selftest.rs) 中 `selftest_probe_drain_race_golden` 的時鐘推進順序，等待真實 release 回覆後再推動 FakeClock，消除人為時鐘競態。
7. `d9b8de39` merge: 合入 P5 第八輪修正。唯一衝突點位於 [`crates/sw-core/src/uart_bridge.rs`](file://crates/sw-core/src/uart_bridge.rs)，衝突解法完整保留了 P7 尾部新增的 [`RunCancel`](file://crates/sw-core/src/uart_bridge.rs#L812)、[`WaitOutcome`](file://crates/sw-core/src/uart_bridge.rs#L841)、[`wait_for_regex_cancellable`](file://crates/sw-core/src/uart_bridge.rs#L851) 與 [`RxObserver`](file://crates/sw-core/src/uart_bridge.rs#L916)，並正確接入 P5 的 `wait_for_rx_dispatch`；語法與括號正確，建置及所有相關單元與整合測試均全綠（0 failed）。

### 驗證執行結果
- `CARGO_TARGET_DIR=/tmp/solrev-p7-target cargo check --workspace --tests`：通過（無錯誤）。
- `CARGO_TARGET_DIR=/tmp/solrev-p7-target cargo test -p sw-pal --features test-fakes`：通過（0 failed）。
- `CARGO_TARGET_DIR=/tmp/solrev-p7-target cargo test -p sw-core --features test-fakes --test drain_release_e2e --test io_worker --test lease_selftest --test event_dispatcher --test event_engine --test event_scope_p7 --test capture_p7`：全數通過（101 passed, 0 failed）。
- `python3 -m pytest -q tests/test_slice21_gate_judge.py tests/test_rust_cargo_l1_results.py`：全數通過（83 passed, 1 skipped, 0 failed）。
- `python3 -m tests.acceptance.regen_lock --check`：OK（鎖檔與 cases.yaml 一致）。
- `python3 -m tools.contracts check`：OK（ok: true, 0 findings）。
- `python3 -m policy_check --repo .`：OK（25 pass, 0 fail, 2 pre-existing warnings）。

---

## 既有碼待辦（不計入結論）

1. [`tests/test_l1_judge.py`](file://tests/test_l1_judge.py) 的 `test_l1_lock_frozen_set_excludes_capability_gated` 與 `test_l1_lock_cargo_sets_exclude_ignored_tests` 在 Linux 端執行時，會因 `s21_core.windows` 尚含能力與 `#[ignore]` 節點而報錯。此為已知且在 [p7-local-windows-todo.md](file://docs/rust-refactor/reviews/p7-local-windows-todo.md)、編排者裁決（P7-BO-1、P7-LW-1）及 [p7-fix-responses.md](file://docs/rust-refactor/reviews/p7-fix-responses.md) 記載的既有待辦，待原生 Windows 環境收集新 nodeid 後一對一替換移除。
2. [`tests/acceptance/regen_lock.py`](file://tests/acceptance/regen_lock.py) 的冒號集合支援已由 P5 交付並合入本分支，後續 P7 可依 OD-70 與 OD-72 執行 `--allow-removal` 移除相關節點。

---

## 對裁決的異議（不計入結論）

無

---

結論：可放行
