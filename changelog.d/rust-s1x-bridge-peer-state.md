---
type: fix
scope: bridge-tool
---

依 COM20:COM21 原生實測（`g3-peer-gate/261023-061416-2646662`），win-serial 端點於 com0com 且 `peer_gate=True` 時宣告 `peer_state`，對外 `status.peer_open` 為 DSR 即時取樣；非 gate 情境仍為 null。`transport_disconnect` 恆為 False（com0com 無不需管理員權限的斷線原語）。測試名稱同步改為描述新行為（`test_win_serial_status_reports_peer_open_when_peer_gated`、`test_peer_gate_declares_peer_state_capability`），並移除「函式名稱保留以維持 nodeid」之註解（該名稱未見於任何凍結鎖或腳本）。工具 tree 已變動，核准狀態見 `CHG-S1X-BRIDGE-PEER-STATE-01`（未核准）。
