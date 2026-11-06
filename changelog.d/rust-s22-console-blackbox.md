---
type: change
scope: contract-blackbox
---
新增 Slice 2.2 console 黑箱契約 `tests/contract_blackbox/slice22/`（第一批 9 案：首個 PTY console 取得 raw owner、第二個走 line buffer、RX 分流到所有 console、owner detach 後第二個 console 的整行送出與 WAL `cmd_id`、backspace 本地回顯、detach 釋放 owner、`CONSOLE_NOT_FOUND`、`INVALID_ARGS`／alias selector／`SESSION_NOT_READY{selector}`、PTY 平台無 `console_endpoint`、`session.vtty` 快取），可對 Python 基線與 Rust artifact 執行。
第二批 11 案：lease 期間第二個 console 的行不送出、agent 命令期間 human 位元組進 deferred 並於命令結束後依序回放（WAL seq）、agent 多輪後 raw owner 保留、無 peer 的 console 於 2 s 後被 prune、line editor 的 interactive 命令開 human lease、行溢位 WARNING、斷線前最後一行仍送出；interactive lease 的 open／send／status／close 形狀、重複 open BUSY 零 TX、soft preempt 還原回放、send 的 key／base64／hex 編碼。
第四批：`FakeTarget` 追加 keyword-only 選用能力 `scripts`（指定行延遲回覆，可選不回 prompt）、`ready=False`（不回 ready nonce）與 `hold_reply(on)`（暫停所有回覆），預設行為不變；新增 open 拒絕（RECOVERING／BUSY）零 TX、ATTACHED 救援（boot interrupt、login required、NOT_BOOTLOADER、未帶 allow_attached）、stash 丟棄不回放（Rust-only）、BC-02 banner 使 deferred 失效、恢復窗輸入不回放（Rust-only）、雙板交錯命令不互相餓死、第二個 daemon 被動偵測、tty 持有者回報；lazy 到期後 send／status／close 一律 `INTERACTIVE_EXPIRED`（#54），BC-02 不交錯改以每個 agent 命令的 WAL TX seq 區間斷言。
