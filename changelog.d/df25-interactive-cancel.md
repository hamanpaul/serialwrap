---
type: change
scope: rust
---

- sw-pal：`FakeWalFs` 支援依路徑設定一次性寫後暫停（`pause_after_next_write`），供 DF-25 interactive cancel 測試在 TX WAL 寫入後、執行器返回前確定性暫停並釋放鎖；寫入端等待至明確 release 或 handle Drop 才放行，離開暫停時重設 paused 狀態（P8-a94151a-1）。
- sw-daemon：新增 DF-25（OD-77 B1）ServiceRig 黑箱回歸 `df25_interactive_cancel_preserves_lease`（interactive 命令 TX 完整交 OS 並落 TX WAL、執行器返回前經 RPC 取消，存活 lease 不被撤銷，後續兩次 `session.interactive_send` 送達）與 `df25_cancel_queued_interactive_zero_tx`（排隊中取消零 TX、不開 lease）；Rust 取消只撤銷原命令前景 grant 的 op、lease 依 interactive id 判定，初次即綠，產品碼不變。
