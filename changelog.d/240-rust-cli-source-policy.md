---
type: change
scope: cli
---
Rust CLI 的 `cmd submit`、`file push`、`file pull`、`device release` 與 `session interactive-open` 對齊 #240 來源檢查：拒絕 daemon 保留的 `system` 前綴，接受 bare `human` 並保留原始 RPC 參數，空白判定遵循 Python `str.strip()`；錯誤輸出與 Python CLI 逐位元組一致。
