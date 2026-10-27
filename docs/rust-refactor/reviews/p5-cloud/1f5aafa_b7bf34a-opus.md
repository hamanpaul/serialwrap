# feature/rust-refactor-p5 1f5aafa..b7bf34a 非實作者審查（Claude Opus）

- **範圍**：`feature/rust-refactor-p5`，`1f5aafa..origin/feature/rust-refactor-p5`（`b7bf34a`），共 10 個 commit：`18358a5`、`c43bb3e`、`5f625a2`、`f346f6b`、`177dfd9`、`348eb2f`、`10e4ea6`、`992a42b`（merge，只審衝突解法與合併後行為）、`5270c9d`、`b7bf34a`。
- **1:1 基準**：`origin/feature/rust-refactor` 的 `sw_core/*.py`（`session_manager.py`、`uart_io.py`、`wal.py`、`multi_open.py`、`device_watcher.py`、`service.py`、`rpc_posix.py`），以及 `docs/rust-refactor/serialwrap-rust-spec-final.md`、`owner-decisions-2026-10-15.md`（OD-14 之後與 OD-21）、`/home/user/pa/docs/rust-refactor/plans/parity-amendments-2026-10-26.md`、`06-slice21-daemon-core.md`。
- **前次報告**：`/home/user/pa/docs/rust-refactor/reviews/p5-cloud/edfc741_1f5aafa-opus.md`，以及處理紀錄 `docs/rust-refactor/reviews/p5-fix-responses.md` 所回應的 p5-cloud 各報告。
- **審查方式**：唯讀 detached worktree `/home/user/rv-p5`；反證另開 scratch worktree `/home/user/rv-p5-scratch`，用完即還原並移除；`CARGO_TARGET_DIR=/home/user/tgt-p5`（反證用 `/home/user/tgt-p5-scratch`），cargo 一律 `-j2`。本機執行身分為 root。
- **結論**：前輪 High 的 F06、F15、F25、F59、F60 都有對應的回歸測試，我逐一實測：只撤銷產品端修正，測試就轉紅（F01 是測試本身的修正，已逐行核對）。本範圍沒有 High。發現 3 項 Medium（其中一項是前輪 Medium（`session clear` 回應）仍未處理）、11 項 Low。

## 實際執行的驗證命令與結果

| # | 命令（cwd） | 結果 |
|---|---|---|
| 1 | `cargo test -j2 -p sw-core -p sw-pal -p sw-daemon --features test-fakes --no-fail-fast`（rv-p5，HEAD b7bf34a） | 失敗 4 支：其中 3 支是 root 環境的既知基準（`holder_scan_permission`、`file_eacces_matches_golden`、`unix_fixture_rows_match_python`）；另 1 支是 `command_exec::reboot_flushes_commands_queued_behind_it`（偶發，見第 3、4 列）。其餘全綠，含 `daemon_process` 28 支、`framing` 18 支、`session_manager` 49 支、`session_core` 61 支。 |
| 2 | 同一個 `command_exec` binary 以 `--exact` 單獨連跑 40 次 | 0/40 失敗 |
| 3 | `command_exec` 整個 binary 以 `--test-threads=8` 連跑 8 次（HEAD） | 4/8 失敗，全是 `reboot_flushes_commands_queued_behind_it`（AUTOBOOT_QUIET 搶在 q1 submit 前生效） |
| 4 | 同上，對照二分：`1f5aafa`／`5f625a2`／`f346f6b`／`348eb2f` 各 8 次 | 1/8、2/8、4/8（其中 3 次 reboot_flushes，1 次 `reboot_arms_quiet_only_after_tx_settled_e2e`）、2/8。**基準 `1f5aafa` 就已偶發**，不屬本範圍引入；`f346f6b` 另外單獨引入一支偶發測試，下一個 commit 才修好 |
| 5 | 反證：撤銷 `5270c9d` 的 `configure` 呼叫 | `session_manager::dynamic_session_applies_template_uart_config` **FAILED**（等待逾時） |
| 6 | 反證：註解掉 `update_devices` 送出 `DeviceMissing` 的那一行 | `session_manager::missing_explicit_target_marked_device_not_found` **FAILED** |
| 7 | 反證：`login_finished` 改回以 core `fsm_gen` 比對 | `session_core::banner_during_attach_login_{settles_attached,ready_result_applied,failed_drains_detached}` 與 `login_quiet::attach_settles_attached_after_quiet_revokes_probe` **4 支皆 FAILED** |
| 8 | 反證：READY recover 改回前景忙時回 `SESSION_BUSY` | `command_exec::recover_while_foreground_command_sends_recover_keys` **FAILED** |
| 9 | 反證：移除 `CancelQueued` 的 `revoke_ops` | `command_exec::command_cancel_running_revokes_tx_op` **FAILED** |
| 10 | 反證：移除 WAL 輪替的 `next_at` 跳過 | `wal_writer::rotate_retry_not_counted_before_next_at`、`rotate_backoff_defers_after_three_transient_failures` **FAILED** |
| 11 | 反證：framing `write_line` 改回 `write_all` 遇逾時即斷 | `framing::slow_reader_large_response_is_not_truncated` **FAILED**；`stop_bounds_total_write_time_with_trickle_reader` 仍綠（PAL 已是 100 ms，屬預期） |
| 12 | 反證：`PosixProcScan::serialwrapd_tty_holders` 改回 readlink 字串比對 | `cargo test -p sw-pal --features test-fakes` **沒有任何新的失敗**（仍只有 3 支 root 基準）→ 見 18358a5 第 1 項 |
| 13 | `python3 tests/acceptance/regen_lock.py --check --base 1f5aafa` | 兩項 OK；`l1_required.lock.json` 為 60 行純新增、0 刪除 |
| 14 | 本範圍新增的 `#[test]`（39 支）逐一比對 lock；lock 新增的 60 個名稱逐一 grep 有無對應的 `fn` | 39/39 已登記；60/60 都有對應的 `fn` |
| 15 | `python3 scripts/rust/check_cfg_boundary.py` | rc=0 |
| 16 | `cargo check -j2 --workspace --all-targets --target x86_64-pc-windows-gnu` | 通過；sw-pal lib 只有 3 個既有的 `PosixDeviceSource` 未使用警告，與本範圍無關 |
| 17 | `python3 -m pytest -q tests/test_slice21_scope.py tests/test_slice21_skeleton_guards.py` | 12 passed、1 skipped |
| 18 | `python3 -m tools.contracts check` | `ok:true`、findings 空 |
| 19 | `python3 -m policy_check --repo .` | 未執行：本機沒有安裝 `policy_check` 模組 |

---

### 18358a5 fix(sw-pal): holder 測試改用替身檔並以 inode 身分判定 multi_open 持有者

F01 的兩支測試已不再依賴主機 `/dev/pts/9`。`holder_scan_missing_proc_root_is_permission` 對齊 Python `probe_holders` 的 `os.listdir(proc_root)` 失敗回 `permission`；F03 改為回傳 probe 原值，與 Python 鎖後 `raise DeviceHeldError(holder=report)` 的原值一致。`tty_holders_with` 逐行對照 Python `detect_multi_open` 的 holders 段（tty stat 失敗略過、fd 目錄 PermissionError→permission、其他→unknown、fd stat 失敗略過），結果一致。

1. **[Medium] `crates/sw-pal/src/proc_scan/posix.rs:96-100`**
   - **問題**：F02 的產品接線（`PosixProcScan::serialwrapd_tty_holders` 改呼叫 `tty_holders_with`）沒有任何測試鎖住。新增的 4 支 `multi_open_*` 測試都直接呼叫 `tty_holders_with`，不經過 `PosixProcScan`。
   - **反證**：我把 `serialwrapd_tty_holders` 改回舊的 readlink 字串比對（第 12 列），`cargo test -p sw-pal` 沒有任何新的失敗。換句話說，F02 宣稱的修正可以被無聲退回。
   - **具體差異**：tty 以 symlink 別名路徑傳入，`<proc>/<pid>/fd/5` 指向真實檔。
     - Python 與修正後的 Rust：同一 inode → 列入 holder。
     - readlink 版：字串不同 → 不列入。
   - **修法**：在 `crates/sw-pal/tests/` 新增 tempdir 版的 `PosixProcScan` 測試：
     - 佈置 `<root>/<pid>/cmdline`（`/x/bin/serialwrapd\0--socket\0/s`），以及 `<root>/<pid>/fd/5 -> <tmp>/real`。
     - 以 `<tmp>/alias -> <tmp>/real` 作為 `paths`，呼叫 `PosixProcScan::with_root(..).serialwrapd_tty_holders(&[alias])`，斷言結果為 `([pid], Ok)`。
     - readlink 版會回 `([], Ok)` 而轉紅。
     - 同步 add-only 登記進 L1 鎖。

2. **[Low] `crates/sw-pal/src/serial/posix.rs`（`holder_scan_permission`）**
   - **問題**：改為 root 下以 FAIL 報「證據不足」，方向正確。但這支測試已在 `s21_core.linux` L1 鎖內，任何以 root 執行的 L1 收集（例如容器）都會固定紅燈。本範圍的 commit 也一再把它列為「root 基準失敗」。
   - **修法**：在 06 計畫或 L1 收集說明中載明「L1 linux 必須以非 root 執行」，並在 `cargo_l1_results.py` 偵測 euid==0 時整體標記為證據不足，不要逐支吞下。

### c43bb3e fix(sw-daemon): RPC 回應寫入平時無期限、stop 後以停止時刻起算 2 s 總期限

POSIX 行為已對齊 Python `writer.drain()` 的無逾時；stop 的總期限自 `ConnStop::request` 起算。反證（第 11 列）確認 `slow_reader_large_response_is_not_truncated` 能鎖住寫迴圈。F07、F08 的修法合理。

1. **[Medium] `crates/sw-pal/src/rpc_transport.rs:21-25`；`crates/sw-daemon/src/framing.rs:135-150`**
   - **問題**：Windows 仍設 2 s `SO_SNDTIMEO`，但 framing 對所有平台都把 `TimedOut` 當成「重試」。註解寫「Winsock 逾時後 socket 狀態未定義」，緊接著又寫「framing 仍會重試」，兩者自相矛盾。Winsock 文件明言，send 逾時後 socket 狀態不確定、不應再使用。std 在 Windows 把 `WSAETIMEDOUT` 映射為 `ErrorKind::TimedOut`，所以這條重試路徑在 Windows 一定會被走到。
   - **具體情境**：client 超過 2 s 不讀、回應大於 socket 緩衝。
     - Python（asyncio proactor）：無逾時，也不重送。
     - Rust Windows：逾時後從原 offset 重送，可能重送已部分進入核心的 bytes，或在不確定狀態的 socket 上繼續寫，JSON 行可能因此重複或毀損。
   - **另外**：commit 說「Windows 維持 2 s 單次逾時」，與實際行為（無限重試）不符，屬 Windows 原生項目的敘述失真。
   - **修法**（任選其一，cfg 只能放在 sw-pal）：
     - (a) Windows 改用 non-blocking socket 加 `WSAPoll`，在 100 ms 內等可寫後再 `send`，讓逾時不發生在 `send` 本身；
     - (b) 由 PAL 把 Windows send 逾時映射成不可重試的錯誤種類（例如 `ErrorKind::Other` 並帶上 `WSAETIMEDOUT` 文字），framing 只對 `WouldBlock`／`Interrupted` 重試。
   - 無論採哪種，都要在 `p5-fix-responses.md` 的 F06 列改為「Windows：未完成（LOCAL-WINDOWS-TODO）」，並在 Windows 原生測試清單加入慢讀大回應案例。

2. **[Low] commit 訊息「驗證」段**
   - **問題**：寫「見處理紀錄 F91」，但 `docs/rust-refactor/reviews/p5-fix-responses.md` 沒有 F91 這一列（只有 F92）。`daemon_stop_during_force_recover_with_client_gone` 的偶發逾時因此無從追溯。
   - **修法**：補上 F91 列，內容包括失敗輸出、重現命令、連跑次數與狀態（open／deferred 承接處）。

### 5f625a2 fix(sw-core): WAL 輪替干擾改為 OD-19 有界退避與延遲佇列並由 watcher tick 推進

逐段對照 `wal.py` 的 `_RotatePending`、`_schedule_rotate_retry_locked`、`tick` 與 `_write_rotation_continuity_meta_locked`：退避 10／50／200 ms、第 4 次轉入 1 s 延遲佇列、`rotate_deferred` 只在轉入時加一、告警節流三條件、未到 `next_at` 不計數，都一致。`Service._on_watcher_tick` 對應到 `WatcherEvent::Tick`；watcher 掃描失敗時不送 Tick，與 Python `DeviceWatcher._loop` 的 `poll_once` 拋出後略過 `on_tick` 一致。反證（第 10 列）會轉紅。

1. **[Low] `crates/sw-core/src/wal.rs:1207-1210`（tick 路徑）**
   - **問題**：tick 先用 `observe_sizes()` 取得大小，`fs.size` 失敗時回 `None`，接著就走「檔案不存在 → 清除排程、不計數」。Python `tick()` 以 `sizes=None` 呼叫 `_rotate_if_needed`，`fs.size` 若拋 `OSError` 會進 `except`：計為 `rotation_failed`，EACCES／EBUSY 時還會排程重試並把 `rotate_retry` 加一。
   - **具體輸入**：現行檔超過門檻且有待完成的輪替，tick 時 `fs.size(wal)` 回 EACCES。
     - Python：`rotate_retry` +1、`failures` +1、保留排程。
     - Rust：排程被清除，`rotate_retry` 不變。
   - **修法**：在 `SizeSnapshot` 保留錯誤種類；`wal_error`／`mirror_error` 為真時改走 `rotate_failed_locked`（依 `is_transient_rotate_error` 分類），不要清除 pending。補一支 `rotate_size_error_in_tick_is_counted_transient`。

### f346f6b fix(sw-core): 撤銷 op tombstone 上限 512 並於取消前景命令時撤銷 TX；reboot quiet 於 TX 臨界區同步生效

`CancelledOps` 的行為與 Python `_revoke_ops_locked` 一致：重複撤銷時先 pop 再插入、超過 512 淘汰最舊。`CancelQueued` 命中前景命令時撤銷 op，對應 Python `revoke_tx_op`（`interactive:<id>` 已登記延後到 P7）。F14 在同一次 `Fence::with` 內設定 `quiet_active`／`reconfirm_pending`，對應 Python 在 `_write_lock` 內 arm。反證（第 9 列）會轉紅。

1. **[Low] `crates/sw-core/tests/command_exec.rs:755-761`（本 commit 狀態）**
   - **問題**：本 commit 讓 fence quiet 先於 actor `QuietState` 生效，但 `reboot_arms_quiet_only_after_tx_settled_e2e` 仍是「等到 fence quiet 就立刻斷言 `boot_quiet_remaining_s != Null`」，因此在 `f346f6b` 上會偶發失敗。二分實測 1/8，失敗點 `command_exec.rs:761`，`left: Null`。下一個 commit `177dfd9` 才把等待條件改成兩者都成立。
   - **影響**：中間 commit 不乾淨，會干擾日後的 bisect；commit 訊息「全綠」也不準確。
   - **修法**：現況已修好，不必改碼；在 `p5-fix-responses.md` 的 F14 列註明這支測試的修正落在 `177dfd9`。若之後要整理歷史，把該測試的修改併入本 commit。

### 177dfd9 fix(sw-core): boot banner 不再使 attach 登入卡在 ATTACHING，登入 TX 失敗依 Python 分流

- **F15**：改以 run 自身的 `fsm_gen` 判定事件是否屬現行 run，結果立即套用，與 Python `_attach_by_id` 一致（attach thread 照常寫終態；Ready 時 `confirm_ready`）。這同時解決了前輪 Opus 對 18abb03 提出的 Medium 第 (a)(b)(c) 項，以及「延到 Tick」的時序問題。反證（第 7 列）4 支轉紅。
- **F16**：分類對照 Python 的 `_login_tx_refused`（只有 `quiet`／`stale_login_gen`，含 `revoked:` 前綴）、`WalUnavailableError(TxIncompleteError)` 以及 `_attach_by_id` 外層的 `ATTACH_FAILED:{type(exc).__name__}`，皆一致。既有 bridge 再探時例外穿出，經 `rpc_posix.py:60` 回 `EXCEPTION`＋`str(exc)`，也一致。
- **§10.4 獨立審閱意見**：
  - `boot_banner_invalidates_old_fsm_and_owner_generations` 末段改為 READY，對齊 Python 行為，同意。
  - `disk_full_blocks_login_probe_but_not_agent_cmd` 前半改為 `EXCEPTION "tx incomplete: not_submitted 0/1 WAL_UNHEALTHY"`、state／last_error 不變，與 Python `_probe_existing_bridge` 的例外上拋一致，同意。

1. **[Low] `crates/sw-core/src/tx_writer.rs:480-494`（`reclassify_unsubmitted`）**
   - **問題**：只要是 `Completed(0)`、沒有 fence 原因、而寫後重驗被拒，就一律改寫為 `NotSubmitted`＋該原因，`result.error` 被丟棄。這也涵蓋了「0 byte 時真實 OS 錯誤（EIO／ENXIO）、恰好同時 fence 因 banner quiet 或 drain 改變」的情況。
   - **具體差異**：此情境下 Python 拋 `TxIncompleteError("completed", 0, N, "[Errno 5] ...")`；Rust 回 `not_submitted 0/N quiet`（或 `draining`），錯誤原因被換掉。
   - **修法**：只在 `result.error` 是 PAL 取消（ECANCELED／`Operation canceled`）時才改寫；其他錯誤保留原值。補一支 `zero_progress_os_error_after_freeze_keeps_error`。

### 348eb2f fix(sw-core): READY recover 與 self-test probe 與前景命令並行，foreground_busy 只計命令

對照 Python：
- `recover_session` 的 READY 分支與 `self_test` 都不檢查 `foreground_busy`（`session_manager.py` 7635 以下、7297 以下）。
- `foreground_busy` 只由 `_begin_foreground_operation_locked` 設定，呼叫點只有 command 與 file 兩種（Slice 2.1 沒有 file）。
- `outstanding = len(background_cmd_ids) + (1 if foreground_busy else 0)`。
- bridge 為 None 且裝置在場時回 `REATTACH`。

以上都一致。反證（第 8 列）轉紅。

1. **[Low] `crates/sw-core/src/op_runner.rs:97-125`**
   - **問題**：並行 lane 只有一條 thread。同一 session 的 READY recover 與 self-test probe 若同時到達，會互相排隊。Python 兩者各在自己的 RPC thread 內，彼此並行。
   - **具體情境**：recover（`timeout_s=2`，target 不回 prompt）進行中又送 `session.self_test`。
     - Python：self-test 的 nonce probe 立即寫出。
     - Rust：最多延後 2 s 才寫出，回應時間也跟著拉長。
   - **修法**：側 lane 改為每個 op 一條短命 thread，或使用小型 pool（上限比照 Python RPC 併發）；或在處理紀錄登記為 Slice 2.1 範圍內的已知時序差異，並由擁有者核准（OD-21）。

2. **[Low] `docs/rust-refactor/reviews/p5-fix-responses.md`（F25 列）**
   - **問題**：寫「parity #58 一併提前完成」，但 parity-amendments #58 指定的 nodeid 是 `system_recover_key_not_gated_by_foreground_slot`（承接 `09 Task 5`），在 repo 中找不到這個名稱（已 grep）；`09-slice24-transfer-flash.md` Task 5 也沒有更新。P8-T5 之後可能重做一次或漏驗。
   - **修法**：在 parity 表與 09 Task 5 註明已由 `command_exec::recover_while_foreground_command_sends_recover_keys` 承接；或依計畫名稱另加同義測試並登記進鎖。

### 10e4ea6 fix(sw-core): bridge 建 POSIX 哨兵 PTY 並逐 bridge 重建 RX 視窗；self-test 分類與 log 檔名對齊 Python

對照 Python `UARTBridge.start`：
- `_pty_available()` 時建 internal primary；無 PTY 平台不建，與 Python Windows 行為相同，所以「Windows 無哨兵」不是假裝完成。
- `_suggest_profile_from_rx` 依 template 載入順序、略過 passthrough、不合法 regex 略過。
- `expired()` 為嚴格大於。
- BRIDGE_DOWN 以 `snapshot()` 判定。

以上都一致。相關測試在第 1 列全綠。

1. **[Low] `crates/sw-core/src/session_manager.rs:1714-1717`**
   - **問題**：`create_pty()` 的任何錯誤都靜默視為「無哨兵」。Python 只有在 `_pty_available()` 為假時才不建；一旦可用，`_create_console_client("primary")` 拋出的例外（例如 fd 耗盡 EMFILE）會讓 `start()` 失敗，attach 隨之 `ATTACH_FAILED:<ExcType>`。
   - **具體情境**：POSIX、PTY 可用但 `openpty` 回 EMFILE。
     - Python：attach 失敗、DETACHED。
     - Rust：照常 attach，但 `vtty=null`、`console_count=0`。
   - **修法**：只把 `PalErrorKind::Unsupported` 視為無哨兵；其他錯誤關閉 handle 並回 `OpenFailure::Other`。補一支以 fake console 工廠注入 EMFILE 的測試。

### 992a42b merge: 併入 origin/feature/rust-refactor-p5 的 18abb03／9363d9b／1f5aafa

衝突解法正確：
- `session_core.rs` 對第一親代（`10e4ea6`）只多出 9363d9b 的 5 行；18abb03 的 `quiet_bumps`／`revoked_by_quiet`／`settle_quiet_revoked_attach` 已完全移除（grep 無殘留）。
- 上游兩支測試保留 nodeid：`attach_settles_attached_after_quiet_revokes_probe` 的 Ready 期望改為 READY，正是前輪 Opus 報告要求的修法；`attach_stale_result_not_from_quiet_still_ignored` 維持原樣。
- L1 鎖對兩個親代都是純新增。
- 反證（第 7 列）確認合併後的測試鎖得住 run 身分判定。

1. **[Medium] `crates/sw-core/src/session_core.rs:2940-2947`（前輪 Opus 9363d9b 的 Medium，仍未處理）**
   - **問題**：`set_last_error(None, None)` 仍在回覆等待者之前執行，所以 `session clear` 的 RPC 回應是 `"last_error": null`，Python 回 `"CLEARED"`（前輪黑箱實測 3/3）。這次 merge 原樣帶入，處理紀錄也沒有這一列。
   - **修法**：把清除移到 `for waiter in stop.waiters` 迴圈之後；在 `drain_release::clear_reopen_attaching_clears_last_error` 增加斷言，回覆的 `session.last_error == "CLEARED"`、之後 `public()` 為 null。並把這項發現補進 `p5-fix-responses.md`。

2. **[Low] `changelog.d/rust-s21-blackbox.md`**
   - **問題**：仍描述 18abb03 的語意（「改與 Python 一致落定 ATTACHED＋`PROMPT_UNAVAILABLE`」）。合併後實際語意是：撤銷→ATTACHED＋`PROMPT_UNAVAILABLE`、Ready→READY、Failed→DETACHED，而且立即生效。
   - **修法**：改寫成上述三種結果，或改為引用 `rust-s21-p5-review-fixes.md` 的 F15。

3. **[Low] 處理紀錄缺項**
   - **問題**：前輪 Opus 報告（`edfc741_1f5aafa-opus.md`）對 `1f5aafa` 的兩項 Medium（黑箱交付報告未入版控；`ab_normalization.yaml` 未保留 null／型別）在 `p5-fix-responses.md` 中沒有任何對應列。這不屬本範圍的程式變更，但「處理紀錄涵蓋 p5-cloud」的宣稱因此不完整。
   - **修法**：補列，狀態可以是 deferred 並寫明承接處。

### 5270c9d fix(sw-core): 預開 port 套用 template UART 設定；缺席裝置標記 DEVICE_NOT_FOUND

- **F59**：對應 Python `UARTBridge.start` 對預開 port 呼叫 `port.configure(self.profile)`。
- **F60**：移植 `_mark_missing_devices_locked`，條件為非 RELEASED、有 by_id、無 bridge、不在快照；`last_error`／`detached_at` 只在為 None 時填入；呼叫時機在 detach／attach 之前。
- 兩項都一致。反證（第 5、6 列）轉紅。
- Windows `configure_port` 已標 LOCAL-WINDOWS-TODO，沒有宣稱完成。

1. **[Low] `crates/sw-core/tests/session_manager.rs:1742-1754`；`crates/sw-core/src/session_core.rs:1343-1357`**
   - **問題**：F60 只有「啟動時快照為空」一個案例。Python 的另外四個條件沒有被鎖住：RELEASED 跳過、有 bridge 跳過、既有 `last_error` 不覆寫、既有 `detached_at` 不覆寫。Rust 另外多了一個 Python 沒有的條件 `self.stop.is_some()`（drain 中不標記），也沒有說明依據。
   - **修法**：補 4 支案例：RELEASED 維持、READY 時拔線不改 last_error、`last_error="CLEARED"` 保留、`detached_at` 保留。並在程式註解與處理紀錄說明 `stop.is_some()` 對應 Python 的哪個條件（drain 期間 `session.bridge` 仍非 None）；若無對應，就移除這個條件。

### b7bf34a test(acceptance): L1 鎖 add-only 登記 P5 審查修正新增的 34 個 cargo 案例

NO FINDINGS。
- 34 行純新增、0 刪除；`regen_lock.py --check --base 1f5aafa` OK。
- 本範圍新增的 39 支 `#[test]` 全數在鎖內，鎖內新增的 60 個名稱都有對應的 `fn`。
- `s21_core.windows` 未登記，已標 LOCAL-WINDOWS-TODO，沒有假裝完成。

### 範圍外觀察（不計入本範圍統計）

- `command_exec::reboot_flushes_commands_queued_behind_it`（已入 L1 鎖）在基準 `1f5aafa` 就會偶發失敗：整個 binary 平行跑時 1/8，HEAD 4/8；單獨跑 0/40。
- **原因**：`submit_ok("reboot")` 之後才送 `echo a`，若 actor 先處理到 reboot 的 `UartTxCompleted`、arm 了 quiet，q1 就會回 `AUTOBOOT_QUIET`，而不是排進佇列後被 flush。Python 在 reboot TX 送出後提交也會回 AUTOBOOT_QUIET，所以這是測試本身的時序假設，不是產品差異。
- **建議**：先以 `dev.pause` 擋住 reboot 的 TX，等 q1、q2 都入列後再放行。依 §10.4 由測試資產審閱處理。

---

## High／Medium 摘要

High：無。

| 等級 | commit | 位置 | 問題 | 修法 | 證據 |
|---|---|---|---|---|---|
| Medium | 18358a5 | `crates/sw-pal/src/proc_scan/posix.rs:96-100` | F02 的產品接線（`PosixProcScan` 改用 inode 比對）沒有測試鎖住 | 新增 tempdir 版 `PosixProcScan` 別名路徑測試（readlink 版會紅），並登記進 L1 鎖 | 改回 readlink 後 `cargo test -p sw-pal` 無新失敗（第 12 列） |
| Medium | c43bb3e | `crates/sw-pal/src/rpc_transport.rs:21-25`；`crates/sw-daemon/src/framing.rs:135-150` | Windows 在 `WSAETIMEDOUT` 後仍重試 send（Winsock 規定此時 socket 狀態不確定），可能重複或毀損 JSON 行；commit 說「維持 2 s 單次逾時」與實際行為不符 | Windows 改 non-blocking＋`WSAPoll`，或 PAL 把逾時映射為不可重試；處理紀錄改標 Windows 未完成 | 程式碼路徑分析（std 把 `WSAETIMEDOUT` 映射為 `TimedOut`，framing 對 `TimedOut` 重試）；本機無 Windows，未實跑 |
| Medium | 992a42b（帶入 9363d9b） | `crates/sw-core/src/session_core.rs:2940-2947` | `session clear` 回應 `last_error` 為 null，Python 為 `CLEARED`；前輪 Medium 仍未處理，處理紀錄也沒有這一列 | 清除移到回覆迴圈之後；測試加上回覆值斷言 | 程式碼順序（清除在 `Effect::Reply(ok_session)` 之前）；前輪黑箱實測 3/3 |

Low 共 11 項：18358a5、c43bb3e、5f625a2、f346f6b、177dfd9、10e4ea6、5270c9d 各一項，348eb2f 與 992a42b 各兩項。
