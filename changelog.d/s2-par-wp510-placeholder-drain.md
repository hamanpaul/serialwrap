---
type: fix
scope: rust-daemon
---

OD-63 佔位交接遵守 `device.release` 的取消與排空語意；落盤失敗會回傳錯誤並保留佔位，併發 attach 與 `SESSION_RECOVERING` 回應補齊 Python 基線欄位。
