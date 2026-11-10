---
type: fix
scope: rust-core
---
依據 S2-PAR-wp510r 第 3 項審查反饋（P8-6ebd0d2-1）修正凍結時間區間截斷與一致快照機制：
- P8-6ebd0d2-1 [Medium]：`AdmissionGate` 改為記錄歷史凍結區間（`frozen_intervals`），使 `frozen_total(now)` 精確截斷至指定取樣時刻，避免輪詢時間取樣後並發之凍結／解凍區間被誤扣除導致逾時送達義務未被丟棄；
- 提供鎖內一致時間與狀態快照 `GateSnapshot`（含 `snapshot()` 與 `snapshot_at(now)`），`Deliverer` 於建立義務與單步送達時一致使用快照，杜絕取樣時差造成的計時偏差；
- 在 `crates/sw-core/tests/custody.rs` 補齊時間取樣後凍結解凍交錯回歸測試，並在 `crates/sw-core/tests/admission.rs` 驗證多區間精準截斷特性。
