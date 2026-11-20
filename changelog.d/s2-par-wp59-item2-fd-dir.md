---
type: fix
scope: sw-pal
---
holder probe 遇到 fd 目錄列舉權限拒絕時，僅在確認行程屬於他 uid 才回報 `partial_other_uid`；同 uid／owner 未知維持 `permission`，其他 I/O 錯誤維持 `unknown`，health 掃描仍回 `permission`。
