---
type: feat
scope: stage3
---
新增 A/B 判定 `tools/stage3/ab_verdict.py`，依判定表把 L1.5／L2／L3 的 A、B 狀態與差分合成為單一 verdict 與 `HARNESS_*` 原因碼。L2 逐案比較 TestPilot 報告（完整性先行，哨兵豁免須未到期且含 signature，帶安全不變量的案例不得豁免；B 側 XPASS 與 XFAIL 兩側不同者列入審查）；L3 只讀兩側 `release-gate.json`，不重算門檻、不改寫 `engine_result`；已登記變更由 P0 `behavior_changes.yaml` 載入，並核對 `scope` 與 `ab_participation` 一致。
