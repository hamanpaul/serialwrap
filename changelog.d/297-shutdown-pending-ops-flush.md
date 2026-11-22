---
type: fix
scope: rust-daemon
---
新增 Shutdown 專用的 RX regex 等待取消狀態與喚醒介面，供 issue 297 後續停機操作使用；一般 RX 等待與 login／file-transfer 取消路徑維持原行為。
