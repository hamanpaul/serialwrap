---
type: fix
scope: rust-daemon
---
修正停止 daemon 時進行中的 `file.push`／`file.pull` 偶發回 framing `EXCEPTION`（`session actor stopped`）而非 OD-62 中止碼：drain 收尾的 `WorkerStopped` 先於傳輸 op 的 `OpFinished` 被 session actor 處理時，actor 隨即結束並丟棄傳輸的回覆通道。現在停機已排空但傳輸回覆仍待 OpRunner 結果時，actor 等該結果回覆後才結束，回應維持 `COMMAND_ABORTED_IN_FLIGHT`（已寫出 ≥1 byte，附 `chunks_sent`／`chunks_total`）或 `FLUSHED_BY_RECOVERY`；停止序順序、期限與其他命令不變。
