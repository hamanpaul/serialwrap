---
type: fix
scope: rust-refactor
---
Rust daemon 的 `command.submit` 對成功且未帶 `expect_reboot:true` 的非 interactive 串接重開機命令加入非致命 `CHAINED_REBOOT_WARNING` 與 `warnings`；命令仍照常送出，既有 `CMD_LENGTH_WARNING` 保持 `warning` 首則相容欄位。
