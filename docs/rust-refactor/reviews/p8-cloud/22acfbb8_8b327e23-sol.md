> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD：`8b327e23`。已逐一檢查 `22acfbb8..8b327e23` 的唯一提交及全部六個檔案差異，無 merge。未找到 `p8-fix-responses.md`；已核對本輪修正報告、相關裁決與 Python 基線。

既有已修項沿用前輪結案證據，並核對修正提交均在 HEAD 沿革內；相關定向回歸全部通過。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-7bb9935-1 | High | 已修 `4fca6390` | 0 | 認領與單次在途回歸通過。 |
| P8-7bb9935-2 | Medium | 已修 `4fca6390` | 0 | 退休 drain 收斂回歸通過。 |
| P8-7bb9935-3 | Medium | 已修 `4fca6390` | 0 | bind／clear 首次期限回歸通過。 |
| P8-7bb9935-4 | Medium | 已修 `424aee24` | 0 | 保管路徑 clear／拔除回歸通過。 |
| P8-7bb9935-5 | Low | 已修 `354a96e2` | 0 | 出生即接管修正保留。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 已核對 s2 裁決原文；Held handle 的 panic 收尾回歸通過。 |
| P8-591d2ff-1 | Low | 未修 | 49 | DF-27 固定要求 APPROVED 的斷言仍在。 |
| P8-0896933-1 | Low | 未修 | 41 | DF-25 報告未修改，統計及時間誤植待辦沿用。 |
| P8-c89cd78-1 | Low | 未修 | 39 | DF-26 固定要求 APPROVED 的斷言仍在。 |
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
| P8-e385ed0-1 | High | 已修 `22acfbb8` | 0 | 恢復 RELEASED、清 drain 欄位與鏡像、重新落盤回歸通過。 |
| P8-22acfbb-1 | High | 已修 `8b327e23` | 0 | 替換窗口保持待排空鏡像；新增成功／逾時 shutdown 回歸均通過。 |

前輪未逐列提供的 37 項已修 finding，以及 P8-566d90b-1、P8-d7af8df-1／-2，沿用前輪結案證據，不補造編號。

## B 新發現

NO FINDINGS。

本項接線與驗收完整：

- [session_core.rs:3658](crates/sw-core/src/session_core.rs:3658) 移除 token 後不再發布 false，後續必經 `retire_opened_bridge()` 加入 epoch 並發布。首次期限與 PendingOpenStop 收尾邏輯保留。
- [dispatch.rs:10464](crates/sw-daemon/tests/dispatch.rs:10464) 固定重現「shutdown 已取得 RELEASED、actor 正在替換 token」的排程，並確認 AdoptBridge 已移除帳本，避免 Phase 2 靠帳本補收而掩蓋漏列。
- 新回歸驗證替換窗口與 close 阻塞期間鏡像皆為 true；成功時列為已排空，逾時時列為未排空，遲到成功回條仍清除鏡像。新增 hook 均限於 `test-fakes`，未改既有測試斷言或放寬判準。

本輪實測：

| 驗證 | 結果 |
|---|---:|
| `sw-core custody wp510r_` | 12 passed |
| `sw-core drain_release wp510r_` | 71 passed |
| `sw-core session_manager wp510r_` | 30 passed |
| `sw-daemon dispatch wp510r_` | 38 passed，含新增兩支 |
| `sw-daemon shutdown_order` | 11 passed |
| L1 結構比對 | 僅新增兩個節點；原欄位、selector、節點順序保留 |
| 契約檢查 | `ok=true`，0 findings |
| cfg 邊界檢查 | 通過 |
| Policy check | 25 PASS／0 FAIL／2 WARN；非 PR 上下文 |
| 增量 `git diff --check` | 通過 |

未重演修改前紅燈；本輪獨立實跑修正後回歸，並以程式核對原缺陷會命中新斷言。未跑完整 pytest、Docker／bundle 或硬體測試；未接觸 production daemon、指定設定／狀態目錄或實體 UART。建置產物使用指定 `/tmp` target，未修改來源檔，驗證後工作樹乾淨。本結論限於此次增量審查，不代表完整出口驗收。

## 既有碼待辦（不計入結論）

沿用前輪，未另行結案：

- 完整 shutdown 與 parked close 遲到回條的停止順序。
- 無 release 介入時，parked 關閉期間重插被舊拔除事件覆蓋的情境。
- 保管開埠途中 clear 的 DETACHED／CLEARED 差異。

## 對裁決的異議（不計入結論）

無。

結論：可放行