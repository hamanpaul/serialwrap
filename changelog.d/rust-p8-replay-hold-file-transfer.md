---
type: change
scope: rust-core
---
P8（Slice 2.4）S2-P8X-df28b：回放保留期間 `file.push`／`file.pull` 的兩邊一致回歸測試，不改產品碼（rulings s2「編排者更正（2026-11-08）」：OD-82 的前提不成立，DF-28 作廢、不登記任何 DF）。agent 命令執行期間 human console 打的字在命令結束後回放到 UART，回放寫完之前 Python（前景 token）與 Rust（07 D-22 replay hold）都立即回 `{"ok":false,"error_code":"SESSION_BUSY","selector":"COM0"}`、不排隊（EVT-D-F6），回放照常寫出，結束後也不補執行傳檔。新增 Rust L1 `session_core_console::file_transfer_busy_during_replay_hold_not_queued`（`CoreHarness`），以及 A/B 黑箱 `tests/contract_blackbox/test_s24_xfer_replay_hold.py`：`ShellTarget` 新增 `hold_read_on(marker)`／`read_held()`／`release_read()`，DUT 讀到回放開頭的 marker 即停讀，回放剩餘量遠大於 kernel PTY 緩衝，回放保留窗口由寫入量而非等待時間保證（附 fixture 自測 `test_shell_target_hold_read_on_marker`）。
