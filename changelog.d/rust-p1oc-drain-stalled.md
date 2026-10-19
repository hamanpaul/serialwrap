---
type: change
scope: session
---

`device release` drain 超過 3 秒或 handle 關閉回條失敗時回新錯誤碼 `DRAIN_STALLED`，並附上 `drain_stalled` 與 `drain_timeout_epoch`。release 仍可能在背景完成；請在有限等待內以 `session get_state` 確認狀態為 `RELEASED` 後再交接。等待期限內仍為 `RECOVERING` 時停止重試並交由操作員檢查；期間勿重開裝置。release 尚未完成時，`session attach`／`session clear` 仍回 `SESSION_RECOVERING`。
