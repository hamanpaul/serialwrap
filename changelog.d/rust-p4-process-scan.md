---
type: feat
scope: sw-pal
issue: null
---
新增 POSIX daemon 背景啟動、非阻塞輪詢與前景執行；Windows 以 `DETACHED_PROCESS` 啟動 daemon，並保留完整 DWORD 結束碼。加入保守處理不可讀 `/proc` 狀態的 daemon 與 zombie／PID 世代掃描；以 Toolhelp32 掃描 Windows daemon，合併 launcher 與其 Python 或 launcher 子行程。
