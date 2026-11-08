> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

審查完成。工作樹未改動（`git status` 乾淨），只在 `/tmp` target 目錄建置與執行定向測試。

## A 前輪 finding 核對

無（本輪無前輪報告）。

## B 新發現

**P8X-6eaf5bb-1** [Medium] `crates/sw-daemon/tests/shutdown_order.rs:213`（另見 :235、:261；`tests/contract_blackbox/test_s24_supervision_systemd.py:1`）— 計畫 09 Task 12 列出的測試有四項未交付或斷言弱於計畫，commit 訊息未說明略過理由：
- `multiple_failures_accumulate_in_stop_order`（計畫 `shutdown_multiple_failures_accumulate_exits_1`）只注入 flash＋handler 兩種失敗；計畫要求 flash＋handler＋drain stalled＋WAL sync 四種同時注入並斷言單行順序。失敗情境：若 `drain stalled` 與 `WAL sync failed` 的累積順序或串接被改錯（例如 WAL 失敗時提早 return），現有測試仍綠。
- `stopping_gate_before_watcher_stop`（計畫 `shutdown_hotplug_after_phase1_never_opens_uart`，R1-F2 標「修正前必 FAIL」）缺少對 COM0 注入 `DeviceDetached(Rebound)`＋新 real_path、`spawned_worker_count` 不增、WAL 無 `usb-NEW` 紀錄、log 含「shutdown 中，略過 attach」等斷言。失敗情境：Phase 1 之後的 Rebound reconcile 若重開 UART，現有測試不會紅。
- 計畫列的 `shutdown_inflight_attach_drained_before_wal_close`（越過 gate 的在途 attach 必須納入 drain 並於 WAL close 前 close）與黑箱 `stop_during_flashing_removes_ttymcu`（FLASHING 中 `systemctl --user stop`）完全缺席。
- `order_is_exactly_1_to_7` 未如計畫啟用一條 event rule，`engine.stop` 在無 handler 情況下的回條為空是平凡成立。
確切修法：補齊上述四個案例（或在 PR／fix-responses 寫明由哪個既有測試覆蓋、為何免加並經編排者裁決），並以 `regen_lock --l1-add-only` 登記。

其餘本輪變更（`stop_accept`／`join_handlers` 拆分、Phase 1 屏障、`FLUSHED_BY_SHUTDOWN` 收尾、重複訊號只記 log、`StallConsole`／`HookedHandler` 僅 test-fakes、L1 鎖只增）我未發現具體失敗情境。

## 既有碼待辦（不計入結論）

- `crates/sw-daemon/tests/daemon_process.rs:310` `sigterm_graceful_exit_0_and_flushes_queue` 間歇紅燈，與本輪無關。四 thread 併跑時 1／6 失敗（實際 flushed 只有 c3），單跑 5／5 綠。根因推測：`submit "blk"` 在 `wait_socket` 後立即送出、未等 READY；負載高時 blk 被拒，q2 成為第一筆寫入（BlockUntilCancel）而非排隊，最終碼為 in-flight 中止而非 `FLUSHED_BY_SHUTDOWN`。改法：submit 前先 `wait_state(COM0, READY)`。編排者 host 全套若見此紅燈，請先以此判讀。
- `crates/sw-daemon/src/daemon_main.rs:1296`→`:1309`：Python `service.stop()` 在 `begin_shutdown` 後立即對全部 session `flush_session(FLUSHED_BY_SHUTDOWN)`；Rust 延到 Phase 1。`watcher.stop`（≤2 s）與 `flash.stop`（≤2 s）窗口內若前景命令完成，actor 會起下一筆排隊命令並對 DUT 送 TX。此窗口在 P5 舊順序（先等 handler 20 s）更大，屬計畫既定順序，非本輪引入。
- `crates/sw-core/src/session_manager.rs:3400` Phase 1 屏障逐 actor 串行等待；單一 actor thread 卡死會吃掉全部 5 s，使其餘 session 在 Phase 2 被連帶列為 stalled（exit code 不變，只是診斷清單變多）。
- 計畫 `09-slice24-transfer-flash.md:2085` 寫 `stalled_threads:["serialwrap-ttyMCU"]`，Task 9 實作為 `endpoint`／`pump`，測試依實作斷言 `endpoint`；計畫文字待對齊。

## 對裁決的異議（不計入結論）

無。

---

實測紀錄（皆在 `/tmp` 隔離目錄，未碰 production daemon）：
- `cargo test -p sw-daemon --features test-fakes --test shutdown_order --test framing`：9＋20 全綠；`--test daemon_process` 33 案僅上述間歇案 1 次紅。
- systemd 黑箱 `test_s24_supervision_systemd.py`：Python 基線 3 passed（31.7 s）、Rust debug daemon 3 passed（4.9 s；push 停止案 1.45 s），補上 commit 自述「本機未實跑」的證據缺口。transient 單元已清理。
- `regen_lock.py --check --base 2c5018f1`：L1 鎖只增、結構一致。
- `tests/test_resource_markers.py` 9 passed。

結論：需修正
