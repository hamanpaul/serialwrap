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
| V051-b319c0e-1 | High | 已修 48b8308d | 0 | 自動登入改依內容遮罩。 |
| V051-b319c0e-2 | Medium | 依裁決不處理 | 3 | 編排者已結案。 |
| V051-114b4e4-1 | High | 已修 48b8308d | 0 | 未恢復持 fence 等待 reader。 |
| V051-114b4e4-2 | High | 依裁決不處理 | 3 | 編排者已結案。 |
| V051-48b8308-1 | High | 已修 271cb965 | 0 | 自動登入部分／未知 TX 的 reader 案例保留。 |
| V051-48b8308-2 | High | 依裁決不處理 | 1 | 編排者已結案。 |
| V051-7034d52-1 | High | 已修 271cb965 | 0 | 自動登入部分／未知 TX 的正式 reader 案例保留。 |
| V051-7034d52-2 | High | 依裁決不處理 | 1 | 編排者已結案。 |
| V051-8972413-1 | High | 已修 271cb965 | 0 | 自動登入未知 TX 的受控案例保留。 |
| V051-0047421-1 | High | 未修 | 2 | 舊 RX 重放交錯已有測試，但整行期間曾 active 的裁決仍未滿足；見 B-1。 |
| V051-0047421-2 | High | 已修 dfe159bd | 0 | secret line 強制走 command 模式的實作與測試保留。 |
| V051-0047421-3 | High | 已修 dd4b6f01 | 0 | actor 不再把 RX 重放進 tracker；核心受控測試通過。 |
| V051-0047421-4 | High | 已修 dd4b6f01 | 0 | actor 不再快取 active；獨立 CR 受控測試由 3da0246a 補入並通過。 |
| V051-0047421-5 | High | 未修 | 2 | 緩衝行終止符前若已有未決 secret 前綴，仍會撤銷遮罩；見 B-2。 |
| V051-0047421-6 | High | 已修 dd4b6f01 | 0 | 自然斷線依 client ID 與 lifetime 結算；定向案例通過。 |
| V051-0047421-7 | High | 已修 dfe159bd | 0 | profile 更新 matcher 的實作保留。 |
| V051-0047421-8 | High | 已修 dd4b6f01 | 0 | 自然斷線測試檢查 META 長度及 `human:<client_id>` source。 |
| V051-dfe159b-1 | High | 未修 | 1 | 整行 fail-safe、回顯遮罩及各出口測試仍有缺口；見 B-1、B-2、B-4。 |
| V051-dfe159b-2 | High | 已修 dd4b6f01 | 0 | tracker 不再由 actor 重放；raw／line-buffer 受控交錯測試通過。 |
| V051-dfe159b-3 | High | 已修 dd4b6f01 | 0 | detach 依 client lifetime 結算；雙 console 定向測試通過。 |
| V051-dfe159b-4 | High | 已修 dd4b6f01 | 0 | decoder 持續處理同 chunk 的有效文字；受控測試通過。 |
| V051-dfe159b-5 | Medium | 已修 2ac7dc6d | 0 | 測試等待新 profile 及 `ATTACHED` 後再連線。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 1. 逐項處理 finding、先紅後綠 | 部分完成 | 修正與紅燈紀錄可追溯；A 中三項仍未閉合，見 B-1、B-2、B-4。 |
| 2. 保留既有斷言、測試名稱與範圍 | 部分完成 | 未見凍結鎖移除或測試改名；既有測試的 secret fixture 已改值，但原檢查仍查舊值，見 B-4。 |
| 3. finding 對照與可追溯驗收回報 | 完成 37fe9ae7 | 實作者回報含逐項對照、SHA 與 log 路徑；本次核對了提交及所列紅燈 log。 |
| 4. 單一 tracker、整行 fail-safe、lifetime、內容遮罩、UTF-8、profile 切換 | 部分完成 | 單一 tracker、lifetime、UTF-8 與 profile 修復有對應程式及測試；整行與內容遮罩的反例見 B-1、B-2。 |
| 5. 指定交錯、雙 console、無效 byte、META source 與各出口測試 | 部分完成 | 指定測試存在且定向通過；部分各出口斷言可空通過，見 B-4。 |
| 6. 交編排者整包驗收及開 PR | 完成 37fe9ae7 | 實作者已交付提交與驗收回報；整包驗收及 PR 屬編排者後續工作。 |

本次在 `37fe9ae77678f85cd7a6a9823e299e0d616ffa94` 以指定 `CARGO_TARGET_DIR` 執行 issue 312 定向測試：**15 passed**；核心 stale-RX 定向測試：**1 passed**。未跑完整 pytest、Docker／bundle、Windows 原生或實機測試；工作樹保持乾淨。

## B 新發現

1. **V051-37fe9ae-1 [High]** [console.rs:426](crates/sw-core/src/session_core/console.rs:426) — **整行期間的 fail-safe 未完成。**程式只在處理輸入時讀取當下 `active`，而 raw 前綴已立即寫入 WAL。例：human 先輸入 `abc`、reader 隨後看到 `Password:`、human 再輸入 `def\r`；裁決要求整行 `abcdef` 只記長度 META，實際 `abc` 已作一般 TX 記錄。若提示在兩次輸入之間出現又被 CR 清除，後續輸入也不會被鎖定。現有 [測試](crates/sw-daemon/tests/issue311_secret_echo.rs:2519) 本身就先送未換行的 `before-312`，並期待它留在 WAL。— **修法：**按輸入者追蹤整行與該行期間的提示世代，避免在整行分類確定前不可逆地發布可能需遮罩的前綴；補 raw／line-buffer、提示出現後又消失及斷線的受控測試。

2. **V051-37fe9ae-2 [High]** [uart_bridge.rs:462](crates/sw-core/src/uart_bridge.rs:462) — **提交前緩衝資料仍可撤銷 human echo 遮罩。**例：secret 是 `secret`，提交前 OS RX buffer 已有 `s\r\n`，之後才是 DUT 回顯 `secret\r\n`。reader 把 `s` 留為未決前綴，遇到 CR 時遮掉該前綴、清空 secret 並停用 matcher；真正回顯隨後明文發布至 WAL、console 等出口。現有測試只緩衝了沒有前綴的 CR/LF。— **修法：**行終止符不得因遇到未決前綴而撤銷依內容遮罩；補同一 chunk 與跨 chunk 的「前綴＋CR/LF＋真正回顯」受控測試，檢查所有出口。

3. **V051-37fe9ae-3 [Medium]** [console.rs:791](crates/sw-core/src/session_core/console.rs:791) — line-buffer 對整個輸入 chunk 套用同一個 echo 過濾。例：提示 active 時一次收到 `secret\rstatus\r`，第一行應遮罩，第二行 `status` 已在 secret 換行之後，應正常本地回顯；實際只輸出兩個 CR，`status` 也被隱藏。— **修法：**依行界線分段處理 echo，只過濾被鎖定為 secret 的行；補單一 chunk 含 secret 與後續一般命令的測試。

4. **V051-37fe9ae-4 [High]** [issue311_secret_echo.rs:2465](crates/sw-daemon/tests/issue311_secret_echo.rs:2465) — **要求的各出口測試未有效完成。**共用 helper 對 WAL 原始 JSON 搜尋明文，但 payload 以 base64 儲存；例如 [未完成密碼測試](crates/sw-daemon/tests/issue311_secret_echo.rs:2907) 中，即使 `partial-312` 進入 WAL payload，這個搜尋仍可通過。此外，部分呼叫沒有設定 event probe，空 event tail 也會通過。該測試把 fixture 從 `abc` 改為 `partial-312`，原本的 `!wal_text.contains("abc")` 卻仍查舊值。實作者的 SPEC-GAPS 未記錄此缺口。— **修法：**逐筆解碼 WAL payload 後檢查實際 secret；每個要求 event 驗證的案例設定可觸發的 probe，並證明 event 確實到達後再檢查內容；更新過期的 fixture 斷言，對各出口做洩漏突變確認測試轉紅。

## 既有碼待辦（不計入結論）

- 沿用前輪記錄：`ATTACHED` 下 line-buffer 受既有命令佇列只於 `READY` 出列的限制；一般 line-buffer 命令可由既有 `command.get` 路徑讀取。
- `echo_paced::wait_regex_full_window_when_trimmed` 的既有競態本輪未重跑。

## 對裁決的異議（不計入結論）

沿用前輪靜態疑慮：若與 secret 相同的無關資料先完整命中，或登入結束後才收到真正回顯，已停用的遮罩可能無法保護該回顯。本輪未實跑重現。

結論：需修正