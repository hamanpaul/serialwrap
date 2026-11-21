---
type: fix
scope: tests
---
`wp73_rpc_reboot_clears_rx_for_console_unlock_service_rig` 等待 recovery 時每圈改推 10 ms 假時間（原為 200 ms）：原本的節奏會在較慢的 CI runner 上把 `ensure_console_unlocked` 的 2 秒假時間預算耗盡，使每次解鎖嘗試都在送出密鑰前逾時，session 落在 ATTACHED 而測試等到上限。等待上限由 60 秒改回 10 秒，逾時或落入終態時印出 session 狀態與 TX。只改測試，產品行為不變。
