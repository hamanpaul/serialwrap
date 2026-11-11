## A 前輪 finding 核對

HEAD 為 `6ca9bc84`，工作樹乾淨。本輪只有一個提交，無 merge；未找到 `p8-fix-responses.md`。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-7bb9935-1 | High | 已修 `4fca6390` | 0 | 原認領與單次在途守衛保留。 |
| P8-7bb9935-2 | Medium | 已修 `4fca6390` | 0 | 退休 drain 未收斂時不另開的守衛保留。 |
| P8-7bb9935-3 | Medium | 已修 `4fca6390` | 0 | bind／clear 不繼承舊 release 期限的修正保留。 |
| P8-7bb9935-4 | Medium | 已修 `424aee24` | 0 | 原回歸保留；裁決紀錄缺口仍待補。 |
| P8-7bb9935-5 | Low | 已修 `354a96e2` | 0 | 出生即接管的修正保留。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 沿用前輪裁決。 |
| P8-591d2ff-1 | Low | 未修 | 34 | DF-27 固定要求 APPROVED 的斷言仍在。 |
| P8-0896933-1 | Low | 未修 | 26 | 對應驗收報告未變。 |
| P8-c89cd78-1 | Low | 未修 | 24 | DF-26 固定要求 APPROVED 的斷言仍在。 |
| P8-6a679fb-4 | Low | 已修 `354a96e2` | 0 | 送交失敗 fallback 與可見性修正保留。 |
| P8-03333f7-2 | Low | 已修 `354a96e2` | 0 | parked 認領條件保留。 |
| P8-03333f7-3 | Low | 已修 `354a96e2` | 0 | delivered 計數仍限 test-fakes。 |
| P8-7e34afb-1 | Low | 已修 `354a96e2` | 0 | PROBE 鉤子的鎖外呼叫保留。 |
| P8-566d90b-5 | Low | 已修 `354a96e2` | 0 | shutdown／退休 bridge 修正保留。 |
| P8-c92ef3e-1 | Low | 已修 `354a96e2` | 0 | recover 與舊期限通知修正保留。 |
| P8-a07fdfb-1 | High | 已修 `02bd822c` | 0 | 不可撤銷的裝置取消守衛保留。 |
| P8-add3ca0-1 | High | 已修 `2c5eb6b4` | 0 | 滿載時保留義務與退避重送未撤銷。 |
| P8-2c5eb6b-1 | High | 已修 `6ca9bc84` | 0 | Permanent 不再直接刪除義務，新增經 RPC、跨凍結的回歸；完整停止序另見既有碼待辦。 |
| P8-2c5eb6b-2 | High | 已修 `6ca9bc84` | 0 | 事件攜帶 by-id／綁定世代，actor 套用前核對；新增快照後改綁回歸。 |
| P8-2c5eb6b-3 | Medium | 已修 `6ca9bc84` | 0 | 原「首次已回 DRAIN_STALLED」情境會沿用期限及快取回覆；未快取分支另列 B。 |

前輪未逐列提供的 37 項已修 finding，以及 P8-566d90b-1、P8-d7af8df-1／-2，沿用既有結案證據；本輪差異未撤銷其修正。

## B 新發現

**P8-6ca9bc8-1 [Medium] [session_manager.rs:6134](crates/sw-core/src/session_manager.rs:6134) — 期限已過的 fallback 仍等待 actor，查詢失敗後回傳不完整 session。**

- **具體輸入：**首次 release 等待 parked 關閉；actor 暫停後首次期限到，`MarkAttachStalled` 已入列、`stalled=true`，但尚未取得 actor 回覆，因此 `stalled_reply=None`。接著關閉成功，完成義務因 safety lane 滿載等待重送，此時再次呼叫 `device.release`。
- **預期：**立即沿用首次期限回 DRAIN_STALLED，保留完整 session 與原 timeout epoch，背景重送繼續。
- **實際：**`wait_late_release_enqueued()` 雖判定期限已過，卻轉入 fallback 的 `session_public()`；該函式仍透過 actor `GetState` 等待預設 **5 秒**。actor 持續暫停時，查詢逾時後 `unwrap_or_default()` 產生空字典，最後回覆的 `session` 僅含 `state`、`drain_stalled`、`drain_timeout_epoch`，缺少 session 身分等正常欄位，epoch 也被預設為 0。
- **確切修法：**首次等待前保存完整 session 快照及 epoch；期限後的 fallback 僅使用已保存資料，不再查詢 actor。新增「首次 stalled 回覆尚未快取＋actor 暫停＋完成 lane 滿載」的正式 RPC 回歸，驗證回覆期限與完整欄位。

上述為程式路徑推導，未完成執行探針。

驗證結果：

- L1 鎖僅新增 **3 列**，未刪除節點、修改 selector 或削弱既有斷言。
- `regen_lock.py --check --base 2c5eb6b4`、增量 `git diff --check`、契約檢查通過。
- Policy：**25 passed、0 failed、2 warnings**（R-19／R-22；非 PR context）。
- 定向 Cargo 測試因指定 target 的 `.cargo-build-lock` 回報唯讀檔案系統而未啟動，不能宣稱測試通過。

## 既有碼待辦（不計入結論）

- **完整關機仍有既有收尾缺口。**首次 release 已 stalled、parked 關閉尚未返回時，真正的停止序會在 [session_manager.rs:7567](crates/sw-core/src/session_manager.rs:7567) 向 RECOVERING actor 送 Shutdown；無 bridge 的 actor 在 [session_core.rs:2168](crates/sw-core/src/session_core.rs:2168) 完成停止並退出。之後才到的關閉回條即使保留送達義務，也已沒有 actor 可以完成 release 持久化。新增測試只呼叫 `shutdown_freeze()`，未涵蓋此交錯。這是未改動的停止序問題，未計入本輪新增 finding。
- 沿用前輪：無 release 介入時，parked 關閉期間重插可能被舊拔除事件覆蓋；保管開埠途中 clear 的 DETACHED／CLEARED 差異；`wp510r_r3_p8_566d90b_clear_completes_when_new_open_fails_with_retired_drain` 預期值改寫的裁決紀錄仍待補。

未修改任何檔案；結束時 HEAD 與工作樹狀態未變。未跑完整 pytest、Docker／bundle，未接觸 production daemon、指定設定／狀態目錄或實體 UART。

## 對裁決的異議（不計入結論）

無。

結論：需修正