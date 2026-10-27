---
type: feat
scope: rust-daemon
---
Rust daemon 承接 `session.console_attach`／`console_detach`／`console_list`（Slice 2.2）：PTY 後端在 RPC thread 預建 PTY 後交 session actor，core 決定 adopt（交 ConsoleHub）或丟棄；產品 executor 接上每 session 的 ConsoleHub（RX 分流、console 輸出）與每 epoch 的 human TX pump，並以 core 的 `LeaseIndex` 效果在回覆前同步更新 interactive 索引（console attach 開的 human lease 可立即 `interactive_status`）。查無 selector 時 console 三方法與 `interactive_open` 回 `SESSION_NOT_READY{selector}`（Python 原樣 selector）；`sw_core::SLICE` 改為 `2.2`，延後 method 剩 event／file 12 個。
