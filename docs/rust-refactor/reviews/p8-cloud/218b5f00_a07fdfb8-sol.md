> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD 為 `a07fdfb8`，工作樹乾淨。已逐一檢視 `776133e5`、`a07fdfb8`，無 merge；未找到 `p8-fix-responses.md`。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-7bb9935-1 | High | 已修 `4fca6390` | 0 | 接受條件、recover 認領與開埠種類區分仍保留；相關回歸通過。 |
| P8-7bb9935-2 | Medium | 已修 `4fca6390` | 0 | retired drain 未收斂時仍不另開；回歸通過。 |
| P8-7bb9935-3 | Medium | 已修 `4fca6390` | 0 | 首次期限仍限定目前 release；bind／clear 不繼承舊期限兩案通過。 |
| P8-7bb9935-4 | Medium | 已修 `424aee24` | 0 | 保管路徑 clear、拔除回歸通過；節點改寫的裁決紀錄缺口仍保留。 |
| P8-7bb9935-5 | Low | 未修 | 3 | 新增 Raw 開埠的凍結處理，未修 `ClaimCustody` 效果本身；仍屬 R6。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 維持 2026-11-10 裁決，不新增 panic 超限退出行為。 |
| P8-591d2ff-1 | Low | 未修 | 28 | `tests/test_contracts_catalog.py` 未變。 |
| P8-0896933-1 | Low | 未修 | 20 | 對應驗收報告未變。 |
| P8-c89cd78-1 | Low | 未修 | 18 | 同 P8-591d2ff-1，對應部分未變。 |
| P8-6a679fb-4 | Low | 未修 | 8 | 回收者停止後的送出失敗處理與公開範圍未修。 |
| P8-03333f7-2 | Low | 未修 | 7 | `claim_parked_locked`／`claim_offered` 的 `parts: None` 處理未修。 |
| P8-03333f7-3 | Low | 未修 | 7 | `Deliverer::delivered` 的正式路徑計數與清理未修。 |
| P8-7e34afb-1 | Low | 未修 | 6 | `tests/custody.rs` 對應註解未變。 |
| P8-566d90b-5 | Low | 未修 | 5 | `776133e5` 已修 shutdown 收尾保留 RELEASED；原指定的兩個退休 bridge 交錯回歸仍未補齊。 |
| P8-c92ef3e-1 | Low | 未修 | 2 | 未補「已取消保管後 recover」及「舊期限通知撞上新 release」兩支指定回歸。 |

前輪未逐列提供的 37 項已修 finding，以及 P8-566d90b-1、P8-d7af8df-1／-2，沿用既有結案證據。

## B 新發現

**P8-a07fdfb-1 [High] [crates/sw-core/src/session_manager.rs:3832](crates/sw-core/src/session_manager.rs:3832) — OSError 收尾遺失裝置取消，誤退休 RELEASED 佔位並開啟新路徑。**

- **具體情境：** 佔位 `gone-template:COM3` 對舊路徑執行 `device.attach`，開埠阻塞；同一 by-id 改指新路徑，帳本已標記 `cancel=Device`；舊開埠接著回 `PermissionError`。
- **預期：** Python [sw_core/session_manager.py:3531](sw_core/session_manager.py:3531) 的已取消 OSError 分支回 `SESSION_RECOVERING`，保留 RELEASED 佔位，不啟動新路徑交接。
- **實際：** stdin 編譯的定向探針連跑兩次均得到 `ok:true`、`session.state=DETACHED`、`ATTACH_FAILED:PermissionError`；舊佔位已不存在，新路徑 `open_attempts=1`。
- **原因：** `no_handle` 移除帳本後固定回傳 `cancel=None`；`placeholder_opened` 的 OSError 分支遂發出退休效果，而 [session_manager.rs:4482](crates/sw-core/src/session_manager.rs:4482) 對無 handle 路徑只確認 by-id 仍在線，沒有核對原路徑及已發生的取消。前版退休檢查仍保有 `op.cancelled` 與原 real path 守衛，因此屬本輪回歸。
- **確切修法：** 將原路徑及取消狀態保留到退休決策完成；已取消的 OSError 直接回 `SESSION_RECOVERING`、丟棄 prepared worker，不退休、不解除 RELEASED。無 handle 退休亦須在 manager 鎖內核對原作業身分、路徑及取消狀態。新增上述交錯回歸，斷言佔位保留、新路徑零開埠。

**驗證：** 新增 R5 16 案、既有佔位 manager 42 案、前輪 `p8_7bb9935` 8 案全部通過；上述額外探針兩次失敗。L1 鎖檔新增 16 列、刪除 0 列，`regen_lock.py --check --base 218b5f00` 與 `git diff --check` 通過。

## 既有碼待辦（不計入結論）

- 保管開埠途中 clear 收尾為 DETACHED／CLEARED 的既有差異仍存在。
- `wp510r_r3_p8_566d90b_clear_completes_when_new_open_fails_with_retired_drain` 預期值改寫的裁決紀錄仍待補齊。
- R6 清理、T14 剩餘案例與整包驗收不列為本項缺陷。

## 對裁決的異議（不計入結論）

無。

全程未修改來源、測試或文件；探針由 stdin 編譯，建置產物僅落在指定 target。定向建置／測試未達 15 分鐘；未跑完整 pytest、Docker／bundle 或實機測試，未接觸 production daemon、指定設定／狀態目錄或實體 UART。結束時 HEAD 未變、工作樹乾淨。

結論：需修正