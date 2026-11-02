---
type: fix
scope: rust-s21
---

修正 sw-pal 測試的 fd 重用與 fork 時窗競態：`invalid_serial_fd_is_a_poll_wait_error` 改用永不可能被配置的 fd 號（POLLNVAL／EBADF 斷言不變）；實例鎖測試的失效 socket 改為只 bind 不 listen、需「立即重新取得」的權限斷言與並行 spawn 壓力測試改在單執行緒子行程執行。Slice 2.1 gate 的收集器 `--caps` 改由 L1 鎖 selector 推導（與 Slice 2.2 gate 相同的 OD-53 規則），推導不出即證據不足。
