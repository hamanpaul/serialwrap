---
type: fix
scope: rust-s2-rh
---

Docker 升級驗證改以 `/proc/<pid>/exe` 的實際執行檔路徑確認 Stage 2 daemon 來自候選 bundle；權限不足時以 `swtest` 身分重讀，並將 argv 結尾 NUL 正確解析。
