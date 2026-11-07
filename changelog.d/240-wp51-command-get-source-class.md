---
type: change
scope: rust-refactor
---

- `command.get` 由 daemon service 對命令紀錄副本追加 `source_class`，對齊 Python 顯示分類且不修改 registry 原始紀錄。
