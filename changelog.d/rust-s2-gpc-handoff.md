---
type: fix
scope: rust-device-handoff
---
Rust `device.attach` 遇到 Held／AccessDenied 開埠失敗時，依 Python `attach_device` 回覆鍵序並省略 `session`；force 仍拒絕已識別的外部 holder，維持 RELEASED 且不新增 UART TX。
實際開埠 NotFound 回覆 Python 的 `DEVICE_NOT_PRESENT` 鍵序；一般 RELEASED session 在 OSError 後若 unreleased 落盤失敗，維持 DETACHED 並清除 released provenance；reboot recovery 已有 bridge 時，`device.attach` 冪等回覆 `already_attached`。
