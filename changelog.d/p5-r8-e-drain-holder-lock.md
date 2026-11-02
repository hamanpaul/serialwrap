---
type: fix
scope: rust-s21
---

對齊 OD-69：release／shutdown 回條送出前，先排空讀取器已入列的同 epoch RX，並在回條前完成 capture 與 WAL；逾時仍送回條並記錄證據缺口。holder 重掃改排除 daemon 自身行程樹，避免 fork→exec 窗口內繼承 tty 的子行程被判為外部 holder。新增 `s21_gate:py_parity_76_80` 實際收集集合，修正凍結鎖工具對冒號集合名稱與全平台移除的處理，依 OD-68 移除核准的 Linux 列，並將 event_latency 契約改為 frozen。
