---
type: fix
scope: sw-pal
---
修正 Windows 委派 Job 的終止傳遞：`exec_or_wait` 的 Job 不再設定 `SILENT_BREAKAWAY_OK`，委派父行程被終止時，一般方式建立的孫行程不再靜默脫離 Job 而殘留；正常結束時改於關閉 Job 前解除 `KILL_ON_JOB_CLOSE`，維持 `daemon start` 類背景行程存活。
