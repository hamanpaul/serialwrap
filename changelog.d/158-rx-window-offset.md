---
type: fix
scope: uart_io
---
修復 RX 視窗有界修剪破壞 offset 語意（#158）：`UARTBridge` 的 `_rx_text` 觸頂（131072 字元）修剪前端但不記帳，使 `rx_snapshot_len()` 飽和後恆等於視窗上限、`wait_for_regex_from()`／`rx_text_from()` 以舊 offset 切片永遠空字串——prompt 永不匹配 → 快速命令迴圈於百次級必中 PROMPT_TIMEOUT（stdout 空），且 recovery 同因匹配不到 CTRL_C 拿回的 prompt 而續送 CTRL_D 誤登出 console。改為**絕對串流偏移**記帳：新增 `_rx_trimmed`（修剪／clear 累計丟棄量，單調不減），`rx_snapshot_len()` 回傳絕對偏移、切片 API 每次持鎖重算相對偏移，頭段已修剪時降級回傳現存全窗；`clear_rx_buffer()` 亦推進基準保持單調（同時修復飽和下 RX 靜默偵測恆真的隱性誤判）。`bridge.snapshot()` 新增 `rx_dropped_chars` 欄位供鑑識。回歸 plugin：`f2-history-bounded-rss` 收緊為零容忍（移除 ≤3 次 PROMPT_TIMEOUT 容忍，恢復即紅），新增決定性重演 case `f2-rx-window-crossing-prompt`（awk 大輸出推飽和 RX 視窗後連發短命令，驗證 prompt 跨界不失效）。
