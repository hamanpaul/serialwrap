---
type: fix
scope: rust-refactor
---

修正 Spike 3 在 Windows 使用粗解析度 `monotonic_ns()` 量測 append 的問題，改用 `perf_counter_ns()` 並將實際時鐘與解析度寫入證據；helper 到期釋放改以相同 QPC 截止值判定。oplock ACK 記錄 CloseHandle 呼叫區間，gate 對齊 helper 原始 break 記錄及延遲樣本。
