---
type: feat
scope: rust-console
---
Rust daemon 的 POSIX bridge 起來且沒有保留 console 時，建立 internal 哨兵 primary console（`label="primary"`，對齊 `uart_io.py` `_create_console_client("primary")`），交 ConsoleHub 管理；`session.vtty` 指向哨兵 PTY、`console_count` 與 `console_list` 皆計入哨兵，release 回應的 `closed_consoles` 改為 `len(list_consoles())` 語意（哨兵＋全部 console）。
哨兵改由 executor 在開埠成功時預建並隨 `BridgeOpened` 交給 core，於 bridge adopt 的同一事件登記，passthrough 等立即 ATTACHED 的平台也不會被搶先 attach 的 human console 取代；drain 後 `vtty` 改指保留 console 的 primary、release 後為 null。executor 交付 PTY 失敗時移除該 console，不留下無 fd 的幽靈 console。
