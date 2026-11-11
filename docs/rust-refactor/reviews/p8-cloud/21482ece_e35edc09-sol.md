## A 前輪 finding 核對

HEAD 為 `e35edc09`，工作樹乾淨。已審查 `21482ece..e35edc09` 的唯一提交；未找到 `p8-fix-responses.md`。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-7bb9935-1 | High | 已修 `4fca6390` | 0 | 認領與單次在途守衛保留，相關回歸通過。 |
| P8-7bb9935-2 | Medium | 已修 `4fca6390` | 0 | 退休 drain 收斂前不另開的守衛保留。 |
| P8-7bb9935-3 | Medium | 已修 `4fca6390` | 0 | bind／clear 不繼承舊期限的回歸通過。 |
| P8-7bb9935-4 | Medium | 已修 `424aee24` | 0 | 保管路徑 clear／拔除回歸通過；原裁決仍適用。 |
| P8-7bb9935-5 | Low | 已修 `354a96e2` | 0 | 出生即接管改用同一 ActorId，回歸通過。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 已核對遠端 `rulings/s2.md` 的「panic 超限退出」裁決。 |
| P8-591d2ff-1 | Low | 未修 | 40 | DF-27 固定要求 APPROVED 的斷言未變。 |
| P8-0896933-1 | Low | 未修 | 32 | DF-25 接線與驗收報告本輪未變。 |
| P8-c89cd78-1 | Low | 未修 | 30 | DF-26 固定要求 APPROVED 的斷言未變。 |
| P8-6a679fb-4 | Low | 已修 `354a96e2` | 0 | 送交失敗 fallback 回歸通過。 |
| P8-03333f7-2 | Low | 已修 `354a96e2` | 0 | parked 認領條件保留。 |
| P8-03333f7-3 | Low | 已修 `354a96e2` | 0 | delivered 計數仍限 test-fakes。 |
| P8-7e34afb-1 | Low | 已修 `354a96e2` | 0 | PROBE 鉤子的鎖外呼叫保留。 |
| P8-566d90b-5 | Low | 已修 `354a96e2` | 0 | shutdown／退休 bridge 回歸通過。 |
| P8-c92ef3e-1 | Low | 已修 `354a96e2` | 0 | recover 與舊期限通知回歸通過。 |
| P8-a07fdfb-1 | High | 已修 `02bd822c` | 0 | 不可撤銷的裝置取消守衛及回歸保留。 |
| P8-add3ca0-1 | High | 已修 `c3f78960` | 0 | actor 完成義務及遲到關閉回歸通過。 |
| P8-2c5eb6b-1 | High | 已修 `c3f78960` | 0 | 跨凍結的遲到完成回歸通過。 |
| P8-2c5eb6b-2 | High | 已修 `c3f78960` | 0 | 改綁後不被舊完成重新釋放的回歸通過。 |
| P8-2c5eb6b-3 | Medium | 已修 `c3f78960` | 0 | 首次期限共用回歸通過。 |
| P8-6ca9bc8-1 | Medium | 已修 `c3f78960` | 0 | session 欄位與 timeout epoch 回歸保留。 |
| P8-0c50830-1 | High | 已修 `c3f78960` | 0 | release 無前置快照查詢，期限回歸通過。 |
| P8-990a04b-1 | High | 已修 `c3f78960` | 0 | 正式 RPC 暫停／關閉交錯回歸通過。 |
| P8-398b85f-1 | High | 已修 `e35edc09` | 0 | 正式 W6 offer、一般開埠及停止統一使用不可變 ActorId；歷史退出不再命中新 handle。 |
| P8-21482ec-1 | High | 已修 `e35edc09` | 0 | 正式 W6 的 Held owner 與 ExitReclaim 身分一致；自身退出回收、再次開埠及停止回歸通過。 |

前輪未逐列提供的 37 項已修 finding，以及 P8-566d90b-1、P8-d7af8df-1／-2，沿用既有結案證據；未發現本輪撤銷其產品修正。

## B 新發現

**P8-e35edc0-1 [High] [crates/sw-core/tests/custody.rs:1893](crates/sw-core/tests/custody.rs:1893) — 三支既有凍結測試失去「認領給指定 session」的身分斷言。**

同檔 `1607`、`1749`、`1893` 都改成從待驗紀錄的 owner 取得 `actor`，再拿它驗證 taker；owner 的比較也因此成為自我比較。前版明確要求 `sid == full_sid`，本版只要求 owner 與 taker 彼此一致，無法拒絕兩者同時指向錯誤 actor。

具體情境：`bind COM0→B3` 的指定 actor 為 A，待驗快照卻回報 owner＝B、taker＝B。預期身分斷言失敗；實際本輪斷言接受。

已透過 stdin 編譯探針，只替換測試中的快照副本，執行完整 `test_parked_continuation_bind_rebind_away_settles_claimed_close`：

```text
target=ActorId(1), snapshot_owner=ActorId(3)
independent_target_check=false
現行斷言：1 passed
恢復指定 session 身分斷言：1 failed
```

這是既有凍結測試的判準削弱，依本次嚴重度定義列 High。

**確切修法：**三處均以 `m.mgr.actor_id_for_test(&full_sid)` 獨立取得預期 ActorId，分別精確比對 owner 與 `Taken` 的 taker；保留其他斷言。以錯誤 actor 快照確認恢復後確實轉紅。

本輪定向驗證：

| 範圍 | 結果 |
|---|---|
| `sw-core / custody` | 61 passed |
| `sw-core / drain_release / wp510` | 83 passed |
| `sw-core / session_manager / wp510` | 65 passed |
| placeholder B1、profile rematerialize | 各 1 passed |
| `sw-daemon / dispatch / wp510` | 77 passed |
| L1 凍結鎖 | add-only，新增 1 節點 |
| 增量 `git diff --check` | 通過 |

未修改來源或工作樹檔案；探針與建置產物均在指定 `/tmp` target。未跑完整 pytest、Docker／bundle，未接觸 production daemon、指定設定／狀態目錄或實體 UART。

## 既有碼待辦（不計入結論）

沿用前輪，本輪未擴大驗證：

- 完整 shutdown 與 parked close 遲到回條的停止順序。
- 無 release 介入時，parked 關閉期間重插被舊拔除事件覆蓋的情境。
- 保管開埠途中 clear 的 DETACHED／CLEARED 差異。

## 對裁決的異議（不計入結論）

無。

結論：需修正