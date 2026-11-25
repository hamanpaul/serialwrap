---
type: change
scope: stage3
---

新增 Stage 3 共用測試替身，提供穩定時間、WAL、bundle、A/B manifest、stub CLI、假時鐘／探針與命令記錄 runner。OD-113 增量加入可重現的 Linux tar.gz／Windows zip RC archive，以及隔離假 repo 的 current 與 historical-deferred 接受資料；R4-01 增量以 T1 逐出口完整分母鎖、seed 附件與同一 SHA 建立 manifest 和候選 context。補上各階段案例分母與 A/B 身分、跨平台共用輪次、依 release manifest 只涵蓋 `sw_core/` 的 Python package/delegate tree digest 與 CLI SHA 覆寫；runner 依明定程式分類和值旗標清單產生精確命令鍵，archive 另以兩平台成員、內容及 metadata 測試固定佈局。R4-01 修正 bundle 摘要，使 profiles 以來源目錄樹計算、wheel 無來源時為 `null`，並以實際 bundle 資產檔案重算 skills／supervisor／package／delegate 欄位。
