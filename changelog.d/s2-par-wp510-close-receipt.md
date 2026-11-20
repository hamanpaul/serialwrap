---
type: fix
scope: rust-refactor
---

- 佔位 attach 開埠逾時後，未取得 handle 可完成排空；已取得 handle 須有 `closed:true` 回條才解除阻擋，關閉失敗時保留 stall 診斷並阻止再次開埠。
