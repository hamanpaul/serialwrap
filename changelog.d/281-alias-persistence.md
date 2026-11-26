---
type: fix
scope: alias
---

修正 Rust dynamic／pin session 的自訂 alias 在 daemon 重啟後遺失，以及 `alias assign --by-id` 不會立即生效的問題。by-id alias 優先於 session alias 與自動 alias；有效名稱跨重啟和 pin template 切換保留，`alias list` 顯示有效狀態與衝突。YAML `targets` 行為不變。

延伸修正 issue 281 alias 持久化的名稱衝突與即時解析：保留離線 registry claim、避免 alias 遮蔽其他 session 的 COM／session ID、序列化 alias 更新，並讓 `alias.unassign` 立即更新線上 session。`alias.list` schema 同時接受既有 Python 與 Rust 輸出。

補強 alias 操作的提交重驗與 selector 唯一性：同名並行設定只有一方成功；名稱由 YAML、dynamic 或 pin session 持有時，公開 `alias.set` 一律拒絕另一 owner；COM 與 session ID 形狀的 alias 一律拒絕，舊登錄資料在載入時停用；解除名稱若等於原自動名稱不會復活。`alias.unassign` 的 Rust 交錯錯誤也同步登錄既有錯誤碼與 RPC 契約。
