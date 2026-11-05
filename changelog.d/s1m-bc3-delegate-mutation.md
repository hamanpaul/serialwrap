---
type: fix
scope: tests
---
委派測試新增子行程在 `exec_or_wait` 前停住的負向自測，與 Windows Ctrl 事件測試共用 ready 等待判準，避免將子行程存活誤認為孫行程已就緒。
