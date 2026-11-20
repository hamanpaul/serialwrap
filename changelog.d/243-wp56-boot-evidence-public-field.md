---
type: fix
scope: sw-core
---
Rust session public 投影新增 `boot_evidence_wait_remaining_s`（對齊 Python `SessionRuntime.to_public_dict()`，`sw_core/session_manager.py:897-903`，#243）：插在 `ready_reconfirm_remaining_s` 之後、`idle_for_ms` 之前，與同一次投影的其他欄位共用 `now`；非同步 reboot 開機證據等待有效時照 Python `round(until - now, 1)` 輸出，未開始、已解除或恰好到期為 null。欄位為 transient、不寫 `state.json`，讀取 public 不清除等待、不動 reconfirm 的 pending／deadline。本項只公開欄位，尚未接 reboot 路徑與 probe 守衛。
