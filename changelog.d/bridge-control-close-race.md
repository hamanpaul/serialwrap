---
type: fix
scope: bridge
---

- 修正 `uart_stdio_bridge` 收尾競態：`shutdown` action 觸發結束後，主執行緒 `_finish()` 可能在 control 執行緒寫 `completed` 前就關閉 `control.ndjson`，造成 `HARNESS_EVIDENCE_WRITE_FAILED`（`control.ndjson 未開啟`）與 return code 3。`_finish()` 現在關檔前會有上限（2 秒）等待進行中的 control action 寫完；`_finish()` 後才到的 action 直接回 `{"ok": false}`，不寫證據、不影響 return code；等待逾時仍維持 fail-closed。

- `uart_stdio_bridge` 核准 tree 更新為 `330e0754…`（審查欄位待編排者更新）
