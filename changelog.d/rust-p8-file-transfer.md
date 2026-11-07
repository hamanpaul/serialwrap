---
type: feat
scope: rust-daemon
---
P8（Slice 2.4）Task 6：Rust daemon 承接 `file.push`／`file.pull`。RPC 層逐行照 Python 轉型（`chunk_size=max(1,int(x or 512))`、`chunk_timeout_s` 原值 `float()`、`ack_mode` 白名單、`check_source` 先於必填檢查；轉型失敗回 `INVALID_ARGS`、保留前綴回 `SOURCE_RESERVED`，成功回應依正規化來源附 `source`／`source_normalized_from`）；session actor 在同一次事件處理內完成 resolve 層（passthrough 任何狀態回 `PROFILE_NOT_COMMAND_CAPABLE`、`SESSION_RECOVERING`／`FLASHING_BUSY`／`SESSION_NOT_READY`、quiet／reconfirm 回 `AUTOBOOT_QUIET`、`READY_UNCONFIRMED`）與 manager 層（recovering、agent lease 回 `SESSION_INTERACTIVE_BUSY`、human lease 暫停並於結束時回放、前景槽占用回 `SESSION_BUSY`，皆零 TX）後取得前景槽交 OpRunner。傳輸中被拒依 OD-62 比照 BC-03：quiet 且被拒那筆零寫出回 `AUTOBOOT_QUIET`＋`retry_after_s`；drain／取消／世代過期且整段已確認寫出 ≥1 byte 回 `COMMAND_ABORTED_IN_FLIGHT` 並寫 `command_aborted_in_flight` META（`cmd_id` 為傳輸前綴 `ft-<8hex>`、`confirmed_tx_bytes` 為整段累計），零寫出回 `FLUSHED_BY_RECOVERY`；其餘回 `TRANSFER_TIMEOUT`；不自動重送。
