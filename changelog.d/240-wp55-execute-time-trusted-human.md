---
type: fix
scope: rust-refactor
---
Rust session 出列時的 execute-time 判定改用 `SubmitReq::is_trusted_human()`（`trusted_human` 旗標與 `HumanConsole` 來源取 OR），quiet gate 與 interactive lease 判斷共用同一結果，對齊 Python `session_manager.py` 的 `human = trusted_human`：RPC 依原始 source 判定的可信 human 於 quiet 期間出列不再被 `AUTOBOOT_QUIET` 終結，遇 human lease 改回 `SESSION_INTERACTIVE_BUSY`；明寫 `human:` 字首但非可信的 RPC 與 `agent:*` 一律以 agent 對待，仍被 gate 擋下且零 TX。
