## A 前輪 finding 核對

HEAD 為 `2c5eb6b4`，工作樹乾淨。增量僅一個提交，無 merge；未找到 `p8-fix-responses.md`。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-7bb9935-1 | High | 已修 `4fca6390` | 0 | 原認領與單次在途守衛保留。 |
| P8-7bb9935-2 | Medium | 已修 `4fca6390` | 0 | 退休 drain 未收斂時不另開的守衛保留。 |
| P8-7bb9935-3 | Medium | 已修 `4fca6390` | 0 | bind／clear 不繼承舊 release 期限的修正保留。 |
| P8-7bb9935-4 | Medium | 已修 `424aee24` | 0 | 原回歸保留；裁決紀錄缺口仍待補。 |
| P8-7bb9935-5 | Low | 已修 `354a96e2` | 0 | 出生即接管的修正未撤銷。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 沿用前輪裁決。 |
| P8-591d2ff-1 | Low | 未修 | 33 | DF-27 固定要求 APPROVED 的斷言仍在。 |
| P8-0896933-1 | Low | 未修 | 25 | 對應驗收報告未變。 |
| P8-c89cd78-1 | Low | 未修 | 23 | catalog 對應部分未變。 |
| P8-6a679fb-4 | Low | 已修 `354a96e2` | 0 | 送交失敗 fallback 與可見性修正保留。 |
| P8-03333f7-2 | Low | 已修 `354a96e2` | 0 | parked 認領條件保留。 |
| P8-03333f7-3 | Low | 已修 `354a96e2` | 0 | delivered 計數仍限 test-fakes。 |
| P8-7e34afb-1 | Low | 已修 `354a96e2` | 0 | PROBE 鉤子的鎖外呼叫未變。 |
| P8-566d90b-5 | Low | 已修 `354a96e2` | 0 | shutdown／退休 bridge 修正保留。 |
| P8-c92ef3e-1 | Low | 已修 `354a96e2` | 0 | recover 與舊期限通知修正保留。 |
| P8-a07fdfb-1 | High | 已修 `02bd822c` | 0 | 不可撤銷的裝置取消守衛保留。 |
| P8-add3ca0-1 | High | 已修 `2c5eb6b4` | 0 | 原滿載情境已有保留義務、退避重送及正式 RPC 回歸；本次新增問題另列 B。 |

前輪未逐列提供的 37 項已修 finding，以及 P8-566d90b-1、P8-d7af8df-1／-2，沿用既有結案證據；本輪 diff 未撤銷其修正。

## B 新發現

以下為程式路徑可達的具體交錯；本次未完成執行探針。

**P8-2c5eb6b-1 [High] [session_manager.rs:5276](crates/sw-core/src/session_manager.rs:5276) — shutdown 凍結會直接刪除既有 release 完成義務。**

- **輸入：**parked 關閉逾時、release 已回 DRAIN_STALLED；接著要求 daemon.stop，於 admission 進入 Permanent 後才收到成功關閉回條。
- **預期：**既有 release 完成 RELEASED、清除 stalled 並持久化。Python `_finish_attach_op` 不看交接凍結；既有 `complete_stalled_release_under_each_freeze_state` 亦要求此行為。
- **實際：**送達者把 Permanent 下的義務列為 stale 並移除，完全不送 `CompleteStalledRelease`。正常停止序確實先呼叫 `shutdown_freeze()`；後續 Shutdown 無法補回首次 release 的持久化結果，重啟可能重新 attach。
- **確切修法：**讓既有 release 收尾在 Open／Pending／Permanent 都可送達；只有 session／作業身分失效才取消。新增經 manager／RPC、跨 shutdown 凍結的回歸，不能只直接呼叫 core。

**P8-2c5eb6b-2 [High] [session_manager.rs:5305](crates/sw-core/src/session_manager.rs:5305) — 鎖外送達使用過期快照，可能 release 改綁後的新裝置。**

- **輸入：**舊裝置 A 的遲到完成義務已到期；送達者取得 `due` 快照後暫停。另一執行緒完成同一 session 改綁至 B，再恢復送達者。
- **預期：**A 的舊完成請求不得把 B 轉成 RELEASED。
- **實際：**舊事件仍送往同一 actor，payload 只有 source／reason。actor 在 `session_core.rs:2571` 無條件執行當前 profile 的 release；送達後才做的 `same_session` 檢查已無法阻止副作用，因此 B 被 release 並持久化。
- **確切修法：**完成事件攜帶原 by-id 與可失效的作業／綁定世代，由 actor 在套用 release 前核對；bind 必須使舊完成事件失效。補入「取得重送快照後完成改綁，再送達」的確定性交錯回歸。

**P8-2c5eb6b-3 [Medium] [session_manager.rs:6036](crates/sw-core/src/session_manager.rs:6036) — 後續 release 等待重送沒有期限。**

- **輸入：**首次 release 已逾時，成功關閉後 safety lane 持續滿載；此時再次呼叫 device.release。
- **預期：**沿用首次期限回 DRAIN_STALLED，同時保留背景完成義務。
- **實際：**進入 `wait_late_release_enqueued()`，每 50 ms 重等，但沒有絕對期限或錯誤回傳；外層 route timeout 尚未開始生效。mailbox 不恢復就一直占住 RPC handler。
- **確切修法：**保存並傳遞首次 release 期限；期限到即回 DRAIN_STALLED，背景重送繼續。新增在關閉回條之後才發出第二次 release、且持續維持滿載的回歸。

驗證：L1 鎖新增 **321 列、刪除 0 列**，selector 未變；新滿載回歸已登記。`regen_lock.py --check --base 926aec8c` 與 `git diff --check` 通過。定向 Cargo 測試因指定 target 的 `.cargo-build-lock` 回報唯讀檔案系統而未啟動，不能宣稱本輪測試通過。

## 既有碼待辦（不計入結論）

- 沿用前輪：無 release 介入時，parked 關閉期間重插可能被舊拔除事件覆蓋，最後停在 DETACHED。
- 保管開埠途中 clear 收尾為 DETACHED／CLEARED 的既有差異。
- `wp510r_r3_p8_566d90b_clear_completes_when_new_open_fails_with_retired_drain` 預期值改寫的裁決紀錄仍待補齊。
- 整包基線由編排者另跑。本次未跑完整 pytest、Docker／bundle，未接觸 production daemon、指定設定／狀態目錄或實體 UART；未修改檔案，結束時 HEAD 與工作樹狀態未變。

## 對裁決的異議（不計入結論）

無。

結論：需修正