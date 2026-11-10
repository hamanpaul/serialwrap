---
type: change
scope: rust
---

- sw-pal：`FakeWalFs` 支援依路徑設定一次性寫後暫停（`pause_after_next_write`），供 DF-25 interactive cancel 測試在 TX WAL 寫入後、執行器返回前確定性暫停並釋放鎖。
