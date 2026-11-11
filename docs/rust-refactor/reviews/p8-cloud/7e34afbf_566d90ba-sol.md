> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

HEAD `566d90ba`，detached，工作樹乾淨。本輪兩筆提交、無 merge：`d3ce730d`（產品碼與測試）、`566d90ba`（L1 鎖檔）。未找到 `p8-fix-responses.md`，編排者裁決尚無。本輪沒動到的項目沿用前輪證據。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-3786805-1 | High | 已修 f61de224 | 0 | 本輪未動。 |
| P8-19df186-1 | High | 已修 f40c01f0 | 0 | 本輪未動。 |
| P8-19df186-2 | High | 已修 f40c01f0 | 0 | 本輪未動。 |
| P8-f40c01f-1 | High | 已修 693a29ea | 0 | 本輪未動。 |
| P8-693a29e-1 | High | 已修 abeff2da | 0 | 本輪未動。 |
| P8-fdc1413-1 | High | 已修 8196bca6 | 0 | 本輪未動。 |
| P8-503c341-1 | High | 已修 6334a896 | 0 | 本輪未動。 |
| P8-e25b69c-1 | High | 已修 8463b22c | 0 | 本輪未動。 |
| P8-e25b69c-2 | High | 已修 b1a2e83a | 0 | 本輪未動。 |
| P8-8463b22-1 | High | 已修 14fe5922 | 0 | 本輪未動。 |
| P8-14fe592-1 | High | 已修 c4913aed | 0 | 既有 wp510_review12 節點仍通過。 |
| P8-c4913ae-1 | High | 已修 d3ce730d | 0 | 原情境（先逾時、新 bridge 先關、退休 bridge 最後關）已修，詳下。 |
| P8-1533cb5-1 | High | 已修 2a660d96 | 0 | 本輪未動。 |
| P8-a190a57-1 | Medium | 已修 a81ff33b | 0 | 本輪未動。 |
| P8-a190a57-2 | Medium | 已修 479f8d2b | 0 | 本輪未動。 |
| P8-a81ff33-1 | Medium | 已修 479f8d2b | 0 | 本輪未動。 |
| P8-591d2ff-1 | Low | 未修 | 23 | `tests/test_contracts_catalog.py:1450` 起仍固定要求 DF-27 為 `APPROVED`。 |
| P8-a94151a-1 | Medium | 已修 3c357879 | 0 | 本輪未動。 |
| P8-32219f2-1 | Medium | 已修 08969338 | 0 | 本輪未動。 |
| P8-0896933-1 | Low | 未修 | 15 | 驗收報告統計與時間欄位未修（本輪未動文件）。 |
| P8-c89cd78-1 | Low | 未修 | 13 | `tests/test_contracts_catalog.py:1438` 起仍固定要求 DF-26 為 `APPROVED`。 |
| P8-057a065-1 | High | 已修 8f2d661b | 0 | 本輪未動。 |
| P8-057a065-2 | High | 已修 50e1e798 | 0 | 本輪未動。 |
| P8-057a065-3 | Medium | 已修 50e1e798 | 0 | 本輪未動。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 沿用 rulings/s2.md 2026-11-10 裁決。 |
| P8-8f2d661-1 | High | 已修 50e1e798 | 0 | 本輪未動。 |
| P8-50e1e79-1 | Medium | 已修 03eb4d32 | 0 | 本輪未動。 |
| P8-03eb4d3-1 | Medium | 已修 6ebd0d2e | 0 | 本輪未動。 |
| P8-6ebd0d2-1 | Medium | 已修 67a1bc10 | 0 | 本輪未動。 |
| P8-bb1a899-1 | High | 已修 6a679fb0 | 0 | 本輪未動。 |
| P8-bb1a899-2 | High | 已修 9ee1f3e2 | 0 | 本輪未動。 |
| P8-bb1a899-3 | Medium | 已修 f2e33308 | 0 | 本輪未動。 |
| P8-7d19d5c-1 | Medium | 已修 6a679fb0 | 0 | 本輪未動。 |
| P8-6a679fb-1 | Low | 已修 f2e33308 | 0 | 本輪未動。 |
| P8-6a679fb-2 | Low | 已修 f2e33308 | 0 | 本輪未動。 |
| P8-6a679fb-3 | Low | 已修 f2e33308 | 0 | 本輪未動。 |
| P8-6a679fb-4 | Low | 未修 | 3 | `session_manager.rs:3993` 的 `Reclaim` 送出失敗仍以 `let _ =` 忽略；`:813-814` 欄位仍為 `pub`。 |
| P8-03333f7-1 | Medium | 已修 affdee6a | 0 | 本輪未動。 |
| P8-03333f7-2 | Low | 未修 | 2 | `session_manager.rs:2086-2091` 未改。 |
| P8-03333f7-3 | Low | 未修 | 2 | `custody.rs` 的 `delivered` 計數本輪未動。 |
| P8-7e34afb-1 | Low | 未修 | 1 | `crates/sw-core/tests/custody.rs:2036` 的註解未改。 |

**P8-c4913ae-1：已修。**
- `worker_stopped` 轉等退休 bridge 時不再先清 `drain_stalled`（`session_core.rs:4538-4563`）。
- 經在途作業收尾的 release 一併清 `drain_timeout_epoch`（`:3119`）。
- 新測試 `wp510r_r3_p8_c4913ae_new_receipt_first_keeps_stalled_until_retired_receipt` 走原 finding 的回條順序。
- 突變驗證：把提前清除加回去，或拿掉 `:3119`，這支測試都轉紅。
- 設計列另要求的「開埠在途直接轉自身排空」路徑，收斂後 epoch 仍留 0，見「既有碼待辦」。

## B 新發現

**本項只交付了一段。** 實作者回報第 7 項只做了 retired_drains 改寫與收斂規則，其餘 R3 內容（單次在途、出生即接管、兩種期限等）列為 SPEC-GAP 等裁決。未交付的部分列在「既有碼待辦」；下面是這批 diff 本身的問題。

探針做法：`git archive` 取 `566d90ba` 與 `7e34afbf` 各一份到 `/tmp`，放同一支 CoreHarness 探針，各自獨立的 target 目錄。

**P8-566d90b-1 [Medium] `crates/sw-core/src/session_core.rs:2974` — 開埠失敗時，所有種類的待決停止都改等退休 bridge，但非 release／shutdown 的停止沒有收尾路徑。**
- **情境：**
  1. 開埠途中改指，舊路徑開埠成功後退休，以新路徑重開。
  2. `session.clear`。
  3. 新路徑開埠失敗。
  4. 退休 bridge 回條 `handle_closed:false`。
- **`7e34afbf`：** 第 3 步 clear 就回 `ok:true`、DETACHED／CLEARED；之後的 release 等到首次期限。
- **`566d90ba`：**
  - clear 永不回覆，推進 10 秒仍 RECOVERING，RPC 會等到 route 逾時。
  - 之後的 release 立即回 `SESSION_RECOVERING`，不是 `DRAIN_STALLED`。
  - 再送 clear 也不回覆。
  - 把第 2 步換成拔除，結果相同：停在 RECOVERING／`DEVICE_REMOVED_DURING_ATTACH`，`7e34afbf` 是 DETACHED／`DEVICE_REMOVED`。
- **原因：** `begin_stop`（`:1594`）與 `worker_stopped`（`:4538`）只讓 release、shutdown 等退休 bridge，這裡卻不分種類。失敗回條與 `DrainTimeout` 都不回覆非 release 的等待者。
- **測試缺口：** 把 `:2974` 還原成只限 release，`drain_release`、`session_core`、`session_manager` 整檔與 `dispatch` 的 wp510 篩選都沒有轉紅。
- **修法：** `:2974` 改成 `matches!(pending.kind, StopKind::Release | StopKind::Shutdown) && !self.retired_drains.is_empty()`，並補上述 clear 與拔除兩個回歸。

**P8-566d90b-2 [Medium] `crates/sw-core/src/session_core.rs:2676` — `attach_inflight` 的 retired 條件沒有限定在無 bridge 的 session。**
- **情境：** READY 的 session 收到遲到的 `BridgeOpened`，該 bridge 退休、回條未到。這和新測試 `wp510r_r3_own_stop_success_with_failed_retired_drain_waits_first_deadline` 的前置相同。
- **`7e34afbf`：** `device.attach` 回 `ok:true, already_attached:true`；`session.attach` 回 `ok:true`。
- **`566d90ba`：** `device.attach` 回 `ok:true` 但少了 `already_attached`；`session.attach` 回 `SESSION_RECOVERING`，session 仍是 READY。回條若是 `handle_closed:false`，`session.attach` 會一直是 `SESSION_RECOVERING`。
- **Python：** 有 bridge 的冪等判斷（`origin/main:sw_core/session_manager.py:3420`）先於在途表分流（`:3427-3443`）。
- **修法：** 條件改成 `self.generations.conn_epoch.is_none() && !self.retired_drains.is_empty()`，或移到有 bridge 的分支之後；補 READY 加退休 bridge 的回歸。DR-4(c) 的 RELEASED 情境不受影響。

**P8-566d90b-3 [Medium] `crates/sw-core/src/session_core.rs:2795` — 退休 bridge 回條前 bind 立即回 `SESSION_RECOVERING`，沒有等終態，也沒有測試。**
- **情境：** RELEASED 的 session 做 `device.attach` 時 unreleased 落盤 ENOSPC，bridge 退休；回條前 `session.bind` 到另一個 by_id。
- **`7e34afbf`：** bind 回 `ok:true`、DETACHED／DEVICE_NOT_FOUND。
- **`566d90ba`：** 回 `SESSION_RECOVERING`、`drain_stalled:false`、state 仍 RELEASED；回條成功後重試才成功。
- **Python：** 取出作業後 `_finish_attach_op` 關閉，再遞迴 bind 成功（`:3680-3694`、`:3727`）。關閉失敗時設 RECOVERING、`drain_stalled:true` 與 epoch（`:3697-3711`）。
- **設計：** §3.1.3、§3.1.6 要求 BindWait 等到終態，上限 3000 ms。
- **測試缺口：** 刪掉 `:2795` 這一行，上述四組測試都沒有轉紅。
- **修法：** 仍待回條時把 bind 掛成等待，成功回條後照原流程改綁；失敗或逾時照 Python 設 RECOVERING、`drain_stalled:true`、epoch 後回 `SESSION_RECOVERING`。BindWait 若要等 SPEC-GAP 2 裁決，過渡期先還原 `7e34afbf` 的行為，或至少補測試並把差異交裁決。

**P8-566d90b-4 [Medium] `crates/sw-core/src/session_core.rs:3212-3229` — 本項指定的 retired drain T-1 切換沒做，#10 沒翻轉，新測試也不驗時點。**
- **情境：** DR-4(a) closed:false。release 等待退休 bridge 時，失敗回條在 3 秒期限前到達。
- **預期：** 擁有者 D1（T-1）、設計 §3.1.6 第二處與 Python（`:3188-3204`、`:4320-4332`）都是不提前回覆，到首次期限才回 `DRAIN_STALLED`。
- **實際：** 收到回條就立即回覆並設 drain 欄位。
- **測試：** `wp510r_r3_dr4_released_attach_commit_failure_close_false_stalls` 先推進 4 秒才取回覆，兩種時點都會過。
- **實作者的理由：** 改成 T-1 後凍結節點 #71 會在 ServiceRig 逾時，因為 actor 期限由 FakeClock 驅動而該節點不推進時鐘。我沒有獨立重現這個說法。
- **修法：** 需要編排者先裁決 SPEC-GAP 2（rig 時鐘的處理，或核准相關節點改寫）。之後移除 `:3215-3229` 的即時回覆，改由 `mark_drain_timeout` 回覆，同一 commit 翻轉 #10，並在上述測試加「期限前不得回覆」。若裁決延後，請寫進 rulings。

**P8-566d90b-5 [Low] `crates/sw-core/src/session_core.rs:1621`、`:3159`、`:4538` — shutdown 等退休 bridge 的兩處細節沒有測試，且 RELEASED 會被改成 DETACHED。**
- `:1621` 特意讓 RELEASED 的 session 在 shutdown 等待期間維持 RELEASED，但 `finish_pending_open_stop`（`:3159`）收尾時無條件設 DETACHED。探針：回條後 state 為 DETACHED、`last_error` 為 RELEASED。只影響記憶體內狀態，持久化的 released 不變，沒有對外失敗情境。
- 拿掉 `:1621` 的條件，或把 `:4538` 還原成只限 release（自身 bridge 停止成功後 shutdown 改等退休 bridge），上述四組測試都沒有轉紅。
- 修法：shutdown 收尾時原本是 RELEASED 就保留；補「有自身 bridge 加退休 bridge 的 shutdown」與「RELEASED 加 shutdown 的終態」兩個測試。

**其餘核對無問題：**
- **突變轉紅：** 還原 `begin_stop` 的 shutdown 條件，DR-4(b) 兩案轉紅；刪 `:2676`，DR-4(c) 轉紅；拿掉 manager `shutdown_phase1` 的 `retired_drains_pending` 判斷，ServiceRig 端到端案在 shutdown 斷言轉紅。
- **定向測試：** `drain_release` 整檔 119 過；`dispatch` 的 wp510 篩選 49 過。
- **既有測試：** `drain_release.rs`、`dispatch.rs` 只有新增（327/0、56/0），既有節點斷言未改。
- **L1 鎖檔：** 新增 10 列、刪除 0 列；`regen_lock.py --check --base 7e34afbf` 通過。
- **其他：** `git diff --check` 通過；`tests/test_changelog_fragments.py` 通過。

## 既有碼待辦（不計入結論）

- **本項未交付、實作者列為待裁決：**
  - 單次在途（DR-8 a／b／d）與 #5、#6 改寫，等 SPEC-GAP 1：與凍結節點 `attach_finish_respawns_when_state_requires` 衝突。
  - 出生即接管與 `ClaimCustody`、adopt 提交點取消、BindWait／ReleaseWait 分開、移除 `ReleaseAttachPending`／`ClearRevokedHandoff`、#1～#4 改寫，等 SPEC-GAP 2。
  - 紅燈 P8-8463b22-1、P8-14fe592-1（post-take）、WP5-1、P8-fdc1413-1、G-3、DR-6 的新回歸未補。
- **SPEC-GAP 3：** 「開埠在途直接轉自身 bridge 排空」的 release 收斂後 `drain_timeout_epoch` 仍是 0，Python 為 null（`_finish_attach_op` `:4346-4351`）。既有凍結節點 `release_during_pending_open_reply_is_stalled_until_close_receipt` 釘住 0。本輪新登記的 `wp510r_r3_p8_c4913ae_open_in_flight_rebound_release_timeout_converges` 也斷言 0。裁決若採設計的 null，兩個節點都要經核准改寫。
- 退休 bridge 失敗回條在沒有 release 等待時，現在會經 `pending_open_close_unconfirmed` 清掉 `placeholder_attach_stall_token`（`:3109`）。未找到失敗情境。
- s24 黑箱的 5 項既有紅燈本輪未涵蓋。
- 沿用前輪：
  - `route()` 在 release 成功後無條件結算為 `Closed`。
  - 送達者的 `next_at` 只寫不讀。
  - `custody.rs` 的 R1、R2 共 35 案未進 L1。
  - bind 的 `DeviceOnline` 可能重複送達。
  - 正式 `human_active` 接線。
  - 一般 RELEASED session 開埠失敗再遇落盤失敗的終態差異。
- 本輪未驗：完整 sw-core／sw-daemon／workspace 測試、Clippy、Windows 交叉編譯、`tools.contracts check`、`policy_check`、host 完整 pytest、實機。實作者回報的四支間歇失敗我沒有重跑取證。

## 對裁決的異議（不計入結論）

無。

本輪驗證說明：探針與突變都在 `/tmp/solrev-p8-rust-refactor-p8-target` 下的拋棄式副本進行，用獨立 target 目錄，結束時已刪除。未修改工作樹，未碰 production daemon、指定設定／狀態目錄或實體 UART。結束時 HEAD 仍為 `566d90ba`、工作樹乾淨。

結論：需修正
