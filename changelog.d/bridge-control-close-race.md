---
type: fix
scope: bridge
---

- 修正 `uart_stdio_bridge` 收尾競態：`shutdown` action 觸發結束後，主執行緒 `_finish()` 可能在 control 執行緒寫 `completed` 前就關閉 `control.ndjson`，造成 `HARNESS_EVIDENCE_WRITE_FAILED`（`control.ndjson 未開啟`）與 return code 3。`_finish()` 現在關檔前會有上限等待進行中的 control action 寫完；`_finish()` 後才到的 action 直接回 `{"ok": false}`，不寫證據、不影響 return code；等待上限 7 秒（另加關 control 檔取鎖上限 1 秒，總和須小於黑箱 SIGTERM 窗 10 秒；取不到鎖同樣判失敗並仍寫 manifest）；逾時於 wait() 返回前即判 HARNESS_EVIDENCE_WRITE_FAILED（rc 3、manifest 非乾淨關閉）。另修 control 檔關閉與寫入並行的 ValueError 逃逸，及收尾期間 pause_pump/resume_pump 回報假狀態。

- `uart_stdio_bridge` 核准 tree 更新為 `fa3c2b4b…`（審查欄位待編排者更新）
