---
type: fix
scope: contracts
---

- Slice 2.4 覆蓋 plugin 改以具名 plugin 由根 conftest 註冊，記錄 `tests/contract_blackbox` 以外測項的各 phase 結果；受測 artifact 改取根 conftest 清除前的 `ab_env` 快照並與實際受測入口交叉核對；載入時清除覆蓋環境變數，避免巢狀 pytest 把結果併入外層 run。
