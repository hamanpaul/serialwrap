---
type: fix
scope: rust-refactor
---

Spike 3 現在記錄主案例 lease／刺激時點，以及 oplock 變體的實際 rename request、完成時間、append 樣本時間窗、RH break 通知與 close-handle 隱含 ACK。Stage 1A gate 以同一 lease request id 關聯這些事件，允許 ACK 後完成輪替，但要求 request 位於有效 lease、完成時間有界且與 append 樣本一致；主案例與 oplock append 樣本各自依 OD-22 檢查 p99 ≤100 ms、最大值 ≤600 ms。缺證據仍為 INSUFFICIENT_EVIDENCE，超限仍為 FAIL。
