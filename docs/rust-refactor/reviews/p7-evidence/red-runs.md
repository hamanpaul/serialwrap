# P7 紅燈時點紀錄（審查 76de0cf_96b28dd c58e8d4-5）

TDD 紅燈在「骨架簽章已調整、本體仍為 `todo!()`」時點以 `cargo test` 實跑；Task 13 當時未保留輸出檔，下表以 session 實跑結果記錄。
以最終版測試對 Task 0 凍結骨架（76de0cf）編譯會因簽章修訂（見 08 Task 13「P7 修訂」）而編譯失敗，紅燈仍成立。

| Task／commit | 紅燈命令 | 紅燈結果 | 實作後 |
|---|---|---|---|
| Task 13（c58e8d4） | `cargo test -q -p sw-core --features test-fakes --test event_store` | `0 passed; 23 failed`（todo! panic） | 23 passed |
| Task 13（c58e8d4） | `cargo test -q -p sw-proto --test runtime_paths_golden` | `error[E0609]: no field events_log_path on type DaemonDirs` | 6 passed |
| Task 13-py（d9cacd4） | `python3.12 -m pytest -q tests/test_event_replace_df18.py` | `2 failed` | 2 passed |
| Task 14（fa7e409） | `cargo test -q -p sw-pal --features test-fakes --test handler_spawner`（以 HEAD 的 todo! 骨架 process.rs 暫代） | `0 passed; 9 failed` | 9 passed |
| Task 15（91109e7） | `cargo test -q -p sw-core --features test-fakes --test event_dispatcher` | `0 passed; 17 failed` | 17 passed |
| 審查第 5 輪 | `cargo test -q -p sw-core --features test-fakes --test event_store` | `diff_against_nan_field_is_unchanged`、`tail_filter_list_with_nan_matches_python`、`tail_decode_error_position_is_chunk_relative` 紅 | 全綠 |
| 審查第 8 輪（0722f75-1） | `cargo test -p sw-daemon --features test-fakes --test event_lifecycle shutdown_closes`（暫時註解 `daemon_main.rs` drain 前的 `engine.close_intake()`） | `__B2__ match_recorded 50 筆` FAILED | 還原後 1 passed |
| Task 10 | `cargo test -p sw-core --features test-fakes --test capture_p7`（骨架簽章已調整、本體 todo!） | `3 passed; 4 failed`（4 個純函式／observer 測試 todo! panic；3 個 actor 層測試為既有 actor 端寫入的回歸鎖，紅燈前即綠） | 7 passed；另以突變（log_start 不掛 `RxShared::set_capture`）確認 3 個 actor 層測試轉紅（reader 端為唯一寫入路徑） |
| Task 9 | `cargo test -p sw-core --features test-fakes --test recover_p7`（`plan_recover` 簽章已加 `com`、本體 todo!） | `2 passed; 6 failed`：`plan_*`／golden 3 項 todo! panic；`recover_resets_reprobe_progress`（attempts 仍 1，缺 `_reset_reprobe_progress_locked`）；`force_recover_replies_on_one_second_poll_grid`（READY 當下即回覆，未依 1 s 輪詢點）；`duplicate_force_recover_joins_same_stop` 初版期望錯誤（依 Python `_pending_drain` 先行更正為第二個立即 `SESSION_RECOVERING`） | 9 passed（含後補 `stale_force_recover_poll_is_ignored`） |
| Task 17 | `cargo test -p sw-daemon --features test-fakes --test session_rpc_p7` | 7 passed（皆為 06 T18／P7 Task 5 已實作行為的回歸鎖：轉型順序、`bool("false")`、SC-35 passthrough、log 鍵序、public 鍵序與 reprobe 實值；無新增行為故無紅燈時點） | 7 passed |
| 審查第 9 輪 sol-backfill #1 | `cargo test -p sw-core --features test-fakes --test event_dispatcher`＋`-p sw-pal --test handler_spawner`（`git stash` 暫還原 `dispatcher.rs`、`handler_posix.rs`、`handler_windows.rs`） | `handler_panic_releases_slot_and_worker_survives`、`huge_timeout_native_handler_then_normal_rule_completes`、`native_huge_timeout_overflow_is_python_like` FAILED（19／21、13／14 passed） | 21 passed；14 passed |
| 審查第 9 輪 sol-backfill #2 | `cargo test -p sw-core --features test-fakes --test event_engine completion_log`（暫還原 `event_engine/mod.rs` 為忽略寫檔錯誤的 `start`） | `逾時等待 fire_completed`（第二次命中被 cooldown 擋住）FAILED | 1 passed |
| 審查第 9 輪 sol-backfill #3 | `cargo test -p sw-core --features test-fakes --test event_scope_p7`（修前） | `interactive_send_tx_in_flight_is_not_foreground_for_events` FAILED（fence foreground_busy 為 true）；對照 `foreground_command_in_flight_is_foreground_for_events` 綠 | 2 passed |
| 審查第 9 輪 sol-backfill #4 | `cargo test -p sw-core --features test-fakes --test capture_p7 first_log`（修前） | `首次 log_start 的 session.capture 不得為 null：Null` FAILED | 8 passed |
| 審查第 9 輪 Opus 397a473-1 | `cargo test -p sw-core --features test-fakes --test capture_p7 session_capture_counts`（修前） | `log_status 的 session.capture 須為即時計數`：`(Int(0), Int(0))` FAILED | 13 passed（含 397a473-2 四支 actor 層測試） |
| 審查第 9 輪 Opus a6b24f7-1 | `cargo test -p sw-core --features test-fakes --test recover_p7`（修後、鎖內 4 支仍為舊期望） | 4 FAILED（舊偏離期望值）；更正期望值並新增 `force_recover_poll_grid_starts_after_clear_drain` 後，突變「一律立即排輪詢」→ 5 FAILED | 13 passed |
| 審查第 9 輪 Opus a6b24f7-2 | `cargo test -p sw-core --features test-fakes --test recover_p7 recover_resets`（突變：刪除 `credentials_warned = false`） | `recover_resets_credentials_warned` FAILED；#57／not_ready 兩支為既有行為的回歸鎖（無紅燈時點） | 13 passed |
| #234 boot_done_regex | `cargo test -p sw-core --features test-fakes --test quiet_gate_p7 rx_sequences`（golden 追加 014～020 後、實作前）；`--test session_core_quiet_p7 boot_done`（`git stash` 暫還原 `quiet_gate.rs`） | `case quiet_rx:014 t=20 until`：`Some(180.0)` FAILED；`boot_done_regex_clears_quiet_but_keeps_reconfirm_pending` FAILED | 6 passed；21 passed |
| Task 7（SC-30，OD-35） | `cargo test -p sw-core --features test-fakes --test session_core_login`（突變：`apply_quiet_armed` 不呼叫 `sc30_cancel_login_run`） | 4 FAILED（attach／手動再探／READY 再確認／reboot recovery）；`console_unlock_banner_does_not_cancel_run` 綠（對照） | 6 passed；P5 F15 與 P7 d5b2dd6-1 共 6 支期望依 OD-35 更正後 sw-core 全綠 |
| 審查 71fe017_27b5bf4 P7-71fe017-1 | `cargo test -p sw-pal --features test-fakes --test handler_spawner keeps_pipes`（突變：背景執行緒先關 stdin） | `native_huge_timeout_overflow_keeps_pipes_open` FAILED（`read x` 收到 EOF） | 15 passed |
| 審查 71fe017_27b5bf4 P7-27b5bf4-1 | `cargo test -p sw-core --features test-fakes --test capture_p7 single_snapshot`（修前；test-fakes `counts_hook` 於第一次取樣後注入 `b\n`） | 頂層 `(1, 2)`、`session.capture` `(2, 4)` FAILED | 14 passed |
| Task 17 `attach_state_result_mapping` | `cargo test -p sw-core --features test-fakes --test session_core_login attach_state`（修前） | FLASHING 子案鍵多 `selector`（`["ok","error_code","session","selector"]`）FAILED；修 FLASHING 後 RECOVERING 子案多 `drain_stalled`／`drain_timeout_epoch` FAILED；Python 特性測試 `tests/test_p7_attach_parity.py` 2 passed（基線鍵 `ok, error_code, session`） | 7 passed |
