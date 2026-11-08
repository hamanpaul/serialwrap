> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

基線（6eaf5bbb）副本的 daemon_process 建置與連跑仍在背景進行；結果到達後我會比對失敗集合並產出最終報告。

## A 前輪 finding 核對

無（本次無前輪報告；commit 訊息引用的 P8X-6eaf5bb-1 未列入本次輸入，不另行核對）。

## B 新發現

**P8X-8b5cbe4-1** [Medium] crates/sw-daemon/tests/daemon_process.rs:343 — 本輪之後 `sigterm_graceful_exit_0_and_flushes_queue`（L1 鎖內三個集合都登記）在 daemon_process 全集下穩定失敗，6eaf5bbb 同環境穩定通過。

- 失敗情境（實測，HEAD 8b5cbe47，`CARGO_TARGET_DIR=/tmp/solrev-p8x-rust-refactor-p8x-target`）：

| 版本 | 執行方式 | 結果 |
|---|---|---|
| 8b5cbe47 | `cargo test -p sw-daemon --features test-fakes --test daemon_process`（33 案並行） | 2 次皆 FAILED，失敗集合相同（僅此案） |
| 8b5cbe47 | 單跑此案 | 3/3 通過 |
| 6eaf5bbb（`git archive` 匯出到 /tmp 建置） | 同全集執行 | 2/2 全通過 |

- 實際與預期差異：`SessionsDrained.flushed` 預期為 `[(c2, FLUSHED_BY_SHUTDOWN), (c3, FLUSHED_BY_SHUTDOWN)]`，實際只有 `[(c3, FLUSHED_BY_SHUTDOWN)]`；trace 其他 11 步（含本輪新增的 `drained: [("t:COM0", true)]`）皆相符。即排在 BlockUntilCancel 前景命令後面的第一筆排隊命令 c2 在停止序中沒有拿到新的 `FLUSHED_BY_SHUTDOWN`。
- 依 CLAUDE.md 測試政策，紅燈預設視為真實回歸；兩次失敗集合相同，不符「環境問題」的舉證門檻。本輪對產品碼的改動只有 test-fakes 計數器與 trace 欄位，我未能在時限內定位根因；若根因在 drain Phase 1 的 fence 凍結與 RunCancel 順序（c2 於取消後被 worker 撿起執行而非被沖刷），則屬 1:1 外部行為偏差，應升為 High。
- 修法：先以同全集重現並記錄 c2 實際拿到的 error_code（在 `SessionsDrained` 前 dump `registry.error_codes()`），據此修正產品碼或補正測試對 c2 進入佇列的同步條件；修正後需在 daemon_process 全集下連跑兩次綠燈，不得以單跑結果放行。

**P8X-8b5cbe4-2** [Low] crates/sw-core/src/session_manager.rs:1009 — `spawned_workers` 在 `thread::Builder::spawn` 之前遞增，spawn 失敗時計數仍多一（test-fakes 專用，無具體失敗情境）。修法：移到 `spawned.is_ok()` 分支內遞增。

其餘本輪變更核對結果（無 finding）：
- `shutdown_order` 11 案全綠（含兩個新案）；`spawned_worker_count` 的兩個遞增點與 `spawn_actor`、attach worker 的唯一生成點一一對應，login／job queue／manager thread 不在計數語意內，與 doc comment 一致。
- hotplug 新案的 WAL 斷言使用的 `com` 欄位確實存在於 Rust WAL 紀錄，非空轉斷言。
- `regen_lock.py --check --base 6eaf5bbb` 通過，L1 鎖為純新增。
- `test_resource_markers.py` 9 passed；新黑箱案例 collect 正常。
- 新黑箱案例 `test_stop_during_flashing_removes_ttymcu` 以隔離 transient unit 實跑 Python 基線與 Rust debug 二進位各一次皆通過（journal 證實兩側各自的 daemon 真的被執行），補上了實作者「本機只 collect」的證據缺口。

## 既有碼待辦（不計入結論）

- `multiple_failures_accumulate_in_stop_order` 斷言的 flash 端點 stalled thread 名為 `endpoint`，計畫表（09-slice24 §Task 12）寫 `serialwrap-ttyMCU`；此命名在 6eaf5bbb 已存在，本輪只擴充該行其餘段落。
- hotplug 新案斷言的 gate log 為 `shutdown 中，略過 attach <by_id>`，計畫表寫的是 by-id 路徑；訊息文字源自 6eaf5bbb 的產品碼。

## 對裁決的異議（不計入結論）

無。

結論：需修正
