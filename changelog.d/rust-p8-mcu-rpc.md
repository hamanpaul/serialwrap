---
type: feat
scope: rust-daemon
---
P8（Slice 2.4）Task 10：Rust daemon 承接 `mcu.patterns`／`mcu.status` 的 flash 真值與 `/dev/ttyMCU` 端點組裝（進行中：測試先行）。`RuntimePaths` 新增 `ttymcu_path`（`$SERIALWRAP_TTYMCU_PATH` 或 `RUN_DIR/dev/ttyMCU`），`runtime_paths` golden 加欄並補 `SERIALWRAP_TTYMCU_PATH` 案例。
