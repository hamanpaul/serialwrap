---
type: fix
scope: rust-core
---
依據 S2-PAR-wp510r 第 3 項編排者指示修正送達者（Deliverer）與凍結閘門（AdmissionGate）凍結累計時間記帳機制：
- P8-03eb4d3-1 [Medium]：`AdmissionGate` 在鎖內於實際轉態（Open→Pending、Pending→Open、Pending→Permanent）時記帳，提供可注入時間來源的 `with_clock` / `with_time_source` 與單調不遞減的 `frozen_total(now)`；
- 送達義務建立時記下 `created_at` 與 `frozen_at_create`，有效 Open 時間精確計算為 `(now - created_at) - (gate.frozen_total(now) - frozen_at_create)`，杜絕因輪詢時間推算誤差導致提早判定逾時丟棄；
- 補齊審查四步重現回歸、多次凍結解凍交替、Pending 期間建立義務、凍結轉永久取消義務與純超時丟棄回歸測試。
