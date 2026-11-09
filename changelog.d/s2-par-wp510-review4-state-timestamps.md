---
type: fix
scope: rust-refactor
---

- session 狀態改變時一律更新 `last_state_change_at`（對齊 Python state setter）：啟動還原的 RELEASED（含 OD-63 佔位）會寫入時間；自 RELEASED 的 `device.attach` 落盤成功轉 ATTACHING、開埠或落盤失敗還原先前狀態時也會更新。
- OD-63 佔位 `device.attach` 的交接回應（ATTACHING 與一般開埠錯誤的 DETACHED）改帶交接當下的 `last_state_change_at`，不再沿用佔位建立時的時間。
