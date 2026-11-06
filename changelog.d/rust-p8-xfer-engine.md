---
type: feat
scope: rust-core
---
Rust sw-core 新增 `file_transfer` 的 `push_file`／`pull_file` 引擎（只依賴 `TransferPort`），含 OD-62 的 TX 被拒證據與 OD-80 的分段 pull（不再受 131072 字元 RX 視窗限制）。
