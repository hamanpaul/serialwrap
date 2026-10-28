---
type: fix
scope: daemon
issue: null
---

命令回顯剝除支援同一行中多次 readline 自動折行，並只在第一個換行前驗證及移除回顯標記，避免命令內容殘留在有效標準輸出中。
