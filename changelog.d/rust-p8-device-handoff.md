---
type: change
scope: rust-core
---
P8（Slice 2.4）Task 11：外部工具裝置交接（`device.release`／`device.attach`）的黑箱與整合驗證，不改產品碼。新增 POSIX 黑箱 `tests/contract_blackbox/test_s24_device_handoff.py`（17 案，Python 基線與 Rust artifact 同檔 A/B；含凍結案例 `s24-ho-ext-tool`、`s24-ho-second-daemon`、`s24-ho-persist-restart`，以 `HolderSampler` 10 ms 連續取樣證明交接期間從未雙持有、以 DUT 端 `rx_log()` 證明零 TX）與 Windows 原生黑箱 `test_s24_device_handoff_windows.py`（`s24-ho-windows-com`，OD-15 擁有者 host gate；無原生 Windows／`SW_TEST_COM_PAIR` 時 SKIP、記證據不足）。Rust L1 `crates/sw-core/tests/handoff.rs`（6 案，`ActorRig`）驗證 P8 新增交互：FileTransfer 占前景槽時 release 回 `aborted_cmd:true`、flash-critical 標記到 probe 結束解除前 release／clear 皆回 `FLASHING_BUSY`、FLASHING 中 release 被拒，以及 drain 卡住（`DRAIN_STALLED`、attach 回 `SESSION_RECOVERING` 且零 open、重複 release 不產生第二個 StopOp）。
