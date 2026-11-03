---
type: feat
scope: rust-refactor
issue: 241
---

Rust CLI 的 setup 對齊 Python #241 的 `--no-restart` 預設值與 help；stale 指紋且 systemd 服務 active 時會重啟 daemon、比對重啟後指紋，並依結果輸出 action、after 與 hint。
