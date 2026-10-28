---
type: fix
scope: rust-refactor
---

Windows Rust 黑箱 workflow 建立來源 HEAD 的 Git bundle 並傳給 `win-blackbox.ps1`，讓 staging 測試可驗證提交歷史；A/B comparator 明確驗證兩側 config digest 各自有效，但不把不同的 runner／夾具設定誤當成不相容。新增回歸固定這兩項行為。
