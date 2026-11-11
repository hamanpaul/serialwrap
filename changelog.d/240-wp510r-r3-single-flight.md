---
type: fix
scope: rust-core
---
依據 S2-PAR-wp510r 第 8 項（設計 DR-8；擁有者 2026-11-11 SPEC-GAP 1 裁決）實作同一 session 的單次在途：
- 開埠途中裝置改指、舊路徑開埠成功而退休時，不再於同一事件以新路徑送 `OpenBridge`；舊 bridge 的關閉回條（`handle_closed:true`）到了、且沒有停止待決時，才以目前路徑重開。回條 `handle_closed:false` 時不重開，session 停在 ATTACHING（Python attach worker 同步關閉舊 handle、結束後才重試，`_draining_by_ids` 內不重試）。
- 等待舊路徑回條期間收到的 `DeviceAttached` 不另開埠，回條後以最新路徑開一次；release／shutdown 照常等到舊回條，release 在途不重開。
- clear、拔除、改指等停止在只等舊路徑回條（沒有開埠在途）時照無 bridge 收尾，不被退休 bridge 卡住（Python clear／`update_devices` 不等被取消的 attach 作業）。
- 凍結節點依裁決改寫：`session_manager::attach_finish_respawns_when_state_requires`（表外，擁有者核准列入 S 類）、`drain_release` #5、#6（設計 §6 S 類）；第 7 項登記的三個過渡節點前置改為單次在途下的可達交錯。
