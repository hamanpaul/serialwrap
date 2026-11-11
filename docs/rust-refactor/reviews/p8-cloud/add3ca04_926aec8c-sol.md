> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD 為 `926aec8c`，工作樹乾淨。已逐一檢視本輪兩個提交，無 merge；未找到 `p8-fix-responses.md`。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-7bb9935-1 | High | 已修 `4fca6390` | 0 | 原認領與單次在途守衛保留。 |
| P8-7bb9935-2 | Medium | 已修 `4fca6390` | 0 | 退休 drain 未收斂時不另開的守衛保留。 |
| P8-7bb9935-3 | Medium | 已修 `4fca6390` | 0 | bind／clear 不繼承舊 release 期限的修正保留。 |
| P8-7bb9935-4 | Medium | 已修 `424aee24` | 0 | 原回歸保留；裁決紀錄缺口仍列待辦。 |
| P8-7bb9935-5 | Low | 已修 `354a96e2` | 0 | 出生即接管的凍結回歸通過。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 沿用前輪裁決，不新增 panic 超限退出行為。 |
| P8-591d2ff-1 | Low | 未修 | 32 | DF-27 固定要求 APPROVED 的斷言仍在。 |
| P8-0896933-1 | Low | 未修 | 24 | 對應驗收報告未變。 |
| P8-c89cd78-1 | Low | 未修 | 22 | catalog 對應部分未變。 |
| P8-6a679fb-4 | Low | 已修 `354a96e2` | 0 | 原送交失敗 fallback 與可見性修正保留。 |
| P8-03333f7-2 | Low | 已修 `354a96e2` | 0 | parked 認領條件未撤銷。 |
| P8-03333f7-3 | Low | 已修 `354a96e2` | 0 | delivered 計數仍限於 test-fakes。 |
| P8-7e34afb-1 | Low | 已修 `354a96e2` | 0 | PROBE 鉤子的鎖外呼叫未變。 |
| P8-566d90b-5 | Low | 已修 `354a96e2` | 0 | 兩個 shutdown／退休 bridge 回歸通過。 |
| P8-c92ef3e-1 | Low | 已修 `354a96e2` | 0 | recover 與舊期限通知兩案通過。 |
| P8-a07fdfb-1 | High | 已修 `02bd822c` | 0 | 不可撤銷的裝置取消守衛保留。 |
| P8-add3ca0-1 | High | 未修 | 1 | `e1c39064` 修好一般遲到回條，但 mailbox 滿載時仍遺失 release 義務，實測如下。 |

前輪未逐列提供的 37 項已修 finding，以及 P8-566d90b-1、P8-d7af8df-1／-2，沿用既有結案證據；本輪未撤銷其修正。

**P8-add3ca0-1 未完成的情境：**[session_manager.rs:1095](crates/sw-core/src/session_manager.rs:1095) 先移除完成義務，再於 [1108 行](crates/sw-core/src/session_manager.rs:1108) 對有界 safety mailbox 執行一次 `try_send`；失敗只告警，沒有保留或重送。

- **輸入：**沿用新增測試的 parked 關閉逾時前置；暫停 actor 處理查詢，以 256 筆重複 release 填滿 safety lane，再放行關閉回條，最後恢復 actor。探針只縮短公開查詢等待時間，未修改 release 的三秒期限。
- **預期：**關閉成功後最終完成 RELEASED、清除 stalled 並持久化。
- **實際：**探針 `review_late_close_safety_full` 失敗，輸出 `lost_completion=true state=DETACHED stalled=true persisted=false`，且收到「遲到 release 收尾未能送達」警告。
- **確切修法：**保留完成義務至成功入列；`Full` 時在 manager lock 外可靠重送，保留 token／session 身分核對，不能只告警後丟棄。補入 mailbox 滿載後恢復的回歸，斷言最終 RELEASED、stalled 清除及持久化。

此為前輪 release 義務遺失尚未完整修復，沿用原編號，不重複列入 B。

## B 新發現

NO FINDINGS。

本輪正式接線與新增五個 L1 節點已核對；鎖新增 **5 列、刪除 0 列**，既有測試斷言未削弱。

驗證：**13 個不同定向測試通過，另 1 個上述探針失敗**；未啟用 test-fakes 的 `cargo check -p sw-core`、`regen_lock.py --check --base add3ca04` 與 `git diff --check` 通過。探針由記憶體來源經 stdin 編譯，建置產物限指定 target。

## 既有碼待辦（不計入結論）

- 沿用前輪：沒有 release 介入時，parked 關閉期間重插可能被舊拔除事件覆蓋，最後停在 DETACHED；本輪未處理此情境。
- 保管開埠途中 clear 收尾為 DETACHED／CLEARED 的既有差異。
- `wp510r_r3_p8_566d90b_clear_completes_when_new_open_fails_with_retired_drain` 預期值改寫的裁決紀錄仍待補齊。
- 整包完整基線由編排者另跑，本次定向結果不替代整包驗收。

## 對裁決的異議（不計入結論）

無。

全程未修改來源、測試或文件；建置／測試未達 15 分鐘。未跑完整 pytest、Docker／bundle，未接觸 production daemon、指定設定／狀態目錄或實體 UART。結束時 HEAD 未變、工作樹乾淨。

結論：需修正