---
type: fix
scope: rust-refactor
---

Rust 佔位 attach 作業對齊 Python `_AttachOp` 生命週期：取消後關閉失敗一律保留 inflight 阻擋，重複及並行 release 共用首次排空期限。
