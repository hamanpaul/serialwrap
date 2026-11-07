> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

無（本輪無前輪報告）。

## B 新發現

審查範圍：`fc55cf40`（紅燈測試＋骨架）、`08ee21b8`（測試前置補正）、`8b6f4e20`（實作），無 merge commit。

核對方式：逐一 `git show` 三個 commit；以 `sw_core/uart_io.py` 的 `send_command`／`send_command_echo_paced`／`_await_echo_progress`／`cancel_input_line`／`wait_for_regex_from`／`_send_bytes_with_permit` 與 `sw_core/file_transfer.py` 為基線比對；對照計畫 09 Task 5 段落與 T4 既有的 `TransferAbort` 型別；實跑定向測試與 clippy。

實測結果（`CARGO_TARGET_DIR=/tmp/solrev-p8x-rust-refactor-p8x-target`）：

| 測試 | 結果 |
|---|---|
| `echo_paced` | 31 passed |
| `tx_writer` | 48 passed |
| `io_worker` | 9 passed |
| `drain_release` | 67 passed |
| `p8_upstream_interfaces` | 1 passed |
| `cargo clippy -p sw-core --features test-fakes --tests --cap-lints warn`（只看本輪檔案） | 無警告 |
| `pytest tests/test_p7_skeleton_boundary.py tests/test_evidence_index.py`（L1 鎖檔） | 24 passed |

已確認無缺陷的重點：
- 切段以字元計、去尾端 CR／LF、`strip_ansi` 後去 CR/LF 以移動起點 `find`，與 Python 逐項相符；`timeout` 為零不檢查直接 `false` 符合 EVT-D-F1。
- Python 的 fence 拒絕（`not_submitted` 且 reason 非 `deferred`／`stale_owner_gen`）會拋 `TxIncompleteError` 並在 `finally` 落已送 bytes 的 TX；Rust 以 `Finish` 後回 `Err(TxRejected)` 對應，被拒那筆不重送。Unknown 先 META 再 TX 的順序一致。
- TxWriter 收尾順序：先結算 settle、標 closed、處理佇列中的 `Finish`、其餘以 Frozen 收斂、再 `flush_open_aggregates`，最後才交回 writer；`finish_async` 在 join 後組回條，故回條必晚於聚合 TX。writer 停止後 port 的 `Finish` 因 channel 關閉回 Frozen，不會重複落 WAL。
- `cancel_transfer_runs` 加在 `begin_stop` 兩個凍結點；另兩個 `frozen = true` 位置（`drain_opened_bridge`、drain 完成的 detach）不會有進行中的傳輸等待。`cancel_login_runs` 僅由 `sc30_cancel_login_run` 在 quiet arm 擋 probe 時呼叫，此時 fence 已 `quiet_active`，分類為 Quiet 正確。
- L1 鎖檔只新增 31 筆、位置依字母序，為 add-only。

Low 項目（不擋放行）：

- **P8X-8b6f4e2-1** [Low] `crates/sw-core/src/tx_writer.rs:673` — `finish_aggregate` 在 `payload` 非空（文件明言「不得帶 payload」）時仍先 `write_aggregate` 落 WAL 再回 `NotSubmitted` 錯誤，誤用路徑的副作用與回報不一致。修法：payload 非空時直接回錯誤、不觸碰緩衝（或在 doc 說明「仍會 flush」）。
- **P8X-8b6f4e2-2** [Low] `crates/sw-core/src/tx_writer.rs:763` — `Precheck` 先判 `AggregateOverflow` 再判 Fence；fence 已凍結時若同時溢位，`TxReport.reject` 為 `None`，port 分類成 `Other` 而非 `Draining`。僅程式錯誤路徑可達，無外部可觀察情境。修法：先 `Fence::admit`，通過後才檢查溢位。
- **P8X-8b6f4e2-3** [Low] `docs/rust-refactor/plans/09-slice24-transfer-flash.md:885` — 計畫 Task 5 仍寫 `Err(SerialNotReady)`／`Refused(FenceReject)`、`flush_open_aggregates() -> Vec<UnrecordedGap>`、`TxReport.wal_seq`、`fail_next_writes(wal, 1)`；實作改依 T4 的 `TransferAbort::TxRejected` 分類、`TxReport.wal`、注入 2 次。commit 訊息以「brief 不符 #1～#5」說明，但 brief 不在版本庫，日後無法從 repo 追溯偏離理由。修法：把 #1～#5 的內容與裁決寫進 `docs/rust-refactor/reviews/p8x-fix-responses.md` 或計畫 errata。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

- **SelfTestProbe 可插入 echo-paced 的 slice 邊界**（計畫 09 Task 5 表格 `selftest_probe_between_paced_slices_never_splits_slice`，依 SES-16.3b「self-test 不取前景 token」）。Python 的 `send_command_echo_paced` 在整個 `with self._write_lock` 內完成所有 slice 與換行，self-test probe 的 `send_bytes` 會被鎖住直到該行命令結束，故 probe 不可能落在半行之間。Rust 允許 probe 於 slice 邊界送出：情境為 push 進行中、第 1 段 64 字元已進 DUT 輸入緩衝、probe 的 `\n` 送出 → DUT 把半行 `printf '%s' <部分 base64>` 當完整命令執行，後續 slice 成為另一條垃圾命令，結果多半是 `TARGET_DECODER_FAILED` 或 md5 不符，且 WAL 的「一命令一筆 TX」仍記成完整命令。本輪測試只斷言「不切開單一 slice」，未涵蓋此情境。建議擁有者重新確認：傳輸期間 SelfTestProbe 應等待該行命令結束（與 Python `_write_lock` 對齊），或以文件承認為已知差異。

結論：可放行
