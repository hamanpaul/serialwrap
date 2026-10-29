---
type: fix
scope: interactive
---

修正 interactive 命令改以 `interactive:<lease_id>` permit 送出後，取消若落在送出前檢查與實際提交之間仍會寫出命令的競態：permit 帶著原命令 `cmd_id` 的取消身分（`TxPermit.cancel_op_id`），由 TX fence 內的提交點同時檢查 `cmd_id` 與 lease op 的撤銷狀態，取消後零 TX 並回 `FLUSHED_BY_RECOVERY`、關閉本命令開的 lease（sol S1-6485e90-1）。
