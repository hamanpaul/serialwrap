---
type: change
scope: rust-s21
---
Slice 2.1 出口 gate 的 `event_latency` 改依 OD-45：控制事件不被資料擠掉以 L1 結構測試為 gate（五事件皆保留 `at_most_one_data_item_before_control`），外部只量 stop_request、command_cancel、release_request（RPC 送出 → RPC 回應；idle 與 flood 20000 B/s），新增 `scripts/rust/event_latency_measure.py`；`contracts/resource_limits.yaml` `event_latency` 改 method_revision `2026-10-29.1`，上限值待獨立審閱重新凍結。不在 daemon 加計時欄位、不改 WAL。
