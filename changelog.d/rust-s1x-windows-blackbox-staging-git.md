---
type: fix
scope: test-infra
---

Windows Stage 1 黑箱 runner 改由 WSL 端輸出 exact HEAD 的 Git bundle，再複製至本機 staging 載入歷史；避免 Windows Git 無法沿 UNC 路徑解析 WSL worktree 的 `.git` common-dir。
