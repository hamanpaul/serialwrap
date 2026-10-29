---
type: fix
scope: rust-s21
---
P5 第四輪併入主線：`ProcScan` holder 掃描拆成兩種語意——`serialwrapd_tty_holders` 維持主線 fail-closed（目標 tty 取不到身分或 fd 讀取錯誤即 `Permission`／`Unknown`，供 CLI 更新／回滾確認釋放），新增 `multi_open_tty_holders` 對齊 Python `detect_multi_open`（略過無法 stat 的 tty 與 fd），daemon `health.status` 改用後者；Rust daemon 的最小 passthrough template 補上 `boot_done_regex` 預設空字串。
