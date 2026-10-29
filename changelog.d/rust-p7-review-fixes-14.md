---
type: change
scope: rust-p7
---
Slice 2.3 第七輪（擁有者裁決 OD-59 與編排者裁決落實）：新增黑箱 `test_control_latency_under_flood_with_events`，以 `event_latency` 的 flood 規則（stop_request／command_cancel 每輪最大值 ≤ 100 ms）在 RX 洪水＋全行比對規則下判定控制延遲，並確認 `event_dropped` 有界出現；l15 `s23-interleave-login-agent-human` 另以 WAL 先後判定 nonce probe 到 READY 之間沒有 agent／human 的寫入；Slice 2.3 出口 gate 的案例涵蓋改為先前切片 L2 回歸依 B-09～B-16 記擁有者待辦、2.1／2.2 案例匯入對應切片 gate 的逐案證據，Windows COM 列依 OD-53 以能力集合（`--ignored cap_windows_com__`）執行。匯入的先前切片證據須與本次 gate 的 commit 與受測 daemon 相符才採用。
