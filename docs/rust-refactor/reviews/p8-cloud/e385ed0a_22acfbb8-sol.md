> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD：`22acfbb8`，工作樹乾淨。本輪只有一筆提交，已逐一檢查全部差異，無 merge。未找到 `p8-fix-responses.md`；已核對本輪修正報告、Python 基線及相關裁決。

既有已修項沿用前輪結案證據，並核對提交沿革及相關定向回歸。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-7bb9935-1 | High | 已修 `4fca6390` | 0 | 認領與單次在途回歸通過。 |
| P8-7bb9935-2 | Medium | 已修 `4fca6390` | 0 | 退休 drain 收斂回歸通過。 |
| P8-7bb9935-3 | Medium | 已修 `4fca6390` | 0 | bind／clear 首次期限回歸通過。 |
| P8-7bb9935-4 | Medium | 已修 `424aee24` | 0 | 保管路徑 clear／拔除回歸通過。 |
| P8-7bb9935-5 | Low | 已修 `354a96e2` | 0 | 出生即接管修正保留。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 已核對 s2 裁決；真實 Held handle 的 panic 收尾回歸通過。 |
| P8-591d2ff-1 | Low | 未修 | 48 | DF-27 固定要求 APPROVED 的斷言仍在。 |
| P8-0896933-1 | Low | 未修 | 40 | DF-25 報告未修改，沿用前輪統計及時間誤植待辦。 |
| P8-c89cd78-1 | Low | 未修 | 38 | DF-26 固定要求 APPROVED 的斷言仍在。 |
| P8-6a679fb-4 | Low | 已修 `354a96e2` | 0 | 回收者停止後 fallback 保留。 |
| P8-03333f7-2 | Low | 已修 `354a96e2` | 0 | parked 認領條件保留。 |
| P8-03333f7-3 | Low | 已修 `354a96e2` | 0 | delivered 計數邊界未改。 |
| P8-7e34afb-1 | Low | 已修 `354a96e2` | 0 | PROBE 鉤子鎖外呼叫保留。 |
| P8-566d90b-5 | Low | 已修 `354a96e2` | 0 | shutdown／退休 bridge 回歸通過。 |
| P8-c92ef3e-1 | Low | 已修 `354a96e2` | 0 | recover 與過期通知回歸通過。 |
| P8-a07fdfb-1 | High | 已修 `02bd822c` | 0 | 不可撤銷的裝置取消回歸通過。 |
| P8-add3ca0-1 | High | 已修 `c3f78960` | 0 | parked close 遲到收尾回歸通過。 |
| P8-2c5eb6b-1 | High | 已修 `c3f78960` | 0 | 跨凍結遲到完成回歸通過。 |
| P8-2c5eb6b-2 | High | 已修 `c3f78960` | 0 | 改綁後拒絕舊完成回歸通過。 |
| P8-2c5eb6b-3 | Medium | 已修 `c3f78960` | 0 | 首次期限共用回歸通過。 |
| P8-6ca9bc8-1 | Medium | 已修 `c3f78960` | 0 | 原路徑欄位回歸通過。 |
| P8-0c50830-1 | High | 已修 `c3f78960` | 0 | release 無前置快照查詢的路徑保留。 |
| P8-990a04b-1 | High | 已修 `c3f78960` | 0 | 正式 RPC 交錯回歸通過。 |
| P8-398b85f-1 | High | 已修 `e35edc09` | 0 | W6 歷史 ActorId 隔離回歸通過。 |
| P8-21482ec-1 | High | 已修 `e35edc09` | 0 | W6 owner／回收身分回歸通過。 |
| P8-e35edc0-1 | High | 已修 `5e357c76` | 0 | 指定 actor 的身分斷言保留。 |
| P8-6536d16-1 | High | 已修 `9ab75ab0` | 0 | shutdown 共用期限與阻塞後收尾回歸通過。 |
| P8-9ab75ab-1 | Medium | 已修 `f661291b` | 0 | 逾時不空轉及遲到回條回歸通過。 |
| P8-849e605-1 | High | 已修 `0b37df5d` | 0 | Reaper 關閉屏障及成功／遲到／失敗回歸通過。 |
| P8-e385ed0-1 | High | 已修 `22acfbb8` | 0 | 新增兩支正式 RPC 回歸通過：恢復 RELEASED、清 drain 欄位及鏡像、重新落盤。 |

前輪未逐列提供的 37 項已修 finding，以及 P8-566d90b-1、P8-d7af8df-1／-2，沿用前輪結案證據，不補造編號。

## B 新發現

**P8-22acfbb-1 [High]** [crates/sw-core/src/session_core.rs:3648](crates/sw-core/src/session_core.rs:3648) — **token 換成退休 epoch 時短暫發布無等待狀態，shutdown 可漏掉尚未關閉的 worker。**

本輪新增的 `publish_retired_drains()` 在移除唯一 custody token 後，先發布 `retired_drains_pending=false`；直到後續 `retire_opened_bridge()` 才重新發布 true。兩次各自取得 fence 鎖，對另一執行緒並非原子替換。

具體可達交錯：

1. COM0 已 RELEASED；`device.attach` 停在 Bridged，`session.bind` 已取消該 token、仍在期限內等待。
2. shutdown 的 `GetState` 先取得 RELEASED。
3. actor 處理 `BridgeOpened`，執行新增的第 3648 行，發布 false。
4. shutdown 此時讀到 false，於 [session_manager.rs:7684](crates/sw-core/src/session_manager.rs:7684) 跳過該 session，沒有送 Shutdown，也沒有登記回條或 actor 退出等待。
5. actor 繼續加入退休 epoch、執行 `AdoptBridge`，於 [session_manager.rs:5518](crates/sw-core/src/session_manager.rs:5518) 移除帳本紀錄，再開始非同步關閉；讓 close 持續阻塞。
6. shutdown phase2 隨後才收集帳本，已找不到該紀錄。一般 `register_plain_open` 也沒有對應的 `inflight` 項目可補收，因此這個未關閉 worker 不會出現在未排空結果中。

**預期：**關閉回條前持續等待，期限到則回報未排空。  
**實際依上述排程：**shutdown 漏列該 session，可在 handle 仍未關閉時完成停止判定，違反排空安全條件。

這是依共享旗標與正式呼叫路徑確認的靜態交錯；本輪未以實測命中這個短暫窗口。現有新增回歸只觀察 close 暫停時已重新變為 true 的鏡像，無法捕捉中間的 false。

**確切修法：**token→epoch 替換期間保持鏡像為 true；移除第 3648 行的中間發布，待 `retire_opened_bridge()` 完成替換後統一發布，成功回條結算時才清除。補入可控排程的 shutdown 回歸，驗證替換窗口及 close 阻塞期間均不得漏列排空目標。

本輪定向驗證：

| 驗證 | 結果 |
|---|---:|
| `sw-core custody wp510r_` | 12 passed |
| `sw-core drain_release wp510r_` | 71 passed |
| `sw-core session_manager wp510r_` | 30 passed |
| `sw-daemon dispatch wp510r_` | 36 passed |
| `sw-daemon shutdown_order` | 11 passed |
| L1 結構比對 | 僅新增兩個節點；原欄位、selector、節點順序保留 |
| 契約檢查 | `ok=true`，0 findings |
| Policy check | 25 PASS／0 FAIL／2 WARN；非 PR 上下文 |
| 增量 `git diff --check` | 通過 |

未跑完整 pytest、Docker／bundle；未接觸 production daemon、指定設定／狀態目錄或實體 UART。建置產物位於指定 `/tmp` target，未修改來源檔，驗證後工作樹仍乾淨。

## 既有碼待辦（不計入結論）

沿用前輪，未另行結案：

- 完整 shutdown 與 parked close 遲到回條的停止順序。
- 無 release 介入時，parked 關閉期間重插被舊拔除事件覆蓋的情境。
- 保管開埠途中 clear 的 DETACHED／CLEARED 差異。

## 對裁決的異議（不計入結論）

無。

結論：需修正