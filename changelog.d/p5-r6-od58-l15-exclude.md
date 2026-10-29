---
type: change
scope: rust-s21
---
OD-58：`s21-event-latency` 自 Slice 2.1 gate 的 `l15_ab` 判定逐名移出（情境檔保留），改由 `event_latency` section 承擔；gate 判定器的 `l15_ab_excluded` 新增 `covered_by_sections`，承擔的 section 非 PASS 時 `l15_ab` 不得 PASS。
