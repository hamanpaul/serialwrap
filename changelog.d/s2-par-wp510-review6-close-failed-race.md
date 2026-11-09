---
type: fix
scope: rust-refactor
---

- OD-63 佔位交接的 attach worker 收尾與正式 bridge 開埠失敗並行時，若正式 bridge 關閉 UART handle 的回條為 `closed:false`，該 attach 作業不再被收尾以取鎖前的舊判定移除：收尾改在 manager 鎖內重讀未確認關閉標記，release 維持回 `DRAIN_STALLED`、shutdown 維持列出 `attach:<by_id>` 未收斂（對齊 Python `_finish_attach_op` 關閉失敗保留作業）。
