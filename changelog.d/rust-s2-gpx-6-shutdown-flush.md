---
type: fix
scope: rust-s2
---
Rust daemon 停機 gate 立即凍結 session 與偵測 PROBE 的 TX，並由 actor 沖刷尚未啟動的命令為 `FLUSHED_BY_SHUTDOWN`；已在執行的前景命令仍可收尾，watcher 與 flash endpoint 停止期間不再啟動新命令或送出 TX。
