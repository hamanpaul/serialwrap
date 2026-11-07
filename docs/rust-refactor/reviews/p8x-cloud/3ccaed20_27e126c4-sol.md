> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

無（本次無前輪報告，為 P8 Task 9 整包首審）。

## B 新發現

審查範圍：`3ccaed20..27e126c4` 共 5 個 commit（f3650d0c 紅燈測試、fd8b0013 實作、9289c358 SC-32 狀態回退、52a8b95f 測試台修正、27e126c4 stalled 去重），無 merge commit。已對照 Python 基線 `sw_core/flash_endpoint.py`、`service.py:363-567`（`_BridgeProbe`／`_flash_rx_observer`／`_on_flash_open`／`_watch_flasher_disconnect`）與計畫 Task 9 的行為 1～8 逐點核對；工作樹未改動。

已執行驗證（皆綠）：
- `cargo test -p sw-core --features test-fakes --test flash_coordinator --test flash_session_core`：36 passed、53 passed。
- `cargo test -p sw-core --features test-fakes --doc -- flash_endpoint`：4 passed（含 holder_watch／pump 兩個 `compile_fail`）。
- `python3 -m pytest -q tests/contract_blackbox/test_s24_flash_endpoint.py`（Python 基線，隔離 throwaway）：4 passed。
- `python3 -m tools.contracts check`：ok、無 findings。
- `python3 -m pytest -q tests/test_slice24_cases.py`：18 passed（SC-32 回 APPROVED 被接受）。
- `python3 -m pytest -q tests/test_resource_markers.py`：9 passed（新黑箱檔 marker 自洽）。

未執行：完整 pytest、Rust artifact 側黑箱（daemon 端點組裝屬 Task 10，commit 內文自述目前 4 失敗、屬預期）、`win-test.sh`。

核對結論：端點 thread 迴圈只看 `EndpointShutdown`、未匹配零寫入、probe 只送 flasher 自己的 sync（首讀為空才退回 pattern）、ACK 子字串命中後回放整段（保留最後 4096）、matched 後釋放其他 held 並解除全部 flash-critical、holder-probe 依 `pids` armed→歸零／1800 s 上限只 `cancel()` 本次 run、pump 每筆 `TxClass::Flash`＋`source:"flash"`＋`cmd_id:None`（與 Python `flash_tx` 相同）、`request_shutdown` 與 `current_run` 登記同鎖、`stop()` 逾時不關 master、`Drop` 仍 unlink、POSIX `link` 以暫名＋rename 取代 stale symlink（等價 Python 先 remove 再 symlink）、`session_core` 的 `flash_critical_skips_detach` 對齊 `session_manager.py:4443-4455` 的 `_flash_busy_locked` 過濾。凍結鎖 `l1_required.lock.json` 僅新增 nodeid（add-only）；52a8b95f 只改測試台 helper 等待條件、斷言未變。無 High／Medium。

- **P8X-27e126c-1** [Low] `crates/sw-core/src/session_core.rs:741`、`crates/sw-core/src/session_core/flash.rs:174` — 新增的 `flash_critical_skips_detach` 行為引用「編排者裁決 2026-11-08，rulings/s2.md『P8 T8 的兩項 SPEC-GAP』」，但該裁決不在 repo，也不在本次提供的 rulings/p8x.md 節錄，審查者無法追溯原文。行為本身與 Python 一致（略過不延後），無失敗情境。修法：由編排者把該裁決摘入 `rulings/p8x.md` 或 `docs/rust-refactor/reviews/p8x-fix-responses.md`，供後續輪次引用。
- **P8X-27e126c-2** [Low] `crates/sw-core/src/flash_endpoint.rs:194-201` — Python `FlashEndpoint._loop` 在呼叫 `on_flash_open` 前先 `admission.admit("flash.sync_probe")`，admission 已 `freeze()`（daemon 關機序）時直接略過；Rust 改以 `EndpointShutdown` 作唯一閘門。只要 Task 12 的停止序在 Python `admission.freeze()` 的同一時點呼叫 `FlashEndpoint::stop`／`request_shutdown`，外部行為等價；否則關機期間 flasher 開端點仍可能觸發 probe TX。無目前可重現的失敗情境。修法：Task 12 接線時把「先 `request_shutdown` 再 drain」寫進停止序並以測試釘住。
- **P8X-27e126c-3** [Low] `crates/sw-core/src/flash_endpoint/coordinator.rs:556-580` — `clone_writer()` 失敗或 holder watch thread `spawn` 失敗時，已送出 `FlashSyncMatched`（session 進 FLASHING）卻不鏡射 baud、不回放 ACK、不 pump，直接 `FlashHolderGone` 回 ATTACHED；Python 在 `slave_path` 取不到時仍照常 pump（只是沒有 watcher）。僅在資源耗盡路徑現形，無正常情境可重現。修法：spawn 失敗時改為 WARNING 後仍進入 mirror／ACK／pump（run 只靠 pump 結束條件收斂），與 Python 對齊。

## 既有碼待辦（不計入結論）

- `FlashCoordinator::new` 透過 `SessionManager::add_rx_observer` 登記觀察者，而觀察者在 bridge 建立時取快照（`session_manager.rs:1996`）：Task 10 組裝 daemon 時必須在 bootstrap attach 之前建立 coordinator，否則既有 bridge 的 RX 永遠進不到 probe 緩衝（coordinator 文件註解已載明，列此提醒）。

## 對裁決的異議（不計入結論）

無。

結論：可放行
