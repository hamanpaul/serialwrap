---
type: feat
scope: rust-daemon
---
Rust daemon 承接 `session.console_attach`／`console_detach`／`console_list`（Slice 2.2）：PTY 後端在 RPC thread 預建 PTY 後交 session actor，core 決定 adopt（交 ConsoleHub）或丟棄；產品 executor 接上每 session 的 ConsoleHub（RX 分流、console 輸出）與每 epoch 的 human TX pump，並以 core 的 `LeaseIndex` 效果在回覆前同步更新 interactive 索引（console attach 開的 human lease 可立即 `interactive_status`）。查無 selector 時 console 三方法與 `interactive_open` 回 `SESSION_NOT_READY{selector}`（Python 原樣 selector）；`sw_core::SLICE` 改為 `2.2`，延後 method 剩 event／file 12 個。
SessionPublic 的 `console_count` 改為哨兵計數加 human console（無 bridge 時為保留的 console 數），`vtty` 取首個 console 設定的快取（D-20）；`session.console_*`、`session.get_state`／`activity`／`list` 在送 request 前先以同一條 control lane 送 `ConsoleScan{source: Rpc}`（D-09），`console_list` 據此 prune 逾 2 s 無 peer 的非 primary console。
`config.yaml` 新鍵 `windows.serial_console`（D-12／SC-29，Rust-only、OD-01 選配）：`<by_id>: {console_port, peer_port}` 的兩個 com0com 埠自動併入 `DeviceSelection.exclude_coms`；形狀不符寫一筆 WARNING 並忽略整個鍵，非 Windows 平台出現此鍵寫「windows.serial_console 在此平台不支援，已忽略」後忽略（hub 開啟 com0com console 屬 Task 12，LOCAL-WINDOWS-TODO）。
