---
type: feat
scope: pal
---
sw-pal 新增 `/dev/ttyMCU` flash 端點所需的平台能力：`ConsoleTransportFactory` 的 `link`／`unlink`／`tty_setraw`／`tcgetattr_speed`、`ConsoleIo` 的 `clone_writer`／`wait_readable`、`SerialLineControl`（`set_baud`／`mirror_termios_from`，絕不把 B0 寫到實體 UART）與 `Capabilities.flash_endpoint`（POSIX true、Windows false，其餘 Windows 路徑回 `Unsupported`）。記憶體替身同步補上。
