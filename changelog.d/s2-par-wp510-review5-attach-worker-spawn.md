---
type: fix
scope: rust-refactor
---

- OD-63 佔位 `device.attach` 的 attach worker 執行緒建立失敗時改回 RPC 錯誤（`can't start new thread`，對齊 Python `worker.start()` 的 `RuntimeError`）並留下含 by_id 的告警；worker 於開埠與退休佔位前建立，失敗時 RELEASED 佔位與持久化阻擋原樣保留，不再回 `ok:true`／`ATTACHING` 卻新舊 session 皆不存在。
- 自動 attach 的 worker 建立失敗同樣以 by_id 告警留下診斷。
