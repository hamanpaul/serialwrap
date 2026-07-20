---
type: feat
scope: remote
---
`serialwrap remote`：按需開關 SSH 反向隧道（`-R` expose 預設／`-L` connect），讓遠端 agent 取得本機 serialwrap 操作；background 常駐、flock registry、readiness 確認、`--remote-socket` 硬化、fail-closed 遠端 bind 驗證。daemon 零改動、不做預設、僅 POSIX（native Windows 回 `REMOTE_NOT_SUPPORTED`）。
