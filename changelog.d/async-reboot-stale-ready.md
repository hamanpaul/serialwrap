---
type: fix
scope: boot-quiet-window
---

修正非同步 reboot（prplOS／OpenWrt 的 `reboot` 在 shell 重印 prompt 後才慢慢關機）後，關機中的舊 shell 回應 READY 確認 probe 的 nonce，讓 READY 在板子重開前就被確認（stale-READY）、#162 防線被繞過的問題。過去 `_handle_reboot_command` 在 2 秒內 prompt 回來時只清 TX 靜默、保留 `ready_reconfirm_pending`，但 quiet 一清，readiness reprobe 在 RX 閒置 3 秒後就送 `\n`＋`echo __READY__<nonce>`，舊 shell 照常應答，`confirm_ready()` 清掉 pending，agent 命令因此通過 gate、落進關機中的系統（讀到舊 boot_id 或被重開打斷）；IB8 實機 8 次單獨 `reboot` 的 WAL 都在 +3.1～+6.3s 發生，而 U-Boot banner 要到 +16.9～+25.9s 才出現。現在判定為非同步 reboot 後，確認 probe 要等 RX 出現 boot banner（沿用既有 `detect_boot_banner`，不寫死平台字串）才送；寬限 `BOOT_EVIDENCE_GRACE_S`（60s，約最慢觀測值的 2.3 倍）內都沒看到 banner 則視為板子其實沒重開，照常確認，不卡死。等待期間 agent 命令維持可重試的 `AUTOBOOT_QUIET`，`session list` 新增 `boot_evidence_wait_remaining_s`；`READY_RECONFIRM_MAX_S` 的有界上限、`READY_UNCONFIRMED` 終態、自發重開機與 human 來源的 reboot 路徑不變。
