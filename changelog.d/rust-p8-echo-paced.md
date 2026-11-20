---
type: feat
scope: rust-core
---
P8（Slice 2.4）Task 5：新增 `OpTransferPort`（`crates/sw-core/src/file_transfer/op_port.rs`），以前景 op 的 `OpCtx` 實作檔案傳輸的 `TransferPort`：一般命令補一次換行、echo-ACK 節流逐段送出並以 echo 確認後才送下一段（以字元切段、去尾端 CR／LF、容許段間雜訊與 ANSI；全部確認才送換行，停滯回 `ok:false` 且不送換行）、取消半行 `\x15\n`（`cmd_id` 為 null）、RX 讀取與可取消的 prompt 等待（期限依注入 Clock；timeout 為零不檢查即回 false）。TX 被拒或等待被取消時回 `TxRejected`，被拒那筆不重送，reason 依 fence 現況分類（quiet／reconfirm → Quiet、凍結 → Draining、epoch 過期 → StaleGeneration、op 撤銷 → Cancelled、其餘含 FLASHING → Other）。`TxJob` 新增 `aggregate`（`Append`／`Finish`）：節流各段 OS 確認的 bytes 累加在 TxWriter 的有界緩衝（超過該行命令長度或未結上限即零 TX 拒絕），結束時以一筆 TX 落 WAL（結果不明仍先寫 META）；`Finish` 不經 Fence，凍結後仍能落已確認 bytes。TxWriter 收尾時先 flush 未結緩衝再交回 writer，`WorkerStoppedReceipt` 必晚於這些 TX 紀錄，寫入失敗列入回條 `unrecorded`。drain Phase 1（`begin_stop`，所有停止種類）新增 `RxShared::cancel_transfer_runs`，只喚醒以 `RunCancel::watching_transfers` 監看的檔案傳輸等待，不改 login run 的取消行為。
