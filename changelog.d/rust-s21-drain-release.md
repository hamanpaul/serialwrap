---
type: feat
scope: rust-s21
---

Rust 實作兩階段 drain 與 release 收尾：先凍結新 I/O、取消在途寫入，再等 worker 關閉 handle 的回條；3000 ms 未收斂時標記 `drain_stalled` 並回覆 `DRAIN_STALLED`。Drain 期間的 attach、clear、bind 與 recover 回覆 `SESSION_RECOVERING`，部分寫出中止命令依帳本回覆 `COMMAND_ABORTED_IN_FLIGHT` 並寫入 META。動態 session 的 RELEASED 狀態會跨重啟持久化，`device.attach` 重新取得獨占後才回到 ATTACHING。真 actor 端到端案例另驗證 data lane 滿載時 release、命令取消與 lease 到期最多延後 1 個 data item 才被處理。
