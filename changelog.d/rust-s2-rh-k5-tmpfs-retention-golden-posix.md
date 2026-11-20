---
type: fix
scope: rust-s2-rh-k5
---
CI 的 Stage 2 黑箱步驟以 `tmp_path_retention_policy=failed` 讓通過的測試即時清掉暫存，避免 tmpfs（8G）被各測試複製的 bundle 塞滿；p7 golden 新鮮度測試改為 `posix_only`（重現的是 Linux Python 基線，Windows 的 Python 走不同平台分支）。
