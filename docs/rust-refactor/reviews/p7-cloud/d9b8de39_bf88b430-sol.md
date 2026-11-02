> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 `f150a24` | 0 | [`crates/sw-core/src/event_engine/dispatcher.rs`](file://crates/sw-core/src/event_engine/dispatcher.rs#L191-L202)：deadline 改用 `checked_add`，名額以 RAII（`Release` guard）釋放，worker 以 `catch_unwind` 逐 job 收斂 panic；超大 timeout 走非 OSError 錯誤返回，worker 與併發名額均正常保留。新增回歸測試。 |
| 2 | Medium | 已修 `f150a24` | 0 | [`crates/sw-core/src/event_engine/mod.rs`](file://crates/sw-core/src/event_engine/mod.rs#L269-L283)：正式 EventEngine 改接 `Dispatcher::start_fallible`；完成事件寫檔失敗即中斷 counter 與 cooldown 更新，對齊 Python `_emit` 穿出行為。新增回歸測試。 |
| 3 | Medium | 已修 `f150a24` | 0 | [`crates/sw-core/src/session_core.rs`](file://crates/sw-core/src/session_core.rs#L761-L770)：改以 `foreground_busy()` 發布分類旗標，排除 interactive open/send TX 對前景 command scope 分類的干擾。新增 interactive TX 與前景命令對照測試。 |
| 4 | Medium | 已修 `f150a24` | 0 | [`crates/sw-core/src/session_core.rs`](file://crates/sw-core/src/session_core.rs#L4501-L4505)：先安裝 `self.log_capture` 再組裝回應字典，確保首次呼叫的回應中巢狀 `session.capture` 與頂層欄位一致且非空。新增首次回應一致性測試。 |

---

## B 新發現

NO FINDINGS

### 審查範圍核對說明（`d9b8de39..bf88b430`）
1. **`bf88b430`（test: 校正 P7 凍結節點收集歸屬）**：
   - [`tests/acceptance/l1_required.lock.json`](file://tests/acceptance/l1_required.lock.json)：
     - **OD-70**：以 `--allow-removal` 逐名從 `p7_gate:l15-slice21-b` 移出 `test_case[rust-SERIALWRAP_L15_RUST_DAEMON-s21-event-latency]`。
     - **OD-72**：從 `p7_gate:contract-blackbox-slice21-b`（Linux 集合）移出 7 個 `test_windows_nocom.py` 節點並改由 `s0_win_pytest.windows` 承擔；移出 `test_stale_context_not_replayed`（DF-20 deferred）但保留於 `p7_gate:python3-m-pytest-q-tests`。
     - 伴隨合入 P5 帶入之 L1 新增節點，鎖檔 add-only 推進合規，`python3 tests/acceptance/regen_lock.py --check` 通過。
   - [`tests/test_p7_gate_collection.py`](file://tests/test_p7_gate_collection.py)：
     - 新增 [`test_p7_gate_frozen_nodes_are_collectable`](file://tests/test_p7_gate_collection.py#L21) 驗證 P7 gate 所有凍結 nodeid 皆可被 selector 收集。
     - 新增 [`test_od72_slice21_nodes_keep_their_assigned_owner`](file://tests/test_p7_gate_collection.py#L44) 斷言移出節點之平台與集合承擔歸屬。
     - 測試標註 `[pytest.mark.posix_only, pytest.mark.process_rpc]` 完全符合 P0 T14 規範；實測 2 passed，`test_resource_markers.py` 9 passed。
2. **`d9b8de39`（merge: 合入 P5 第八輪修正）**：
   - 唯一衝突檔案 [`crates/sw-core/src/uart_bridge.rs`](file://crates/sw-core/src/uart_bridge.rs#L807-L880)：完整保留 P7 的 [`RunCancel`](file://crates/sw-core/src/uart_bridge.rs#L812)、[`WaitOutcome`](file://crates/sw-core/src/uart_bridge.rs#L841)、[`wait_for_regex_cancellable`](file://crates/sw-core/src/uart_bridge.rs#L851) 與 [`RxObserver`](file://crates/sw-core/src/uart_bridge.rs#L910)，並正確合入 P5 的 [`wait_for_rx_dispatch`](file://crates/sw-core/src/uart_bridge.rs#L916)。
   - `cargo check --workspace` 與 `cargo check --workspace --target x86_64-pc-windows-gnu` 均通過，未破壞既有行為。
3. **治理檢查**：
   - `python3 -m tools.contracts check`：`ok: true, findings: []`。
   - `python3 scripts/rust/check_cfg_boundary.py`：exit 0。
   - `python3 -m policy_check --repo .`：25 pass / 0 fail / 2 warn（非 PR 預期告警）。

---

## 既有碼待辦（不計入結論）

無。

---

## 對裁決的異議（不計入結論）

無。

---

結論：可放行
