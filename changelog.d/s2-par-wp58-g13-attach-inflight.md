---
type: fix
scope: rust-refactor
---
Rust daemon 的 `device.attach` 遇一般 attach 進行中（待開埠、開埠中、已開埠等落盤、bridge 已開但登入未完成）時回 `{"ok":true,"session"}`，不再回 `SESSION_RECOVERING`，登入途中也不再帶 `already_attached`，且不重複開埠或登入；drain、release 進行中、session 仍為 RELEASED 或 release 持久化未完成時維持 `SESSION_RECOVERING`（含 `drain_stalled`、`drain_timeout_epoch`、`session`，SC-04）。`session.attach` 行為不變（對齊 Python `sw_core/session_manager.py` S1X-a-3）。
