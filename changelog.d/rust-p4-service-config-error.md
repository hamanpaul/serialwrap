---
type: fix
scope: rust-cli
issue: null
---
`service` 收到不可 JSON 序列化的 `supervision_mode` 時，改輸出與 Python CLI 相同的 `INTERNAL_ERROR` JSON 與 stderr 鏡像。
