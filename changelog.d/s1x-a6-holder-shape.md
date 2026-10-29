---
type: fix
scope: diagnostics
---

修正一般使用者執行的 daemon 每次開埠都因讀不到他 uid 行程的 `/proc/<pid>/fd` 而寫 WARNING、`daemon status`／`doctor` 的 `foreign_holders` 全面變成 `{dev: {"device":…,"holder_unknown":true}}` 的回歸：掃描不完整只因他 uid 行程不可讀時記為 `partial_other_uid`、不升為 holder_unknown，`foreign_holders` 回到 0.3.2 的 `{dev: pid}` 形狀；同 uid 行程不可讀仍為 holder_unknown（BC-04）。實機 case `f8-tty-holders-reported` 與 `p0-multiopen` 改為同時接受 dict entry，不再拋 TypeError 或假 FAIL（S1X-a-6）。
