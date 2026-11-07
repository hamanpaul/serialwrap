---
type: feat
scope: rust-core
---
P8（Slice 2.4）Task 5：新增 `OpTransferPort`（`crates/sw-core/src/file_transfer/op_port.rs`），以前景 op 的 `OpCtx` 實作檔案傳輸的 `TransferPort`：一般命令補一次換行、echo-ACK 節流逐段送出並以 echo 確認後才送下一段（全部確認才送換行；停滯回 `ok:false`）、取消半行 `\x15\n`、RX 讀取與可取消的 prompt 等待（timeout 為零不檢查即回 false）。TX 被拒時回 `TxRejected`，reason 依 fence 現況分類（quiet／reconfirm → Quiet、凍結 → Draining、epoch 過期 → StaleGeneration、op 撤銷 → Cancelled、其餘 → Other）。`TxJob` 新增 `aggregate`（`Append`／`Finish`）：節流各段 OS 確認的 bytes 累加在 TxWriter 的有界緩衝、結束時以一筆 TX 落 WAL；TxWriter 收尾時先 flush 未結緩衝再交回 writer，`WorkerStoppedReceipt` 必晚於這些 TX 紀錄，寫入失敗列入回條 `unrecorded`。drain Phase 1（`begin_stop`，所有停止種類）新增 `RxShared::cancel_transfer_runs`，只喚醒檔案傳輸等待，不改 login run 的取消行為。
