---
type: fix
scope: rust-device-handoff
---
Rust `device.attach` 遇到 Held／AccessDenied 開埠失敗時，依 Python `attach_device` 回覆鍵序並省略 `session`；force 仍拒絕已識別的外部 holder，維持 RELEASED 且不新增 UART TX。
