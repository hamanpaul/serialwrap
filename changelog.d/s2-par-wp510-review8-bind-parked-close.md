---
type: fix
scope: rust-refactor
---

- OD-63 佔位退休落盤失敗留下未消費的 UART handle 後，若把 session 改綁到該裝置又遇存檔失敗，空間恢復後重試同一 `session.bind` 不再卡住約 3 秒並回 `SESSION_RECOVERING`（`drain_stalled:true`）。Rust daemon 改照 Python：先關閉這個沒有 worker 的 handle 再重試 bind，回 ok＋ATTACHING 並重新排程 attach；關閉未確認時仍照常改綁，但該裝置維持未確認關閉阻擋，不重新開埠、不送 TX。
