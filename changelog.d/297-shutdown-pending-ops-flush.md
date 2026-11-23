---
type: fix
scope: rust-daemon
---
issue 297：Shutdown Phase 1 凍結 Fence 後立即取消在途 self-test 的 nonce／prompt RX 等待，並在 WorkerStopped 前以 `FLUSHED_BY_SHUTDOWN` 回覆；probe 已送出時仍使用同一錯誤碼，不延長停機上限。
