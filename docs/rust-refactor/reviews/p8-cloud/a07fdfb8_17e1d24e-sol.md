> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD 為 `17e1d24e`，工作樹乾淨。已逐一檢視 `c8fb4cb5`、`17e1d24e`，無 merge；未找到 `p8-fix-responses.md`。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-7bb9935-1 | High | 已修 `4fca6390` | 0 | 接受條件、recover 認領與開埠種類區分仍保留；相關回歸通過。 |
| P8-7bb9935-2 | Medium | 已修 `4fca6390` | 0 | retired drain 未收斂時仍不另開；相關回歸通過。 |
| P8-7bb9935-3 | Medium | 已修 `4fca6390` | 0 | bind／clear 不繼承舊 release 期限兩案通過。 |
| P8-7bb9935-4 | Medium | 已修 `424aee24` | 0 | 保管路徑 clear、拔除回歸通過；既有裁決紀錄缺口仍保留。 |
| P8-7bb9935-5 | Low | 未修 | 4 | `ClaimCustody` 效果本身未變；仍屬 R6。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 維持 2026-11-10 裁決，不新增 panic 超限退出行為。 |
| P8-591d2ff-1 | Low | 未修 | 29 | `tests/test_contracts_catalog.py` 未變。 |
| P8-0896933-1 | Low | 未修 | 21 | 對應驗收報告未變。 |
| P8-c89cd78-1 | Low | 未修 | 19 | 同 P8-591d2ff-1，對應部分未變。 |
| P8-6a679fb-4 | Low | 未修 | 9 | 回收者停止後的送出失敗處理與公開範圍未變。 |
| P8-03333f7-2 | Low | 未修 | 8 | `claim_parked_locked`／`claim_offered` 的 `parts: None` 處理未變。 |
| P8-03333f7-3 | Low | 未修 | 8 | `Deliverer::delivered` 的正式路徑計數與清理未變。 |
| P8-7e34afb-1 | Low | 未修 | 7 | `tests/custody.rs` 對應註解未變。 |
| P8-566d90b-5 | Low | 未修 | 6 | 原指定的兩個退休 bridge 交錯回歸仍未補齊。 |
| P8-c92ef3e-1 | Low | 未修 | 3 | 未補「已取消保管後 recover」及「舊期限通知撞上新 release」兩支指定回歸。 |
| P8-a07fdfb-1 | High | 未修 | 1 | `c8fb4cb5` 修復原先單次改指案例，但退休前仍可能遺失裝置取消，詳見下述殘留情境。 |

前輪未逐列提供的 37 項已修 finding，以及 P8-566d90b-1、P8-d7af8df-1／-2，沿用既有結案證據。

**P8-a07fdfb-1 的未修部分：取消狀態仍未保留到退休決策完成。**

- **具體情境：** 佔位 `gone-template:COM3` 開埠回 `PermissionError`；`no_handle` 結算帳本後，在既有 `H-retire-pre-commit` hook 依序呼叫 `update_devices`，使同一 by-id 從原路徑改指新路徑，再改回原路徑，才讓退休提交繼續。
- **預期：** 裝置變動已取消本次 attach，應回 `SESSION_RECOVERING`、保留 RELEASED 佔位、不啟動交接。Python [取消登記](sw_core/session_manager.py:4031) 不因路徑恢復而撤銷；[OSError 收尾](sw_core/session_manager.py:3530) 在同一鎖區段檢查取消並決定是否退休。
- **實際：** stdin 編譯的定向探針連跑兩次，均為 `ok:true`、`state=DETACHED`、佔位不存在；原路徑 `open_attempts=2`，表示失敗後仍啟動交接並再次開埠。
- **原因：** [no_handle](crates/sw-core/src/session_manager.rs:3843) 仍先刪除帳本紀錄；之後的裝置變動無處保留本次作業的取消。[退休檢查](crates/sw-core/src/session_manager.rs:4494) 只比較目前路徑字串，無法識別「改指再改回」。
- **確切修法：** 將本次作業身分與不可撤銷的取消狀態保留至退休提交，在 manager 鎖內一併核對、消費；或使用能辨識中途裝置變動的世代。新增上述交錯回歸，斷言佔位保留、原路徑開埠總次數仍為 1。

此為原 finding 的取消遺失殘留，不另編本輪新 finding。

## B 新發現

NO FINDINGS。

驗證結果：R5 定向測試 **20 passed**；前輪 `p8_7bb9935` 定向回歸 **7 passed**。上述額外探針兩次失敗。L1 鎖新增 4 列、刪除 0 列；`regen_lock.py --check --base a07fdfb8` 與 `git diff --check` 通過。

## 既有碼待辦（不計入結論）

- 保管開埠途中 clear 收尾為 DETACHED／CLEARED 的既有差異仍保留。
- `wp510r_r3_p8_566d90b_clear_completes_when_new_open_fails_with_retired_drain` 預期值改寫的裁決紀錄仍待補齊。
- R6 清理、T14 剩餘案例與整包驗收由後續子項承接。

## 對裁決的異議（不計入結論）

無。

全程未修改來源、測試或文件；探針由 stdin 編譯，建置產物僅落在指定 target。定向建置／測試未達 15 分鐘；未跑完整 pytest、Docker／bundle，未接觸 production daemon、指定設定／狀態目錄或實體 UART。結束時 HEAD 未變、工作樹乾淨。

結論：需修正