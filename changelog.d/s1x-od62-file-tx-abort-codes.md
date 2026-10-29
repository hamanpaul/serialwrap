---
type: change
scope: file-transfer
---

`file.push`／`file.pull` 傳輸中遇 drain、bridge 世代過期或取消時，改依擁有者裁決 OD-62（比照 BC-03）回應：本次傳輸尚未寫出任何 byte 回 `FLUSHED_BY_RECOVERY`（可在 READY 後重送），已寫出 ≥1 byte 回 `COMMAND_ABORTED_IN_FLIGHT`（不得自動重送），push 附 `chunks_sent`／`chunks_total`；0.3.2 在這些情境回 `TRANSFER_TIMEOUT`。「已寫出」以整段傳輸共用的操作 permit 累計。同步 `contracts/rpc_schema.json` 兩方法 `errors`／`error_order`、`contracts/error_codes.yaml` 兩碼 scope、BC-03／SC-04 `compatibility_impact` 與 SKILL 受管區段（S1X-a-4 第 2 輪）。
