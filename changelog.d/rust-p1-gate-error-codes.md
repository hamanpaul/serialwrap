---
type: fix
scope: session
---

修正 RECOVERING 與 FLASHING 狀態下送命令及開啟 interactive 時的錯誤碼；已排隊命令與檔案傳輸遇到 autoboot quiet 時附上 `retry_after_s`；已取消但 worker 仍執行中的命令計入 pending 上限；RPC 偽造的 `human:` 來源不再繞過 gate 或升格為 interactive。
