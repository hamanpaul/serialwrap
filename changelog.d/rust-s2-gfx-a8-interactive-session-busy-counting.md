---
type: fix
scope: realhw-longrun
---
依 OD-103 將 interactive 模式的 `SESSION_BUSY` 納入長跑預期拒絕；line 與 background 模式仍計為錯誤，並保留 `SESSION_INTERACTIVE_BUSY` 的既有計數規則。
