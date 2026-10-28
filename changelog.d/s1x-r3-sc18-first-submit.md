---
type: fix
scope: rust-refactor
---

SC-18 原生案例的首次 `cmd submit` 接受原驗收形狀（直接成功）或零 TX 的 `SESSION_NOT_READY` 拒絕，其他錯誤一律 FAIL；passthrough READY 等待上限改為 120 秒。
