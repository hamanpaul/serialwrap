---
type: feat
scope: rust-core
---
依據定稿設計第 5 版與擁有者裁決（OD-63 佔位交接重寫），建立 S2-PAR-wp510r R1 保管帳本（CustodyLedger）基礎設施與相關協同機制：
- 建立 Custody 狀態機型別（Opening／Held／Taken／Bridged／BridgeDraining／Closing／CloseFailed）與狀態轉換 API（C1～C8、claim_parked 等）。
- 建立 terminals、tombstones、prepared、deliveries 結構與 DeliveryObligation 追蹤。
- 建立關閉工作者、回收者（ReaperThread）、送達者（Deliverer）協同架構，支援退避重試、取消與交接凍結分類。
- 實作 ExitReclaim 與 Reclaim<T> 守衛，Drop 時僅發送回收命令而不取 SessionManager 鎖。
- 在 IoWorker 與 wait_for_rx_dispatch 區分斷線與逾時缺口文字。
- 支援 CustodyPoll 本地計時器事件與啟動效果（startup_effects）管理。
- 增加 SessionManager 與 SessionCore 查詢輔助介面與單元測試。
