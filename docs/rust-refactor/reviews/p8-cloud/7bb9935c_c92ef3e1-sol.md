> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

HEAD `c92ef3e1`，工作樹乾淨，本輪三筆提交、無 merge。前輪已標「已修」的 37 項（High 18、Medium 15、Low 4）本輪未動到對應路徑，沿用前輪結論不逐列。P8-566d90b-1 與 P8-d7af8df-1／-2 維持已修；P8-566d90b-1 的回歸缺口已由 `424aee24` 補上（見 P8-7bb9935-4）。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-7bb9935-1 | High | 已修 `4fca6390` | 0 | 三項修法都做了：recover 遇持有紀錄改發 `ClaimCustody`（`session_core.rs:6708-6721`）、開埠種類改用 `custody_opening` 明確記錄（`:3209`）、`AcceptCustody` 只在 DETACHED 或 clear 後等待的 ATTACHING 接受（`:3728-3731`）。兩支 CoreHarness 回歸在舊碼轉紅；ServiceRig 回歸在 HEAD 通過，舊碼轉紅本輪未重跑。 |
| P8-7bb9935-2 | Medium | 已修 `4fca6390` | 0 | `:6722-6730` 遇 retired drain 未收斂只設 ATTACHING 與重開旗標。回歸在舊碼轉紅。行為與 Python `recover_session`＋`_spawn_attach` 去重一致（py:7970-7977、4113-4145）。 |
| P8-7bb9935-3 | Medium | 已修 `4fca6390` | 0 | `custody_claimed` 只抄目前 release 停止的期限（`:3645`、`:3795-3800`）。兩個情境的回歸都在舊碼轉紅。 |
| P8-7bb9935-4 | Medium | 已修 `424aee24`（回歸部分） | 0 | 把 `:3324` 還原成不分停止種類，新增的保管路徑 clear、拔除兩支都轉紅。節點改寫的裁決仍未記錄，見下方說明。 |
| P8-7bb9935-5 | Low | 未修 | 1 | `ClaimCustody` 在交接凍結下照樣執行；本輪未動，屬 R6。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 沿用 2026-11-10 裁決。 |
| P8-591d2ff-1 | Low | 未修 | 26 | `tests/test_contracts_catalog.py` 未動。 |
| P8-0896933-1 | Low | 未修 | 18 | 驗收報告未動。 |
| P8-c89cd78-1 | Low | 未修 | 16 | 同 P8-591d2ff-1。 |
| P8-6a679fb-4 | Low | 未修 | 6 | `session_manager.rs` 本輪未動。 |
| P8-03333f7-2 | Low | 未修 | 5 | `claim_parked_locked`／`claim_offered` 未動。 |
| P8-03333f7-3 | Low | 未修 | 5 | `custody.rs` 未動。 |
| P8-7e34afb-1 | Low | 未修 | 4 | `tests/custody.rs` 註解未動。 |
| P8-566d90b-5 | Low | 未修 | 3 | shutdown 等退休 bridge 的兩處細節未動。 |

**P8-7bb9935-4 待編排者記錄的部分：** `wp510r_r3_p8_566d90b_clear_completes_when_new_open_fails_with_retired_drain` 的結束狀態由 DETACHED／CLEARED 改成 ATTACHING／null，裁決檔仍無對應紀錄。這不是實作者能補的。我的判斷是可以追認：
- 原前置「新路徑開埠在途」在 SPEC-GAP 1 的單次在途下不可達。
- 新值與 Python `clear_session` 無 bridge、裝置在線的分支一致（py:3058-3082）。
- 名稱與步驟保留，另增斷言。

## B 新發現

**P8-c92ef3e-1 [Low] `crates/sw-core/src/session_core.rs:6714-6718`、`:4988-4990` — 本輪新增的兩個分支沒有回歸。**

- 兩個分支：recover 遇「紀錄已被取消者認領」只設 ATTACHING 不認領；`release_deadline_elapsed` 多一道「身分須為目前 release 停止」的守衛。
- 突變驗證：同時移除兩者，`drain_release`、`custody`、`session_manager` 全部維持通過。
- 沒有找到經 RPC 可達的錯誤輸出：前者實測行為正確（bind 取消後 recover 回 REATTACH、零開埠，終態後 bind 完成、DETACHED）；後者需要「已結束的 release 的遲到通知」撞上「未經 ack 建立的新 release」，manager 的 release 一律帶 ack。
- 修法：各補一支 CoreHarness 回歸。
  - `AcceptCustody{open_now:false}` → bind 到他處 → recover，斷言無 `ClaimCustody`／`OpenBridge`。
  - 第一次 release 完成後，以不帶 ack 的 `Release` 建立有 retired drain 的新停止，再送舊的 `ReleaseDeadlineElapsed`，斷言不回 DRAIN_STALLED。

無 High／Medium。

**其餘核對：**
- 定向測試（HEAD）：`drain_release` 158、`custody` 44、`session_manager` 114、sw-daemon `dispatch` 的 wp510 篩選 60，全過。
- L1 鎖檔新增 8 列、刪除 0 列；`regen_lock.py --check --base 7bb9935c` 通過。
- 持有保管紀錄時的一般開埠入口：recover、`device_attached`、`attach_request` 都已擋或改走認領。reprobe 的 DETACHED 分支需要逾時類 `last_error`，bind 窗口內是 `DEVICE_NOT_FOUND`，不會觸發。
- 探針：clear 取消保管開埠後 recover，回 REATTACH 不開埠；關閉終態成功才以目前路徑重開，失敗則停在 ATTACHING。與 changelog 敘述相符。
- commit 訊息引用的突變輸出放在 `target/`，不在版本庫；我已自行重現同一結果。

## 既有碼待辦（不計入結論）

- **保管開埠途中 clear 的結束狀態與 Python 不同。** `finish_pending_open_stop` 的 Clear 分支（`session_core.rs:3496-3513`）以 DETACHED／CLEARED 收尾、不重新 attach。Python 同情境（無 bridge、裝置在線）立即回 ATTACHING／null，在途作業續行，結束後仍 ATTACHING 就重試（py:3058-3082、4126-4145）。本輪新增的 `wp510r_r3_p8_7bb9935_custody_clear_completes_before_close_terminal` 把 DETACHED／CLEARED 釘住，這個預期值是我前輪建議的，依據是 Rust 既有收尾慣例，沒有對過 Python。影響是出生即接管的開埠窗口內下 `session.clear`，session 會停在 DETACHED 直到熱插或手動 attach。窗口很短，建議 R6 或另請裁決決定是否改成重新 attach。
- 保管開埠工作回 `CloseBlocked` 且沒有停止待決時，session 停在無開埠在途的 ATTACHING（由 manager 層取消者收尾的情形）。
- 沿用前輪、本輪未涵蓋：
  - W6 與 DETACHED-rebind 的 `Held{Worker}` 時間窗（R4）。
  - manager 層 parked release 遇關閉失敗立即回 DRAIN_STALLED（R2／D4）。
  - `begin_stop` 的取消種類對應。
  - 無 bridge 的 release 不清 `drain_stalled`。
  - bind 的 `DEVICE_ALREADY_BOUND` 檢查與套用之間沒有保護。
  - `MarkAttachStalled` 遇 RELEASED 不改 state。
  - s24 黑箱 5 項既有紅燈。
- 本輪未驗：完整 sw-core／sw-daemon／workspace 測試、Clippy、Windows 交叉編譯、`tools.contracts check`、`policy_check`、host 完整 pytest、實機。

## 對裁決的異議（不計入結論）

無。

本輪探針與突變都在 `/tmp/solrev-p8-rust-refactor-p8-target` 下的拋棄式副本進行，結束時已刪除；未修改工作樹，未碰 production daemon、指定設定／狀態目錄或實體 UART。

結論：可放行
