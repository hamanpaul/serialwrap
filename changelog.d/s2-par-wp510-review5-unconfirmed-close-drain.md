---
type: fix
scope: rust-refactor
---

- OD-63 佔位交接後、正式 bridge 接手前關閉 UART handle 失敗（`closed:false`）時，該裝置持續參與排空判定與阻擋：shutdown 回報 `attach:<by_id>` 與該 session 未收斂，release 回 `DRAIN_STALLED`，自動 attach 與佔位交接維持阻擋；不再因後續 `daemon stopping` 或 UART 設定失敗等一般開埠錯誤清除 stall 而回報成功（對齊 Python `_finish_attach_op` 保留作業與 `shutdown_drain` 收集未完成作業）。
- shutdown 只把 `ok:true` 的收斂回覆計為已排空。
