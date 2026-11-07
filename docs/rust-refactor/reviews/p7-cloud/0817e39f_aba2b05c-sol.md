> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `f150a246` | 0 | `crates/sw-core/src/event_engine/dispatcher.rs` 與 `crates/sw-pal/src/process/handler_posix.rs`：新增 `sw_pal::process::py_communicate_timeout_overflows` 識別期限溢位對齊 CPython 3.12 行為、計算期限改用 `checked_add`、worker 迴圈加入 `catch_unwind` 且名額以 RAII 釋放；回歸測試 `huge_timeout_native_handler_then_normal_rule_completes` 與 `handler_panic_releases_slot_and_worker_survives` 通過。 |
| 2 | Medium | 已修 `f150a246` | 0 | `crates/sw-core/src/event_engine/mod.rs` 與 `dispatcher.rs`：新增 `Dispatcher::start_fallible`，在完成事件寫檔失敗時立即返回、跳過 `post_fire_counter_update`（對齊 Python `_emit` 例外穿出），避免推進 counter 與 cooldown；回歸測試 `completion_log_write_failure_skips_counter_and_cooldown` 通過。 |
| 3 | Medium | 已修 `f150a246` | 0 | `crates/sw-core/src/session_core.rs:740`：發布事件分類旗標改用 `self.foreground_busy()`（與公開 session 及 capture gate 判準一致），interactive open／send 在途中不誤標為 foreground；回歸測試 `interactive_send_tx_in_flight_is_not_foreground_for_events` 通過。 |
| 4 | Medium | 已修 `f150a246` | 0 | `crates/sw-core/src/session_core.rs:4356`：`session.log_start` 改為先將 `log_capture` 安裝至 session 後再產生公開回應，對齊 Python 先更新 `session.active_capture` 再 `to_public_dict()` 的順序；回歸測試 `first_log_start_reply_session_has_active_capture` 通過。 |

---

## B 新發現

- **P7-aba2b05-1** [Low] `crates/sw-core/src/actor.rs:447-449` — 衝突解法殘留無效的重疊 match 分支 — 確切修法：
  合入 P6 時，在 `dispatch()` 函式的 match 區塊同時保留了前置的第 424–426 行與第 447–449 行兩組完全相同的分支（`ControlEvent::ConsoleConnected`、`ConsoleScan`、`HumanTxCompleted`，皆回傳 `(None, "...")`）。導致第 447–449 行為無法觸及的 dead pattern，產生編譯器警告 `warning: unreachable pattern`。功能層面兩分支行為完全一致，無執行期缺陷。確切修法：刪除 `crates/sw-core/src/actor.rs:447-449` 的重疊 match 分支，消除編譯器警告。

- **P7-aba2b05-2** [Low] `crates/sw-core/src/session_core.rs:33` — 未使用的符號引用 — 確切修法：
  合入 P6 分檔後，`crates/sw-core/src/session_core.rs:33` 檔頭仍匯入 `detect_boot_banner` 與 `matches_login_or_password`，產生編譯器警告 `warning: unused imports`。確切修法：自該行匯入清單中移除這兩個未使用的符號。

---

## 既有碼待辦（不計入結論）

1. `tests/test_p7_goldens_fresh.py::test_goldens_exist_and_are_fresh` 失敗（既有基線同步遺留）：先前合入 0.4.0 主線（`a681db61`）時，Python `sw_core/session_manager.py` 納入非同步 reboot 欄位 `boot_evidence_wait_remaining_s`，而 `contracts/golden/p7/gate_responses.ndjson` 仍為切片早期產物，未包含該欄位，導致新鮮度比對失敗（`At index 3957 diff: b'q' != b'e'`）。待後續以 `python3 scripts/rust/capture_p7_goldens.py` 重新擷取 `gate_responses.ndjson` 並確認欄位對齊。
2. `tests/test_l1_judge.py::test_l1_lock_frozen_set_excludes_capability_gated` 與 `test_l1_lock_cargo_sets_exclude_ignored_tests` 失敗（P5/擁有者既有待辦）：P5 凍結集合 `s21_core.windows` 包含 13 筆能力 SKIP／`#[ignore]` 案例（如 `cap_windows_native__*`、`cap_windows_com__*`），已在 `docs/rust-refactor/reviews/p7-local-windows-todo.md` 列為 `P7-BO-4` 與 `P7-LW-1` 追蹤，待 P5 於原生 Windows 收集後以 `--allow-removal` 逐名替換移出。

---

## 對裁決的異議（不計入結論）

無

---

結論：可放行
