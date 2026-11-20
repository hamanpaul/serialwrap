---
type: fix
scope: rust-refactor
---
Rust session actor 的 reboot 判定改為與 `OpRunner::run_command` 相同的 `expect_reboot || (!passthrough && 命令文字猜中)`，對齊 Python `_handle_reboot_command` 不分旗標或猜中的處理：帶 `expect_reboot:true` 的命令（如 `echo a && reboot`）送出前失效 human deferred 輸入；TX 實際提交（confirmed > 0 或結果不明）後由 TxWriter 在同一 Fence 臨界區 arm boot quiet 與 READY 再確認，提交點被撤銷、零 OS 提交時不 arm、不進 RECOVERING；prompt 在等待期限內回來時只清 TX 靜默、保留 `ready_reconfirm_pending`，下一筆 agent 命令以 `AUTOBOOT_QUIET`（retry 5 s）拒絕且零 UART TX，不再把串接重開命令後的 prompt 當成 READY 再確認證據。
