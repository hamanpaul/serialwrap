# S1X Bridge 工具 revision 第三輪審查

審查記錄日：2026-10-27（Asia/Taipei）
候選工具樹：`76ec0480b0e675330fba56fa1a11e40eb782f2412f849ef7c52626637bf0dfd5`
審查者：Claude Sonnet、Codex gpt-6.1-sol；兩位均非實作者。

## 本輪驗證

- Linux bridge selftest 原生報告為 PASS；14 項中 13 項 PASS，ST-12 因 `transport_disconnect` 能力未宣告而為 NOT_RUN。
- Windows COM20:COM21 selftest 原生報告 verdict 為 PASS；外層 qualification exit 1，因核准 tree `c4c22c0e…` 與候選 tree `76ec0480…` 不同。ST-06／13／14 輸出的 `HARNESS_DUT_EXITED`、`HARNESS_WRITE_UNCERTAIN`、`HARNESS_EVIDENCE_OVERFLOW` 是故障注入觀測，不是 selftest 失敗原因。
- `python3 -m pytest -q tools/uart_stdio_bridge/tests`：299 passed、5 skipped、1 failed。唯一失敗為 `test_approved_revision_matches_tool_tree`，其斷言正確反映本輪尚未更新核准值。
- Windows COM run：`target/win-blackbox/261026-232001-1616734/windows-com/selftest-result.json`。Linux 與 Windows raw selftest 均未被外層資格失敗改寫。

## Claude Sonnet 審查摘要

- R1（Medium）：`read(..., 0.05)` 的實際路徑以 `in_waiting` 輪詢，不進 pySerial 阻塞讀；建議補一個時鐘前進的讀取測試並在 changelog 說明。已在本輪 changelog 補充 50 ms 呼叫與 10 ms 輪詢行為。
- R2（Medium-Low）：1 ms setter slack 與跨量化桶的 churn 缺少邊界測試；建議於後續 src 修改時一併處理。
- R3（Low）：仍有少數測試使用實時視窗；未作阻擋性判定。
- R4（Low）：凍結時鐘搭配真實 sleep 的測試在失敗時可能無法退出；未作阻擋性判定。
- R5（Low）：factory timeout、非有限 timeout、DSR 讀取例外等測試覆蓋缺口；`+inf` 溢位屬既有行為，review 未判為本輪 regression。
- R6（Info）：`peer_open()` lock-free 與 close/reconnect 競態可能帶來瞬時 peer-state 樣本；為未實測推論。

Claude 原始 BRIDGE 段：

> BRIDGE: APPROVE — tree 76ec0480b0e675330fba56fa1a11e40eb782f2412f849ef7c52626637bf0dfd5 重算相符，上一輪三項修訂成立，未發現阻擋性缺陷；R1/R2 建議併入下次 src 修改、R3–R5 為不影響 hash 的 test-only 跟進，原生 Windows 重跑與重新 pin 仍為前置

## Codex gpt-6.1-sol 審查摘要

獨立靜態核對確認受控時鐘一致、setter 期間 DSR 降低會禁止寫入，窄期限案例保留剩餘預算與禁止阻塞讀取的斷言；未新增發現。

Codex 原始 BRIDGE 段：

> 無新增發現。靜態核對證據：
>
> - `test_win_serial_endpoint.py:1275`：`_peer_on_since` 與期限使用同一受控時鐘。
> - `:690`：setter 期間 DSR 掉低，明確斷言寫入回 0、payload 未送出。
> - `:1209、:1636`：窄期限改由受控時鐘驗證，保留剩餘預算與禁止阻塞讀取的斷言。
>
> 工具樹：`76ec0480b0e675330fba56fa1a11e40eb782f2412f849ef7c52626637bf0dfd5`。
>
> BRIDGE: APPROVE

## 裁定狀態

兩位非實作者均對候選 tree 明確寫下 `BRIDGE: APPROVE`。本輪不改 `tools/uart_stdio_bridge/APPROVED_REVISION.json`；依本輪指示，下一輪才以此文件作為 `review_ref` 更新 tree hash、reviewer 與日期並另行提交。核准檔更新前，Windows qualification、bridge revision 測試及依賴該資格的 Docker smoke 仍須按原判定保持 FAIL／INSUFFICIENT。
