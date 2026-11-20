---
type: fix
scope: sw-pal
---
holder probe 遇到他 uid 行程的 fd `stat` 權限拒絕時回報 `partial_other_uid`，不把空 PID 清單標成 `holder_unknown`；同 uid 或 UID 無法確認時仍回報 `permission`。POSIX 開埠接受此狀態且不發警告；health 掃描的對外狀態仍為 `permission`。
