---
type: feat
scope: rust-refactor
issue: 241
---

Rust setup reconcile 新增可選的同模式 systemd daemon restart 與重啟後程式碼指紋輪詢；system scope 未帶 sudo 時將 restart 列入待執行指令。
