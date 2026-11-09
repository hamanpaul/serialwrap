---
type: feat
scope: rust-core
---
P8（Slice 2.4）Task 8：Rust daemon 的 SessionCore 承接 MCU flash 仲裁。`TxPermit` 新增 `probe_id`，fence 只在同一 probe 的 `Probe(id)` 放行 `FlashProbe` TX、只在 FLASHING 放行 `Flash` TX，上一個 probe 的 stale permit 一律零 TX；新增 `FlashSyncProbeStart`／`FlashProbeReleased`／`FlashSyncMatched`／`FlashHolderGone` 事件（拒絕原因 `FlashReject`，含 SC-14 前景忙），Matched 是同一 bridge handle 的模式切換（不重開、不 drain）並依 BC-02 失效 human deferred 輸入；FLASHING 與 probe 窗口期間 submit／interactive／release／clear／bind／recover 回 `FLASHING_BUSY`，human console 輸入丟棄，probe 窗口的 interactive send 依 UIO-12.3-d 靜默回 ok、外部零 TX。`SessionManager` 新增 `collect_flash_candidates_and_mark`／`unmark_flash_critical`／`flash_critical`，flash-critical 期間 destructive RPC 回 `FLASHING_BUSY`、passthrough 不安靜升 READY（SC-35）。

SC-32（Rust-only）：Probe／FLASHING 期間 `UartIoError` 照常進 Two-Phase Drain 轉 RECOVERING（回條前不重開 UART），`DeviceDetached` 延後到 flash 結束（HolderGone、未匹配釋放或 I/O error drain）後再 reconcile（裝置已回同一路徑則不 detach、換路徑則依 DEVICE_REBOUND_REQUIRED）；所有 flash 結束路徑無條件清 Fence 與 Ownership 的 `flash_mode`。不新增錯誤碼與 WAL META；Python 基線不變。
