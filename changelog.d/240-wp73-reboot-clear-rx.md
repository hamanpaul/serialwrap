---
type: fix
scope: rust-refactor
---

- Rust daemon：`run_reboot` 在送出 reboot 命令前，若 `console_unlock_env` 已宣告，丟棄 `ctx.rx` 舊資料（`ctx.rx.clear()`，對應 Python `session_manager.py:6421-6423` 的 `bridge.clear_rx_buffer()`），避免已重開的舊 root prompt 在 recovery unlock 流程中被誤當成解鎖成功的證據；WAL 紀錄仍完整保留。
