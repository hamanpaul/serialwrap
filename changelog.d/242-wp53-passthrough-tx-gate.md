---
type: fix
scope: rust-refactor
---
Rust TX Fence 新增 `bridge_passthrough`，由 `SessionCore::adopt_opened_bridge` 在每次 adopt 時依目前 profile 寫入（預設 false），對齊 Python `agent_gate_active()` 對 passthrough 固定回 False、`_push_tx_gate()` 據此發布 bridge 的 `_gate_quiet`（SC-35、裁決 s2 D2）。passthrough 帶 `expect_reboot:true` 的命令照常走 reboot 路徑，TX 提交後照常 arm boot quiet 與 READY 再確認，但這兩個狀態不再擋 passthrough 後續的合法 agent 命令：修正前 prompt 回來後的下一筆命令在 TX 提交點被擋，回 `AUTOBOOT_QUIET`，修正後會實際寫入 UART 並完成。豁免只限 `Command` 的 quiet／reconfirm，frozen、stale epoch、cancelled、flash 等共同拒絕維持原樣；`FileTransfer`、登入／probe、救援鍵與非 passthrough 的 `Command` 不受影響。
