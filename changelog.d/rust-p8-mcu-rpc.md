---
type: feat
scope: rust-daemon
---
P8（Slice 2.4）Task 10：Rust daemon 組裝 `/dev/ttyMCU` flash 端點，`mcu.status` 回傳真值。`mcu.status` 的 `flashing` 取自端點（flasher 首讀之後、偵測進行中即為 true，MCU-STATUS-01），`flashing_com`／`last_detect` 取自認線協調者；`candidates` 仍為唯讀快照、不標 flash-critical；`mcu.patterns` 位元組不變。daemon 於 bootstrap attach 之前建立認線協調者（RX observer），bootstrap attach 與 watcher 啟動之後才開端點（`caps.flash_endpoint=false` 的 Windows 不建端點，`flashing` 恆 false）；端點啟動失敗 exit 1；端點以 RAII 持有，RPC 綁定失敗、正常停止、panic unwind 都在釋放實例鎖之前移除 link（有序的端點停止步驟由 Task 12 承接）。`RuntimePaths` 新增 `ttymcu_path`（`$SERIALWRAP_TTYMCU_PATH` 或 `RUN_DIR/dev/ttyMCU`，與 Python `TTYMCU_PATH` 相同），`runtime_paths` golden 加欄並補 `SERIALWRAP_TTYMCU_PATH` 案例。
