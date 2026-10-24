---
type: fix
scope: bridge-tool
---

修正 sol ca0106f（Medium）：win-serial 端點的期限設定原本在取得讀寫串行鎖之前完成，並行呼叫會互相覆寫 pySerial 的 `timeout`／`write_timeout`，可能使第一筆寫入提早逾時並觸發 `HARNESS_WRITE_UNCERTAIN`；且 setter 會在另一方向 I/O 進行中呼叫，pySerial 會重設整個 COM handle。修正後寫入期限設定於雙向靜止（寫入鎖與讀取鎖皆取得）時執行，鎖序固定為寫入 → 讀取，且期限設定與本次寫入共用寫入鎖。讀取期限固定為開啟時的 50 ms；`read()` 以短讀取輪詢至呼叫期限，不再等待寫入鎖來改設讀取期限，使阻塞寫入期間讀取仍能排空資料。已補交錯測試（以 fake serial 記錄實際期限、資料排空與 setter 違規），既有 com0com 與 peer gate 行為不變。工具 tree 已變動，核准狀態見 `PENDING-OD-S1X-BRIDGE-REVISION` 之 G9 段落（未核准；`APPROVED_REVISION.json` 本輪未變更）。
