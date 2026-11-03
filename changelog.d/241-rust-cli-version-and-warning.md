---
type: feat
scope: rust-refactor
issue: 241
---

Rust CLI 的 `--version` 改從同 bundle manifest 輸出 Python daemon 程式碼指紋；共用警告邏輯依 Python 基線先判版本差異，再判程式碼指紋差異。
