> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

無（本輪無前輪報告）。

## B 新發現

審查範圍 `8b6f4e20..7af8adc4`（3 個 commit：b8bbdd4e 紅燈骨架＋測試、9828aed5 追加 4 案、7af8adc4 綠燈實作；無 merge commit）。

逐項對照結果（無缺陷者不列）：RPC 層 `from_rpc` 的轉型順序、falsy 預設、`int()`／`float()` 的 TypeError／ValueError→`INVALID_ARGS`、OverflowError 穿越、`ack_mode` 白名單、`check_source` 先於必填、pull 的 `local_path` truthy 才 `str()`，皆與 `sw_core/service.py:1426-1500` 一致；resolve 層（passthrough→`PROFILE_NOT_COMMAND_CAPABLE`＋session、RECOVERING／FLASHING／ATTACHED 非 command-capable／其餘非 READY、submit gate）與 manager 層（無 bridge→裸 `SESSION_NOT_READY`、`execute_gate_reject`、recovering、lease、`SESSION_BUSY`＋selector）順序與回應形狀對齊 `service.py:731-838`、`session_manager.py:8270-8471`；OD-62 映射（quiet 零寫出→`AUTOBOOT_QUIET`＋`retry_after_s`、四種被拒且整段 ≥1 byte→META＋`COMMAND_ABORTED_IN_FLIGHT`、否則 `FLUSHED_BY_RECOVERY`、其餘 `TRANSFER_TIMEOUT`）與 `_file_tx_rejected_result` 一致，META 欄位同 `_append_aborted_in_flight_meta`；`confirmed_bytes()` 累計含被拒那筆的部分確認，與 Python tally 同源；timeout 推導與 `≤0`／NaN／過大飽和無溢位（`saturating_add` 路徑）；`fg_cmd_expected_duration_s` 由 grant.cmd_id（file 為 None）推得 null，與 Python `kind="file"` 不帶 expected_duration 一致；L1 lock 為 add-only（50 行新增、0 行刪除）；slice21 scope 把 file.* 移入 served 並連動更新 Rust／Python 測試，`test_reject_paths_zero_tx[METHOD_NOT_FOUND]` 改要求 `INVALID_ARGS` 有 ab_rows 原註記依據，非放寬。

定向實測（`CARGO_TARGET_DIR=/tmp/solrev-p8x-rust-refactor-p8x-target`）：

| 測試 | 結果 |
|---|---|
| `sw-core --test file_transfer_session --features test-fakes` | 20 passed |
| `sw-daemon --features test-fakes --test file_transfer_rpc／dispatch／deferred_methods／event_rpc` | 2／55／7／2 passed |
| `tests/contract_blackbox/test_s24_file_transfer.py`＋`test_s24_no_retry.py`（Python 基線輪） | 13 passed（38.6 s） |

Rust artifact 的黑箱輪未重跑（需另建 bundle），沿用 commit 訊息所述 13 passed 為實作者宣稱。

- **P8X-7af8adc-1 [High]** `crates/sw-core/src/session_core/file_xfer.rs:55`（配合 `crates/sw-core/src/session_core.rs:4316` 的 `start_next`）— 檔案傳輸占用前景槽期間，Rust 把 arbiter 佇列中的 `command.submit` 命令**延後到傳輸結束後才執行**；Python 基線在同情境下由 arbiter worker 立即出列並呼叫 `execute_command`，於 `_begin_foreground_operation_locked`（`sw_core/session_manager.py:1712-1717`）因 `_foreground_operation is not None` 直接以 `{"ok":false,"error_code":"SESSION_BUSY","selector":<com>}` 結束該命令。具體失敗情境：READY session 開始 `file.push`（例如 256 chunk、約 30 s），期間送 `command.submit {"cmd":"echo hi"}`：兩邊 submit 皆回 `ok:true`＋cmd_id；Python 的 `command.get` 隨即為 `status:"failed"`、`error_code:"SESSION_BUSY"`、`selector:"COM0"`，Rust 的 `command.get` 停在 queued，傳輸結束後才執行並回 `echo hi` 的 stdout。此互動在 P8 之前不會出現（Python 只有命令與 file op 兩種 `_foreground_operation` 持有者，而命令彼此由 arbiter 序列化），因此是本輪把 `PendingOp::FileTransfer` 納入前景槽後新產生的 1:1 偏差；計畫 09 L1018／L1100 與 EVT-D-F6 只規範「file op 遇前景忙回 SESSION_BUSY 不排隊」，未涵蓋反向情境，`spec-reconciliation.md` 與 parity-amendments 亦無登記。修法：在 `start_next` 出列（或 `queue.push` 之後的出列點）判定前景槽由 `PendingOp::FileTransfer` 持有時，比照 Python 以 `registry.complete(cmd_id, CmdOutcome::Result({"ok":false,"error_code":"SESSION_BUSY","selector":com}))` 終結該命令並繼續出列下一筆，不延後；若編排者認定 Rust 的延後執行是刻意改善，則須登記為 DF／OD 並在 A/B（`test_s24_file_transfer.py`）加對應案例後，本項依裁決轉為不處理。

- **P8X-7af8adc-2 [Low]** `crates/sw-core/src/session_core.rs:4856-4858` — `op_finished` 的 `PendingOp::FileTransfer` 分支內已呼叫 `command_fg_finish`，match 之後的共用尾段（L4877）再呼叫一次；第二次因 `fg_adm` 已 `take()` 為 no-op，行為正確但重複。建議移除分支內的呼叫、改在分支內只計算 result 並把 `Effect::Reply` 排在共用 `command_fg_finish` 之後（需保持「resume／回放 effects 先於 Reply」的順序），或加註說明為何兩處都保留。

- **P8X-7af8adc-3 [Low]** `crates/sw-core/src/op_runner.rs:452-456` — `transfer_cmd_id` 為空時的 META `cmd_id` 退路為 `file:<op_id 整數>`，Python 對應退路為 `permit.op_id`＝`file:<uuid4 hex>`。引擎目前一律在送出前設定 `ft-<8hex>` 前綴（`file_transfer.rs:343／529`），退路實際不可達；若日後可達，WAL 的 `cmd_id` 形狀會與 Python 不同。建議改為以 `OpCtx.rng` 產生 `file:<uuid4 hex>`，或加 `debug_assert!` 標明不可達。

## 既有碼待辦（不計入結論）

- `crates/sw-daemon/src/service.rs` 尚無 Python `_ADMISSION_METHODS`（含 `file.push`／`file.pull`）對應的 admission／`daemon.handoff_stop` 凍結邊界；計畫已排在 P8 Task 14，本輪不計。

## 對裁決的異議（不計入結論）

無。

結論：需修正
