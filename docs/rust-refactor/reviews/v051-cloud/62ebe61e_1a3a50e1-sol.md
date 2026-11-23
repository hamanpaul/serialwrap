> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-585d03b-1 | High | 已修 731e745e | 1 | 目前仍要求窗口終點前至少四筆樣本。 |
| V051-731e745-1 | Medium | 已修 62ebe61e | 1 | 實際樣本與邊界反例共用同一判準。 |
| V051-c10df19-1 | Medium | 已修 62ebe61e | 1 | 計算窗口內樣本數；終點或之後的額外樣本不會否決合格結果。 |

## B 新發現

- **V051-1a3a50e-1 [High]** [test_p7_invariants.py:217](tests/contract_blackbox/test_p7_invariants.py:217) — Python 分支新增接受 `LOGIN_PROMPT_TIMEOUT`，放寬了既有 DF-23 oracle。具體而言，輸入 `DETACHED`、`last_error=LOGIN_PROMPT_TIMEOUT` 且重試欄位不變時，新判準通過，前一版斷言則失敗；DF-23 登錄的 Python 結果是 `ATTACH_FAILED:TxIncompleteError`。測試註解聲稱有編排者裁決，但所提供的裁決節錄及本機 `origin/docs/parity-amendments` 的 `rulings/v051.md` 均未找到該裁決，無法確認放寬已有授權。**修法：**先恢復原本錯誤判準並保留新增的不重試檢查；若確有授權接受第二種終態，應補上可追溯的裁決及對應契約依據後，再依核准範圍調整 oracle。

定向驗證以記憶體執行 oracle：確認上述新舊判準差異，且四個重試欄位發生變化時會被拒絕。未執行完整 pytest、Docker 或 bundle 測試；工作樹無變更。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正