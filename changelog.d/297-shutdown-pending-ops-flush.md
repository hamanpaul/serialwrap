---
type: fix
scope: rust-daemon
---
issue 297：Shutdown Phase 1 凍結 Fence 後立即取消在途 self-test 與 recover 的 nonce／prompt、救援鍵、force 輪詢及 reprobe 等待，並在 WorkerStopped 前以 `FLUSHED_BY_SHUTDOWN` 回覆；probe 或救援鍵已送出時仍使用同一錯誤碼，不延長停機上限。
