> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

HEAD `d7af8df8`，detached，工作樹乾淨。本輪兩筆提交、無 merge：`c5f9b38a`（產品碼與測試）、`d7af8df8`（L1 鎖檔）。未找到 `p8-fix-responses.md`，`rulings/p8.md` 尚無裁決。本輪沒動到的項目沿用前輪證據。

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
| P8-c4913ae-1 | High | 已修 d3ce730d | 0 | 本輪未動該路徑。 |
| P8-1533cb5-1 | High | 已修 2a660d96 | 0 | 本輪未動。 |
| P8-a190a57-1 | Medium | 已修 a81ff33b | 0 | 本輪未動。 |
| P8-a190a57-2 | Medium | 已修 479f8d2b | 0 | 本輪未動。 |
| P8-a81ff33-1 | Medium | 已修 479f8d2b | 0 | 本輪未動。 |
| P8-591d2ff-1 | Low | 未修 | 24 | `tests/test_contracts_catalog.py:1450` 起仍固定要求 DF-27 為 `APPROVED`。 |
| P8-a94151a-1 | Medium | 已修 3c357879 | 0 | 本輪未動。 |
| P8-32219f2-1 | Medium | 已修 08969338 | 0 | 本輪未動。 |
| P8-0896933-1 | Low | 未修 | 16 | 驗收報告統計與時間欄位未修（本輪未動文件）。 |
| P8-c89cd78-1 | Low | 未修 | 14 | `tests/test_contracts_catalog.py:1438` 起仍固定要求 DF-26 為 `APPROVED`。 |
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
| P8-6a679fb-4 | Low | 未修 | 4 | `session_manager.rs:3993` 的 `Reclaim` 送出失敗仍以 `let _ =` 忽略。 |
| P8-03333f7-1 | Medium | 已修 affdee6a | 0 | 本輪未動。 |
| P8-03333f7-2 | Low | 未修 | 3 | 本輪未動該段。 |
| P8-03333f7-3 | Low | 未修 | 3 | `custody.rs` 的 `delivered` 計數本輪未動。 |
| P8-7e34afb-1 | Low | 未修 | 2 | `crates/sw-core/tests/custody.rs` 的註解本輪未動。 |
| P8-566d90b-1 | Medium | 已修 c5f9b38a | 0 | `session_core.rs:3067` 改為只限 release／shutdown，並補 clear、拔除兩個回歸；突變驗證見下。 |
| P8-566d90b-2 | Medium | 已修 c5f9b38a | 0 | `:2704` 加上「沒有自身 bridge」的條件，並補 READY 加退休 bridge 的回歸；突變驗證見下。 |
| P8-566d90b-3 | Medium | 已修 c5f9b38a | 0 | bind 改掛 BindWait（`:2827`、`:2842-2899`），CoreHarness 4 案加 ServiceRig 2 案。 |
| P8-566d90b-4 | Medium | 已修 c5f9b38a | 0 | 即時回覆已移除（`:3302-3307`、`:4573-4590`），#10 同一 commit 翻轉，#71 未改且通過。 |
| P8-566d90b-5 | Low | 未修 | 1 | `finish_pending_open_stop` 的 shutdown 收尾仍無條件設 DETACHED，兩處細節仍無測試。 |

**四項 Medium 的驗證（拋棄式副本，獨立 target）：**
- **-1、-2：** 把 `:3067` 還原成不分種類、`:2704` 還原成不看自身 bridge，clear、拔除、READY 三支回歸都轉紅。
- **-3：** 四支 bind 回歸都斷言「回條前不回覆」與終態後的結果，拿掉 BindWait 必然轉紅（依斷言判讀，未另跑突變）。
- **-4：**
  - #10 只新增「期限前不得回覆」與推進 4 秒三行，其餘斷言原樣保留，符合設計第 5 版 §6 的 F 類改寫與擁有者 2026-11-10 的 T-1 裁決。
  - 把 `request_with_notice` 換回一般 `request`，#71 與新的端到端案都等到 route 逾時後失敗。實作者「actor 期限由 FakeClock 驅動、#71 不推進時鐘」的說法成立。

## B 新發現

**P8-d7af8df-1 [Low] `crates/sw-core/src/session_core.rs:2892`、`:4491-4497` — 兩個新分支沒有測試。**
- **分支：** `resume_bind_waits` 遇到停止進行中時回 `SESSION_RECOVERING`；`release_deadline_elapsed` 作用在自身 bridge 排空中的 release。
- **突變：** 把前者改成永不成立、後者改成直接返回，`drain_release` 132 案與 `session_core` 89 案全過。`session_manager` 整檔只有下方那支既有間歇案失敗。`dispatch` 未在這組突變下重跑。
- **現行行為正確：** BindWait 期間插入 release、再收成功回條，bind 回 `SESSION_RECOVERING`（`drain_stalled:false`），release 回 `already_released:true`，by_id 不變，與 Python `:3716-3726` 一致。
- **修法：** 補兩個測試：「BindWait 加 release 加成功回條」；「READY 加退休 bridge 的 release 收到 `ReleaseDeadlineElapsed` 即回 `DRAIN_STALLED`」。

**P8-d7af8df-2 [Low] `crates/sw-core/src/session_manager.rs:4728-4739`、`crates/sw-core/src/actor.rs:255-306` — SPEC-GAP 2 的處理方式沒有裁決紀錄。**
- 前輪要求編排者先裁決 SPEC-GAP 2。本輪改由 RPC 執行緒在首次期限（實時）送 `ReleaseDeadlineElapsed`，讓 #71 不改斷言就通過。
- 我沒有找到這條通知造成錯誤回覆的情境：它只在有 retired drain 的 release 停止上生效，已逾時者冪等，與 release 同走 safety lane 所以順序在 release 請求之後。
- 修法：編排者在 `rulings/p8.md` 記下採用這個做法，或另行裁決。

**其餘核對無問題：**
- **定向測試：** `drain_release` 整檔 132 過；`dispatch` 的 wp510 篩選 52 過（需 `--features test-fakes`）；`session_manager` 整檔在 HEAD 連跑三次都是 113 過。
- **既有測試：** `drain_release.rs` 只刪兩行註解，`dispatch.rs` 只改一行 `use`；除 #10 的核准翻轉外，既有節點斷言未改。
- **L1 鎖檔：** 新增 16 列、刪除 0 列；`regen_lock.py --check --base 566d90ba` 通過。
- **探針：**
  - READY 加退休 bridge 時 bind，成功回條後改走自身的 Bind 排空並完成改綁。
  - BindWait 逾時後遲到的成功回條維持 RECOVERING 與 stalled、不改綁，符合 `design-v3.md` §3.1.4。
- **其他：** `git diff --check` 通過。

## 既有碼待辦（不計入結論）

- **`session_manager::wp510_review7_open_bridge_gate_rejects_late_unconfirmed_close` 間歇失敗。**
  - 單跑 100 次：基準 `566d90ba` 失敗 4 次，HEAD 失敗 1 次。
  - 失敗點是 `tests/session_manager.rs:4814`，state 讀到 ATTACHING 而非 DETACHED。
  - 當時主機 load 約 60，非本輪引入。
- **無 bridge 的 release 不清 `drain_stalled`。**
  - Python `:3182` 會清；Rust 的 `finish_stop_without_worker` 與 `finish_release` 不清。
  - 既有路徑即可重現：`MarkAttachStalled` 之後 release，回 ok、RELEASED，但 `drain_stalled` 仍為 true。
  - 本輪的 BindWait 逾時加遲到成功回條再 release，走到同一個缺口。
- **bind 的 `DEVICE_ALREADY_BOUND` 檢查與 actor 套用之間沒有保護。** manager 在送 actor 前檢查，actor 等待期間另一個 session 可綁到同一 by_id。既有的 Bind 排空等待就有這個窗口，BindWait 多了一個最長 3 秒的同類窗口。依程式判讀，未實測。
- **`MarkAttachStalled` 遇 RELEASED 不改 state**（`session_core.rs:2353`），Python bind 逾時（`:3701`）無條件設 RECOVERING。本輪新增的 `bind_stalled_reply` 照 Python，兩條路徑不一致。依程式判讀，未實測。
- **沿用前輪、本輪未涵蓋：**
  - 單次在途（DR-8）與 #5、#6 改寫，等 SPEC-GAP 1。
  - 出生即接管、`ClaimCustody`、adopt 提交點取消、移除 `ReleaseAttachPending`／`ClearRevokedHandoff`、#1～#4 改寫。
  - 紅燈清單的新回歸未補。
  - SPEC-GAP 3（開埠在途 release 收斂後 `drain_timeout_epoch` 為 0 或 null）。
  - s24 黑箱 5 項既有紅燈。
  - 前輪列的 `route()` 結算、`next_at`、`custody.rs` 35 案未進 L1、`DeviceOnline` 重複送達、`human_active` 接線等。
- **本輪未驗：** 完整 sw-core／sw-daemon／workspace 測試、Clippy、Windows 交叉編譯、`tools.contracts check`、`policy_check`、host 完整 pytest、實機。

## 對裁決的異議（不計入結論）

無。

本輪驗證說明：探針與突變都在 `/tmp/solrev-p8-rust-refactor-p8-target` 下的拋棄式副本進行，結束時已刪除。未修改工作樹，未碰 production daemon、指定設定／狀態目錄或實體 UART。結束時 HEAD 仍為 `d7af8df8`、工作樹乾淨。

結論：可放行
