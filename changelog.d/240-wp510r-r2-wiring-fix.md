---
type: fix
scope: rust-core
---
依據 S2-PAR-wp510r R2 astra 審查意見修復真實資源遷移、回收者與送達者執行緒接線及正式路徑測試（P8-bb1a899-1、P8-bb1a899-3）：
- 真實資源全面遷入保管帳本（C2）：AttachOp 不再持有 preopened，改由 CustodyLedger 之 Held 狀態持有 HandleParts；Bridged 狀態持有 IoWorker。
- 接通 AdoptBridge 認領路徑（W3→W3'）：AdoptBridge 自帳本取出真實 IoWorker 並移除 Bridged 紀錄；過渡期 manager release/bind 路徑改由帳本 claim_for_release 提取資源。
- 啟動回收者與送達者背景執行緒（C4、C8）：SessionManager 啟動 ReaperThread 與 DelivererThread，並透過 Shared 實作 ReaperCallback 與 DelivererCallback 完成非同步回條結算與送達義務重試。
- 補齊經由正式 manager/actor 路徑之確定性回歸測試：同型事件連續 panic 自動觸發 actor 停止並關閉資源結算帳本、DR-7 首次 DeviceOnline Full 送達失敗之背景送達者重試閉環、DR-1(f1)～(f5) 真實 IoWorker 經回收者排空結算。
