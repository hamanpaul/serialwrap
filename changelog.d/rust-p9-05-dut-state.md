---
type: feat
scope: stage3
---
新增 A/B 實例隔離與乾淨 DUT 狀態檢查：每側建立全新實例目錄（A、B 不共用），以 artifact 環境變數選定 CLI 與 daemon；L1.5 隔離 9 個 SERIALWRAP 目錄並複製凍結的 profile，L2／L3 於執行前核對 DUT 重置、指紋、板卡 READY 與 state.json 污染。
