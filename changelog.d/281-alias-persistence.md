---
type: fix
scope: alias
---

修正 Rust dynamic／pin session 的自訂 alias 在 daemon 重啟後遺失，以及 `alias assign --by-id` 不會立即生效的問題。by-id alias 優先於 session alias 與自動 alias；有效名稱跨重啟和 pin template 切換保留，`alias list` 顯示有效狀態與衝突。YAML `targets` 行為不變。

延伸修正 issue 281 alias 持久化的名稱衝突與即時解析：保留離線 registry claim、避免 alias 遮蔽其他 session 的 COM／session ID、序列化 alias 更新，並讓 `alias.unassign` 立即更新線上 session。`alias.list` schema 同時接受既有 Python 與 Rust 輸出。
