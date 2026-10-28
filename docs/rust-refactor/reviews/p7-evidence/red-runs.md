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
