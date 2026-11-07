---
type: fix
scope: rust-refactor
---
Rust reboot 分支的 prompt 等待期限對齊 Python `session_manager.py` 的 `prompt_wait_s`：帶 `expect_reboot:true` 且命令文字未被猜中（如 `echo a && reboot`，或 passthrough 上的 `reboot`）時，`OpRunner::run_reboot` 等待 `max(timeout_s, expected_duration_s or 0)`，前段執行超過 2 秒後 prompt 才回來仍判為正常完成（partial:false），不再提早進 `EXPECT_REBOOT` recovery；一直沒有 prompt 則等滿該期限才進 recovery。被猜中的單獨 `reboot` 維持 `min(timeout_s, 2 s)`；無旗標命令仍走一般命令 timeout。超出 `Duration` 範圍的期限（如 `expected_duration_s` 為 `1e300` 或 `inf`）飽和為無限等待，不再讓 OpRunner thread panic。
