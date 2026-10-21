---
type: feat
scope: stage3
---
新增 Python daemon 退役核准與發佈守門：Stage 3 bundle 使用無核准欄位的靜態退役標記；發佈前核對 Stage 3 證據、manifest、component 與完整檔案樹，封裝後再驗 archive 位元組，只允許上傳已驗證的受測 bundle。
