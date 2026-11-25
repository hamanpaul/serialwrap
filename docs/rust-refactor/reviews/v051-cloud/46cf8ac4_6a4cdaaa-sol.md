> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

審查基準為 `6a4cdaaa81a70cd250e6ba9af73166847ec67a5c`。本輪只檢視 `46cf8ac4..6a4cdaaa` 的變更；未改動的舊項沿用前輪結論，並核對本輪 diff 是否影響該結論。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-e066458-1 | High | 依裁決不處理 | 0 | 登入結束時停用遮罩。 |
| V051-e066458-2 | High | 依裁決不處理 | 0 | 不回顯且 secret 與 prompt 重疊時，允許 fail-safe 逾時。 |
| V051-e066458-3 | High | 已修 a59eeb9c | 0 | reader 結束時的 evidence gap 處理保留。 |
| V051-e066458-4 | High | 已修 06264e25 | 0 | 原斷言保留。 |
| V051-e066458-5 | High | 已修 06264e25 | 0 | 逐 byte reader 測試保留。 |
| V051-a59eeb9-1 | High | 已修 b319c0e6 | 0 | 有回顯的前綴分段測試保留。 |
| V051-a59eeb9-2 | High | 依裁決不處理 | 0 | 不回顯的重疊情境依裁決逾時。 |
| V051-b319c0e-1 | High | 已修 48b8308d | 0 | 自動登入依內容遮罩。 |
| V051-b319c0e-2 | Medium | 依裁決不處理 | 3 | 編排者已結案。 |
| V051-114b4e4-1 | High | 已修 48b8308d | 0 | 原持 fence 等待 reader 的設計已移除；本輪新交錯見 B-2。 |
| V051-114b4e4-2 | High | 依裁決不處理 | 3 | 編排者已結案。 |
| V051-48b8308-1 | High | 已修 271cb965 | 0 | 部分／未知 TX 的正式 reader 案例保留。 |
| V051-48b8308-2 | High | 依裁決不處理 | 1 | 編排者已結案。 |
| V051-7034d52-1 | High | 已修 271cb965 | 0 | 自動登入部分／未知 TX reader 案例保留。 |
| V051-7034d52-2 | High | 依裁決不處理 | 1 | 編排者已結案。 |
| V051-8972413-1 | High | 已修 271cb965 | 0 | 自動登入未知 TX 受控案例保留。 |
| V051-0047421-1 | High | 依裁決不處理 | 4 | 2026-11-26 裁決接受替代證據，不再要求重現 `00474218` 紅燈。 |
| V051-0047421-2 | High | 未修 | 2 | 模式仍在入列時判定，與 TX 時窗口可能不同；見 B-1。 |
| V051-0047421-3 | High | 已修 dd4b6f01 | 0 | actor 不再重放 RX 至 tracker。 |
| V051-0047421-4 | High | 已修 dd4b6f01 | 0 | 獨立 CR 定向測試保留。 |
| V051-0047421-5 | High | 已修 7f47a5b4 | 0 | 未決前綴遇 CR／LF 的測試保留。 |
| V051-0047421-6 | High | 已修 46cf8ac4 | 0 | 完成的 TX 不再依賴 console record 結算；部分寫入依裁決另列待辦。 |
| V051-0047421-7 | High | 已修 dfe159bd | 0 | profile 更新 matcher 與等待新 profile 的測試保留。 |
| V051-0047421-8 | High | 已修 46cf8ac4 | 0 | 已完成的 secret TX 由 writer 寫含原 source 的 META；部分寫入依裁決另列待辦。 |
| V051-dfe159b-1 | High | 依裁決不處理 | 3 | 剩餘部分寫入缺口移至既有碼待辦。 |
| V051-dfe159b-2 | High | 已修 dd4b6f01 | 0 | tracker 維持 reader 單一 RX 寫入者。 |
| V051-dfe159b-3 | High | 已修 dd4b6f01 | 0 | 依裁決不再按 console lifetime 結算。 |
| V051-dfe159b-4 | High | 已修 dd4b6f01 | 0 | 同 chunk 無效 UTF-8 後的有效提示測試保留。 |
| V051-dfe159b-5 | Medium | 已修 2ac7dc6d | 0 | profile 測試等待新 profile 與 `ATTACHED`。 |
| V051-37fe9ae-1 | High | 依裁決不處理 | 2 | 剩餘部分寫入缺口移至既有碼待辦。 |
| V051-37fe9ae-2 | High | 已修 7f47a5b4 | 0 | 前綴加 CR／LF 的定向測試保留。 |
| V051-37fe9ae-3 | Medium | 已修 7f47a5b4 | 0 | line-buffer 依行分段的測試保留。 |
| V051-37fe9ae-4 | High | 已修 7f47a5b4 | 0 | sink 解碼 helper 與出口洩漏偵測保留。 |
| V051-7f47a5b-1 | High | 已修 46cf8ac4 | 0 | 提示前前綴依裁決屬窗外；提示後逐鍵回顯受控案例保留。 |
| V051-7f47a5b-2 | High | 已修 46cf8ac4 | 0 | disconnect、detach、stop 後完成的 job 可寫 META；部分寫入依裁決另列待辦。 |
| V051-7f47a5b-3 | High | 依裁決不處理 | 1 | 剩餘部分寫入缺口移至既有碼待辦。 |
| V051-7f47a5b-4 | High | 已修 6a4cdaaa | 0 | 移除手動 `interactive_close`；下一行直接送出的斷言恢復。 |
| V051-7f47a5b-5 | High | 已修 46cf8ac4 | 0 | 原 `raw_line`／`raw_line_chunks` 累積已移除；回歸證據缺口見 B-3。 |
| V051-46cf8ac-1 | High | 未修 | 1 | 一般定向案例已修，但排隊交錯仍可能使用過期模式；見 B-1。 |
| V051-46cf8ac-2 | High | 未修 | 1 | tracker 鎖已縮短；指定的阻塞中 stop 測試尚未證明，見 B-4。 |
| V051-46cf8ac-3 | High | 依裁決不處理 | 0 | 部分寫入缺口移至既有碼待辦。 |
| V051-46cf8ac-4 | High | 未修 | 1 | 新測試有上限觀測，但所附編譯失敗不能證明能抓到原先無界累積；見 B-3。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 1. 逐項處理並提供有效紅綠證據 | 部分完成 | `vim` 與 tracker 鎖有修前失敗紀錄；上限案例的修前紀錄是缺少測試 helper 的編譯錯誤，見 B-3。 |
| 2. 不放寬既有斷言、不刪除或改名、不擴大範圍 | 完成 6a4cdaaa | 指定的手動 close 已移除；鎖檔 diff 只有新增節點。本輪修改超限案例以對應分 chunk 輸入，保留並加強各出口檢查。 |
| 3. finding 對照、驗收與 commit 可追溯 | 完成 6a4cdaaa | 實作者回報逐項列出處置、SHA 與 log；本審查另核對內容，沒有把編譯錯誤當成上限回歸紅燈。 |
| 4. 依裁決處理原始紅燈、部分寫入與四項修正 | 部分完成 | 結案與部分寫入的處置符合裁決；模式判定、上限紅燈及阻塞中 stop 驗證見 B-1、B-3、B-4。META 與 TX WAL 均在 `write_gated` 返回後由 writer 結算。 |
| 5. 不做全域格式化，交編排者後續驗收 | 完成 6a4cdaaa | 本輪 diff 限於六個檔案；實作者回報未執行 `cargo fmt --all`。 |

本審查以指定 `CARGO_TARGET_DIR` 實跑：`vim` 定向測試 **1 passed**、保存量單元測試 **1 passed**。`issue311_window_regression` 首次為 **10 passed、1 failed**：event 斷言尚未看到遮罩事件；該節點單獨重跑通過，同整檔隨後連跑兩次均為 **11 passed**。這不足以把首次失敗歸因於環境，也不足以證明是本輪產品回歸；該 event 等待方式在起點已存在。

未跑完整 pytest、Docker／bundle、Windows 原生或實機測試。審查結束時工作樹乾淨。

## B 新發現

1. **V051-6a4cdaa-1 [High]** [console.rs:745](crates/sw-core/src/session_core/console.rs:745) — **line 模式依入列時窗口決定，可能與實際 TX 分類不同。**例如 console 在提示後一次送入 `密碼\rvim\r`：第一行的 TX 尚未執行時，第二行也以 active 窗口入列為 `line`；等第一行送出並消耗窗口後，第二行實際是普通 `vim`，卻不會建立原本需要的 interactive lease。反向交錯也可能讓入列時為 interactive 的密碼在 TX 時落入 secret 窗口，仍先建立 lease。[start_next](crates/sw-core/src/session_core.rs:7400) 使用已保存的 mode，而 writer 在另一時點分類。  
   **修法：**讓 human line 的模式決定與實際 TX 的 secret 分類使用同一有效判定，並在建立 lease 前處理狀態變化；加入同一輸入批次及排隊期間窗口開啟／關閉的受控測試，確認普通 `vim` 有 lease、實際 secret 無 lease。

2. **V051-6a4cdaa-2 [High]** [uart_bridge.rs:368](crates/sw-core/src/uart_bridge.rs:368) — **writer 等待期間，RX 行尾可清除尚未送完的 secret 登記。**例如提示 active 後，writer 登記 `password`，OS 接受前綴後 would-block；此時 reader 收到一個尚未含 secret 前綴的 `\r\n`，[遮罩器會清除登記](crates/sw-core/src/uart_bridge.rs:525)。writer 恢復送完後，DUT 回顯的 secret 沒有 matcher，可明文進入 WAL 與其他出口。這個交錯由本輪釋放 tracker 鎖後允許 reader 在 `write_gated` 返回前前進而出現。  
   **修法：**在不阻塞 reader 的前提下，讓在途 secret 登記跨越這種先於 TX 完成的 RX 行尾，直到能安全處理該 job 的回顯；用 `AcceptThenWouldBlock`、中途 RX 行尾、恢復 TX 和延後回顯的受控測試檢查 WAL、event、console 與 agent log。此項是靜態交錯分析，未另建測試實跑。

3. **V051-6a4cdaa-3 [High]** [uart_bridge.rs:1776](crates/sw-core/src/uart_bridge.rs:1776) — **上限案例沒有交付可驗證的修前紅燈。**所附紅燈是缺少 `issue311_test_human_secret_echo_storage_len` 的 `E0599` 編譯錯誤；任何未加入該 helper 的版本都會同樣失敗，不能證明測試能抓到原先 `raw_line`／`raw_line_chunks` 的無界累積。現有 helper 只計算 echo matcher 相關儲存，也沒有觀察舊版 console 行緩衝。實作者回報 `SPEC-GAPS: none`，因此這是未完成的指定驗收。  
   **修法：**提供能觀察原無界保存處的受控回歸，證明它在修前版本因超限斷言而失敗、在修後通過；若無法取得該證據，在 SPEC-GAPS 明列嘗試與限制。

4. **V051-6a4cdaa-4 [High]** [issue311_window_regression.rs:528](crates/sw-daemon/tests/issue311_window_regression.rs:528) — **指定的「would-block 期間 stop 能結束」尚未驗證。**測試啟動 shutdown 後，先呼叫 `release_pause()` 才等待 shutdown 結果；因此即使 stop 無法喚醒阻塞中的 writer，只要外部放行後能結束，測試仍會通過。fake 的 [do_pause](crates/sw-pal/src/fakes/serial.rs:454) 也只等待手動 release。實作者未將這個驗收缺口列入 SPEC-GAPS。  
   **修法：**使用可由取消／stop 喚醒的受控 would-block backend，先斷言未手動放行時 shutdown 在期限內完成，再檢查 RX 與結算結果；保留修前紅、修後綠證據。

## 既有碼待辦（不計入結論）

- OS 已接受部分 TX、`write_gated` 尚未返回時若異常終止，既有 TX WAL 可能缺少已送出的前綴；依 2026-11-26 裁決另開工作包處理。
- `ATTACHED` 下 line-buffer 命令仍受既有 READY 出列條件限制。
- 首次整檔測試的 event 斷言失敗後，單項及兩次整檔重跑均通過；原測試在起點已使用相同等待方式，根因尚未經受控驗證。

## 對裁決的異議（不計入結論）

無。

結論：需修正