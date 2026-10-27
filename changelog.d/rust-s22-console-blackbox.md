---
type: change
scope: contract-blackbox
---
新增 Slice 2.2 console 黑箱契約 `tests/contract_blackbox/slice22/`（第一批 9 案：首個 PTY console 取得 raw owner、第二個走 line buffer、RX 分流到所有 console、owner detach 後第二個 console 的整行送出與 WAL `cmd_id`、backspace 本地回顯、detach 釋放 owner、`CONSOLE_NOT_FOUND`、`INVALID_ARGS`／alias selector／`SESSION_NOT_READY{selector}`、PTY 平台無 `console_endpoint`、`session.vtty` 快取），可對 Python 基線與 Rust artifact 執行。
