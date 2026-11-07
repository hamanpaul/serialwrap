---
type: feat
scope: rust-core
---
P8（Slice 2.4）Task 9：Rust daemon 的 `/dev/ttyMCU` flash 端點與認線協調介面與失敗測試。新增 `sw_core::flash_endpoint`（`FlashEndpoint`、`FlashCoordinator`、兩層停止旗標 `EndpointShutdown`／`FlashRunCancel`、`detect_mcu_line`、`pump_endpoint_to_sink`、`watch_flasher_disconnect`，`todo!("P8-T9")` 骨架）與 `SessionManager::tx_for`／`line_control_for`／`send_control`；`IoWorker` 於 split 前保留序列線控制。新增 `flash_coordinator` 測試台（`FlashRig`／`Mcu`）、Slice 2.4 黑箱 `test_s24_flash_endpoint.py`，以及 flash-critical 期間 DeviceDetached 對齊 Python 略過的回歸測試。
