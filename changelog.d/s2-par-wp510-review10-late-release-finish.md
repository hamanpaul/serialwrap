---
type: fix
scope: rust-refactor
---

- 一般 session 的 `device.release` 等待進行中的 attach 作業逾時、回 `DRAIN_STALLED` 之後，作業遲到收尾（開埠返回後關閉 handle）時照 Python 完成 release：session 轉 RELEASED，`released_by`／`released_reason` 取第一個 release 請求的 source 與 reason，清除 `drain_stalled`／`drain_timeout_epoch` 並寫入 state.json；不再停在 RECOVERING、也不必再下一次 release。
