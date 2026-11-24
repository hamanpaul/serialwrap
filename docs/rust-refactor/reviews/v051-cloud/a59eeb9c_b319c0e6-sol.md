> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-e066458-1 | High | 已修 a59eeb9c | 0 | 部分及未知 TX 的守衛測試本輪通過；本輪另發現守衛起點交錯問題，見 B。 |
| V051-e066458-2 | High | 已修 b319c0e6 | 0 | 一般登入及 console unlock 的相等 prompt、不回顯案例均通過定向測試。 |
| V051-e066458-3 | High | 已修 a59eeb9c | 0 | reader 結束時的未決前綴 evidence gap 處理保留；本輪未重跑該測試。 |
| V051-e066458-4 | High | 已修 06264e25 | 0 | 本輪未移除該斷言。 |
| V051-e066458-5 | High | 已修 06264e25 | 0 | 本輪未移除逐 byte reader 確認。 |
| V051-a59eeb9-1 | High | 已修 b319c0e6 | 0 | 未決前綴不再加入 prompt 比對；兩條路徑的受控前綴測試通過。 |
| V051-a59eeb9-2 | High | 已修 b319c0e6 | 0 | 原列的完整重疊與前綴交錯情境均有兩條路徑的測試；另見 B 的新時序缺陷。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 1. 逐項處理、先紅後綠 | 部分完成 | 三項均有程式與測試；紅燈日誌有四個預期失敗，但無法綁定測試版本及基準 commit，見 B-2。 |
| 2. 不放寬既有斷言、不刪或改名測試 | 完成 b319c0e6 | 未見刪除、改名或放寬既有斷言；兩個既有測試輸入增加行終止符，符合本輪裁決的 DUT 前提。L1 鎖只追加節點。 |
| 3. finding 對照及可追溯驗收 | 部分完成 | 實作者回報含逐項對照與修正 SHA；先紅證據缺少可核對的程式版本，見 B-2。 |
| 4. 第一行守衛與 prompt 判定 | 部分完成 | [uart_bridge.rs](crates/sw-core/src/uart_bridge.rs:314) 實作行邊界，但送出前到達的換行也會關閉窗口，見 B-1。 |
| 5. 兩條路徑各四種受控情境 | 完成 b319c0e6 | [八個測試](crates/sw-daemon/tests/issue311_secret_echo.rs:1125)涵蓋指定情境及出口。相等 prompt 的公開字串判準已列實作者 SPEC-GAPS，且裁決要求行終止符後原樣發布。 |
| 6. issue 312 留待後續項 | 完成 b319c0e6 | 本輪 diff 未實作 issue 312。 |

## B 新發現

**V051-b319c0e-1 [High] — [uart_bridge.rs:314](crates/sw-core/src/uart_bridge.rs:314) — 送出 secret 前的換行會提早關閉遮罩。** [send_secret](crates/sw-core/src/login_fsm.rs:511) 先武裝守衛，再執行 audit 與 TX；reader 可在這段期間並行處理 RX。若尚未送出 secret 時先收到非同步 `\r\n`，本輪新增的邏輯就把 `line_open` 設為 false。接著 DUT 回顯 secret 時，[此分支](crates/sw-core/src/uart_bridge.rs:307)會原樣發布，經 [WAL 與其他 reader 出口](crates/sw-core/src/uart_bridge.rs:929)洩漏。這是靜態追查出的具體交錯，尚未實跑重現。

修法：讓窗口的行終止符判定與 secret 實際提交的起點正確同步，同時保護可能在 TX 回報前到達的回顯；補受控交錯測試，在 secret 提交前注入換行，再回顯 secret，驗證所有出口均無明文，並涵蓋部分／未知 TX。

**V051-b319c0e-2 [Medium] — [issue311_secret_echo.rs:1125](crates/sw-daemon/tests/issue311_secret_echo.rs:1125) — 先紅證據無法追溯到所測程式版本。** 實作者提供的紅燈日誌確有四個對應失敗，但日誌未記錄 HEAD、測試 diff 或檔案 hash；`a59eeb9c..b319c0e6` 只有一個同時加入測試與修正的 commit。因此無法核對紅燈是否來自 `a59eeb9c` 加上與本輪相同的測試。

修法：提供可追溯的紅燈測試版本，例如測試先行 commit，或附基準 SHA、完整測試 diff／hash 與對應執行輸出。

本次在 `b319c0e6` 執行 ServiceRig 定向測試，結果為 **13 passed、1 failed**；失敗的 `issue311_login_echo_is_masked_at_rpc_and_every_rx_exit` 等待 READY 逾時，單獨重跑為 **1 passed**。目前無法判定首次失敗原因。`sw-core` 的兩個 issue 311 守衛定向測試 **2 passed**。未跑完整 pytest、Docker、bundle、Windows 原生或真機測試；工作樹保持乾淨。本執行環境無法核實底層模型是否為指定的 `gpt-6.1-terra xhigh`。

## 既有碼待辦（不計入結論）

issue 312 的手動登入 secret TX 遮罩留待 i311-i2。

## 對裁決的異議（不計入結論）

無。

結論：需修正