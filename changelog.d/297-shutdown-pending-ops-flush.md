---
type: fix
scope: rust-daemon
---
issue 297：Shutdown Phase 1 凍結 Fence 後立即取消在途 self-test、recover、interactive open／send 等待，並在 WorkerStopped 前以 `FLUSHED_BY_SHUTDOWN` 回覆；已送出的 probe、救援鍵或 interactive bytes 不回滾，Fence 凍結後不再新增 TX，不延長停機上限。沿用 Phase 1 清除 interactive lease，忽略晚到完成結果。
