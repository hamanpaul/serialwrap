---
type: feat
scope: session
---

release、clear、bind、斷線與關機改用 drain barrier，期間提供 drain 診斷並等 handle 關閉回條；已確認送出至少 1 byte 後中止的命令回報 `COMMAND_ABORTED_IN_FLIGHT`。新增 `daemon.handoff_stop`，原子凍結新操作後再有序關機並同步 WAL。
