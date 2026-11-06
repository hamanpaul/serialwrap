---
type: fix
scope: interactive
---

修正 interactive 命令 TX 完成後、執行器返回前取消時，連帶撤銷存活 lease，導致後續 interactive send 持續回 SESSION_BUSY 的競態（OD-77 B1，登記 DF-23）；取消只撤銷原命令的 op，提交前取消仍維持零 TX。同步補 `session.bind` 佔位早退排在 FLASHING_BUSY 之前的契約說明（OD-77 B5，不改行為）。
