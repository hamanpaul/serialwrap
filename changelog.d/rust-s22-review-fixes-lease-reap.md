---
type: fix
scope: rust-console
---
Slice 2.2 interactive lease 與 console 對齊 Python（p6-cloud 雙審 1f1fa67..4f710c3）：`interactive_open` 的 refresh 若惰性結束過期 lease 並排出 human 回放，本次回 `SESSION_BUSY`（tick 還原期間回 `SESSION_INTERACTIVE_BUSY`），不再撤銷剛排出的回放後立即 soft preempt；ATTACHED 救援只在確定開 lease 時撤銷回放授權；tick 還原結算前提交的 interactive 模式命令以 `SESSION_INTERACTIVE_BUSY` 結束；ConsoleHub 每次掃描時 core 依 Python `reap_stale_consoles` 回收無外部持有者、逾 2 s 的非 owner PTY console（primary 轉給下一個）。
