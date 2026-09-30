---
type: fix
scope: regression-plugin
issue: 224
---

修正 #224 TestPilot 實機 case 的 WAL 分頁判定，並讓 Windows TestPilot 可在既有 serialwrap broker 上執行單一 BRCM 重開機登入回歸 case；保留版本、doctor、WAL、READY 與 benchlock 前置檢查，其餘 POSIX 專用 case 在 Windows 明確略過。
