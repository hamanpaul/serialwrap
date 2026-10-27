# feature/rust-refactor-p5 b7bf34a..e36089f 非實作者審查（Claude Opus）

- **範圍**：`origin/feature/rust-refactor-p5`，`b7bf34a..e36089f`，共 5 個 commit、18 檔、+680/-117：`21de0db`、`620ca8e`、`9fdd89d`、`d46c323`、`e36089f`。
- **1:1 基準**：本分支 `sw_core/*.py`，逐段對照了這些函式：`session_manager.py` 的 `clear_session`、`_complete_drain`、`_spawn_attach`、`_attach_by_id`、`_reresolve_profile_on_reattach`、`_push_tx_gate`、`reset_passthrough_boot_gate`、`interactive_open`、`get_background_result`、`_force_recover`、`_login_tx_refused`；`service.py` 的 `command.result_tail`、`result.tail`、`_bg_fallback_from_arbiter`、`session.interactive_open`；`wal.py` 的 `_unhealthy_reason`。
- **依據文件**：`docs/rust-refactor/serialwrap-rust-spec-final.md`、`owner-decisions-2026-10-15.md`、`/home/user/pa/docs/rust-refactor/plans/parity-amendments-2026-10-26.md`、`06-slice21-daemon-core.md`（Task 19 的 main.rs 規格、`server.stop(Duration::from_secs(10))`、Review Focus 3 與 5）。
- **前次報告**：`1f5aafa_b7bf34a-opus.md`，以及 F59～F88 所回應的 `b0f2853_8a7986e-{opus,grok}.md`、`8a7986e_edfc741-{opus,grok}.md`、`edfc741_1f5aafa-{opus,grok}.md`；處理紀錄 `docs/rust-refactor/reviews/p5-fix-responses.md`。
- **審查方式**：
  - 唯讀 detached worktree `/home/user/rv-p5`（e36089f），`CARGO_TARGET_DIR=/home/user/tgt-p5`。
  - 反證用 scratch worktree `/home/user/rv-p5-scratch`，target 為 `/home/user/tgt-p5-scratch`。每次只撤銷一處產品修正就實跑對應測試，跑完即還原，`git status` 保持乾淨。
  - cargo 一律加 `-j2`。本機執行身分為 root；非 root 證據以 `setpriv --reuid=65534` 執行同一個測試 binary 取得。
  - 黑箱比對用 `SERIALWRAP_DAEMON_EXE` 切換 Python 與 Rust daemon，探測用的暫時案例跑完即刪除。
- **結論**：
  - **High：0。**
  - 前輪 High F61、F62、F63、F73、F82、F87 都有回歸測試。只撤銷產品修正時，9 個突變全部讓測試轉紅（見下表第 6～14 列）。
  - F73 與 F63 另外以真 daemon 黑箱比對，Python 與 Rust 輸出一致。
  - **Medium：2。**
    1. 有 bridge 的 `session.clear`，RPC 回應的 `last_error` 仍是 null，Python 是 `CLEARED`。黑箱實測 Python 3/3、Rust 3/3。這是第三輪仍未處理的前輪 Medium。本範圍 `21de0db` 改寫了同一段，卻保留了這項錯誤。
    2. 處理紀錄宣稱涵蓋「F59～F87」，但多項前輪 Medium 沒有任何列，而且有懸空引用（`見 F67`）。
  - **Low：6。**

## 實際執行的驗證命令與結果

| # | 命令（cwd） | 結果 |
|---|---|---|
| 1 | `cargo test -j2 -p sw-core -p sw-pal -p sw-daemon --features test-fakes --no-fail-fast`（rv-p5） | 失敗 6 支，全部是 root 造成：<br>• 既知基準 3 支：`holder_scan_permission`、`file_eacces_matches_golden`、`unix_fixture_rows_match_python`<br>• 本範圍 F81 刻意改成 root 下 FAIL 的 3 支：`endpoint_written_after_bind`、`wal_dir_readonly_at_start_blocks_login_probe`、`wal_fault_after_ready_splits_agent_and_auto_tx`<br>其餘全綠。本次 `command_exec::reboot_flushes_commands_queued_behind_it` 也通過 |
| 2 | 第 1 列中 F81 的 3 支，以 `setpriv --reuid=65534 --regid=65534 --clear-groups` 執行，cwd 為 `crates/sw-daemon` | `endpoint_written_after_bind` 2/2 PASS；`wal_fault_after_ready_splits_agent_and_auto_tx` 2/2 PASS；`wal_dir_readonly_at_start_blocks_login_probe` 26/27 PASS。唯一一次失敗發生在與其他 cargo 編譯同時執行時，之後無負載連跑 25 次全過，未抓到失敗訊息（見 d46c323 第 4 項） |
| 3 | `daemon_process` 的 `daemon_stop_from_second_connection_during_force_recover` 與 `daemon_stop_during_force_recover_with_client_gone`，各以 `--exact` 跑 3 次（root） | 6/6 PASS，前者每次 23～30 s |
| 4 | `python3.12 -m pytest tests/contract_blackbox/slice21/test_safety_core.py::{test_pipelined_stop_after_client_gone_not_processed,test_daemon_stop_from_second_connection_during_force_recover}`，Python 基線與 `SERIALWRAP_DAEMON_EXE=/home/user/tgt-p5/debug/serialwrapd` 各跑一輪 | Python 2 passed；Rust 2 passed |
| 5 | 黑箱探測（暫時案例，跑完即刪）：`shell_daemon` 在 READY 時呼叫 `session.clear`，Python／Rust 各 3 次 | **Python：`{"state":"ATTACHING","last_error":"CLEARED"}` 3/3。Rust：`{"state":"ATTACHING","last_error":null}` 3/3**（見 21de0db 第 1 項） |
| 6 | 反證 m1：`finish_stop_without_worker` 移除 `request_reattach`（F61） | `session_core::clear_without_bridge_with_device_reports_attaching_and_reattaches` **FAILED**（`DETACHED` ≠ `ATTACHING`） |
| 7 | 反證 m2：`Effect::Reattach` 改為直接 `send_attached`，跳過再解析（F62） | `session_manager::pin_then_clear_on_attached_session_applies_pin` **FAILED**（等待逾時） |
| 8 | 反證 m2b：`reattach_existing` 移除 `op.after_clear && state == "ATTACHING"` 例外（F62） | 同上 **FAILED** |
| 9 | 反證 m3：drain 收尾恢復 `self.quiet = QuietState::default()`（F63） | `session_core::boot_quiet_survives_clear_drain_and_blocks_reattach_probe` **FAILED**（drain 後 `boot_quiet_remaining_s` 為 Null） |
| 10 | 反證 m3b：`adopt_opened_bridge` 不把 quiet／reconfirm 推回 fence（F63） | 同上 **FAILED**（「新 bridge 的 TX fence 須帶回 quiet」） |
| 11 | 反證 m4：`session.interactive_open` 改回 `self.route(&selector, …)`（F73） | `dispatch::interactive_open_unknown_selector_session_not_ready` **FAILED**（回 `SESSION_NOT_FOUND`） |
| 12 | 反證 m5：`result_tail` 改回 `SessionRequest::ResultTail`，即每個 session 都帶 fallback（F76） | `dispatch::result_tail_stale_owner_finds_capture_in_other_session` **FAILED**（COM0 先回空 chunks） |
| 13 | 反證 m6：`main.rs` 改回 `manifest_absent`（F82） | `launcher::launcher_rejects_rust_implementation_in_stage1` **FAILED**（`serialwrapd: 不支援 daemon implementation：rust`） |
| 14 | 反證 m7：`SERVER_STOP_BUDGET` 改回 10 s（F87） | `daemon_process::daemon_stop_from_second_connection_during_force_recover` **FAILED**（exit 1，`shutdown incomplete: rpc handlers did not converge`） |
| 15 | 黑箱探測：READY 後送 `U-Boot 2024.01` banner，再 `session.clear`，等 3 s。Python／Rust 各 2 次 | 兩邊一致：`clear` 回應 ATTACHING；之後 `ATTACHED`、`PROMPT_UNAVAILABLE`、`ready_reconfirm_pending:true`；clear 後假 DUT 收到 0 bytes |
| 16 | 黑箱探測：`session.interactive_open {"selector":"NOSUCH"}`，Python／Rust | 兩邊皆為 `{"ok":false,"error_code":"SESSION_NOT_READY","selector":"NOSUCH"}` |
| 17 | `python3 tests/acceptance/regen_lock.py --check --base b7bf34a` | 兩項 OK |
| 18 | 逐名核對 L1 鎖 | 鎖檔純新增 8 個名稱、0 刪除。本範圍新增的 `#[test]` 也是 8 支（session_core 3、session_manager 1、daemon_process 1、dispatch 2、launcher 1），8/8 已登記；8/8 名稱都有對應的 `fn` |
| 19 | `python3 scripts/rust/check_cfg_boundary.py` | rc=0。新增的 `#[cfg(feature = "test-fakes")]` 只是 feature gate，不是平台 cfg |
| 20 | `cargo check -j2 --workspace --all-targets --target x86_64-pc-windows-gnu` | 通過（rc=0）；警告都在 sw-cli／sw-proto／sw-pal 的既有檔案，不在本範圍 |
| 21 | `python3.12 -m pytest -q tests/test_slice21_scope.py tests/test_slice21_skeleton_guards.py` | 12 passed、1 skipped |
| 22 | `python3.12 -m tools.contracts check` | `ok:true`，findings 空 |
| 23 | `python3.12 -m policy_check --repo .` | rc=0；R-19、R-22 為 WARN，其餘 pass |

---

### 21de0db fix(sw-core): session clear 經 manager 再解析後才重新 attach（pin+clear 生效）

**F61 的對照**（Python `clear_session` 無 bridge 分支，`session_manager.py:2863-2869`）：

- 裝置在線 → `state="ATTACHING"`、`last_error=None`，然後 `_spawn_attach`。
- 裝置不在 → `DETACHED`＋`CLEARED`。

Rust `finish_stop_without_worker` 經 `request_reattach` 做到同樣的事。具體輸入：開埠 AccessDenied 後 clear，Python 與 Rust 都回 `ATTACHING`／null。

**F62 的對照**：Python `_complete_drain`（`:2757`）→ `_spawn_attach` → `_attach_by_id` →（開埠前）`_reresolve_profile_on_reattach`（`:5091-5107`；pin 可解析就 rematerialize，yaml-target 不動）。Rust 改為 `Effect::Reattach` → `spawn_attach_with(after_clear=true)` → `reattach_existing`，路徑與順序一致。反證 m2、m2b 都會轉紅。

1. **[Medium] `crates/sw-core/src/session_core.rs:3009-3014`（drain 收尾的 Clear 分支）**
   - **問題**：有 bridge 的 `session.clear` 在回覆等待者**之前**就執行 `set_last_error(None, None)`，所以 RPC 回應的 `session.last_error` 是 null。
     - Python `_complete_drain`（`:2746-2747`、`:2757-2759`）在鎖內設 `last_error="CLEARED"`、`state="ATTACHING"`，然後才 `_spawn_attach`。
     - `clear_session` 在 `_await_drain` 之後立即組回應（`:2887`）。
     - attach thread 要等 preopen 與再解析完成後，才在 `_attach_by_id`（`:4638-4641`）把它設成 None。
   - **實測**（第 5 列）：Python 回 `"last_error":"CLEARED"` 3/3；Rust 回 null 3/3。
   - **歷史**：這是 `edfc741_1f5aafa-opus` 與 `1f5aafa_b7bf34a-opus` 連續兩輪的 Medium。本 commit 改寫了同一段（`reopen_known_device` 改為 `request_reattach`），仍沿用「ATTACHING 期間不帶 CLEARED（P5-T20 黑箱）」的註解並保留清除。
   - **註解依據不成立**：`drain_release.rs:1652-1654` 引用的 P5-T20 觀察是回應之後再查詢的 `get_state`，不是 clear 的回應本身。
   - **處理紀錄**：`p5-fix-responses.md` 仍沒有這一列。
   - **修法**：
     - 刪除 `:3009-3014` 的 `set_last_error(None, None)`。core 在 `reattach_pending` 時接受 `DeviceAttached`，原本就會在開埠前 `set_last_error(None, None)`（`session_core.rs` DeviceAttached 處理，`awaiting` 分支），時序正好對應 Python `_attach_by_id`。
     - 無 bridge 分支（`finish_stop_without_worker`）維持 None，因為 Python 在這裡就是 None。
     - 測試：在 `drain_release::clear_reopen_attaching_clears_last_error` 斷言回覆的 `session.last_error == "CLEARED"`，並在 `DeviceAttached` 之後斷言 `public()["last_error"] == Null`。
     - 把本項補進處理紀錄。

2. **[Low] `crates/sw-core/src/session_manager.rs:2125-2135`（`SessionManager::clear`）**
   - **問題**：manager 仍保留舊邏輯：clear 回覆為 `DETACHED` 時直接 `send_attached(&by_id)`。這會繞過 F62 的再解析。
     - F61 修正後，core 只在裝置不在線時回 DETACHED，此時 `send_attached` 因 `device_path` 為 None 而成為 no-op，所以這條路平常是死碼。
     - 但 manager 的 `devices` 與 core 的 `device_present`／`known_real_path` 可能不一致（例如 core 從沒收到 `DeviceAttached`，`known_real_path` 為 None，而 manager 已有裝置）。這時會以舊 profile 直接開埠，Python 則會走 `_spawn_attach` 再解析。
   - **修法**：刪除該分支；或改成 `self.shared.spawn_attach_with(&by_id, true)`，讓所有 clear 後的 attach 都經再解析。

3. **[Low] `crates/sw-core/src/session_manager.rs:851`、`:884-887`**
   - **問題**：`Effect::Reattach` 遇到 `inflight.contains_key(by_id)` 就直接 return，after_clear 的請求因此遺失。Rust 的 attach worker 結束後也沒有 Python `_spawn_attach` 的 `retry_after_worker`（`:3856-3866`：bridge 為 None 且 `ATTACHING` 且裝置在線時再 spawn 一次）。
   - **後果**：若當時在途的是一般 op（`after_clear=false`），`reattach_existing` 會看到 `ATTACHING`，直接 `send_attached` 而不再解析。core 在 `reattach_pending` 時接受它，於是以舊 profile 開埠，pin 不生效。如果在途 op 已過了送 `DeviceAttached` 的時點，session 還會停在 `ATTACHING`。
   - **評等**：窗口窄，例如熱插 attach 剛好與 clear 重疊，列 Low。
   - **修法**：
     - inflight 命中時把 `after_clear` OR 進既有 op（改為 `Arc<AtomicBool>`）。
     - worker 結束後，依 Python 條件（非 stopping、非 released／draining、裝置在線、session 為 `ATTACHING` 且無 bridge）再 `spawn_attach_with(by_id, true)` 一次。
     - 補一支 manager 測試，讓 inflight op 與 clear 重疊。

4. **[Low] `docs/rust-refactor/reviews/p5-fix-responses.md:86`（F62 列）**
   - **問題**：recover／io_error 收尾的再 attach 寫成 deferred，「列為 Slice 2.1 後續項」，但沒有指名承接處（Task 編號、計畫檔或 issue）。處理紀錄的規則要求 deferred 必須附承接處。
   - **Python 行為**：`_complete_drain` 在 `:2775-2781` 對 recover／io_error 同樣 `_spawn_attach`，所以 pin 加 recover 的情境仍與 Python 不同。
   - **修法**：在 06 計畫或 parity-amendments 登記具體承接項（列出受影響的 drain_release／session_core 期望清單），F62 列改為引用該項。

### 620ca8e fix(sw-core): drain 保留 boot quiet，新 bridge adopt 時推回 TX fence

**F63 的對照**：Python 的 `boot_quiet_until`／`ready_reconfirm_pending` 掛在 `SessionRuntime` 上，`_complete_drain` 只呼叫 `_reset_reprobe_progress_locked`。新 bridge 由 `_push_tx_gate(bridge)` 帶回 gate（`:695-708`、`:4836`）。passthrough 重掛時呼叫 `reset_passthrough_boot_gate`（`:644-654`、`:4858`）。

Rust 的做法：

- 不再於 drain 歸零 quiet。
- adopt 時把 `quiet_active` 與 `reconfirm_pending` 推回 fence；quiet 仍有效時為新 epoch 排 `BootQuietExpired`。
- passthrough 在 adopt 時清除 quiet。Python 是在登入結果之後清，但 passthrough 不做登入，可觀察行為相同。

驗證結果：

- 反證 m3、m3b 都會轉紅。
- 黑箱（第 15 列）中 Python 與 Rust 完全一致，clear 後 0 bytes TX。

NO FINDINGS.

### 9fdd89d fix(sw-daemon): interactive_open 查無 session 回 SESSION_NOT_READY；result_tail 先全域查 capture

**F73**：對照 Python `interactive_open`（`session_manager.py:6821-6823`）與 `service.py:1091-1099`，黑箱與測試都是全文比對；反證 m4 轉紅。

**F76**：Python `command.result_tail`／`result.tail` 先 `get_background_result`（`_background` 是 manager 層的全域 dict），只有 `CMD_NOT_FOUND` 時才 `_bg_fallback_from_arbiter`（`service.py:1178-1201`）。

- Rust 改為依序向「命令紀錄的 session、其餘 session」各問一次 `BackgroundResult`，全部查無才呼叫 `bg_fallback_from_registry`。
- `bg_fallback_from_registry` 的判斷逐行對應 `_bg_fallback_from_arbiter`：非 background、done、`started_at` 非 null 的 canceled 都回 `CMD_NOT_FOUND`。
- 反證 m5 轉紅。

**F74**：`QueueLensForTest`、`session_queue_lens`、`set_session_id_for_test` 都在 `cfg(feature = "test-fakes")` 之內，產品 build 不會帶入。

1. **[Low] `crates/sw-daemon/src/service.rs:281-286`（`result_tail` 迴圈中的 `?`）**
   - **問題**：任何一個 session 的 `route` 回 `Err`（actor 已結束或逾時），整個 RPC 就以例外結束。Python 只查 dict，不會因此失敗。
   - **為何本 commit 放大了影響**：修正前在第一個 session 回 ok（含 fallback）時就結束；修正後，capture 尚未建立的常見情況（背景命令排隊中）會逐一詢問**所有** session。
   - **修法**：迴圈內對 `Err` 改成 `continue`；全部失敗時仍照常走 `bg_fallback_from_registry`。

### d46c323 fix(sw-daemon): rust manifest 交 daemon 本體；停機等 handler 上限依 Python force recover 推導；root 證據不足改 FAIL

- **F82**：`dispatch_to_rust_daemon` 與計畫 main.rs 規格一致（`:4469`）：`"python"` 交 launcher；`"rust"` 或無 manifest 交 `daemon_main::run`；不可讀或解析失敗 fail-closed 交 launcher。單元與端到端都有覆蓋，反證 m6 轉紅。
- **F87／F88**：
  - Python 基線黑箱已提交，兩邊各實跑一輪都 PASS（第 4 列）。
  - Rust 的收斂訊號改為 WAL 救援 TX `Aw==`，並等過 11 s 才判斷存活，修掉了「起始即 READY」的假收斂。
  - 新增「停機與 handler 並行」案例；反證 m7 轉紅。
- **F81**：我核對了 Python `wal.py:662-668`：append 失敗才設 `write_failed`，在此之前為 None。也核對了 `_login_tx_refused`（`:83-90`）只吞 quiet／stale_login_gen。所以兩個期望值依 Python 更正有據。非 root 實跑結果見第 2 列。

1. **[Low] `crates/sw-daemon/src/daemon_main.rs:55-60`**
   - **問題**：`SERVER_STOP_BUDGET` 由 10 s 改為 20 s，偏離 06 計畫 `:4457` 寫死的 `server.stop(Duration::from_secs(10))`。理由（Python 無上限等待，force recover handler 實測約 14 s）只寫在 F87 列的處理文字裡。BLOCKED-OWNER 只涵蓋「Review Focus 5 文字」，沒有涵蓋這個常數。
   - **為何需要正式登記**：這是 SC-11「停機未收斂即 exit 1」的判定門檻，屬 Rust-only 細化，計畫值應經 §10.4 或 parity-amendments 修訂。
   - **修法**：在 parity-amendments 登記 06 計畫 `:4457` 與 Review Focus 5 的「最長約 10 s」更正為 20 s（附推導：救援鍵約 4 s ＋ clear drain ≤3 s ＋ 10 × 1 s），並在 F87 列引用這筆修訂。

2. **[Low] `crates/sw-daemon/tests/daemon_process.rs:554-578`**
   - **問題**：Rust 案例的註解寫「同一組斷言」，但實際跑的情境不同：
     - Python 對照案例用 shell DUT，stop 後到 exit 約 8.2 s，斷言 ≤15 s。
     - Rust rig 用登入型 DUT，handler 跑到 `FORCE_RECOVER_TIMEOUT`，斷言放寬為 ≤25 s。
   - 實測單支 23～30 s（含啟動）。距 25 s 上限多近無法得知，因為測試不輸出 elapsed。
   - **修法**：
     - 改用 shell 型假 DUT（或讓假 DUT 在登出後回 prompt），對齊 Python 的 READY 路徑，並沿用 15 s 斷言。
     - 若維持登入型，就在失敗訊息與成功時都印出 `started.elapsed()`，並在註解說明這不是 Python 同情境。

3. **[Low] `crates/sw-daemon/tests/launcher.rs:310-355`**
   - **問題**：鎖定的 nodeid `launcher_rejects_rust_implementation_in_stage1` 現在驗的是「rust manifest 進入 daemon 本體」，名稱與行為相反，日後讀 L1 報告容易誤判。nodeid 已入鎖、不能改名，這點可以理解。
   - **修法**：在函式頂部的 doc comment 與 06 計畫測試表註明「名稱沿用 Stage 1，P5-T19 起驗證 rust 分派」。

4. **[Low] `crates/sw-daemon/tests/daemon_process.rs:425-452`（`wal_dir_readonly_at_start_blocks_login_probe`）**
   - **問題**：非 root 下 27 次有 1 次失敗。那次發生在與其他 cargo 編譯同時執行時，之後無負載連跑 25 次全過，未抓到失敗訊息。
   - **可疑點**：新增的 `wait_until("unhealthy_reason == write_failed", SHORT, …)` 依賴假 DUT 的 `login: ` RX 觸發第一筆 append。負載下 SHORT 期限可能先到。
   - **修法**：該等待改用 LONG；或在等待前先等 `wal.write_failures >= 1`。並在 commit 驗證段記錄非 root 的連跑次數。

### e36089f test(acceptance): L1 鎖 add-only 登記 F59～F87 修正新增的 8 個 cargo 案例

- 鎖檔 add-only 8 筆，與本範圍新增的 8 支 `#[test]` 一一對應，`--check --base b7bf34a` 通過。
- Windows 區塊未登記，標為 LOCAL-WINDOWS-TODO，沒有假裝完成。新增案例都是 Linux／fake 層案例，未宣稱 Windows 原生證據。

1. **[Medium] `docs/rust-refactor/reviews/p5-fix-responses.md:80-101`**
   - **問題**：commit 主旨寫「F59～F87 修正」，處理紀錄卻只有 F59～F63、F73、F74、F76、F81、F82、F87、F88。F64～F72、F77～F80、F83～F86 都沒有列，F63 列另有懸空引用「見 F67」。
   - **具體缺漏**：對照前輪報告，下列 Medium 在處理紀錄中找不到任何處理狀態（我以關鍵字逐一 grep 為 0）：
     - `b0f2853_8a7986e-opus`：
       - c96a91c：UTF-16／32 的 state.json 被當成損毀；Windows LF／CRLF 未登記 BC；#13「啟動時重寫」未驗；`s21_gate:py_parity` selector 收不到 nodeid。
       - 73634db：#26 RECOVERING 未持久化；sticky 在之後的 READY 也寫入；#29 斷言不可能失敗；缺 `no_redetect_during_quiet`／#31 測試（F63 列只以文字帶過）。
       - 2fcdbbf：chgrp 斷言。
     - `b0f2853_8a7986e-grok`：bind 逾時未讓活 session 進入 RECOVERING。
     - `8a7986e_edfc741-opus`：
       - 3938032：後端 env 檢查早於 argparse（exit 2→1）；隔離判定未 strip；YAML 例外類別名不一致。
       - 7a84168：PROBE 被拒送後仍判定模板。
     - `8a7986e_edfc741-grok`：SIGTERM 測試不再觀察 `FLUSHED_BY_SHUTDOWN`。
     - 前一輪 `1f5aafa_b7bf34a-opus` 的 3 項 Medium 也都沒有列：`PosixProcScan` 接線無測試；Windows `SO_SNDTIMEO` 重試；`session clear` 回應 `last_error`，即本報告 21de0db 第 1 項。
   - **後果**：無法追溯這些發現是 fixed、rejected 還是 deferred；commit 主旨的範圍宣稱也與內容不符。
   - **修法**：
     - 為上述每項配 ID 並補列，狀態為 fixed `<commit>`、rejected（附理由）或 deferred（附承接處）其中之一。
     - 補上 F67 列，或刪除「見 F67」。
     - 之後的 commit 主旨只寫實際處理的 ID。

## High／Medium 摘要

| 嚴重度 | Commit | 位置 | 問題 | 修法 | 證據 |
|---|---|---|---|---|---|
| Medium | 21de0db | `crates/sw-core/src/session_core.rs:3009-3014` | 有 bridge 的 `session.clear` 回應 `last_error` 為 null，Python 為 `"CLEARED"`。連續第三輪未處理，處理紀錄沒有這一列 | 刪除回覆前的 `set_last_error(None, None)`，改由 `reattach_pending` 接受 `DeviceAttached` 時清除；測試斷言回覆為 `CLEARED`，`DeviceAttached` 之後為 null | 黑箱 Python 3/3 `CLEARED`、Rust 3/3 null（第 5 列）；Python `session_manager.py:2746-2759`、`:2887`、`:4638-4641` |
| Medium | e36089f（及 d46c323 的處理紀錄） | `docs/rust-refactor/reviews/p5-fix-responses.md:80-101` | 宣稱 F59～F87，但多項前輪 Medium 沒有處理列，且有懸空的「見 F67」 | 逐項配 ID 補列並寫明 fixed／rejected／deferred＋承接處；修正 F67 引用 | 關鍵字 grep 為 0；ID 清單比對 |

**High：0。**

（本報告為唯讀審查。scratch worktree 的突變與暫時黑箱案例都已還原或刪除，`git status` 乾淨；worktree `/home/user/rv-p5`、`/home/user/rv-p5-scratch` 已移除。）
