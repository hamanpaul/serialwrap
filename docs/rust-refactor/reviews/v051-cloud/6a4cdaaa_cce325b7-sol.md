> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

本次核對的 HEAD 為 `cce325b7149d29c99aab6e804531240d7224dfcf`。未修輪數以前輪表格為基準；前輪新發現本輪首次列入核對。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-e066458-1 | High | 依裁決不處理 | 0 | 登入結束時停用遮罩。 |
| V051-e066458-2 | High | 依裁決不處理 | 0 | 不回顯且 secret 與 prompt 重疊時允許 fail-safe 逾時。 |
| V051-e066458-3 | High | 已修 a59eeb9c | 0 | reader 結束的 evidence gap 處理保留。 |
| V051-e066458-4 | High | 已修 06264e25 | 0 | 原斷言保留。 |
| V051-e066458-5 | High | 已修 06264e25 | 0 | 逐 byte reader 測試保留。 |
| V051-a59eeb9-1 | High | 已修 b319c0e6 | 0 | 有回顯的前綴分段測試保留。 |
| V051-a59eeb9-2 | High | 依裁決不處理 | 0 | 不回顯的重疊情境依裁決逾時。 |
| V051-b319c0e-1 | High | 已修 48b8308d | 0 | 自動登入依內容遮罩。 |
| V051-b319c0e-2 | Medium | 依裁決不處理 | 3 | 編排者已結案。 |
| V051-114b4e4-1 | High | 已修 48b8308d | 0 | 原持 fence 等 reader 的設計已移除。 |
| V051-114b4e4-2 | High | 依裁決不處理 | 3 | 編排者已結案。 |
| V051-48b8308-1 | High | 已修 271cb965 | 0 | 部分／未知 TX 的正式 reader 案例保留。 |
| V051-48b8308-2 | High | 依裁決不處理 | 1 | 編排者已結案。 |
| V051-7034d52-1 | High | 已修 271cb965 | 0 | 自動登入部分／未知 TX reader 案例保留。 |
| V051-7034d52-2 | High | 依裁決不處理 | 1 | 編排者已結案。 |
| V051-8972413-1 | High | 已修 271cb965 | 0 | 自動登入未知 TX 受控案例保留。 |
| V051-0047421-1 | High | 依裁決不處理 | 4 | 編排者接受替代證據，不再要求重現 `00474218` 紅燈。 |
| V051-0047421-2 | High | 未修 | 3 | 分類快照仍早於 writer 實際 TX；見 B-1。 |
| V051-0047421-3 | High | 已修 dd4b6f01 | 0 | actor 不再重放 RX 至 tracker。 |
| V051-0047421-4 | High | 已修 dd4b6f01 | 0 | 獨立 CR 定向測試保留。 |
| V051-0047421-5 | High | 已修 7f47a5b4 | 0 | 未決前綴遇 CR／LF 的測試保留。 |
| V051-0047421-6 | High | 已修 46cf8ac4 | 0 | 完成的 TX 不再依賴 console record 結算；部分寫入依裁決另列待辦。 |
| V051-0047421-7 | High | 已修 dfe159bd | 0 | profile 更新 matcher 與等待新 profile 的測試保留。 |
| V051-0047421-8 | High | 已修 46cf8ac4 | 0 | 已完成的 secret TX 由 writer 記含原 source 的 META。 |
| V051-dfe159b-1 | High | 依裁決不處理 | 3 | 剩餘部分寫入缺口移至既有碼待辦。 |
| V051-dfe159b-2 | High | 已修 dd4b6f01 | 0 | tracker 維持 reader 單一 RX 寫入者。 |
| V051-dfe159b-3 | High | 已修 dd4b6f01 | 0 | 依裁決不再按 console lifetime 結算。 |
| V051-dfe159b-4 | High | 已修 dd4b6f01 | 0 | 同 chunk 無效 UTF-8 後的有效提示測試保留。 |
| V051-dfe159b-5 | Medium | 已修 2ac7dc6d | 0 | profile 測試等待新 profile 與 `ATTACHED`。 |
| V051-37fe9ae-1 | High | 依裁決不處理 | 2 | 剩餘部分寫入缺口移至既有碼待辦。 |
| V051-37fe9ae-2 | High | 已修 7f47a5b4 | 0 | 前綴加 CR／LF 的定向測試保留。 |
| V051-37fe9ae-3 | Medium | 已修 7f47a5b4 | 0 | line-buffer 依行分段的測試保留。 |
| V051-37fe9ae-4 | High | 已修 7f47a5b4 | 0 | sink 解碼 helper 與出口洩漏偵測保留。 |
| V051-7f47a5b-1 | High | 已修 46cf8ac4 | 0 | 提示前前綴依裁決屬窗外；提示後逐鍵回顯案例保留。 |
| V051-7f47a5b-2 | High | 已修 46cf8ac4 | 0 | 完成的 job 可寫 META；部分寫入依裁決另列待辦。 |
| V051-7f47a5b-3 | High | 依裁決不處理 | 1 | 剩餘部分寫入缺口移至既有碼待辦。 |
| V051-7f47a5b-4 | High | 已修 6a4cdaaa | 0 | 手動 `interactive_close` 已移除，下一行直接送出的斷言保留。 |
| V051-7f47a5b-5 | High | 已修 46cf8ac4 | 0 | 原無界 `raw_line`／`raw_line_chunks` 已移除；回歸證據仍有缺口，見 B-3。 |
| V051-46cf8ac-1 | High | 未修 | 2 | 一般排隊案例已補，但 actor 快照與實際 TX 間仍可變化；見 B-1。 |
| V051-46cf8ac-2 | High | 已修 cce325b7 | 0 | tracker 鎖未跨 `write_gated`；受控測試涵蓋 RX 發布及未手動放行的 shutdown。 |
| V051-46cf8ac-3 | High | 依裁決不處理 | 0 | 部分寫入缺口移至既有碼待辦。 |
| V051-46cf8ac-4 | High | 未修 | 2 | 新紅燈驗證 matcher guard，未證明能抓到原 console 無界累積；見 B-3。 |
| V051-6a4cdaa-1 | High | 未修 | 1 | 快照前移到 actor 出列，仍與 writer 實際分類時點不同；見 B-1。 |
| V051-6a4cdaa-2 | High | 已修 cce325b7 | 0 | 指定的「在途期間先到無前綴行尾、完成後回顯」案例已補；另一交錯見 B-2。 |
| V051-6a4cdaa-3 | High | 未修 | 1 | 未取得能觀察舊 console 緩衝的修前紅燈，且未列入 SPEC-GAPS；見 B-3。 |
| V051-6a4cdaa-4 | High | 已修 cce325b7 | 0 | 測試先等待 shutdown 結果，失敗後才手動放行。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 1. 逐項處理並提供有效紅綠證據 | 部分完成 | mode、在途行尾及 stop 有修前失敗紀錄；上限突變紅燈未觀察原無界 console 緩衝，見 B-3。 |
| 2. 不放寬既有斷言、不刪除或改名測試、不擴大範圍 | 完成 cce325b7 | 本輪既有測試的變動是將 shutdown 等待移到手動 release 前；鎖檔僅新增節點。 |
| 3. finding 對照、驗收與 commit 可追溯 | 完成 cce325b7 | 實作者回報列有對照、commit 和 log；所稱上限紅燈的適用範圍仍受 B-3 限制。 |
| 4. 依裁決處理結案、部分寫入與四項修正 | 部分完成 | 結案、部分寫入及 stop 處置符合裁決；TX 分類仍有 B-1，上限修前證據仍有 B-3。 |
| 5. 不做全域格式化，交編排者後續驗收 | 完成 cce325b7 | 提交 diff 未見全域格式化；實作者回報未執行 `cargo fmt --all`。 |

本審查以指定 `CARGO_TARGET_DIR` 定向實跑：`issue311_window_regression` **12 passed**；`issue311_secret_echo` 的 `issue312_line_buffer_` 節點 **6 passed**。B-1、B-2 是依程式控制流提出的受控交錯，未另新增測試實跑。未執行完整 pytest、Docker／bundle、Windows 原生或實機測試；審查後工作樹乾淨。

## B 新發現

1. **V051-cce325b-1 [High]** [session_core.rs:7390](crates/sw-core/src/session_core.rs:7390) — **line 命令在 actor 出列時拍的窗口快照，可能早於實際 UART TX。**例如一筆普通文字在窗口未開時出列，actor 保存「非 secret」；接著 reader 收到 `Password:`，才輪到 writer 送出該行。[tx_writer.rs:1138](crates/sw-core/src/tx_writer.rs:1138) 沿用舊快照，因而把實際 TX 時窗口內的輸入寫成明文 TX WAL，也沒有登記其回顯遮罩。若文字是 `vim`，actor 還可能先建立 interactive lease；該 interactive 路徑送 writer 時又重新依當時窗口分類，兩者仍可能不一致。這違反裁決要求的 TX 當下判定。  
   **修法：**以 writer 即將提交 UART 時的有效窗口決定 secret 分類，並讓 lease 決策與同一次有效判定一致；窗口在 actor 判定後變化時，須重新協調模式，不能沿用過期快照。補受控測試，暫停 actor 出列與 writer 提交之間的交錯，分別驗證窗口開啟／關閉時的 lease、TX WAL、META 與 RX 各出口。

2. **V051-cce325b-2 [High]** [uart_bridge.rs:563](crates/sw-core/src/uart_bridge.rs:563) — **在途寫入期間，已收到的 secret 前綴跨行保留後，失配會以明文釋出。**例如登記 `password` 後 writer 已送出 `pas` 並阻塞，reader 依序收到回顯 `pas`、無關的 `\r\n`，然後收到 `X`。新分支先發布行尾但保留 `pending=pas`；處理 `X` 時，[失配迴圈]( crates/sw-core/src/uart_bridge.rs:609) 將 `pas` 原樣發布，進入 WAL 等出口，且行尾與前綴的順序也被交換。現有新測試在行尾前沒有注入已回顯前綴，因此不會抓到此交錯。  
   **修法：**在不清除在途 matcher 的前提下，遇到行尾時先以遮罩結算已收到的未決前綴，並維持 RX 位元組順序；後續仍保留足夠狀態處理完整或延後回顯。新增「部分 echo → 行尾 → 非前綴資料」受控測試，逐一驗證 WAL、event、console 與 agent log。

3. **V051-cce325b-3 [High]** [uart_bridge.rs:1827](crates/sw-core/src/uart_bridge.rs:1827) — **上限修前紅燈仍未證明測試能抓到原先的無界保存。**`/tmp/sw.nYrstg/i311-r2-red-cap-mutation.log` 的確因超過 1024 bytes 而失敗，但突變的是修後 echo matcher 的 guard；原 finding 指的是 `7f47a5b4` 的 console `raw_line`／`raw_line_chunks`。目前 helper 觀察的是 matcher 儲存量，無法觀察該舊 console 緩衝。因此此突變即使有效驗證現行 matcher guard，也不能證明原無界累積會讓回歸測試變紅；實作者回報的 SPEC-GAPS 為 none。  
   **修法：**提供能在含原 console 緩衝的修前產品碼上觀察保存量、因超限斷言而紅，並在修後轉綠的受控證據；若無法取得，依 brief 將實際嘗試、限制與原因明列 SPEC-GAPS。

## 既有碼待辦（不計入結論）

- OS 已接受部分 TX、`write_gated` 尚未返回時若異常終止，既有 TX WAL 可能缺少已送出的前綴；依 2026-11-26 裁決另開工作包處理。
- `ATTACHED` 下 line-buffer 命令仍受既有 READY 出列條件限制。
- 前輪整檔 event 斷言曾首次失敗、後續重跑通過；實作者另回報 `wait_regex_full_window_when_trimmed` 的既有時序失敗。本輪未獨立重現其根因。

## 對裁決的異議（不計入結論）

無。

結論：需修正