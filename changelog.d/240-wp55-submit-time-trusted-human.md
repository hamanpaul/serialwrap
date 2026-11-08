---
type: fix
scope: rust-refactor
---
Rust session 提交時（submit-time）的 quiet／reconfirm gate 改以 `SubmitReq::is_trusted_human()`（`trusted_human` 旗標與 `HumanConsole` 來源取 OR）決定是否豁免，對齊 Python `service._resolve_session_id` 的 `if not trusted_human`：RPC 原始值 bare `human`（及空名 `human:`）於 boot quiet、READY reconfirm pending、READY_UNCONFIRMED 期間可正常 admission；明寫 `human:cli` 等字首與 `agent:*` 仍回原本的 `AUTOBOOT_QUIET`／`READY_UNCONFIRMED` 且不產生命令紀錄。有界化終態仍於 gate 前先評估、不因來源略過；非 READY、RECOVERING、FLASHING 等前置檢查不受可信標記影響。
