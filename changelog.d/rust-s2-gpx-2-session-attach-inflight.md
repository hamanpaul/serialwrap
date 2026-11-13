---
type: fix
scope: rust-refactor
---

- 對齊 Python 的 `session.attach` 在途分流：一般開埠中回覆 `ok` 並沿用現有作業；裝置離線回 `DEVICE_NOT_FOUND`；release 在途與 `RELEASED` 維持各自回應。無 bridge 的 release 收尾清除 `drain_stalled`，bind 等待逾時遇 `RELEASED` 也轉為 `RECOVERING`。
