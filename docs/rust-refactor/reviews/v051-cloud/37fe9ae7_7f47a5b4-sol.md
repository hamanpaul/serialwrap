> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-e066458-1 | High | 依裁決不處理 | 0 | 登入結束時停用遮罩。 |
| V051-e066458-2 | High | 依裁決不處理 | 0 | 不回顯且 secret 與 prompt 重疊時允許 fail-safe 逾時。 |
| V051-e066458-3 | High | 已修 a59eeb9c | 0 | reader 結束時的 evidence gap 處理保留。 |
| V051-e066458-4 | High | 已修 06264e25 | 0 | 原斷言保留。 |
| V051-e066458-5 | High | 已修 06264e25 | 0 | 逐 byte reader 測試保留。 |
| V051-a59eeb9-1 | High | 已修 b319c0e6 | 0 | 有回顯的前綴分段測試保留。 |
| V051-a59eeb9-2 | High | 依裁決不處理 | 0 | 不回顯的重疊情境依裁決逾時。 |
| V051-b319c0e-1 | High | 已修 48b8308d | 0 | 自動登入依內容遮罩。 |
| V051-b319c0e-2 | Medium | 依裁決不處理 | 3 | 編排者已結案。 |
| V051-114b4e4-1 | High | 已修 48b8308d | 0 | 未恢復持 fence 等待 reader。 |
| V051-114b4e4-2 | High | 依裁決不處理 | 3 | 編排者已結案。 |
| V051-48b8308-1 | High | 已修 271cb965 | 0 | 部分／未知 TX 的正式 reader 案例保留。 |
| V051-48b8308-2 | High | 依裁決不處理 | 1 | 編排者已結案。 |
| V051-7034d52-1 | High | 已修 271cb965 | 0 | 自動登入部分／未知 TX reader 案例保留。 |
| V051-7034d52-2 | High | 依裁決不處理 | 1 | 編排者已結案。 |
| V051-8972413-1 | High | 已修 271cb965 | 0 | 自動登入未知 TX 受控案例保留。 |
| V051-0047421-1 | High | 未修 | 3 | 整行 TX 延後分類，但逐鍵回顯仍有洩漏交錯；見 B-1。 |
| V051-0047421-2 | High | 已修 dfe159bd | 0 | secret line 強制 command 模式的實作與測試保留。 |
| V051-0047421-3 | High | 已修 dd4b6f01 | 0 | actor 不再重放 RX 至 tracker。 |
| V051-0047421-4 | High | 已修 dd4b6f01 | 0 | 獨立 CR 定向測試保留。 |
| V051-0047421-5 | High | 已修 7f47a5b4 | 0 | 未決前綴遇 CR/LF 後 matcher 保持啟用；同 chunk 與跨 chunk 測試通過。 |
| V051-0047421-6 | High | 未修 | 1 | 原 owner 比對已修，但本輪引入在途 TX 與斷線交錯造成結算遺漏；見 B-2。 |
| V051-0047421-7 | High | 已修 dfe159bd | 0 | profile 更新 matcher 的實作保留。 |
| V051-0047421-8 | High | 未修 | 1 | 原 META source 測試保留，但在途回報晚於斷線時仍可能缺 META；見 B-2。 |
| V051-dfe159b-1 | High | 未修 | 2 | 整行遮罩與斷線結算仍有缺口，且部分原有測試範圍被縮小；見 B-1、B-2、B-4。 |
| V051-dfe159b-2 | High | 已修 dd4b6f01 | 0 | tracker 維持 reader 單一寫入者。 |
| V051-dfe159b-3 | High | 已修 dd4b6f01 | 0 | 另一個 console detach 不會直接結算 raw owner。 |
| V051-dfe159b-4 | High | 已修 dd4b6f01 | 0 | 同 chunk 無效 UTF-8 後的有效提示有定向測試。 |
| V051-dfe159b-5 | Medium | 已修 2ac7dc6d | 0 | profile 測試等待新 profile 與 `ATTACHED`。 |
| V051-37fe9ae-1 | High | 未修 | 1 | 一般完成路徑已延後整行 WAL 分類，但逐鍵回顯與斷線在途交錯未閉合；見 B-1、B-2。 |
| V051-37fe9ae-2 | High | 已修 7f47a5b4 | 0 | 前綴加 CR/LF 不再撤銷 matcher；兩支定向測試通過。 |
| V051-37fe9ae-3 | Medium | 已修 7f47a5b4 | 0 | line-buffer 依行分段；單 chunk 多行測試通過。 |
| V051-37fe9ae-4 | High | 已修 7f47a5b4 | 0 | 共用 helper 解碼 WAL payload、要求 event 到達，並有各 sink 洩漏偵測測試。原逐鍵 loopback 範圍另見 B-4。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 1. 逐項處理 finding、先紅後綠 | 部分完成 | 紅燈 log 可追溯至 `da32bdf8` 測試及所述 `37fe9ae7` 產品基線；最終定向測試通過。但 A 中仍有未閉合項，見 B-1、B-2。 |
| 2. 保留既有斷言、測試名稱與範圍 | 部分完成 | L1 鎖在本輪為 18 行新增、無刪除；但逐鍵 loopback 與未換行時的 WAL 驗證範圍改變，見 B-4。 |
| 3. finding 對照與可追溯驗收回報 | 完成 37fe9ae7 | 實作者提供逐項對照、SHA 與 log；本輪核對了紅綠 log 標頭及最終測試檔 SHA。 |
| 4. 單一 tracker、整行 fail-safe、lifetime、內容遮罩、UTF-8、profile 切換 | 部分完成 | 單一 tracker、UTF-8 與 profile 測試保留；整行回顯及 lifetime 在途結算仍有缺口，見 B-1、B-2。 |
| 5. 受控交錯、雙 console、無效 byte、META source 與各出口測試 | 部分完成 | 指定測試存在，但沒有鎖住 B-1、B-2 的交錯，且逐鍵 loopback 範圍被移除；見 B-4。 |
| 6. 交編排者整包驗收及開 PR | 完成 37fe9ae7 | 實作者已交付提交與回報；整包驗收及 PR 由編排者接續。 |

本次在 `7f47a5b4`、指定的 `CARGO_TARGET_DIR` 執行 issue 312 定向測試：**21 passed**。下列交錯問題是根據正式程式路徑作的靜態分析，並非本次新增測試的實跑重現。未執行完整 pytest、Docker／bundle、Windows 原生或實機測試；工作樹保持乾淨。

## B 新發現

1. **V051-7f47a5b-1 [High]** [console.rs:585](crates/sw-core/src/session_core/console.rs:585) — **提示在 raw 行中途出現時，逐鍵回顯可先於遮罩器發布，後半段也可能被放行。**例如 human 輸入 `abc`，DUT 立即回顯 `abc`；之後 reader 看到 `Password:`，human 再輸入 `def\r`，DUT 只逐鍵回顯後續 `def`。提示前的 `abc` 已由 reader 明文發布；提示後建立的 matcher 又以完整 `abcdef` 為目標，[不匹配的 `def` 會被放行](crates/sw-core/src/uart_bridge.rs:477)。依裁決整行是 secret，應保護其回顯。現有[測試](crates/sw-daemon/tests/issue311_secret_echo.rs:3506)等整行送完才注入完整 echo，不能覆蓋此交錯。— **修法：**在可能成為 secret 的 raw 行上，於發布前處理已送前綴及後續逐鍵 echo；補 DUT 在提示前、後各自即時回顯的受控測試，逐一檢查 WAL、event、agent log 與 console。此項未列入實作者 SPEC-GAPS。

2. **V051-7f47a5b-2 [High]** [console.rs:675](crates/sw-core/src/session_core/console.rs:675) — **raw TX 已送出、但完成回報尚未由 actor 處理時，斷線或 detach 會遺失紀錄。**例如 `Password:` active，TxWriter 已將 `abc` 寫到 UART，但 `HumanTxCompleted` 尚在途；此時 [detach 先結算並移除 console](crates/sw-core/src/session_core/lease_p6.rs:246)。`raw_line` 尚未收到這次完成回報，故結算長度為零；回報稍後到達時 [console record 已不存在](crates/sw-core/src/session_core/console.rs:731)，不會補 `secret_tx` META。一般 raw TX 也會因 TxWriter 已延後 WAL 而遺失。現有斷線測試先等待 TX 並 `settle()`，未覆蓋此交錯。— **修法：**讓在途 job 的已確認結果在 client lifetime 移除或 drain 後仍可結算，確保 secret META 的長度與 source、一般 TX WAL 各寫一次；補自然斷線、明確 detach 與 stop 的受控交錯測試。此項未列入實作者 SPEC-GAPS。

3. **V051-7f47a5b-3 [High]** [tx_writer.rs:1184](crates/sw-core/src/tx_writer.rs:1184) — **一般 raw TX 在換行或正常結算前不再進 WAL，行程異常結束會永久遺失審計資料。**例如配置了 password regex 的 session 中，human 送出普通的 `abc` 而尚未送 CR/LF，UART 已收到 bytes，但 WAL 無對應 TX；若 daemon 此時異常終止，記憶體中的 `raw_line_chunks` 無法結算。原有即時 WAL 行為因此改變。— **修法：**設計可在異常終止後恢復、又不會把後來判定為 secret 的前綴明文發布的持久結算方式；以普通未完成行及異常終止測試驗證 TX 不遺失，同時維持整行遮罩。

4. **V051-7f47a5b-4 [High]** [issue311_secret_echo.rs:2624](crates/sw-daemon/tests/issue311_secret_echo.rs:2624) — **既有測試的逐鍵回顯與未換行驗證範圍被削弱。**`da32bdf8` 移除兩支 raw 測試的 `set_loopback(true)`，改為密碼及 CR 全部送完後才注入完整 echo；因此「遮罩器直到 CR 才啟用」這類錯誤可通過目前測試，原逐鍵 TX／RX 交錯不再受保護。普通 `after-312` 也改成先送 CR 才等待 WAL，不能再驗未完成普通行的記錄。這違反本項不得縮小既有測試範圍的要求，實作者 SPEC-GAPS 未記錄。— **修法：**恢復逐鍵 loopback 或以受控 barrier 保留等價的逐鍵 TX／RX 交錯與原安全斷言；普通未換行 WAL 行為另保留明確驗證，並確認關閉相應保護的突變會轉紅。

5. **V051-7f47a5b-5 [High]** [console.rs:740](crates/sw-core/src/session_core/console.rs:740) — **新的 raw 行緩衝無容量上限，可耗盡 daemon 記憶體。**例如 raw console 持續傳送不含 CR/LF 的二進位資料：每個已確認 chunk 同時累加到 `raw_line` 與 `raw_line_chunks`，1 GiB 輸入即至少保留約 2 GiB payload，直到換行或斷線。既有 backlog 上限不限制已完成 TX 的這兩個容器，長時間傳輸可使 daemon 因記憶體耗盡而中止。— **修法：**為未結算 raw 資料設明確有界的儲存與背壓／持久化策略，保留整行遮罩及 UART 語意；補超過界限且不含換行的串流測試。

## 既有碼待辦（不計入結論）

- `ATTACHED` 下 line-buffer 命令仍受既有 READY 出列條件限制。
- `echo_paced::wait_regex_full_window_when_trimmed` 的既有競態疑慮，本輪未重跑。

## 對裁決的異議（不計入結論）

無。

結論：需修正