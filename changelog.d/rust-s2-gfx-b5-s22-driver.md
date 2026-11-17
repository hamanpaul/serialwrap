---
type: fix
scope: rust-gate
---

修正 Slice 2.2 gate driver：自動執行鎖定計畫中的 PTY conformance 與共用 Stage 2 release artifact 黑箱，並從共用 manifest 匯入 `win_test`。報告與日誌記錄受測 CLI／daemon 摘要及實際 bundle 來源；Rust L1.5 使用共用 release CLI，非共用正式執行明確拒絕。
