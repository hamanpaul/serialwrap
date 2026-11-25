---
type: fix
scope: alias
---

修正 Rust dynamic／pin session 的自訂 alias 在 daemon 重啟後遺失，以及 `alias assign --by-id` 不會立即生效的問題。by-id alias 優先於 session alias 與自動 alias；有效名稱跨重啟和 pin template 切換保留，`alias list` 顯示有效狀態與衝突。YAML `targets` 行為不變。
