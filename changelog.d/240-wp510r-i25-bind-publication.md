---
type: fix
scope: rust-core
---

綁定世代統一由 actor 在實際套用時產生並同步至 manager，即使 bind RPC 等待逾時、稍後才完成，後續重試與重插的有效通知仍可開埠；保留過期送達義務的路徑與世代重驗。
