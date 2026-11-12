> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-ea9e03d-1 | High | 已修 `2a9a9bc4` | 0 | actor 套用 bind 時產生唯一世代，透過 `PublishBinding` 在回覆前同步 manager、撤銷舊義務；RPC 逾時不再阻止同步。正式 RPC 回歸確認晚到完成後，再次 bind 與後續拔插皆能重新開埠並到達 READY。 |

## B 新發現

NO FINDINGS。

已逐項審查 `ea9e03d2..2a9a9bc4` 唯一提交，核對立即 bind、排空完成、BindWait 與 SetProfile 的發布接線；過期通知檢查保留，L1 鎖僅新增兩個測試節點，未削弱既有斷言。

定向測試 **21 passed、0 failed**，涵蓋本項七個回歸及相關重綁、保管交接與 profile 重建。建置使用指定 `/tmp` 目錄；工作樹維持乾淨，未執行完整 pytest、Docker／bundle 或接觸正式 daemon、設定、狀態與實體 UART。

## 既有碼待辦（不計入結論）

逾時後**未重試 bind、直接拔插**的持久化缺口仍存在：`Unreleased`／`Binding` 提交位於 RPC 等待的 `?` 之後，逾時會略過，舊 RELEASED 紀錄仍可阻擋 attach。已核對起點與本輪均有此控制流程；本輪未另行實測此情境，不將其視為世代修正已涵蓋的成果。位置：[session_manager.rs:6609](crates/sw-core/src/session_manager.rs:6609)。

## 對裁決的異議（不計入結論）

無。

結論：可放行