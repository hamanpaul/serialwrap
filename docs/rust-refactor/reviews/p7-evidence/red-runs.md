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
