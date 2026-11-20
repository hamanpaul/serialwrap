---
type: fix
scope: rust-daemon
---
OD-63 RELEASED 佔位在 attach worker、熱插拔、bootstrap 與 profile 再解析路徑都依佔位標記封鎖，避免持久化 RELEASED 阻擋遺失或狀態變為 DETACHED 後自動開埠；新增零 UART I/O 與命令拒絕回歸。
