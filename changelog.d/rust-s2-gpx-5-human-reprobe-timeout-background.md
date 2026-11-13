---
type: fix
scope: rust-s2
---

- 對齊 Python 的自動重探 human 活躍判定；超大 timeout 轉為飽和 Duration，避免 OpRunner 與登入流程 panic；background reboot 不建立 capture，`command.result_tail` 維持 Python 的執行中 fallback。
