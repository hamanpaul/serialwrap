---
type: fix
scope: rust-refactor
---

- 佔位 `device.attach` 開埠途中被 `device.release` 撤銷時，handle 安全關閉後比照 Python 補齊 `released_by`／`released_at`（`released_by or source`、`released_at or now`，reason 沿用既有值）並寫入 state.json；持久化失敗時 `device.release` 走錯誤路徑而非回成功，磁碟恢復後下一次 release 重試落盤。
- 預開 UART handle 關閉失敗（`closed:false`）時，以 by_id 與關閉錯誤輸出 daemon 告警後保留阻擋；涵蓋 attach 作業收尾，以及已交給 session、正式 bridge 接手前的關閉路徑。
- 已 RELEASED session 的 release 持久化改以 session 自身的 `released_by`／`released_at`／reason 組列（對齊 Python `_save_state`），不再以 `cli`／null 覆寫既有值。
