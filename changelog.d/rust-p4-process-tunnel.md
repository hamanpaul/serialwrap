---
type: feat
scope: sw-pal
issue: null
---
實作 POSIX 與 Windows 的分離行程通道、行程身分核對及終止；補齊 PAL 檔案原子寫入、目錄複製與刪除、symlink、跨行程鎖等寫入操作。POSIX 終止行程群組於 SIGKILL＋回收後即視為完成（同 Python `_terminate_pgid`），不再因 PID 1 不回收的 zombie 把已終止的隧道標為 error。
