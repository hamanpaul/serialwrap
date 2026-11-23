> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-585d03b-1 | High | 已修 731e745e | 1 | 仍要求窗口終點前至少四筆樣本。 |
| V051-731e745-1 | Medium | 已修 62ebe61e | 1 | 實際樣本與邊界反例共用判準。 |
| V051-c10df19-1 | Medium | 已修 62ebe61e | 1 | 窗口外額外樣本不會否決合格結果。 |
| V051-1a3a50e-1 | High | 依裁決不處理 | 1 | 補登裁決核准 Python 的兩種終態。 |
| V051-7f73256-1 | High | 已修 4ff31b47 | 2 | 已依裁決加入逐次 holder 檢查；本輪另發現權限錯誤處理問題，見 B。 |
| V051-7f73256-2 | High | 已修 08c513c9 | 0 | 首次 `device attach` 仍須成功。 |

## B 新發現

**V051-4ff31b4-1 [Medium]** [test_p7_invariants.py:73](tests/contract_blackbox/test_p7_invariants.py:73) — holder 掃描忽略所有 `readlink` 錯誤，可能把無法檢查的 fd 當成沒有 holder。重現情境：替身 daemon 已開啟目標 PTY，掃描列出 fd 後、讀取連結前行程變為不可供 `/proc` 檢查；真實 `readlink` 回 `EACCES`，函式卻回傳空清單，觀察斷言因此通過。這次以真實 `/proc` 權限切換和受控交錯重現。

修法：僅對確認為 fd 在掃描期間消失的情況處理 `ENOENT`；對 `EACCES` 等無法判定的錯誤直接使測試失敗，並補受控交錯測試，確認不可讀的 holder 不會被判為空清單。

## C 完整性核對

- Python 終態及重試欄位：已核對，仍限制為裁決核准的兩種 `DETACHED` 組合。
- 沒有新的 attach 嘗試：已依裁決加入 holder 取樣；替身 daemon 在觀察期重開 PTY 時，原測試分支確實轉紅。但掃描遇權限錯誤時仍有 B 所列缺陷。
- 首次 attach 與 Rust／handle 斷言：已核對；首次 attach 須成功，本輪 diff 未改 Rust 分支或 handle 時序斷言。
- 本次未取得 `brief-ci-thin.md` 全文，無法確認上述三項之外是否還有需逐項核對的工作。

定向驗證使用獨立 PTY、替身行程及記憶體中的受控交錯；未執行完整 pytest、Docker 或 bundle 測試。工作樹保持乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

逐次 holder 取樣不能涵蓋開埠呼叫尚未建立 fd 的情境：例如替身在呼叫實際 `open` 前阻塞時，狀態欄位不變且 `/proc/<pid>/fd` 沒有目標 tty。若驗收語意要求證明「沒有任何新的開埠嘗試開始」，仍需可持續記錄嘗試開始的證據；目前實作符合裁決指定的 holder 做法。

結論：需修正