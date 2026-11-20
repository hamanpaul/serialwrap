---
type: fix
scope: ci
---
rust-linux 黑箱契約步驟改用 Stage 2 Rust CLI 與 daemon；Stage 1A 保留專用 CLI 委派回歸，避免以 Rust label 搭配 Python daemon 執行 Stage 2 測項。
