---
type: fix
scope: rust-pal
---
Windows `NativeConsoleFactory` 的 `bind_tcp_console()`、`new_mux()` 與 `open_com0com_console()` 在 Winsock／WSAPoll／com0com 實作完成前改回 `PalErrorKind::Unsupported`，不再落到 `todo!` 而 panic；新增主機端原始碼守門與 Windows 原生行為測試（sol 補審 P6 #4）。
