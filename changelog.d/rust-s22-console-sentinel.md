---
type: feat
scope: rust-console
---
Rust daemon 的 POSIX bridge 起來且沒有保留 console 時，建立 internal 哨兵 primary console（`label="primary"`，對齊 `uart_io.py` `_create_console_client("primary")`），交 ConsoleHub 管理；`session.vtty` 指向哨兵 PTY、`console_count` 與 `console_list` 皆計入哨兵，release 回應的 `closed_consoles` 改為 `len(list_consoles())` 語意（哨兵＋全部 console）。
