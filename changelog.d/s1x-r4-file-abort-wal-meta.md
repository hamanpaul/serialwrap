---
type: fix
scope: file-transfer
---

`file push`／`file pull` 傳輸中因 drain、bridge 世代過期或取消中止且已寫出 ≥1 byte 而回 `COMMAND_ABORTED_IN_FLIGHT` 時，比照一般命令（BC-03）寫 `command_aborted_in_flight` WAL META（編排者裁決 S1X-a 第 2 輪 SPEC-GAP 1：欄位與時機相同）：`source=system`、`cmd_id` 為該次傳輸的 cmd_id 前綴 `ft-<8hex>`（各子命令 TX 紀錄為 `ft-<8hex>-<子命令>`）、`confirmed_tx_bytes` 為整段傳輸累計、`tx_outcome` 依同一規則判定，於映射當下（回應送出與 foreground 操作釋放之前）寫入；零寫出回 `FLUSHED_BY_RECOVERY` 時照舊不寫。一般命令與 file.* 共用同一個 META 寫入 helper。
