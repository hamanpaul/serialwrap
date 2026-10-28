---
type: fix
scope: stage0
---

Spike 3 將 go/ack 綁定至實際 lease acquisition 與期限，拒絕過期 acknowledgement；producer 與 Stage 1A gate 同時檢查主要 WAL workload 和 oplock 變體的 append 延遲樣本，依 OD-22 驗證 p99 不高於 100 ms、最大值不高於 600 ms。
