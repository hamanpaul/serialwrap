---
type: feat
scope: rust-console
---
Rust daemon 的 TCP console 後端（SC-28 `SERIALWRAP_CONSOLE_BACKEND=tcp`；Windows 原生為同一路徑）：每次開埠由 executor 綁定一個 127.0.0.1 ephemeral Telnet listener 交給 ConsoleHub，端點隨 bridge adopt 同一事件登記（bridge 停止時關閉、端點清為 null）；`session.console_attach` 回 `{"client_id":null,"label",…,"vtty":端點,"endpoint":端點,"transport":"tcp","protocol":"telnet","session"}` 且不開 lease（對齊 `uart_io.py` `attach_console` 無 PTY 分支），SessionPublic 恆輸出 `console_endpoint`。
