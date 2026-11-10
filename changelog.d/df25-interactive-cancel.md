---
type: change
scope: rust
---

- sw-pal：`FakeWalFs` 支援依路徑設定一次性寫後暫停（`pause_after_next_write`），供 DF-25 interactive cancel 測試在 TX WAL 寫入後、執行器返回前確定性暫停並釋放鎖；寫入端等待至明確 release 或 handle Drop 才放行，離開暫停時重設 paused 狀態（P8-a94151a-1）。
