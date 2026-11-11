---
type: fix
scope: rust-core
---
依據 S2-PAR-wp510r 第 10 項（設計第 5 版 §4 R5、§3.1.4 W1）把 OD-63 RELEASED 佔位的 `device.attach` 交接（W1）由 manager 專用路徑改由佔位 actor 執行：
- `device.attach` 一律經 actor（`SessionRequest::DeviceAttachRouted`），帶 manager 在鎖內取得的裝置快照（G-8：重啟後載入、從未收到 `DeviceAttached` 的 RELEASED session 不再誤回 DEVICE_NOT_PRESENT）與呼叫者原 selector（P8-3786805-1）。
- 佔位 actor 先準備交接 worker（`Effect::PrepareHandoffWorker`，D3：建立失敗回 `can't start new thread`、零開埠），再以 PlaceholderRaw 模式開埠（`Effect::OpenPlaceholderRaw`）：開埠工作以預配 token 登記 `Opening{PlaceholderOpen}`（owner 為佔位 actor 世代），提交點有取消就關閉、否則存回 `Held{Session}`，結果以 `PlaceholderOpened` 回報。
- 開埠在途的 release、shutdown 由佔位 actor 的停止機制以 `ClaimForStop` 取消帳本紀錄，等關閉回條後才回覆；A 於開埠返回後依 Python 映射回覆（operation → SESSION_RECOVERING、裝置改指 → DEVICE_NOT_PRESENT、Held／NotFound 照回）。已 RELEASED 的 release 收尾補 `released_by`／`released_at` 並落盤（WP4-1）。
- 退休在佔位 actor 執行緒上、manager 鎖內一次完成（`Effect::RetirePlaceholder`）；落盤失敗不回滾，handle 依 D2 以同一 token 存回 `Held{Parked}`；退休後佔位 actor 回覆已排入的請求為 SESSION_NOT_FOUND 並結束（P8-19df186-1）。
- `already_released` 回應鍵序改為 Python 的 ok／already_released／session（G-2）。
- 移除 manager 的佔位交接路徑（`Shared::attach_released_placeholder`、`stop_retired_placeholder_actor`）與 `PlaceholderAttachStalled／Settled`；attach worker 作業與未確認關閉作業的 release 逾時改以 `MarkAttachStalled` 設定 stall，遲到收尾由 `finish_worker_op` 補完 release。
