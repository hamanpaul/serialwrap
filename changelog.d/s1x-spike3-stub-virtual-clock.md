---
type: fix
scope: rust-refactor
---

Spike 3「主／oplock 分開記錄」案例的替身改以虛擬 `perf_counter_ns` 推進 lease、break 與 rename 等待，判定不再依賴真實排程；失敗訊息另列 `lease_acquired_ns` 與 append 延遲樣本。
