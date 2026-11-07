---
type: fix
scope: rust-p7
---
取消延遲量測（`scripts/rust/event_latency_measure.py` 的 `_sample_cancel()`）現在等被取消命令的 worker 收尾後才結束樣本：取消成功、命令記錄已有 `done_at` 之後，還要在同一份 `session.get_state` 快照看到 READY 且 `foreground_busy` 為 false。Python 與 Rust 都在取消回覆前寫入 `done_at`，原本的等待迴圈第一次輪詢就會結束；實測 Python 200／200、Rust 32／200 筆在 worker 仍忙時結束樣本，下一筆的 accepted 等待因此算進前一筆的收尾時間。收尾等待沿用原有的 15 秒期限，超過就記錯；狀態 RPC 失敗、缺 `foreground_busy` 欄位或 session 不在 READY 都不算收尾。計時區間（`command.cancel` 從 RPC 送出到回應）、10 秒 accepted 等待、樣本數與判準都沒有變更。
