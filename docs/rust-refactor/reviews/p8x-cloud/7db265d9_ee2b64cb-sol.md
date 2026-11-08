> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

無（本輪為首輪，無前輪報告）。

## B 新發現

審閱範圍：`7db265d9..ee2b64cb` 共 4 個 commit（52caa517 紅燈骨架、45621d96 實作、1ee95bcd 黑箱、ee2b64cb L1.5 證據），無 merge commit。三個 L1 鎖變更皆為純新增（34／16／1 行，0 刪除）。`ADMISSION_METHODS` 33 項與 `contracts/rpc_schema.json` 以 `side_effects` 非空且 `retryable_readonly==false` 篩出的 35 項扣除 `daemon.stop`／`daemon.handoff_stop` 完全一致。定向實測（`CARGO_TARGET_DIR=/tmp/solrev-p8x-rust-refactor-p8x-target`）：`sw-core --test admission` 8 passed、`sw-cli --test update_guard` 14 passed、`sw-daemon --test handoff_stop` 13 passed，執行後無殘留 serialwrapd。未執行：完整 pytest、黑箱（需 dev bundle）、clippy。

**P8X-ee2b64c-1 [Medium]** `crates/sw-daemon/src/service.rs:681`（`Service::rpc` 對 `ADMISSION_METHODS` 於 dispatch 前 `admit`，`command.submit` 含在清單內）— 與 Python 基線的錯誤優先序不一致。Python 的 `command.submit` 不在 `_ADMISSION_METHODS`，admission 在 `CommandArbiter.submit()`（`sw_core/arbiter.py:230`）內、位於 `_dispatch` 的 selector 解析、`SourceCheck`、completion regex、`expect_reboot`、`CMD_TOO_LONG`、`CMD_CONTAINS_NEWLINE` 檢查**之後**；Rust 則在任何解析前就被 admission 擋下。失敗情境：交接成立後的 3 秒排空窗口內（或 `shutdown_freeze` 後）送 `{"method":"command.submit","params":{"selector":"COM9","command":"echo 1"}}`（COM9 不存在）。Python 回 `{"ok":false,"error_code":"SESSION_NOT_FOUND",...,"version":...}`；Rust 回 `{"ok":false,"error_code":"FLUSHED_BY_SHUTDOWN","reason":"handoff_frozen","version":...}`。同理 `command` 含 `\n`、超過 16 KB、非法 `completion_regex` 時兩邊錯誤碼不同。SC-11 契約只規定「有副作用的呼叫回 FLUSHED_BY_SHUTDOWN」，未定義與參數驗證錯誤的優先序，故以 Python 基線為準；現有測試（`handoff_ok_then_sc11_stop_order`、`handoff_frozen_rejects_mutating_methods`）皆用合法 selector，不會紅。修法：把 `command.submit` 自 `ADMISSION_METHODS` 移除，改在 `dispatch` 的 submit 路徑完成 selector 解析與參數驗證後、入列前 `admit("command.submit", com)`（對應 `arbiter.py:230-233`），`SESSION_NOT_READY`／`SESSION_QUEUE_FULL` 等入列失敗即 `done(ticket)`，成功才存入 `cmd_tickets`；或取得編排者／擁有者裁決改為 Rust 現行統一前置 admission 並記入 behavior_changes。

**P8X-ee2b64c-2 [Low]** `crates/sw-core/src/session_manager.rs:1301-1307` 與 `:3399-3403` — `probe_send` 在 `handoff_probes.frozen` 為真時建立即帶 `handoff_frozen`，但 `freeze_for_handoff` 撤銷時只對呼叫當下的 `probes.live` 快照 `thaw_handoff`；在快照與撤銷之間新登記的 PROBE fence 旗標不會被清除，該次 PROBE `\n` 會被 `Fence::admit` 以 `Frozen` 拒絕（`sent=false`）。影響僅限交接未成立時的單次偵測探測、下次 attach 可重試，Python 因整段持 `_lock` 無此窗口。修法：撤銷時改讀取當下的 `probes.live` 逐一 `thaw_handoff`，而非只用先前快照。

**P8X-ee2b64c-3 [Low]** `crates/sw-core/src/session_manager.rs:692-694` — 註解稱「在途 attach 越過 gate 後才建立的 session 承接凍結」，但兩條執行期建立路徑（`:1222`、`:1264`）都在同一把 `inner` 鎖內先檢查 `!inner.stopping`，而 `freeze_for_handoff` 在同一把鎖內同時設 `handoff_frozen` 與 `stopping`，故該分支實際不可達。無行為問題，建議修正註解以免誤導後續維護者認為存在此窗口。

**P8X-ee2b64c-4 [Low]** `tests/contract_blackbox/test_s24_update_rollback.py:224-232` — 計畫 09 Task 14 Step 1 的 `rollback_refuses_push_started_after_probe` 要求「與 `--force` 各一次」，本檔 `after_probe` timing 只跑 `force=False`；`test_rollback_force_refuses_while_push[rollback]` 走的是 `before_probe`。修法：對 `after_probe` 增加 `force=True` 參數化一列並同步 L1 鎖 add-only 登記。

**P8X-ee2b64c-5 [Low]** 交付範圍對照計畫 09 Task 14 Step 1 表：黑箱列 `rollback_cross_stage_state_compat_released`、`rollback_cross_stage_ready_never_double_held`（commit 1ee95bcd 已揭露「見回報」，且 `cases.yaml` 的 `s24-upd-rollback-cross-stage` 已釘為 evidence，Task 15 覆蓋判定會列 missing）、`supervision_set_force_refuses_rust_daemon_pushing`、`rollback_verifies_pairing`、`revert_refuses_while_new_rust_daemon_pushing`、`journal_recovery_refuses_while_new_rust_daemon_flashing`、`update_switch_window_relaunched_rust_daemon_flash_not_truncated`、`update_rust_daemon_exit1_after_handoff_not_relaunched`（後者計畫註明預設 ignored、擁有者授權）本輪皆未交付，commit 訊息僅揭露前兩列。非刪除、非削弱，故不列 High；請編排者決定是否為本 Task 收尾項或移交出口 gate。

**P8X-ee2b64c-6 [Low]** 計畫 Step 4／5 要求「以 `tests/test_stage0_handoff_stop.py` 對 Rust artifact 跑 A/B」；該檔 `_spawn_isolated_daemon` 直接啟動 `sw_core.daemon`，無 artifact 切換掛鉤，45621d96 亦未宣稱執行。`handoff_stop.rs` 已逐案鏡射其情境（docstring 標明對應 Python 測試名），建議於回報明寫此替代關係。

## 既有碼待辦（不計入結論）

- `crates/sw-daemon/src/service.rs` `Service::rpc` 只 `setdefault("version")`，Python `_versioned_response` 另會補 `code_fingerprint`；`crates/sw-daemon/src/` 全無 `code_fingerprint`。屬本輪之前既有差異，若已有裁決視為 Rust 不帶此欄則忽略。

## 對裁決的異議（不計入結論）

無。

結論：需修正
