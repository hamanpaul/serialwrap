# P7 審查：feature/rust-refactor-p7 `a0cad06..4221ba2`（Claude Opus，非實作者）

## 範圍與方法

- 範圍（first-parent）：`a089f9f`（merge P5，只看衝突解法與 P7 行為是否被破壞）、`e862522`、`397a473`、`a6b24f7`、`96c9f02`、`4221ba2`。
- 依據：`CLAUDE.md`、`docs/rust-refactor/plans/08-slice23-login-events.md`（Task 9／10／16-wire／17 與各「P7 修訂」）、
  parity-amendments #57／#117、前一份報告 `p7-cloud/01c474c_a0cad06-opus.md`；Python 基線用該 commit 內的 `sw_core/*.py`
  （行號以 `4221ba2` 樹內 `sw_core/session_manager.py` 為準）。
- 建置與測試：`git worktree add /tmp/claude-0/p7rev 4221ba2`。本機 rustup 的 1.96.1 工具鏈缺 cargo 元件，改以 stable cargo
  搭配釘定的 1.96.1 rustc（`RUSTC=~/.rustup/toolchains/1.96.1-…/bin/rustc`）跑：
  `cargo test --workspace --features test-fakes --no-fail-fast` → **2037 passed／8 failed／37 ignored**。8 筆失敗全是 root
  環境類（`daemon_process::{endpoint_written_after_bind, unreadable_config_isolated_fail_closed,
  wal_dir_readonly_at_start_blocks_login_probe, wal_fault_after_ready_splits_agent_and_auto_tx}`、sw-pal
  `serial::posix::tests::holder_scan_permission`、`lock::socket_probe_eacces_is_socket_unavailable`、
  `oserror_golden::file_eacces_matches_golden`、`rpc_client_conformance::unix_fixture_rows_match_python`）。我用
  `setpriv --reuid=65534` 逐筆重跑，**8/8 passed**。P7 自有測試全綠。
- 紅燈反證（在 worktree 還原修正後跑 cargo test，跑完已 `git checkout` 還原）：
  1. `a6b24f7`：拿掉 `recover_request` 內的 `crate::reprobe::reset(&mut self.reprobe)` → `recover_p7::recover_resets_reprobe_progress` **FAIL**，其餘 8 項維持綠。
  2. `397a473`：註解掉 `log_start` 的 `self.deps.rx.set_capture(Some(...))`（session_core.rs:4355）→ `capture_p7` 的 `foreground_rx_is_not_captured_and_counts_match_python_rules`、`no_drop_under_burst`、`detach_auto_stops_capture` **FAIL**（與 commit 所述相符）。
  3. `e862522`：註解掉 `daemon_main.rs:1166` 的 `engine.close_intake()` → `event_lifecycle::shutdown_closes_event_intake_before_drain_behavior` **FAIL**（`__B2__ match_recorded 50 筆`），與 commit 所述相符。
  4. 另寫一支探針測試（暫時附加在 capture_p7.rs，跑完已移除）證實下方 397a473-1 的 High。
- 凍結鎖：`tests/acceptance/l1_required.lock.json` 相對 `a0cad06` 沒有刪除任何列（純 add-only：e862522 +2、397a473 +7、a6b24f7 +9、96c9f02 +7）。相對 P5 `7af6567` 只少 `login_quiet::unsupported_profile_no_auto_tx` 與 `session_manager::detect_skips_passthrough_console_unlock_and_unsupported` 兩列，都是已登記的 P7-BO-1 §0.6 退役。
- platform cfg：範圍內新增的 `#[cfg(unix|windows)]` 全在 `crates/sw-pal`（`fs_ops.rs`、`proc_scan/posix.rs`、`rpc_transport.rs`）。sw-daemon 測試改用執行期 `MAIN_SEPARATOR` 判斷平台，沒有 cfg。
- Windows：沒有把 Windows 原生項目標成完成。P7-LW-2／LW-3 只補列待辦，Windows 分支明寫「未經原生執行」。
- pytest：本環境的 `python3.12`／`python3` 都沒有 pytest 模組，沒能重跑 pytest。4221ba2 的證據只核對了內容與雜湊（見該節）。

---

### a089f9f merge: 同步 P5 審查修正至 P7

用 `git show --remerge-diff` 逐一看了 17 個衝突檔。主要裁決：`LoginOutcome::Raised{exc,message}`、`classify_login_tx_failure`、
`Aborted` 只留 `_login_tx_refused` 為真與 RunCancel、P5 F15 取代 P7 的 quiet_bumps／Tick 收尾（P7-BO-3 已登記）、
`choose_plan` 保留 P7 版、`TEXT_NEWLINE`／`TEXT_NEWLINE_STR` 分名、CoreDeps 兩邊欄位都留、manager 用 P5 的逐 bridge
`RxShared` 加 P7 的 `start_observed`、resource_limits 與 skeleton guard 取聯集。這些都和 Python（`session_manager.py:83-98`
`_login_tx_refused`；`uart_io.py` `TxIncompleteError` 家族）一致，P7 測試全綠，沒有看到 P7 既有行為被破壞。
P7 測試期望改成 Python 值（`str(exc)`、`TxUnknownError`），屬於收緊，不是放寬。

1. [Low] `crates/sw-core/tests/login_fsm_full.rs`（`not_submitted_without_reject_is_cancelled`）— 合併後這支測試斷言的是
   `Raised{TxIncompleteError, "tx incomplete: not_submitted 0/1 draining"}`，和測試名字（cancelled）相反。之後有人看名字會讀錯意思。
   — 修法：鎖是 add-only，nodeid 不能改，所以在測試上方加註解說明「Frozen（draining）不屬 `_login_tx_refused`，名稱是歷史遺留」；
   另以正確名稱新增一支同斷言的測試，並登記 add-only。
2. [Low] merge commit 訊息（驗證段）— 寫的是「6 筆 root 環境類」，本次實跑是 8 筆：多了
   `daemon_process::unreadable_config_isolated_fail_closed` 和 `lock::socket_probe_eacces_is_socket_unavailable`，兩者降為 nobody
   後都 passed。證據不完整。— 修法：在 `p7-evidence/README.md` 補列這 8 筆（含 nobody 重跑結果），之後各輪以清單檔取代口述計數。

### e862522 fix(rust-p7): 審查修正第 8 輪

NO FINDINGS。

前輪 6 項都核對過：
- 0722f75-1：新行為測試存在，移除 `close_intake()` 後確實轉紅（反證 3）。
- 0722f75-2：子行程執行、stderr WARNING 斷言、`WatcherStopped` 起算 < 5.1 s，加上 `daemon.stop` 起算 < 5.6 s。起算點的修訂已登記在 08 Task 16-wire。
- 0722f75-3：改成執行期分支，Windows 分支已列入 P7-LW-3。
- 3030adc-1：`passed == 10`。
- 3030adc-2：`fire_completed_exit_code_follows_py_returncode` 已加入 L1 鎖。
- a0cad06-1：README 已補同範圍 3 次重跑紀錄。

### 397a473 feat(rust-capture): agent log 改由 reader 端寫入並對齊 Python 回應

1. [High] `crates/sw-core/src/session_core.rs:889-901`（`public()` 的 `"capture"` 子 dict）— 本 commit 拿掉了 actor 端
   `capture_rx` 對 `AgentLogCapture.{line_count,byte_count}` 的累加，計數改存在 `CaptureObserver` 的 `CaptureStats`。但
   `public()` 仍讀 `cap.line_count`／`cap.byte_count`，所以任何 session 快照裡的 `capture.line_count`／`byte_count` 永遠是 0。
   影響 `session.get_state`、`session list`、`log_status`／`log_start(already_active)`／`NO_ACTIVE_CAPTURE` 等回應裡的 `session`。
   Python `SessionRuntime.to_public_dict()`（`session_manager.py:818-824`）讀的是 `self.active_capture.line_count`，
   而這個值由 `_on_bridge_rx` 即時累加（`:4395-4396`）。
   - 具體輸入：`session.log_start` → RX `b"a\nb\n"` → `session.log_status`。
   - Python：頂層 `line_count:2,byte_count:4`，`session.capture` 是 `{…,"line_count":2,"byte_count":4}`。
   - Rust：頂層 `2／4`，但 `session.capture` 是 `{…,"status":"active","line_count":0,"byte_count":0}`。
   - 驗證：在 capture_p7 加探針測試（`rig_with_log_dir` → LogStart → `inject_rx(b"a\nb\n")` → LogStatus），實得
     `session.capture.line_count = Int(Small(0))`，斷言 FAIL。改動前 actor 端有累加，所以這是本 commit 引入的回歸。
   — 修法：`public()` 改為
     `let (lc, bc) = self.capture_observer.counts(&cap.capture_id).unwrap_or((cap.line_count, cap.byte_count));` 再輸出。
     同時加回歸測試 `capture_p7::session_capture_counts_are_live`（斷言 `log_status["session"]["capture"]` 的計數等於頂層計數），
     並用 `regen_lock.py --l1-add-only` 登記。也可以考慮刪除 `AgentLogCapture.line_count/byte_count`，避免出現兩份真相。
2. [Medium] `crates/sw-core/tests/capture_p7.rs` — 計畫 Task 10 表列的 `log_start_already_active`、`log_stop_without_capture`、
   `log_status_inactive_and_active`、`log_open_failed_detail_is_python_oserror_str` 都沒有同名或 actor 層測試。`log_responses`
   golden 只拿純函式組回應，`session` 直接取自 golden，所以抓不到上面的 High；actor 層的 `log_status` 測試只要比對 `session`
   就會紅。p7-fix-responses 只把 `slow_capture_fs_…` 列為未承接，這四項既沒登記也沒有逐名對照。
   — 修法：補四支 actor 層測試（`ActorRig`），`log_status_inactive_and_active` 要比對 session 的 `capture` 子 dict 計數；
   或在 p7-fix-responses 第 8 輪 Task 10 列逐名寫明由哪支既有測試覆蓋（06 `lease_selftest::log_capture_*`），沒覆蓋的就補上。
3. [Low] `docs/rust-refactor/reviews/p7-local-windows-todo.md`（P7-LW-3）— `capture_p7` 的期望位元組依賴
   `text_mode_bytes`（Windows 會是 `\r\n`），而 `recover_p7`／`session_rpc_p7` 也是新增測試，但 LW-3 的「含」清單沒列這三檔。
   — 修法：LW-3 補列 `sw-core capture_p7`、`recover_p7` 與 `sw-daemon session_rpc_p7`，判準照既有寫法。

### a6b24f7 feat(rust-recover): session recover 完整分支與 force recover

1. [High] `crates/sw-core/src/session_core.rs:4844-4851`（`start_force_recover`）— force 輪詢的時間格以 RPC 受理時刻為起點
   （`begin_stop(Clear)` 同時就排 `ForceRecoverPoll{n:1}` 在 now+1 s），但 Python 的時間格以 clear **完成**為起點。
   Python `_force_recover`（`:7632-7655`）先呼叫 `clear_session`，而 `clear_session`（`:2829-2885`）在有 bridge 時會同步
   `_await_drain(job)`（`:2872`；最多 `DRAIN_TIMEOUT_MS=3000`），等它返回才開始 `for _ in range(10): time.sleep(1.0)`。
   這不只是時間不同，回應內容也會不同：
   - 輸入 A（測試本身就是這個情境）：ATTACHED、`session.recover {"force":true}`，drain 回條在 t=1.5 s 到達，reattach 後馬上 READY。
     Python 在 t=2.5 s 回 `recovered:true`（1.5 + 1.0）；Rust 在 t=2.0 s 回。`recover_p7::force_recover_replies_on_one_second_poll_grid`
     斷言「2.0 s 回覆」，等於把偏離 Python 的值凍結成期望值，而且這支測試已在 L1 鎖內。
   - 輸入 B：drain 花 3 s（回條逾時，`_await_drain` 回 None），板子在 t=11 s 才 READY。Python 的輪詢點是 4…13 s，在 t=12 s
     回 `{"ok":true,"action":"FORCE_CLEAR_REATTACH","recovered":true,…}`；Rust 的輪詢點是 1…10 s，在 t=10 s 回
     `{"ok":false,"error_code":"FORCE_RECOVER_TIMEOUT",…}`。
   - `ForceKind::AfterPromptTimeout`（READY 救援鍵失敗後自動 force，Python `:6704-6720`）也走同一函式，有同樣的偏離。
   - 驗證方式：比對 Python 程式碼；既有測試通過本身就證明 Rust 在 2.0 s 回覆。
   — 修法：`start_force_recover` 只登記等待者並發 `begin_stop(Clear)`，等該 clear drain 結束（收到回條並完成 reattach 判定，或
   drain 逾時／stalled，或沒有 bridge 時同步 detach 完成）才 `schedule_force_poll(id, 1, now)`。測試改成 `n=1` 以 drain 結束 +1 s
   為準（輸入 A 期望 2.5 s），並新增 `force_recover_poll_grid_starts_after_clear_drain`（含 drain 逾時 3 s 的子案，期望第 10 次
   輪詢在 13 s）。已入鎖的測試要改期望值時，依 OD-16 以替換測試逐名處理，或請擁有者授權。
2. [Medium] `crates/sw-core/tests/recover_p7.rs` — 計畫 Task 9 程式碼區與表列的大多數測試沒有建立，包括 parity-amendments
   **必要**列：#57 `recover_during_drain_returns_session_recovering_immediately`（要斷言 `open_bridge_calls()==0`、沒有 StopOp
   waiter），#117 `recover_resets_credentials_warned`、`recover_not_ready_non_force_returns_not_ready`（含 `p7_gate:py_parity`
   的 Python 側特性測試）。另有 SC-34 `recover_reprobe_unrecovered_null_last_error`、SC-35 的
   `recover_passthrough_{attached_never_probes,ready_never_sends_keys,force_times_out}`、
   `ready_force_runs_keys_then_auto_force_and_returns_prompt_timeout`（約 14 s 虛擬時間與 `\x03`／`\x04`）、
   `recover_ready_force_success`、`recover_ready_quiet_skips_keys`、`recover_reprobe_failure_with_force_goes_to_force`、
   `released_recover_does_not_reset_credentials_warned`、`recover_no_bridge_device_absent`、`recover_non_ready_non_force`。
   p7-fix-responses 只籠統寫「由 06 既有行為涵蓋者未另立同名測試」。部分確實有 06 測試可對應（`command_exec::recover_released`、
   `recover_attached_reprobe_*`、`passthrough_recover_no_probe`、`quiet_rechecked_before_each_recover_key`、
   `login_quiet::attach_and_recover_reset_credentials_warned`），但 #57、SC-35 force、14 s 救援鍵＋自動 force 的端到端時間沒有
   對應測試。上面 High-1 的時序偏離也正好沒被任何端到端時間測試抓到。
   — 修法：補 #57／#117 兩組必要測試並 add-only 入鎖；其餘在 p7-fix-responses 第 8 輪 Task 9 列逐名對照既有 nodeid，沒有
   對應者補測試。
3. [Low] `crates/sw-core/src/session_core.rs:4720`（`plan_recover(..., released=false, ...)`）— 08 Task 9「P7 修訂」寫
   「by_id 在 `_released_by_ids` 的判定由 manager 處理（06 既有）」，但 manager 只在 `stopping` 時檢查
   （`session_manager.rs:2151-2162`）。非停機時，Python `recover_session`（`:7545`）對「state 不是 RELEASED、但 by_id 在
   `_released_by_ids`」的情況回 `{"ok":true,"released":true,"recommended_action":"device_attach",…}`；Rust 會往下走
   REATTACH／REPROBE。
   — 修法：讓 `SessionManager::route` 對 `Recover` 帶入 `released_by_ids().contains(by_id)`（在 `SessionRequest::Recover`
   加欄位，或 manager 先回覆），並修正計畫修訂文字。
4. [Low] `crates/sw-core/src/session_core.rs:4711-4800`（`recover_request`）— `plan_recover` 算出的 `Reattach`／`Reprobe`／
   `Force`／`ReadyKeys` 沒有被使用，後面又用 `conn_epoch`／`state`／`force` 重算一次分支，兩份邏輯日後可能分歧，而且
   `plan_recover_order_matches_recover_session` 鎖住的只是那份沒被使用的計畫。— 修法：後半改成 `match plan { Reattach => …,
   Reprobe => …, Force => …, ReadyKeys => … }`，刪除重算。
5. [Low] 登記編號不一致：08 Task 9「P7 修訂」與 p7-fix-responses Task 9 列寫 BLOCKED-UPSTREAM **P7-BU-1**，commit 訊息與
   `p7-local-windows-todo.md` 寫 **P7-BU-5**。— 修法：統一為 P7-BU-5（windows-todo 已有該列），並更正 08 與 fix-responses。

### 96c9f02 test(rust-daemon): Slice 2.3 session RPC 回歸鎖

NO FINDINGS。

7 項都對照了 Python：
- `service.py:1028` `force = bool(params.get("force"))`。
- `to_public_dict()` 鍵序（`session_manager.py` `to_public_dict`，含 `drain_stalled`／`drain_timeout_epoch`；POSIX 不加 `console_endpoint`）。
- log RPC 鍵序與 `RETRYABLE_READONLY_METHODS`。

這些都是既有行為的回歸鎖，沒有紅燈時點已誠實記入 red-runs.md。沒承接的 lease／Task 7 子案也已列出。

### 4221ba2 docs(rust-p7): pytest 失敗清單證據

1. [Low] `docs/rust-refactor/reviews/p7-evidence/README.md` — 證據可追溯：兩份清單的 sha256 前 16 碼（`2b8fe0b8f0458c21`、
   `5130dd36be4b5478`）和 README 一致，`comm` 比對結果是 +1／−29，與文字相符；+1 確實是
   `test_rust_goldens_fresh::test_golden_is_fresh[gen_allowlist_unreadable_config.py-…]`。不過「golden 內容隨 checkout
   路徑變動」這個成因解釋沒有證據。比較可能的原因是：該 generator 在 root 下以 setpriv 降權跑 `permission_denied` 列，而
   `/tmp/<0700 目錄>/worktree` 讓 nobody 無法進入目錄，例外類別因此改變，和路徑字串無關。另外清單命名為 96c9f02，實際 HEAD
   是 4221ba2（只有文件差異，可以接受）。本環境沒有 pytest，未重跑。
   — 修法：在 README 附上 generator 於兩處的實際輸出差異（`permission_denied` 列的 stderr hex），把成因改成可重現的描述；
   如果確定是父目錄權限造成，就在 P5 範圍登記讓 generator 偵測無法進入的父目錄並 skip 或 fail-closed。

---

## High／Medium 摘要表

| # | 嚴重度 | commit | 位置 | 摘要 | 驗證 |
|---|---|---|---|---|---|
| 397a473-1 | High | 397a473 | `session_core.rs:889-901` | session 公開 dict 的 `capture.line_count／byte_count` 恆為 0（計數改存 `CaptureStats`，`public()` 仍讀 `AgentLogCapture`）；Python `to_public_dict` 是即時值 | worktree 探針測試：LogStart → RX `a\nb\n` → LogStatus，頂層 2／4，`session.capture` 0／0，斷言 FAIL |
| a6b24f7-1 | High | a6b24f7 | `session_core.rs:4844-4851` | force recover 的 10×1 s 輪詢從 RPC 受理時起算；Python 在 `clear_session` 的 `_await_drain`（≤3 s）返回後才起算 → 回覆時刻不同，drain 慢時 `recovered:true` 與 `FORCE_RECOVER_TIMEOUT` 會互換；既有測試把 2.0 s（Python 2.5 s）凍結成期望值 | 對照 `session_manager.py:2829-2885／7632-7655`；`recover_p7::force_recover_replies_on_one_second_poll_grid` 通過即證明 Rust 在 2.0 s 回覆 |
| 397a473-2 | Medium | 397a473 | `tests/capture_p7.rs` | 計畫表列四支 log RPC 的 actor 層測試沒建立也沒登記；golden 只驗純函式，抓不到 397a473-1 | 逐名 grep crates/tests 與 L1 鎖都查無 |
| a6b24f7-2 | Medium | a6b24f7 | `tests/recover_p7.rs` | parity-amendments 必要列 #57／#117 與 SC-34／SC-35、14 s 救援鍵等計畫測試沒建立；fix-responses 沒有逐名對照 | 逐名 grep crates/tests 與 L1 鎖都查無 |

High 2、Medium 2、Low 7。
