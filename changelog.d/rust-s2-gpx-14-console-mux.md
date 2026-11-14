---
type: fix
scope: rust-console
---
Windows com0com console 改以有界背景 writer 完成 overlapped 寫入並喚醒 hub；serial 與 TCP 同時 ready 時，mux 以零 timeout 合併 socket 事件。bridge 重新 attach 時，只重開 hub 已實際關閉的 serial console。
