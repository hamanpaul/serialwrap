---
type: feat
scope: rust-refactor
---

Rust daemon 啟動時讀取 release manifest 指紋快照；`health.status` 依序輸出 `code_fingerprint` 與 `code_path`，一般 RPC 僅在指紋可用時附加 `code_fingerprint`。
