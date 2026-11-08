> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

無（本輪為首輪，無前輪報告）。

## B 新發現

審查範圍 cd57ecc3..2c5018f1 只有一個提交 2c5018f1，純測試與鎖檔新增，未改產品碼。已核對：

- 鎖檔 `tests/acceptance/l1_required.lock.json` 為純 add-only（40 行新增、0 刪除），6 筆 cargo 節點與 17 筆 pytest 節點分別進 `p7_gate:cargo-test-workspace`、`p7_gate:python3-m-pytest-q-tests`、`s21_gate:pytest_full`。
- `tests/acceptance/cases.yaml` 四個凍結案例 `s24-ho-ext-tool`／`s24-ho-second-daemon`／`s24-ho-persist-restart`／`s24-ho-windows-com` 的 `source_ref` 皆對應到實際存在的測試函式。
- 定向實測：`cargo test -p sw-core --features test-fakes --test handoff` 6 passed；`tests/test_resource_markers.py` 9 passed；`tests/test_acceptance_cases.py` 38 passed。
- 斷言對照 Python 基線（`sw_core/session_manager.py` 的 `release_device`／`attach_device`／self-test RELEASED 分支）：`FLASHING_BUSY` 鍵序、`closed_consoles` 含 primary vtty、`DEVICE_NOT_PRESENT` 帶 `device_by_id`、並發 release 第二個加入同一 drain 回完整形狀、self-test `reclaimable = not pids` 皆與基線一致。`_same_uid_proc_fd_unreadable` 的「只看同 uid」條件對齊基線 S1X-a-6 修正與 Rust `posix_uart_holder_scan_covers_any_same_uid_process_and_skips_other_uids`，且僅在 `is_rust()` 時生效，符合 BC-04 `ab_participation: diff` 先例。
- `HolderSampler` 只用在非 force attach 路徑（預檢即擋、daemon 不開檔），force 路徑（會短暫 open→重掃→close）只以回覆後的單次 `holders()` 判定，不會因取樣撞到試開窗口而假紅。

無 High／Medium。Low 如下：

- **P8X-2c5018f-1** [Low] `crates/sw-core/tests/handoff.rs:135` — `elapsed_ms >= 3000` 為空斷言：`t0` 取於 `release_async` 前、`wait_state` 不推進 FakeClock（`support/mod.rs:1623`／`1799` 只以真實時間輪詢）、`recv` 在 `advance(3001)` 之後，故 `elapsed_ms` 恆為 3001，Rust 若把 drain 逾時縮成 500 ms 此斷言仍過（下界實際由 `tests/session_manager.rs:1279` 守住，故不升級）。修法：`wait_state("RECOVERING")` 後先 `h.clock.advance(2999 ms)`、小睡後 `assert!(pending.try_recv().is_err())`，再 `advance(2 ms)` 收回覆，使「不得早於 3,000 ms」真的有效。
- **P8X-2c5018f-2** [Low] `tests/contract_blackbox/test_s24_device_handoff.py:35` — `_RELEASE_ARGS` 定義後未使用（`_release()` 自行展開參數）。修法：刪除或改由 `_release()` 使用。
- **P8X-2c5018f-3** [Low] `tests/contract_blackbox/test_s24_device_handoff_windows.py:126` — 直接寫 `sw_env._env[...]` 私有屬性注入 `SERIALWRAP_TEST_DEVICE_ALLOWLIST`。修法：在 `SwEnv` 加公開 `set_env(key, value)`（或 `prepare(..., extra_env=)`），避免 fixture 內部改名時無聲失效。
- **P8X-2c5018f-4** [Low] `tests/acceptance/l1_required.lock.json` — Windows 黑箱 3 個 nodeid 未登記任何集合（commit 訊息自述「待原生收集」）。既有鎖檔在 linux 集合亦未登記任何 `contract_blackbox/*_windows.py`，與現行慣例一致，列此只為提醒 OD-15 擁有者 host gate 執行時需另有登記出口（`results-rust-windows.json`），否則 `s24-ho-windows-com` 只靠 cases.yaml `source_ref` 追溯。

## 既有碼待辦（不計入結論）

- commit 訊息自報 Rust artifact 黑箱 6 紅（`DEVICE_STILL_HELD` 多帶 `session`、RELEASED 經重啟／重插後 attach 回 `DEVICE_NOT_PRESENT`、OD-63 佔位未實作），皆屬 P5／OD-63 既有路徑，依計畫 Task 11「不代修、回報 P5 owner」處理；本輪未見新的 Rust 側偏差。

## 對裁決的異議（不計入結論）

無。

結論：可放行
