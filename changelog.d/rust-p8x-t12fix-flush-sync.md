---
type: fix
scope: rust-daemon
---
P8（Slice 2.4）Task 12 停止序測試修正：`sigterm_graceful_exit_0_and_flushes_queue` 在 daemon_process 全集並行負載下，socket bind 時 COM0 仍在 `ATTACHING`，前景命令被拒為 `SESSION_NOT_READY`，第一筆排隊命令因此成為執行中的前景命令而不被 `FLUSHED_BY_SHUTDOWN` 沖刷。測試改為等 COM0 `READY`、前景命令進入 `running` 後才排隊，並於停機前確認排隊命令仍為 `accepted`；停止序產品行為不變。test-fakes 的 `SessionManager::spawned_worker_count` 改為只計 spawn 成功的 attach worker。
