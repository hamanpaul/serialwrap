## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | High | 已修 `bd62919` | 0 | lazy expiry 建立回放後，命令停在 `replay_parked`；新增一般命令／reboot 的 Q → B 順序回歸。另見 B-2 的取消收尾缺陷。 |
| 2 | Medium | 已修 `bd62919` | 0 | 先確認 raw owner，樣本須確認 UART TX，並新增 line mode 本地回顯負對照。 |
| 3 | High | 已修 `bd62919` | 0 | release 一律推進 close 世代；晚到的舊世代 `ConsoleConnected` 被拒絕，不登記 console／owner。 |

## B 新發現

### P6-e9b5c61-1 [High] [session_core.rs:5051](crates/sw-core/src/session_core.rs:5051) — 並行 self-test 會遺失 human resume

`2228cc7` 將 self-test 改成可並行的 side op，但仍共用單一 `self_test_suspended` 布林值。

具體情境：c1 持有 human lease，self-test S1 開始後暫停 human；S1 尚未完成，再提交 S2（`strict_human_lock=false`）。S2 的 refresh 發現 owner 已因 suspend 暫時清空，關閉 human lease；接著 `self_test_suspend_begin()` 回傳 false，覆寫 S1 的 true。兩次完成都不再 resume，`suspend_depth` 留在 1，後續按鍵持續 deferred。Python 每次呼叫各自保留旗標，於 `finally` 配對 resume。

此外，[session_core.rs:4258](crates/sw-core/src/session_core.rs:4258) 對 Recover side op 也無條件執行 self-test resume，可能提早解除另一個 probe 的 suspend。

**確切修法：**將 suspended 旗標存入各自的 `PendingOp::SelfTest`，只由該 op 完成時結算；新增兩個重疊 self-test，以及 self-test／recover 交錯完成的回歸。

### P6-e9b5c61-2 [High] [session_core.rs:3760](crates/sw-core/src/session_core.rs:3760) — 取消停放命令後永久占用 pending 額度

`bd62919` 的新跳過路徑直接 `continue`，沒有清除 `mark_running()` 登記的 `in_flight`。`abort_parked_command()` 對已取消命令也同樣略過收尾。

具體情境：依 A-1 建立停放命令 B → `command.cancel(B)` → 回放結算。B 不會送出，但 registry 仍指向 canceled B；[arbiter.rs:461](crates/sw-core/src/arbiter.rs:461) 因此永久多算一筆 pending。後續即使所有命令完成，pending 仍為 1，queue 會提前一格回 `SESSION_QUEUE_FULL`。Python worker 收尾會清除 `_running`。

**確切修法：**停放命令的所有退出路徑都呼叫 registry 結算；利用 `complete()` 保留 canceled 紀錄並清除對應在途指標。新增取消後 pending 歸零、後續容量完整恢復，以及取消後 drain 的回歸。

### P6-e9b5c61-3 [Medium] [cases.yaml:2658](tests/acceptance/cases.yaml:2658) — 主線合併破壞既有案例指紋檢查

`60ab7d2` 改入 `l15-release-inflight.windows_gap_reason`，但兩個切片仍保留凍結前的案例指紋。

已唯讀重算確認：`d6e3b3c` 的兩份指紋檢查均通過；從 `60ab7d2` 起，`test_freeze_case_fingerprints_match_current_cases[2.1/2.2]` 都因該案例失敗。處理紀錄中的「待擁有者裁決」尚非豁免裁決。

**確切修法：**先還原該凍結欄位，將補充說明放入非凍結文件；若須改案例本身，取得逐名核准後同步更新兩份指紋，不得放寬檢查。

### P6-e9b5c61-4 [Medium] [test_l1_judge.py:855](tests/test_l1_judge.py:855) — 合併後 OD-24 檢查與 capability 凍結集合不相容

`60ab7d2` 合入禁止所有 cargo 凍結集合含 ignored 測試的檢查，卻保留 `s22_gate:console_conformance_pty` 的七個 `#[ignore]` 節點；P5 合併另帶入同類節點。

直接執行此唯讀測試函式，確定得到 AssertionError。即使七個 PTY 案例由 `--ignored` 實跑通過，這個靜態檢查仍必紅，無法滿足全套零失敗出口。

**確切修法：**依 OD-24 將這些案例移交實跑 capability gate，保留逐案必要性；既有鎖的移列須逐名取得核准。若要保留特殊 cargo 集合，須先取得擁有者例外裁決，不能直接放寬全域檢查。

驗證：七項對照表靜態測試通過；上述三項整合斷言確定失敗。契約檢查通過，policy check 為 25 pass／0 fail／2 warn。Rust 反例為靜態推導，未建置或實跑 Rust；全程未修改檔案，工作樹乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正