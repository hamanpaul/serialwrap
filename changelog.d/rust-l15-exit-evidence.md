---
type: fix
scope: rust-refactor
issue: null
---

出口 driver 只有在完整 Windows L1.5 原始報告、執行 metadata 與 gate 列逐筆一致，且所有非通過結果皆為核准的 `BLOCKED-OWNER` 延後項時，才略過 Windows producer 的非零結束碼。
