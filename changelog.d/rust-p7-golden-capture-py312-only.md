---
type: change
scope: rust-p7
---
`scripts/rust/capture_p7_goldens.py` 改為只接受 CPython 3.12（其他版本 exit 2）：re 錯誤訊息、sre AST 與 `int()` 位數檢查順序依版本而異，3.11／3.13 重擷取會產生不同 golden（審查 L5-4）。
