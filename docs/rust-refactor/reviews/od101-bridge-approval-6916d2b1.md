# OD-101 獨立審查：UART bridge revision 核准摘要

## 核准記錄

- 審查性質：OD-101 獨立審查；審查者非實作者。原審查報告未列出個別審查者姓名，核准欄以審查報告及其獨立性標示留存。
- 審查對象：commit `6916d2b13d972de5d13762669b7e2d1fdd862ff3`（`fix(bridge): 修正 ST-05 EOF 自測同步`）。
- 結論：核准此 bridge 工具樹 revision。
- `tool_tree_sha256()`：`1bac178b0708643bc22710692f340245827ec8b7e96efdecdf17995f843f4e49`。

## 審查差異與判定

ST-05 原先以固定 `sleep(0.2)` 作為 DUT 關閉 stdout 後的同步等待。核准差異改為有界輪詢 `wire.ndjson`，等待 `dut_stdout_eof` META 出現後才送出第二段 TX；最多等待 5 秒，逾時明確回報 FAIL。原有 stdout EOF META 驗證及 `tx_total >= 1536` 檢查保留，沒有以重試覆蓋失敗，也沒有放寬自測資格判準。

新增的 ST-05 回歸測試涵蓋 META 有出現及未出現兩種情況，並驗證 EOF META 必須先於第二段 TX。依 OD-101 審查紀錄，`emit_eof=True` 案例在舊固定等待行為下會失敗；等待不到 EOF 的路徑仍判 FAIL。

## 審查實跑與限制

OD-101 報告記錄審查前執行 `tools/uart_stdio_bridge/tests/test_selftest.py` 為 11 passed、1 failed；唯一失敗是 `test_approved_revision_matches_tool_tree`，當時核准檔仍綁舊工具樹，屬待本次審查核准後更新的預期紅燈。本輪先實測該測試在更新前因雜湊不符而失敗；更新後執行 bridge 測試套件（排除 Docker 標記項，以遵守本輪不執行 Docker 的限制）為 327 passed、5 skipped、4 deselected，核准雜湊測試包含在通過項目中。

Windows `win-serial` 路徑在該次審查中只有 mock 驗證，沒有真實 COM 實跑。此核准只對上述工具樹雜湊及已審查差異有效，不代表 Windows 實體序列埠驗證、slice gate 通過或部署完成。

## 審查來源

OD-101 獨立審查報告（證據識別 `OD101-REVIEW`），段落「bridge 差異核准結論」；原始審查檔存於編排者稽核資料，不在 repo 文件中記錄本機路徑。
