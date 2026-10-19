---
type: change
scope: daemon
---

依 OD-14 #3 撤回 P1 的 SC-08 權限變更：POSIX Unix socket 維持 `0660`，可用 `SERIALWRAP_SOCKET_GROUP` 指定群組；新建執行期目錄沿用 process umask。
