---
type: fix
scope: rust-refactor
---

- 預開 UART handle 交給 session、正式 bridge 接手之前，`device.release`、shutdown 與 `session.bind` 一律查得到這個 handle，並照 Python 關閉或等待：release 不再在 handle 仍開啟時回 ok（裝置交給外部燒錄工具時不再被 daemon 占住），shutdown 不再漏關 DETACHED／RELEASED session 手上的 handle，release 撤銷的交接也不會另開新 handle。關閉未確認時照 Python 保留作業並回 `DRAIN_STALLED`／列入 stalled。
- 一般 session 的 `device.release` 遇到進行中的 attach 作業（例如重插後的 profile 再偵測）改為取消並等待作業收尾才回覆，取消後不再送 PROBE；session 已改綁到 parked handle 的裝置時，release 會先關閉該 handle（關閉未確認只告警並回 `DRAIN_STALLED`）。
