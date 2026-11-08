---
type: change
scope: rust-pal
---
新增 `/dev/ttyMCU` 端點的真 PTY 回歸測試 `cap_posix_pty__endpoint_slave_never_exclusive`（`crates/sw-core/tests/flash_coordinator.rs`）：端點啟動後與完成一次 matched→pump→FlashHolderGone 循環後，以一般 open 讀 `TIOCGEXCL` 皆為 0，第二次 flasher 一般 open 成功且 sync 仍得 ACK 回放。`sw_pal::testutil`（僅 `test-fakes`）新增 `PlainTty`／`tty_exclusive`，讓 sw-core 測試不需自行分支平台；產品行為不變。
