---
type: fix
scope: rust-s2-rh-h1
---
修正 `event_dispatcher` 測試在完成事件送出後立即讀取 counter 的競態。測試改以有時限的 `fires` 與 `last_fire_ts` 條件同步，並在 stop 前確認 worker 已完成計數寫入。
