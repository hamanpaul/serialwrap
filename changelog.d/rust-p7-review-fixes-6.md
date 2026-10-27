### Fixed

- Rust 重構 P7 審查第 6 輪：POSIX 事件 handler 不再經 glibc `execvp` 的 ENOEXEC 後備以 `/bin/sh` 執行無 shebang 檔（與 Python 同回 `[Errno 8] Exec format error`），子程序環境變數依規格順序；dispatcher 關機逾時後才登記的 handler 立即終止；Windows handler pipe 與其他 CreateProcessW 互斥。
