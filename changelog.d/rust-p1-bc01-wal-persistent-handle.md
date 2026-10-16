---
type: perf
scope: wal
---
WAL 與 mirror 改為常駐 handle 並逐筆 flush，關閉時同步後釋放。mirror 寫入失敗只重試 mirror，不再重複寫入同一 seq 的 WAL 行。Windows 新寫入 WAL 行尾與 mirror 內的換行由 CRLF 改為 LF；`health.status.wal` 新增 `unhealthy_reason`。
