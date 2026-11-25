> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

狀態以 `46cf8ac4` 為準；累計未修輪數沿用前輪計數。

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
| V051-114b4e4-1 | High | 已修 48b8308d | 0 | 未恢復持 fence 等待 reader；本輪另有鎖住 reader 的問題，見 B-2。 |
| V051-114b4e4-2 | High | 依裁決不處理 | 3 | 編排者已結案。 |
| V051-48b8308-1 | High | 已修 271cb965 | 0 | 部分／未知 TX 的正式 reader 案例保留。 |
| V051-48b8308-2 | High | 依裁決不處理 | 1 | 編排者已結案。 |
| V051-7034d52-1 | High | 已修 271cb965 | 0 | 自動登入部分／未知 TX reader 案例保留。 |
| V051-7034d52-2 | High | 依裁決不處理 | 1 | 編排者已結案。 |
| V051-8972413-1 | High | 已修 271cb965 | 0 | 自動登入未知 TX 受控案例保留。 |
| V051-0047421-1 | High | 未修 | 4 | 裁決指定的 `00474218` 紅燈未重現；實作者已列 SPEC-GAPS，替代證據待編排者裁決。 |
| V051-0047421-2 | High | 未修 | 1 | 本輪移除了 secret line 強制 command 模式；`vim` 反例重新開啟 lease，見 B-1。 |
| V051-0047421-3 | High | 已修 dd4b6f01 | 0 | actor 不再重放 RX 至 tracker。 |
| V051-0047421-4 | High | 已修 dd4b6f01 | 0 | 獨立 CR 定向測試保留。 |
| V051-0047421-5 | High | 已修 7f47a5b4 | 0 | 未決前綴遇 CR/LF 的兩支測試保留且通過。 |
| V051-0047421-6 | High | 已修 46cf8ac4 | 0 | 完成的 TX 不再依賴已移除的 console record 結算；部分寫入尚未回報的缺口見 B-3。 |
| V051-0047421-7 | High | 已修 dfe159bd | 0 | profile 更新 matcher 與等待新 profile 的測試保留。 |
| V051-0047421-8 | High | 已修 46cf8ac4 | 0 | 已完成的 secret TX 由 writer 寫含原 source 的 META；部分寫入缺口見 B-3。 |
| V051-dfe159b-1 | High | 未修 | 3 | 整行與 lifetime 結算已依新裁決撤回；部分寫入尚未回報前仍未立即記錄，見 B-3。 |
| V051-dfe159b-2 | High | 已修 dd4b6f01 | 0 | tracker 維持 reader 單一 RX 寫入者。 |
| V051-dfe159b-3 | High | 已修 dd4b6f01 | 0 | 依新裁決不再按 console lifetime 結算。 |
| V051-dfe159b-4 | High | 已修 dd4b6f01 | 0 | 同 chunk 無效 UTF-8 後的有效提示測試保留。 |
| V051-dfe159b-5 | Medium | 已修 2ac7dc6d | 0 | profile 測試等待新 profile 與 `ATTACHED`。 |
| V051-37fe9ae-1 | High | 未修 | 2 | 整行延後已撤回；已完成的 writer job 會記錄，但部分寫入尚未回報時仍有缺口，見 B-3。 |
| V051-37fe9ae-2 | High | 已修 7f47a5b4 | 0 | 前綴加 CR/LF 的兩支定向測試通過。 |
| V051-37fe9ae-3 | Medium | 已修 7f47a5b4 | 0 | line-buffer 依行分段的測試保留。 |
| V051-37fe9ae-4 | High | 已修 7f47a5b4 | 0 | sink 解碼 helper 與各出口洩漏偵測保留。 |
| V051-7f47a5b-1 | High | 已修 46cf8ac4 | 0 | 提示前前綴依新裁決屬窗外；提示後逐鍵回顯的受控案例通過。 |
| V051-7f47a5b-2 | High | 已修 46cf8ac4 | 0 | disconnect、detach、stop 後完成的 job 可寫出 META；部分寫入缺口見 B-3。 |
| V051-7f47a5b-3 | High | 未修 | 1 | 一般完整 job 的未換行 TX 已立即記錄；部分寫入後 writer 尚未返回時仍可能遺失，見 B-3。 |
| V051-7f47a5b-4 | High | 未修 | 1 | 逐鍵 loopback 與未換行普通 TX 斷言已恢復；另一支既有測試新增手動關閉 lease 的前置，削弱原行為驗證，見 B-1。 |
| V051-7f47a5b-5 | High | 已修 46cf8ac4 | 0 | 原 `raw_line`／`raw_line_chunks` 累積已移除；超限回歸測試與紅燈證據不足，見 B-4。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 1. 每項處理、先紅後綠 | 部分完成 | `00474218` 指定紅燈未重現，已列 SPEC-GAPS；1024-byte 超限案例在 [7f47a5b4 紅燈紀錄](/tmp/sw.CRJb13/red-window-regression-final-bound.log)中通過，且未驗證儲存上界，見 B-4。 |
| 2. 不放寬既有斷言、不刪除或改名 | 部分完成 | 兩支 raw loopback 已恢復；但 command-shaped password 測試插入手動關閉 lease，見 B-1。L1 鎖本次 diff 僅新增節點。 |
| 3. finding 對照與可追溯回報 | 完成 46cf8ac4 | 實作者提供對照表、提交與 log；`00474218` 未重現有明確標示，沒有冒稱紅燈。 |
| 4. 撤回整行延後，改為 session 窗口、即時 WAL/META 與有界 echo | 部分完成 | 整行緩衝已移除、世代與 1024-byte 上限已加入；writer 阻塞與部分寫入期間的行為仍有缺口，見 B-2、B-3。 |
| 5. 指定交錯與各出口測試 | 部分完成 | 新增 10 個窗口測試並登記 L1；部分寫入、reader 阻塞及容量界限未被有效鎖定，見 B-2～B-4。 |
| 6. 交編排者整包驗收及開 PR | 完成 46cf8ac4 | 本次審查對象與實作者回報已交付；整包驗收與 PR 由編排者接續。 |

本次以指定 `CARGO_TARGET_DIR` 實跑：`issue311_window_regression` **10 passed**，`issue311_secret_echo` **40 passed**。上述新發現是對正式程式路徑與測試的靜態分析，並非另行新增測試後的實跑重現。未執行完整 pytest、Docker／bundle、Windows 原生或實機測試；工作樹保持乾淨。

## B 新發現

1. **V051-46cf8ac-1 [High]** [console.rs:745](crates/sw-core/src/session_core/console.rs:745) — **密碼若形似互動命令，會重新建立互動 lease。**line-buffer 在 `Password:` 後輸入 `vim\r` 時，現在無條件以文字選擇 interactive 模式；[session_core.rs:7420](crates/sw-core/src/session_core.rs:7420) 因而建立 lease。原本 `7f47a5b4` 對 secret 強制 command 模式，下一行可直接送出；本輪測試卻在下一行前[新增手動 `interactive_close`](crates/sw-daemon/tests/issue311_secret_echo.rs:2965)，使測試掩蓋了此回歸。— **修法：**確保實際作為 secret 送出的 line 不會因密碼文字建立互動 lease；移除測試中新增的手動 close，恢復原本直接送出下一行的斷言，並保留 TX 當下分類的裁決。

2. **V051-46cf8ac-2 [High]** [uart_bridge.rs:368](crates/sw-core/src/uart_bridge.rs:368) — **human TX 等待可寫時持有 tracker 鎖，會停止整條 RX reader。**`with_human_tx_window` 持鎖執行整次 `write_gated`；POSIX writer 遇到 `EAGAIN` 可在 [poll 中無限等待](crates/sw-pal/src/serial/posix.rs:1183)，而 reader 每次發布 RX 前都要取得同一把鎖。假設 UART 輸出因流控停住，但 DUT 仍送 boot log 或提示，reader 無法發布這些資料；需要 reader 繼續消費資料才能解除對端阻塞時，雙向進度可能停住。這也影響普通 human TX，不限密碼。— **修法：**避免跨等待或整個 `write_gated` 持有 reader tracker 鎖，改在實際 OS submit 的短臨界區協調分類與登記；加入受控的 TX would-block、同時持續 RX 與 stop 的測試。

3. **V051-46cf8ac-3 [High]** [tx_writer.rs:1121](crates/sw-core/src/tx_writer.rs:1121) — **部分 TX 已寫出但 writer 尚在等待時，WAL／META 仍未寫入。**例如 `ordinary-open` 的前 3 bytes 已被 OS 接受，後續遇 `EAGAIN`；[WAL append](crates/sw-core/src/tx_writer.rs:1212) 要等整次 `write_gated` 返回才執行。此時 daemon 異常終止，已送出的 `ord` 永久缺少 TX 記錄；secret TX 的已寫前綴同樣缺少 META。現有異常終止測試等待整筆 ordinary TX 已記錄後才殺行程，沒有覆蓋此交錯。— **修法：**在每次確認 OS 接受 bytes 時記錄對應的普通 TX 或 secret META，確保部分寫入、取消與重試不重複記錄；用 `AcceptThenWouldBlock` 等受控步驟，在第一段接受後、writer 返回前驗證 WAL，再測異常終止。

4. **V051-46cf8ac-4 [High]** [issue311_window_regression.rs:493](crates/sw-daemon/tests/issue311_window_regression.rs:493) — **指定的超限回歸與先紅證據未完成。**測試只送一次 1025 bytes，沒有觀察內部保存量或持續無換行輸入；而且 [sink helper](crates/sw-daemon/tests/issue311_window_regression.rs:299) 只搜尋完整 1025-byte secret，若前 1024 bytes 明文、最後一 byte 被遮罩也會通過。實作者的 [7f47a5b4 測試紀錄](/tmp/sw.CRJb13/red-window-regression-final-bound.log)中該案例為 `ok`，不能證明它能抓到原先的無界累積；此缺項未列入 SPEC-GAPS。— **修法：**加入可觀察保存量上限、超過界限後仍持續串流且無換行的受控測試；逐段驗證所有 RX bytes 與各 sink 均符合遮罩，再提供修前紅、修後綠或明確列入 SPEC-GAPS。

## 既有碼待辦（不計入結論）

- `ATTACHED` 下 line-buffer 命令仍受既有 READY 出列條件限制。
- `echo_paced::wait_regex_full_window_when_trimmed` 的既有競態疑慮，本輪未重跑。

## 對裁決的異議（不計入結論）

無。

結論：需修正