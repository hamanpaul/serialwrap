---
type: fix
scope: rust-refactor
---
Rust daemon 的 `device.attach` 遇 attach 在途（開埠中、已開埠等落盤，含自 RELEASED 發起者）但裝置已拔除時，改回 `{"ok":false,"error_code":"DEVICE_NOT_PRESENT","selector","device_by_id"}`，不再回 `{"ok":true,"session"}` 或 `SESSION_RECOVERING`，也不重複開埠；裝置在場檢查依 Python `attach_device` 的順序位於在途分流之前（對齊 `sw_core/session_manager.py` `:3423-3424`）。drain 進行中仍回 `SESSION_RECOVERING`，`session.attach` 行為不變。
