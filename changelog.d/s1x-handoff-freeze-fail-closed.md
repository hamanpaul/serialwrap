---
type: fix
scope: handoff
---

### Fixed

- 修正 Python 基線交接凍結（SC-11 `daemon.handoff_stop`）的 fail-closed 缺口，違反 OD-16 窗口零 TX：TX fence 於期限內未收斂時，`freeze_tx` 回傳未收斂結果，交接回 `SESSION_BUSY`＋`busy`（不再回 `ok:true`），並撤銷本次新建立的 admission 與背景凍結（不凍結、不停止）；probe 於鎖外執行期間完成交接時，結果不得把 session 推進至 READY 或清除 `last_error`；reconcile tick 的 lease 到期、重探排程、console 回收與 lease 自癒區段於鎖內重新檢查凍結；attach 因凍結早退時還原預先標記的 `ATTACHING`。新增互斥交錯測試 `tests/test_handoff_freeze_races.py` 與 `tests/test_stage0_handoff_stop.py::test_handoff_refused_when_background_write_holds_tx_fence`。
