---
type: fix
scope: rust-core
---
S2-PAR mergep8x（P8-1533cb5-1）：session actor 在每個事件分派前判定交接凍結狀態時，改以不取鎖的鏡像讀取 TX fence 的 `handoff_frozen`（每次取 fence 鎖結束前於鎖內發布）。背景 TX 的 OS 提交持 fence 鎖期間，`daemon.handoff_stop` 的 session 查詢不再逾時成 `ops:["session.get_state"]`，恢復由有期限的 TX fence 收斂回 `SESSION_BUSY`＋`ops:["tx.in_flight"]`（Python `_handoff_frozen` 由 manager 鎖保護、不經 bridge TX fence，`origin/main:sw_core/session_manager.py:1146-1172`；`tests/test_stage0_handoff_stop.py:372`）。Pending／Permanent 判定與回滾語意不變。
