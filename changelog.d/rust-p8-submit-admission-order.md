---
type: fix
scope: rust-daemon
---
P8（Slice 2.4）Task 14 審查 P8X-ee2b64c-1：Rust daemon 的 `command.submit` admission ticket 改在 `CommandRegistry::admit` 的命令驗證之後、入列之前取得（對齊 Python `CommandArbiter.submit()`；`command.submit` 不在 RPC 分派前的 admission 清單）。交接排空窗口或 shutdown 凍結期間，selector 查無、缺 selector、非法 completion regex、`expect_reboot` 型別錯誤、`CMD_TOO_LONG`、`CMD_CONTAINS_NEWLINE` 與 session 狀態閘門（`SESSION_RECOVERING`／`FLASHING_BUSY`／`SESSION_NOT_READY`）照 Python 回原錯誤碼，只有通過全部驗證者才回 `FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`；human console 行仍不取 ticket。
