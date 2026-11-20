---
type: fix
scope: rust-process-tunnel
---

接上僅限 POSIX `test-fakes` 的 process_tunnel pre-exec 暫停協定：parent 在 fork 前取得一次性設定，子行程在 setsid 成功後、額外 fd 清理前回報 PID 並有界等待 release。沿用並行 native attach 回歸，確認仍持有非 CLOEXEC PTY slave 的自身子行程不被誤判為外部 holder，且 attach 在 release 前完成；保留 fd 不繼承、stdio／setsid、spawn 錯誤與整群組終止的既有斷言。
