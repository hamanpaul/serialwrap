---
type: fix
scope: rust-refactor
---

- 佔位 attach 開埠逾時後，只有 `closed:true` 回條才會解除排空阻擋；關閉失敗時保留 stall 診斷並阻止再次開埠。
