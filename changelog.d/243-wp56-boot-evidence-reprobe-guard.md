---
type: fix
scope: sw-core
---
Rust readiness reprobe 新增非同步 reboot 開機證據等待守衛（對齊 Python `_prepare_reprobe_locked` 與 `_reprobe_target_still_valid_locked`，`sw_core/session_manager.py:2218-2223`、`:2339-2340`，#243）：`reprobe::prepare` 在 boot quiet 守衛之後檢查 `boot_evidence_wait_active`，等待中直接不規劃 probe／attach，不排 backoff、不設 inflight、不記 attempt；`reprobe::target_still_valid` 在 quiet 守衛之後重驗，規劃後才開始的等待同樣擋下寫入。寬限到期（`now >= boot_evidence_wait_until`）後下一個 tick 恢復原規劃，READY 再確認的 deadline 不延長。既有的 expire、bootloader 終態、帳密、human-active 與 single-flight 次序不變。
