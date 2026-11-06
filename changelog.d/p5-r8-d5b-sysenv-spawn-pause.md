---
type: fix
scope: rust-sysenv
---

新增僅限 POSIX `test-fakes` 的 sysenv fork→exec 暫停點與回歸，讓測試可有界確認子行程進入 pre-exec、等待明確放行，並驗證呼叫與子行程正常收尾。
