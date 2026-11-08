---
type: fix
scope: rust-refactor
---

- T13 測試收尾先停用並結束具名排程工作，確認停止後才刪除，避免延遲啟動的 staging daemon 逃過清理。
