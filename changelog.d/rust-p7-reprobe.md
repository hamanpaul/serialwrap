---
type: feat
scope: rust-p7
---
P7 Task 5：Rust readiness reprobe（`reprobe.rs`）——`_prepare_reprobe_locked` 13 步（帳密終態、exhausted、退避、quiet、RX 安靜 3 s、human 活躍、single-flight）、`_record_reprobe_attempt_locked` 退避 2／4／8／15 s 至 10 次耗盡、`_reprobe_target_still_valid_locked`（READY＋bootloader 尾行 → 直接落 READY_UNCONFIRMED 終態、last_error 不動）、`_finish_probe_reprobe`（pending 中確認失敗累加、bootloader 尾行 → `BOOTLOADER_STUCK`＋`matched_prompt=`）；以 Python golden `reprobe_decisions`（43 列）、`reprobe_valid`（18 列）逐列鎖定。SessionCore 1 s tick 接線：ATTACHED／READY 再確認走既有 bridge probe、DETACHED 逾時類失敗自動重新 attach；`session list` 的 `reprobe_attempts`／`next_reprobe_at`／`reprobe_exhausted` 改輸出實值。
