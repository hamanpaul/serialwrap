---
type: fix
scope: rust-s21
---

event_latency 的 drain_receipt 改依擁有者裁決 OD-64 判定：排空回條等待同 epoch 的依賴事件時，只允許先處理「依賴等待前已讀入 mailbox 的同 epoch data」再加 1 筆（排空順序維持與 Python 相同）。測試用延遲探針另記錄依賴等待前已讀入的筆數，新增 release／shutdown 依賴情境的 L1 gate，既有 flood gate 每個事件改為重複 20 輪並補上探針自測；契約補記 OD-65 的 Python 基線數字與 stop_request 外部量測範圍，附入第 2 次獨立審閱紀錄（凍結狀態待編排者核對條件後才改）。
