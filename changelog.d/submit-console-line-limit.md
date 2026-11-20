---
type: fix
scope: command
---

`cmd submit` 改為遵守 profile 的 `max_console_line_chars`：命令的 UTF-8 長度超過這個 console 單行上限時，在入列與任何 TX 之前回 `CMD_TOO_LONG`，`limit` 為該 profile 的值（broker 的 16 KB 硬上限仍回 `limit: 16384`）。過去這個欄位只用在 `file push`／`file pull`，一般命令照送，被 target 截斷後等不到 prompt、逾時，還會觸發 CTRL_C／CTRL_D 恢復。計法與 file transfer 相同（完整命令的 UTF-8 bytes，不含結尾換行）；未設此欄位的 profile 行為不變，出貨 template 也沒有預設值。
