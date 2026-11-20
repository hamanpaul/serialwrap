---
type: fix
scope: rust-s21
---
Rust WAL health 補齊 Python 的 OD-19 三鍵（rotate_retry、rotate_deferred、deferred_delete_pending，位於 recreated_count 之後，共 14 鍵）；暫時性輪替失敗（EACCES／EBUSY）計入 rotate_retry（parity-amendments-2026-10-26 #61）。
