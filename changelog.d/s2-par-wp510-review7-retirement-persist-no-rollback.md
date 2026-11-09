---
type: fix
scope: rust-refactor
---

- OD-63 佔位交接退休時 state.json 寫入失敗（例如 ENOSPC），Rust daemon 不再把 RELEASED 佔位、binding、aliases 還原回記憶體（對齊 Python：退休已在記憶體完成，`_save_state()` 例外只穿出 RPC）：之後查詢、bind、attach 舊佔位一律 `SESSION_NOT_FOUND`，下一次成功存檔寫入退休結果。已開的 UART handle 照 Python 留在作業內、不啟動 dynamic 偵測；之後的 bind 或 attach 重試沿用同一 handle，裝置拔除或 shutdown 時關閉。
