---
type: fix
scope: command
---

承接外部 PR 288（原作者 weisheng-lin-227，保留其提交歷史）。Rust daemon 的 `cmd submit` 改為遵守 profile 的 `max_console_line_chars`：命令的 UTF-8 長度超過這個 console 單行上限時，在入列與任何 TX 之前回 `CMD_TOO_LONG`，`limit` 為該 profile 的值（broker 的 16 KB 硬上限仍回 `limit: 16384`）。超限命令不建立紀錄、不入列、不送 UART；檢查順序為 16 KB 硬上限之後、換行檢查之前。計法與 file transfer 相同（完整命令的 UTF-8 bytes，不含結尾換行）；未設此欄位的 profile 行為不變，出貨 template 也沒有預設值。所有 `command.submit` mode 均套用，human console 的整行輸入不套用。
