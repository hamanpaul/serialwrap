---
type: feat
scope: stage3
---
新增 L1.5 A/B 差分工具 `tools/stage3/ab_diff.py`，只讀兩側 func-test 報告目錄，依 `contracts/ab_normalization.yaml` 的具名規則（G1 EXCEPTION message、G2 集合順序、volatile 欄位、實例根目錄與 PTY 路徑佔位、輪詢呼叫收斂）正規化後比對 CLI 的 argv、rc 與回應，輸出完整性、安全不變量與差異（含已登記 SC 變更歸屬）。兩側目錄相同或互為祖先時拒絕比較。
