---
type: feat
scope: rust-daemon
---
P8（Slice 2.4）Task 12：Rust daemon 在 systemd 監管下的有序停止。停止序依 arch §6.1 為 `server.stop_accept`（只停 accept、不等 handler）→ stopping gate → `watcher.stop` → `engine.close_intake` → `/dev/ttyMCU` 端點停止（2.0 s join）→ 全部 session drain Phase 1（取消進行中的檔案傳輸等長 RPC）→ `join_handlers`（與 drain 共用 5 s 期限，逾時列出仍在執行的 method）→ Phase 2 回條與 `FLUSHED_BY_SHUTDOWN` 收尾 → `engine.stop` → WAL sync／close → 釋放實例鎖。任一步逾時不跳過後續步驟，flash 端點未收斂（`flash endpoint stop incomplete: <threads>`）與 handler 未收斂（`rpc handlers did not converge: <methods>`）併入既有的 `serialwrapd: shutdown incomplete:` 診斷並 exit 1，讓 `Restart=on-failure` 辨識異常停機；停止序進行中的第二次 SIGTERM／SIGINT 只記一行 log。新增 `crates/sw-daemon/tests/shutdown_order.rs` 與 systemd 黑箱 `tests/contract_blackbox/test_s24_supervision_systemd.py`（transient `systemd --user` unit；docker 執行屬 L1.5-docker）。
