---
type: fix
scope: rust-s21
---
`event_latency` 量測方法依獨立審閱（Codex gpt-6.1-sol，rejected）修正為 method_revision `2026-10-29.2`：flood 實際負載須驗證（送出失敗、送出端中途停止、速率不足、持續時間不足即證據不足；stop_request 每樣本暖機 `stop_warmup_s`）、host 作廢改看量測期間每秒取樣的峰值且監測缺失判證據不足、有效樣本超限不再被錯誤降級、release 重新 attach 失敗保留已取得耗時；gate 建置前移除舊 artifact、建置失敗即 FAIL，且只在建置成功時以建置當下 sha256 綁定量測來源；判定器讀取 event_latency 證據並核對 artifact／commit／run／工具／契約身分。L1 延遲探針改為逐事件見證數（含 `stopped` 中的排空回條）。`release_request` 上限依 OD-58 改 idle 250／flood 300 ms（0.5.0 前須以同工具重測三輪）。
