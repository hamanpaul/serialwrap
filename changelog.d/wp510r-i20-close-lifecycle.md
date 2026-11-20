---
type: fix
scope: rust-core
---

修正 actor 建構失敗與回收者的 UART 關閉責任：鎖內只轉移所有權，關閉交獨立工作者；shutdown 關閉 parked handle 仍遵守排空期限。共用帳本終態保留原關閉錯誤，Held 與未接手 IoWorker 關閉失敗各輸出一次包含 by-id 與錯誤文字的告警。
