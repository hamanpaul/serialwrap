---
type: feat
scope: stage3
---
新增 Sequential A/B manifest 工具 `tools/stage3/ab_manifest.py`：載入並驗證凍結 manifest（schema_version、stage、layer↔adapter 對應、兩側 A/B artifact、DUT、破壞性案例授權與 Windows 測試裝置白名單），重算凍結輸入、bundle 元件與 runtime tree 摘要，並依 `tests/acceptance/cases.yaml`／`cases.lock.json` 檢查本次 run 的案例涵蓋性；argv 範本只接受已登記的佔位鍵。
