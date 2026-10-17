//! 共用 `graceful_stop`（P4-T10 承接）：先停用拉起能力 → RPC `daemon.stop`（2.0 s）→ 等待結束（≤10 s）→ 逾時才硬終止或回 `TIMEOUT`。
