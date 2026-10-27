---
type: feat
scope: rust-p7
---
P7 Task 12：Rust event engine 的 `LineBuffer`（16 KiB 切行、嚴格 `>` 截斷、截斷段不剝 `\r`、UTF-8 replace＋ANSI 剝除，golden `line_buffer` 鎖定）與 `matcher`（regex 只看前 4096 字元、contains 字面量、scope→profile→max_fires→cooldown gate 與 debug `match_skipped`、drop-oldest 有界佇列與 `event_dropped`、求值例外收斂、stop 2 s WARNING、`replace_rules` 失敗保留舊 compiled 與 Python 同）。上游補件 UP-P7-02：`sw_proto::pyre::compile_capturing`／`PyRegex::search_groups`（保留全部擷取群組，供 `MatchResult.groups`）。
