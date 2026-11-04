---
type: fix
scope: rust-refactor
issue: 241
---

Rust CLI 的一般 RPC、event 與 daemon status 警告路徑接入共用 release manifest 程式碼指紋來源，在 daemon 程式碼指紋不符時輸出警告並保留既有錯誤行與 exit code。
