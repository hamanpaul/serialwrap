---
type: fix
scope: rust-refactor
---
Rust daemon 的 `command.submit {mode:"interactive"}` 非空命令未完整送出（提交點被 fence 拒絕、送出失敗、部分寫入或結果不明）時，關閉本命令剛開的 interactive lease 並自全域 LeaseIndex 移除，後續命令不再被遺留 lease 擋成 `SESSION_INTERACTIVE_BUSY` 直到逾時；只在目前 lease 仍是本命令所開時關閉，期間被收回或換掉的 lease 不動。錯誤碼映射、`command_aborted_in_flight` META 與完整送出時的 lease 保留行為不變（對齊 Python `sw_core/session_manager.py` S1X-a-1）。
