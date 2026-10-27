---
type: feat
scope: rust-p7
---
P7 Task 6：Rust transport stall 分類（`transport_stall.rs`：`is_transport_refinable_error`、`classify_probe_failure`（零 RX＋曾有 RX＋凍結 ≥30 s）、`transport_stall_hint` 逐字、`refine_probe_failure` 的一次性告警 log／WAL META 與 login 失敗碼 RX tail detail），以 Python golden `transport_stall`（30 列）鎖定；sw-pal：POSIX `UsbTopology` 由 sysfs tty `device` 連結解析 USB busid（`SysfsUsbTopology`，可注入 root）、Windows 恆 `None`；`FsOps` 寫入側 `open_append`／`create_truncate`／`file_size`／`remove_file_if_exists` 原生實作（錯誤訊息照 Python `str(OSError)` 帶 `: '<path>'`）。
