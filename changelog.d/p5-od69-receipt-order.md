---
type: changed
---

- 補齊 OD-69 receipt 排序整體驗收：release、shutdown 與重掛後舊 epoch 隔離各 20 輪，檢查 receipt 前 RX capture／WAL、background capture、無依賴 N_pre＝0 與證據缺口；保留 OD-64 依賴 gate 與 flood 負載斷言並輸出逐輪證據。沿用既有產品 barrier，未修改 event_latency 凍結契約。
