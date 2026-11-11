> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD 為 `add3ca04`，工作樹乾淨。已逐一檢視本輪 4 個提交，無 merge；未找到 `p8-fix-responses.md`。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-7bb9935-1 | High | 已修 `4fca6390` | 0 | 接受條件、recover 認領與開埠種類區分保留；相關回歸通過。 |
| P8-7bb9935-2 | Medium | 已修 `4fca6390` | 0 | retired drain 未收斂時不另開；回歸通過。 |
| P8-7bb9935-3 | Medium | 已修 `4fca6390` | 0 | bind／clear 不繼承舊 release 期限；回歸通過。 |
| P8-7bb9935-4 | Medium | 已修 `424aee24` | 0 | clear、拔除回歸通過；裁決紀錄缺口仍列待辦。 |
| P8-7bb9935-5 | Low | 已修 `354a96e2` | 0 | 啟動 ClaimCustody 在 Pending 延後、Permanent 只關閉；新增回歸通過。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 沿用前輪記載的裁決，不新增 panic 超限退出行為。 |
| P8-591d2ff-1 | Low | 未修 | 31 | DF-27 固定要求 APPROVED 的斷言未變。 |
| P8-0896933-1 | Low | 未修 | 23 | 對應驗收報告未變。 |
| P8-c89cd78-1 | Low | 未修 | 21 | catalog 對應部分未變。 |
| P8-6a679fb-4 | Low | 已修 `354a96e2` | 0 | 送交失敗退回 finish_async；背景執行緒欄位與停止方法改為非公開。 |
| P8-03333f7-2 | Low | 已修 `354a96e2` | 0 | parked 僅在 Held 且 parts 存在時認領。 |
| P8-03333f7-3 | Low | 已修 `354a96e2` | 0 | delivered 計數限於 test-fakes。 |
| P8-7e34afb-1 | Low | 已修 `354a96e2` | 0 | PROBE 鉤子移到鎖外呼叫，註解同步修正。 |
| P8-566d90b-5 | Low | 已修 `354a96e2` | 0 | 兩個指定 shutdown／退休 bridge 回歸已補齊並通過。 |
| P8-c92ef3e-1 | Low | 已修 `354a96e2` | 0 | 已取消保管後 recover、舊期限通知撞上新 release 兩案通過。 |
| P8-a07fdfb-1 | High | 已修 `02bd822c` | 0 | 取消保留與 `b6da0147` 補強仍在；本輪未撤銷相關守衛。 |

前輪未逐列提供的 37 項已修 finding，以及 P8-566d90b-1、P8-d7af8df-1／-2，沿用既有結案證據。

## B 新發現

**P8-add3ca0-1 [High] [session_manager.rs:5772](crates/sw-core/src/session_manager.rs:5772)、[session_manager.rs:5801](crates/sw-core/src/session_manager.rs:5801) — 新增的 parked 關閉等待在逾時後遺失 release 義務。**

- **具體情境：**沿用本輪 DR-2(b) 前置：退休落盤失敗留下 parked handle，bind 落盤失敗使 COM0 已在記憶體綁到該 by_id；拔除流程停在關閉 handle。此時送 `device.release COM0`，讓關閉超過首次 3 秒期限，再放行成功關閉。
- **預期／實際：**期限到回 `DRAIN_STALLED`，晚到成功回條後應完成 RELEASED、清除 stalled 並持久化。實測卻停在 `DETACHED`、`last_error=DEVICE_NOT_FOUND`、`drain_stalled=true`，`released_by`／`released_at` 皆為 null。
- **原因：**新分支只保存 `release_deadline`；逾時後返回，沒有保存 release 的來源、原因及後續完成義務。`close_session_preopened()` 只結算帳本，不會補送 release。
- **證據：**相同終態探針 `review_late_close_final_state` 在 `b6da0147` 通過，在 `add3ca04` 失敗。兩版由記憶體中的來源經 stdin 編譯，未修改檔案。現有 DR-2(b) 僅測期限內關閉，未覆蓋此分支。
- **確切修法：**保留安全等待；以 token 與 session 世代保存首次 release 請求及期限，成功關閉結算時完成尚未履行的 release，即使原 RPC 已逾時。補入超過 3 秒後才關閉的回歸，斷言 RELEASED、stalled 清除、持久化及不自動重開。

驗證：定向回歸 **74 passed**；不啟用 test-fakes 的 `cargo check -p sw-core` 通過。L1 鎖新增 **65 列、刪除 0 列**，`regen_lock.py --check --base b6da0147` 與 `git diff --check` 通過；既有測試斷言未削弱。

## 既有碼待辦（不計入結論）

- 已綁定 session 的 parked handle 拔除關閉期間重插，舊 `update_devices` 隨後送出的拔除事件會覆蓋重插，最後停在 DETACHED。探針在 `b6da0147`、`add3ca04` **皆失敗**，不計為本輪新增。
- 保管開埠途中 clear 收尾為 DETACHED／CLEARED 的既有差異，沿用前輪待辦。
- `wp510r_r3_p8_566d90b_clear_completes_when_new_open_fails_with_retired_drain` 預期值改寫的裁決紀錄仍待補齊。
- 整包完整基線由編排者另跑；本次定向結果不替代整包驗收。

## 對裁決的異議（不計入結論）

無。

全程未修改來源、測試或文件；建置產物限指定 target，建置／測試未達 15 分鐘。未跑完整 pytest、Docker／bundle，未接觸 production daemon、指定設定／狀態目錄或實體 UART。結束時 HEAD 未變、工作樹乾淨。

結論：需修正