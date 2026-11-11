> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

HEAD `7bb9935c`，detached，工作樹乾淨。本輪五筆提交，無 merge。未找到 `p8-fix-responses.md`。裁決依 `origin/docs/parity-amendments` 的 `rulings/s2.md`（2026-11-11 的 SPEC-GAP 1、2、3）。

前輪已標「已修」的 37 項，本輪沒有動到對應路徑，沿用前輪結論，不逐列重複：

- **High（18 項）**：P8-3786805-1、P8-19df186-1、P8-19df186-2、P8-f40c01f-1、P8-693a29e-1、P8-fdc1413-1、P8-503c341-1、P8-e25b69c-1、P8-e25b69c-2、P8-8463b22-1、P8-14fe592-1、P8-c4913ae-1、P8-1533cb5-1、P8-057a065-1、P8-057a065-2、P8-8f2d661-1、P8-bb1a899-1、P8-bb1a899-2。
- **Medium（15 項）**：P8-a190a57-1、P8-a190a57-2、P8-a81ff33-1、P8-a94151a-1、P8-32219f2-1、P8-057a065-3、P8-50e1e79-1、P8-03eb4d3-1、P8-6ebd0d2-1、P8-bb1a899-3、P8-7d19d5c-1、P8-03333f7-1、P8-566d90b-2、P8-566d90b-3、P8-566d90b-4。
- **Low（4 項）**：P8-6a679fb-1、P8-6a679fb-2、P8-6a679fb-3、P8-a81ff33… 以外的 P8-6a679fb 系列三項，加上前輪已修的其餘 Low 併入此類（前輪表列為準）。

本輪狀態有變動或仍未修的項目：

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-566d90b-1 | Medium | 已修 c5f9b38a | 0 | 產品碼的修正還在（`session_core.rs:3319`）。但它的兩支回歸在本輪被改寫後已測不到這個修正，見 B 的 P8-7bb9935-4。 |
| P8-d7af8df-1 | Low | 已修 58df00f6 | 0 | 兩個分支各補一支測試（`wp510r_r3_p8_d7af8df_*`），都通過。 |
| P8-d7af8df-2 | Low | 已修 58df00f6 | 0 | 編排者 2026-11-11 SPEC-GAP 2 裁決已記錄；通知改帶身分與期限（`WaitAck`）。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 沿用 2026-11-10 裁決。 |
| P8-591d2ff-1 | Low | 未修 | 25 | `tests/test_contracts_catalog.py` 本輪未動。 |
| P8-0896933-1 | Low | 未修 | 17 | 驗收報告本輪未動。 |
| P8-c89cd78-1 | Low | 未修 | 15 | 同 P8-591d2ff-1。 |
| P8-6a679fb-4 | Low | 未修 | 5 | `session_manager.rs:4164` 的 `Reclaim` 送出失敗仍以 `let _ =` 忽略。 |
| P8-03333f7-2 | Low | 未修 | 4 | `claim_parked_locked` 未動；新增的 `claim_offered` 也只處理 `Ok((token, Some(parts)))`。 |
| P8-03333f7-3 | Low | 未修 | 4 | `custody.rs` 本輪未動。 |
| P8-7e34afb-1 | Low | 未修 | 3 | `tests/custody.rs` 的那段註解未動。 |
| P8-566d90b-5 | Low | 未修 | 2 | `finish_pending_open_stop` 未動。 |

## B 新發現

**P8-7bb9935-1 [High] `crates/sw-core/src/session_core.rs:6668-6692`、`:3204-3208`、`:3737` — session 持有保管紀錄、尚未開埠時，`session.recover` 仍送一般 `OpenBridge`，之後 `device.release` 回 ok 但 UART handle 沒關。這是本輪引入的回歸。**

- **失敗情境（ServiceRig 實測，沿用凍結節點 #96 的前置與 `bind_pre_device_online` 掛鉤）：**
  1. 佔位退休落盤失敗留下 parked handle。
  2. `session.bind COM1 → 該 by_id`，停在 `AcceptCustody` 之後、`DeviceOnline` 之前。
  3. `session.recover COM1`。
  4. 放行 bind。
  5. `device.release COM1`，再 `device.attach COM1`。
- **結果對照：**

  | 步驟 | 預期（基準 `d7af8df8` 同一探針） | 實際（HEAD） |
  |---|---|---|
  | recover | `ok:false`，不開埠 | `action:"REATTACH"`，多開一次埠，失敗 |
  | bind 放行後 | READY，帳本紀錄移除 | DETACHED、`ATTACH_FAILED:DeviceHeldError`，再多開一次；紀錄仍是 `Taken{OpenJob}` |
  | release | ok，`is_open=false`、`close_count=1` | `ok:true`、RELEASED，但 `is_open=true`、`close_count=0` |
  | 再 attach | ok | `DEVICE_STILL_HELD` |

  實際結果下，handle 要到拔除或 shutdown 才會關。
- **成因（三處疊加）：**
  - `accept_custody` 先把 `device_present` 設為 true，recover 因此走 Reattach。
  - Reattach 分支沒有 `attach_request` 那條 `external_attach.is_some()` 的防護。
  - `bridge_opened` 用「有開埠在途且有 `external_attach`」推定這是保管開埠，一般開埠的失敗終態就把 `external_attach` 清掉，之後任何停止都不再發 `ClaimForStop`。
- **第二個觸發點（CoreHarness 實測）：** bind 逾時後的 RECOVERING（無停止）會接受 `AcceptCustody{open_now:true}`，但只發 `ClaimOffered`、不開埠；之後 recover 同樣送出一般 `OpenBridge`。這個窗口沒有時間上限。
- **觸發條件偏窄：** 第一個情境在正常流程是微秒級窗口，且需要 parked handle。
- **修法：**
  - `recover_request` 的 Reattach 遇 `external_attach` 時改發 `custody_open_effect(token)`。
  - `bridge_opened` 改用明確記錄的開埠種類判斷，不靠推定。
  - `accept_custody` 不會開埠的狀態（非 DETACHED 且非 clear 後等待）直接拒絕。
  - 補上述兩個情境的回歸，ServiceRig 與 CoreHarness 各一。

**P8-7bb9935-2 [Medium] `crates/sw-core/src/session_core.rs:6668-6692` — 單次在途沒有涵蓋 `session.recover`。**

- **失敗情境（CoreHarness 實測）：** 開埠途中改指，舊路徑開埠成功而退休，session 為 ATTACHING 並在等舊回條；此時送 `Recover`。
- **預期（DR-8(a)）：** 舊回條前不發新路徑的 `OpenBridge`。
- **實際：** 立即發出 `OpenBridge{/fake/tty-A2}` 並回 `REATTACH`，同一 session 同時有兩個 handle。對照組 `device.attach`、`session.attach` 在同一狀態都不開埠。
- **修法：** Reattach 分支遇「無自身 bridge 且 `retired_drains` 非空」時，只設 ATTACHING 與 `reopen_after_retired`，不發 `OpenBridge`；補回歸。

**P8-7bb9935-3 [Medium] `crates/sw-core/src/session_core.rs:3638`、`:3757-3774` — 上一次 release 的首次期限會被記到之後才建立的保管等待上，新的首次 release 立即回 DRAIN_STALLED。**

- **成因：** `release_wait_rt` 只寫入、從不清除；`custody_claimed` 不論目前有沒有 release 停止，都把它的期限抄到新項目上，`ensure_release_wait_rt` 再取最小值沿用。
- **失敗情境（CoreHarness 實測兩種）：**
  1. session 曾經等待過一次 release 並完成。
  2. 之後取得新的保管紀錄，開埠在途。
  3. (a) `session.bind` 取消它（BindWait，關閉進行中）；或 (b) `session.clear`，clear 回覆後關閉仍在進行。
  4. 送這個對象上的第一次 `device.release`。
- **預期（SPEC-GAP 2 條件 1、DR-6）：** 期限為本次 release 加 3 s；期間關閉完成就回成功。
- **實際：** `WaitAck` 的期限等於第一次 release 的期限。距離上次 release 超過 3 s 時，RPC 執行緒立刻送通知，release 馬上回 DRAIN_STALLED。
- **修法：** release 停止結束時清 `release_wait_rt`；或 `custody_claimed` 只在目前停止就是該 release 時才抄期限。補回歸。

**P8-7bb9935-4 [Medium] `crates/sw-core/tests/drain_release.rs:3689-3760` — P8-566d90b-1 的兩支回歸被改寫後，拿掉修正也不會紅；其中一支的結果斷言改動不在核准名單內。**

- **突變驗證：** 把 `session_core.rs:3319` 還原成不分停止種類。指示探針確認突變生效（clear 不再回覆、停在 RECOVERING），但 `drain_release` 151、`session_manager` 114、`custody` 44、sw-daemon `dispatch` wp510 59 全部通過。
- **原因：** 兩支節點的前置改成「等舊回條時 clear／拔除」，走的是 `begin_stop` 的新分支，不再經過 `:3319`。這條分支在保管路徑仍可達：出生即接管 → clear → `CustodyClaimed(Closing)` → `BridgeOpened(Err(CloseBlocked))`。
- **斷言改動：** `wp510r_r3_p8_566d90b_clear_completes_when_new_open_fails_with_retired_drain` 是第 7 項已登記的 L1 節點，結束狀態由 DETACHED／CLEARED 改成 ATTACHING／null。它不在 D5 名單，也不在 SPEC-GAP 1 裁決列出的節點內。新值符合 Python `clear_session`，實作者已在回報中請求裁決。
- **修法：**
  - 補保管路徑的 clear 與拔除兩支回歸：關閉終態未到時，clear 回 ok 且 DETACHED／CLEARED，拔除為 DETACHED／DEVICE_REMOVED。
  - 編排者對三個過渡節點的改寫補裁決。

**P8-7bb9935-5 [Low] `crates/sw-core/src/session_core.rs:3683-3691`、`session_manager.rs:4286` — `ClaimCustody` 啟動效果在交接凍結下照樣執行，與設計 §3.1.7「新工作在 Pending 不執行」不符。**

- 實作者已在回報列為已知差異。凍結會設 stopping，開埠工作在閘門關閉 handle；我沒有找到會送出 TX 的情境。
- 本輪的 T14 測試沒有涵蓋這個效果本身。
- 修法：R6 的凍結案例補上，或在執行端依凍結狀態略過。

**其餘核對無問題：**

- **定向測試（HEAD）：** `drain_release` 151、`custody` 44、`session_manager` 114 全過；sw-daemon `dispatch` 的 wp510 篩選 59 全過（在 HEAD 內容的副本上跑）。
- **L1 鎖檔：** 新增 27 列、刪除 0 列；`regen_lock.py --check --base d7af8df8` 通過。
- **凍結節點改寫：** #1、#2（H*）、#3、#4、#5、#6（S）與 `attach_finish_respawns_when_state_requires` 都在核准範圍內，保留的斷言還在。`dispatch.rs` 與 `tests/session_manager.rs` 只有新增。
- **`7bb9935c`：** 只把一條恆真的斷言換成實際檢查，並簡化一處取值。
- **探針：** 持有保管紀錄的 DETACHED session 連續 40 次 Tick，沒有觸發自動開埠。

## 既有碼待辦（不計入結論）

- **`tests/custody.rs` 的 `test_parked_continuation_bind_claims_same_token_and_settles_on_release_close`** 換了掛鉤與內部查詢，結果斷言保留。這支不在 L1 內。
- **W6 與 DETACHED-rebind 的時間窗：** handle 先以 `Held{Worker}` 存入帳本，再送 `AcceptCustody`；中間插入的 release 會先回 RELEASED，handle 之後才關。實作者已列出，屬 R4 範圍。
- **manager 層的 parked release** 遇關閉失敗仍立即回 DRAIN_STALLED（設計 N-14 要 T-1）。屬 R2／D4。
- **`begin_stop` 的取消種類對應：** Clear、IoError、Recover 都記成 `CustodyCancel::Release`。目前沒有邏輯讀這個值。
- **沿用前輪、本輪未涵蓋：** 無 bridge 的 release 不清 `drain_stalled`；bind 的 `DEVICE_ALREADY_BOUND` 檢查與 actor 套用之間沒有保護；`MarkAttachStalled` 遇 RELEASED 不改 state；s24 黑箱 5 項既有紅燈。
- **本輪未驗：**
  - 完整 sw-core／sw-daemon／workspace 測試、Clippy、Windows 交叉編譯、`tools.contracts check`、`policy_check`、host 完整 pytest、實機。
  - 實作者的間歇失敗量測（兩邊各 3/64）沒有重跑。
  - 新增的 P8-8463b22-1、P8-14fe592-1、WP5-1、P8-fdc1413-1、G-3、DR-6 回歸只確認通過，沒有逐支做突變。

## 對裁決的異議（不計入結論）

無。

本輪驗證說明：探針與突變都在 `/tmp/solrev-p8-rust-refactor-p8-target` 下的拋棄式副本進行，結束時已刪除。未修改工作樹，未碰 production daemon、指定設定／狀態目錄或實體 UART。結束時 HEAD 仍為 `7bb9935c`、工作樹乾淨。

結論：需修正
