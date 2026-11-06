# uart_stdio_bridge 工具 revision 審查：停滯期限修正

審查範圍：`0ea3cc9c..d6cf1c9c`（`fix(bridge): 依寫入進度判定證據停滯`）。兩位審查者皆非實作者，分別獨立進行，結論都是 `BRIDGE: APPROVE`，沒有 High 或 Medium。

核准 tree：`0021ba6703decf05cea53668060984dbdf1fa3dcee32a84cfa0d2c3ef4704c1c`。

編排者另外核對：測試檔 0 行刪除，`test_writer_close_stuck_worker_sets_failure_and_keeps_fd_open`、`test_writer_flush_timeout_sets_failure_and_returns` 未改；`tools/uart_stdio_bridge/tests/test_writer.py` 44 passed。

## 審查者 A：grok-4.7

我以獨立審查者身分只讀 `0ea3cc9c..d6cf1c9c`，先對齊審查範圍與 diff，再逐項找「該失敗卻放行」的路徑。審查範圍已對上。接下來只讀 `0ea3cc9c..d6cf1c9c` 的差異與相關實作，專門找該失敗卻放行的路徑。差異被截斷了。我改讀完整 writer、bridge 失敗路徑與測試，核對進度判定有沒有被誤當成寫出。進度判定與失敗出口還要對到 bridge 收尾、錯誤碼與 tree hash。我接著只讀這些呼叫端。審查範圍是 `0ea3cc9c..d6cf1c9c` 的 writer 停滯期限、bridge 失敗細項與新增測試。只讀程式，沒有改檔，也沒有跑測試。

## 1. 真的卡死時是否仍失敗

真的沒有寫出位元組時，close、flush、背壓都在原本的期限內失敗。進度只在 `os_write` 回傳大於 0 且長度合法之後，由 `_mark_progress` 推進（`writer.py:323-334`）。dequeue、semaphore 釋放、`_notify_progress_waiters`、hasher／bytes 計數都不會重置停滯時鐘。短寫只有在實際寫出那麼多位元組時才算進度。

## 2. 進度是否代表資料已寫出

`os_write` 回 0、負值、非 `int` 或超過剩餘長度時，在標記進度前就丟出 `OSError`（`writer.py:325-326`），worker 記 `write_error` 並停止後續資料。例外同樣不標記進度。flush 路徑的 `fsync` 失敗會設失敗碼、寫 best-effort `evidence_failure`，並讓 flush 失敗（`writer.py:388-404`）。`fsync` 本身不計進度；它卡住時，flush 等待仍受停滯期限與 300 秒上限約束。

## 3. 證據完整性

seq、`loss_flag`、`evidence_failure` marker 與 digest 的計算沒有改語意。digest 仍是成功寫入位元組的 SHA-256，`records` 仍在整筆記錄寫完後才增加。失敗後新的 emit 被 `_require_running_locked` 拒絕（`writer.py:258-264`）。bridge 在 `writer.close()` 之後會呼叫 `_check_writer_failure`（`core.py:1176-1177`），manifest 記 `closed_cleanly: false`，行程以 exit 3 結束。比較器看到 manifest failure 會得到 `INSUFFICIENT_EVIDENCE`。

## 4. 並行

沒有死鎖或會放行的遺失喚醒。`_wait_with_progress` 持有 condition 時，predicate 只做非阻塞的鎖、佇列與 semaphore 操作；`wait()` 會放開 condition（`writer.py:229`）。`_mark_progress` 在放開 `_lock` 之後才拿 condition（`writer.py:329-334`）。通知若錯過，最多 0.05 秒後的輪詢會再查；停滯仍會失敗。等待用的是 `Condition.wait` 逾時，不是 busy loop。

## 5. 絕對上限

close 與 flush 的每次等待都帶入呼叫當下加 300 秒的期限（`writer.py:49`、`620`、`721`、`217-218`）。進度只改停滯時鐘，不改這個期限。超過上限會設 `close_stall` 或 `flush_stall`，bridge 失敗。背壓沒有絕對上限可以接受：沒有真實寫入進度時仍在 `block_timeout_s` 失敗；要成功必須取得 semaphore slot；乾淨收尾仍要經過有 300 秒上限的 close，並檢查 `writer.failure`。

- **Low** `writer.py:782`：worker 結束後的 `os_fsync`／`os.close` 沒有套上 300 秒上限。fsync 若一直不返回，close 不會在上限內失敗；`_finish` 停在這次 close，不會寫出成功 manifest。flush 的 fsync 在 `done` 之前，仍受上限約束。

## 6. 失敗細項與錯誤碼

`harness_codes.py` 沒有改，`HARNESS_*` 集合不變。`BRIDGE_FAILED` 只在有細項時多一個 `detail` 欄位（`core.py:1189-1191`）。現有讀取端使用 `code` 與 `event`；沒有找到要求整行 JSON 鍵集合完全相同的解析器。

## 7. 測試有沒有被放寬

既有斷言沒有改動，diff 只追加測試。慢寫超過固定期限的三支新測試，在舊的固定總時間語意下會失敗；細項測試在舊程式上會因沒有 `failure_detail` 而失敗。300 秒絕對上限沒有對應測試。

## 8. selftest 與 Windows

selftest 與 Windows 路徑沒有改到。ST-14 的阻塞寫入在回傳前記錄進度，阻塞期間仍會在 `block_timeout_s` 觸發 `HARNESS_EVIDENCE_OVERFLOW`。writer 沒有新增僅 POSIX 能用的 API。

## 9. 舊 tree 雜湊 `dde967aa`

`evidence/stage1/od31-ev01-win/win-func-test-meta-261028-091236.json` 記錄的 `tool_tree_sha256` 是 `dde967aa…`。`l15_exit_driver.py:472` 只檢查它是 64 位十六進位，不要求等於核准 tree。更新核准雜湊後，這份舊證據不會讓 stage1 gate 失敗。

BRIDGE: APPROVE

## 審查者 B：gemini-3.8-flash-high（agy）

作為 `uart_stdio_bridge` 工具 revision（`git diff 0ea3cc9c d6cf1c9c`）的獨立審查者，針對本次停滯期限改動與證據判定路徑進行了詳細的靜態程式碼審查與動態測試驗證。

---

### 發現清單

- **嚴重度**：Low
- **file:line**：`tools/uart_stdio_bridge/src/uart_stdio_bridge/evidence/writer.py:421`
- **一句話問題**：`_worker` 例外處理區塊內的 `_set_failure_locked` 縮排為 8 格空格，出現過度縮排。
- **具體會出錯的情境**：雖然符合 Python 語法且不影響執行期語意與鎖保護，但屬於程式碼風格不一致。

---

### 逐項檢查結論

1. **卡死時的期限與進度來源**：
   - **結論**：真的卡死時，close、flush、背壓均在各自的原期限（`close_timeout_s`、`timeout_s`、`block_timeout_s`）內判定失敗；系統內無任何重試循環，計數器（`_progress_generation`）僅在 `_write_complete` 獲得作業系統回傳大於 0 的寫入長度後才推進，絕無未實際寫出卻前進進度的路徑。

2. **進度訊號與 I/O 異常處理**：
   - **結論**：進度訊號嚴格代表底層真實寫入位元組；`os_write` 回傳 0、負數或非整數時立即拋出 `OSError` 進入重大失敗處理；短寫只把實際寫入部分更新至 digest 與 bytes_written 並重設計時器，後續卡死仍會在期限內失敗；fsync 異常在 worker 與 close 路徑皆正確標記 `HARNESS_EVIDENCE_WRITE_FAILED`／`write_error` 並使 bridge 以 exit 3 失敗。

3. **證據完整性（seq／loss_flag／marker／digest）**：
   - **結論**：seq 連續性由 `_emit_lock` 嚴格序列化，背壓失敗時插入帶 `loss_flag: true` 的 overflow marker；digest 僅對實際寫出位元組計算；writer 一旦設定 failure，`_require_running_locked` 立即阻擋所有後續 `emit_data`／`emit_meta` 與 flush，Bridge 同步關閉管線退出，不會在失敗後繼續收受資料。

4. **並行與鎖結構**：
   - **結論**：嚴格維持 `_flush_lock` $\to$ `_emit_lock` $\to$ `_lock` 的單向鎖序；Condition 鎖（`_progress_changed`）僅在非阻塞 predicate 判斷時持有並在 `wait()` 時即時釋放，無鎖倒置死鎖風險；`BoundedSemaphore` 增減精確配對；等待迴圈採用 `min(remaining, 0.05)` 輪詢上限，既有 worker 進度通知，又徹底防禦遺失通知且避免 busy loop。

5. **絕對上限（300 秒）與背壓語意**：
   - **結論**：close 與 flush 的單次操作皆以 `time.monotonic() + 300.0` 為絕對期限，跨多階段等待累計超時即強制以 `close_stall`／`flush_stall` 失敗並回報 exit 3；背壓路徑不設 300 秒絕對上限是合理且正確的，因背壓屬常態串流中單筆紀錄入列的暫時性排隊，設上限將導致超過 5 分鐘的合法長跑測試中斷，且背壓依然受 5 秒停滯期限保護，不會無界卡死。

6. **失敗細項欄位與相容性**：
   - **結論**：`harness_codes.py` 集合完全未變動；`BRIDGE_FAILED` stderr JSON 新增 `"detail"` 欄位符合 JSON 擴展相容慣例，經檢索整個 repository 與測試集，無任何使用 `additionalProperties: false` 的消費者，不破壞現有工具解析。

7. **測試有效性與回歸防護**：
   - **結論**：既有測試無任何行被刪除或放寬；新增的 8 個測試函數中，3 個慢速進度測試在舊版固定總逾時程式碼上必因超時而紅燈，5 個 detail 欄位測試在舊程式碼上必因缺少屬性或 JSON 缺少欄位而失敗，測試覆蓋真實且有效。

8. **selftest 與 Windows 路徑**：
   - **結論**：ST-01～ST-14 核心驗收邏輯不受影響；改動純粹採用 Python 標準庫 `time.monotonic` 與 `threading` 原語，檔案 I/O 依然走 `os.open(..., O_BINARY)` 與標準描述元呼叫，Windows 與 POSIX 平臺行為完全一致。

9. **tree 雜湊更新與 Stage 1 證據相容性**：
   - **結論**：`evidence/stage1/od31-ev01-win/` 僅為 OD-31 Windows reproducer 的靜態歷史留存，無任何 gate 腳本或測試會對其交叉校驗核准 tree；更新 `APPROVED_REVISION.json` 為候選 tree（`0021ba6703decf05cea53668060984dbdf1fa3dcee32a84cfa0d2c3ef4704c1c`）後，`test_approved_revision_matches_tool_tree` 將轉綠，且不會造成任何 Stage 1 gate 失敗。

---

BRIDGE: APPROVE
