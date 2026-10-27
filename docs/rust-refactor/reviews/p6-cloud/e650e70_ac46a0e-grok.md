我以非實作者身份審查 `e650e70..ac46a0e`，先對齊計畫、規格與 commit 範圍，不會改任何檔案。範圍是 12 個 commit，多數是併入 P6 的 P5 Slice 2.1。我先抓計畫驗收條件與機械性紅線，再逐 commit 對 Python 基線。先前雙審沒有涵蓋 T10、T11、T12、T12b 與合併。我改以目前程式核對那些結論是否仍成立，並補審未覆蓋的 commit。審查繼續。先重跑 holder 測試取失敗證據，並核對尚未定論的 commit 與計畫原文。`holder_scan_permission` 在本機失敗（Unknown ≠ Permission）。接著核對 Python 對照與尚未定論的 commit。審查範圍是 `e650e70..ac46a0e`（HEAD `ac46a0e`）。沒有改任何檔案。`holder_scan_permission` 已在本機重跑並失敗。L1 鎖相對 `e650e70` 為 add-only（`s21_core.linux` +205/−0、`windows` +153/−0、`s21_gate:py_parity.linux` +1/−0，s22 三集合淨增 0）。此範圍新增的 Rust `#[cfg]` 沒有 `target_os`／`unix`／`windows`。`python3 scripts/rust/check_cfg_boundary.py` 結束碼 0。

### 2a89f60 test(sw-proto): 偽造 HOME 改用 /fakehome 以符合 R-21

NO FINDINGS

### 335665e feat(sw-core): IoWorker reader、RxWindow 與哨兵 fan-out

NO FINDINGS

### f20bdd0 feat(sw-core): 實作 SessionCore、Mailbox 與 Actor

1. [Low] `tests/acceptance/l1_required.lock.json`（本 commit 的 `s21_core.windows`）— 相對 parent，windows 比 linux 多 32 個 nodeid，含 `cap_windows_native__launcher_*`、`cap_interactive_desktop__*` 與 Windows `device_source`。commit 本文只有 Co-authored-by，沒有原生 Windows collect 的 `summary.json`。這不是 PASS 紀錄，但分母變更無法追溯到原生收集。— 附上該次 `win-pytest`／cargo 原生收集的 `summary.json`（`collected`／`passed`／`failed`）。沒有這份輸出時，把這 32 筆從 `s21_core.windows` 移出，等原生收集後再以 `--l1-add-only` 加回。

### afe4853 feat(sw-core): Two-Phase Drain 核心、IoWorker 收尾與 Release／Attach

1. [Medium] `tests/acceptance/l1_required.lock.json` 的 `s21_gate:py_parity.linux` — selector 被釘成單一 nodeid `tests/test_stage0_session_drain.py::test_release_persist_failure_retried`，沒有 windows 平台。06 計畫 Task 0b（`docs/rust-refactor/plans/06-slice21-daemon-core.md:538-546`）要求此集合以 markexpr 登記 #76–#81（含 `test_holder_probe_namespace.py`、8 個 `test_p1oc_*`、`test_daemon_lock.py`、bridge drain reader 兩筆等），windows 必須用原生收集。後續 commit 都沒有擴充；現有 selector 讓 `--l1-add-only` 無法收進其他測試。— 換成計畫的 markexpr selector，用 collect-only 輸出 add-only 登記 #76–#81。windows 鍵只在原生 `summary.json` 出現後建立，不要複製 linux nodeid。

2. [Low] commit 本文—寫「`s21_core` Linux、Windows 各 3 項」。相對 parent 實際是 linux +73、windows +73，且兩邊新增 nodeid 相同。— 更正訊息為實測增量，並註明 windows 列的收集方式。`cargo check --target` 維持為編譯證據。

### 0fd8fa9 docs(rust-refactor): 06 計畫併入 parity-amendments P5 修訂段落

NO FINDINGS

### fb76460 fix(sw-pal): holder 以 inode 身分判定並對齊 Python open／flock errno 映射

1. [High] `crates/sw-pal/src/serial/posix.rs:1570` — `holder_scan_permission` 已在 L1 鎖（`l1_required.lock.json` 約 9527–9528 列），目標仍是 `/dev/pts/9`。`probe_holders_with`（`crates/sw-pal/src/proc_holders/posix.rs:102`）先 `stat` 目標，失敗即回 `Unknown`，不會讀到後面 chmod `000` 的 fd 目錄。本機沒有 `/dev/pts/9`。實測 `cargo test -p sw-pal --lib holder_scan_ -- --test-threads=1`：`holder_scan_permission` 失敗，`left: Unknown`、`right: Permission`。同檔 `:1557` 的 `holder_scan_unreadable_proc_is_unknown` 也用 `/dev/pts/9`，本機因 stat 失敗而表面通過，沒有測到不可讀的 proc。Python `os.listdir(proc_root)` 的任何 `OSError` 都是 `permission`（`posix.rs:112-115` 已照此實作）。目標節點存在時，這支測試會得到 `Permission`，斷言卻是 `Unknown`。— 兩支測試的目標都改成暫存檔（與 `:1540` 相同）。`holder_scan_permission` 維持期望 `Permission`。`holder_scan_unreadable_proc_is_unknown` 改為：目標 inode 存在、proc 根目錄不存在或不可列，期望 `HolderScanStatus::Permission` 且 `holder_unknown == true`。名稱已在鎖內，只改斷言與替身，不要刪 nodeid。

2. [Medium] `crates/sw-pal/tests/proc_holders_namespace.rs:102` — parity-amendments #64 要求移植 `tests/test_holder_probe_namespace.py` 18 案。本檔有 `TestProbeHoldersNamespace` 的 10 案與額外錯誤分支。`TestDetectMultiOpenNamespace` 的 3 案（`test_foreign_namespace_same_string_not_reported`、`test_same_inode_reported_under_caller_path`、`test_non_devpts_mknod_alias_reported_under_caller_path`）在整個 worktree 都不存在。`TestUartBridgeHeldPathsNamespace` 5 案屬 07 Task 8，不計入本 commit。— 在 `proc_scan` 的假 `/proc` 測試補上這 3 案：同一 devpts 字串、不同 `(st_dev, st_ino)` 不得列入；同一 inode 要列入；非 devpts 字元裝置且 `rdev` 相等才算別名。以 `--l1-add-only` 登記。

### 2d93181 fix(sw-core): WAL health 補齊 OD-19 三鍵為 14 鍵

1. [Medium] `crates/sw-core/src/wal.rs:1206` — 14 個 health 鍵的順序在，OD-19 的計數不在。只有 rename 的暫時性錯誤才 `rotate_retry += 1`（`:1206-1208`）。close（`:1184-1188`）與 `sync_dir`（`:1201-1203`）的暫時性失敗只 warn。`refresh_health_locked`（`:1413-1414`）把 `rotate_deferred` 與 `deferred_delete_pending` 寫死為 0。沒有 `WAL_ROTATE_RETRY_BACKOFF_S`、pending，也沒有 `next_at`。具體輸入：同一輪替目標連續 4 次 rename `EACCES`（errno 13）。Python `origin/feature/rust-refactor:sw_core/wal.py` 的 `_schedule_rotate_retry_locked`（約 `:242`）每次都把 `_rotate_retry_count` 加 1，超過三次 backoff `(0.01, 0.05, 0.2)` 後 `_rotate_deferred_count += 1`，`deferred_delete_pending` 為 deferred pending 的個數。預期 health：`rotate_retry=4`、`rotate_deferred=1`、`deferred_delete_pending=1`，未到 `next_at` 的 append 不再試、也不再加計數。實際：`4/0/0`，而且每次失敗都立刻重開舊檔。`crates/sw-core/tests/wal_writer.rs:1854-1868` 把 `0/0` 鎖成斷言，正確實作會被這支測試打紅。— 移植 `_schedule_rotate_retry_locked` 與 tick；close、rename、`sync_dir` 的暫時性 `OSError` 都走這條路。四次 `EACCES` 後斷言 `4/1/1`。`healthy` 繼續不把這三鍵算進去。

### 0a187c1 feat(sw-core): 最小登入、帳密未解析終態與 boot quiet gate

1. [High] `crates/sw-core/src/session_core.rs:2243` — attach 登入進行中，RX 開機 banner 會把 `fsm_gen` 加 1（`apply_quiet_armed` `:2440-2441`，由 `rx_chunk` `:2424` 呼叫）。`login_finished` 在 `state != Attaching` 或 `fsm_gen` 與事件不符時直接 return，不 `take` `login_run`。狀態停在 `ATTACHING`。`attach_state_result`（`:2310-2333`）對 command-capable 的 `ATTACHING` 回 `ok: true`。具體輸入：command-capable profile、`state=ATTACHING`、已送出 `StartLogin { fsm_gen: N }`，登入 TX 完成前 RX 含 `U-Boot`（`quiet_gate.rs:18`）。Python `arm_boot_quiet`（`sw_core/session_manager.py:597-598`）會把 `login_gen` 加 1，在途 TX 以 `stale_login_gen` 被 `_login_tx_refused`（`:83-90`）收成 `PROMPT_UNAVAILABLE`；attach thread 仍在 `:4850-4852` 寫成 `ATTACHED`，`last_error=PROMPT_UNAVAILABLE`。預期：`state=ATTACHED`、`last_error=PROMPT_UNAVAILABLE`、`login_run` 清空。實際：永久 `ATTACHING`，下一次 `SessionAttach` 仍 `ok: true`。— `LoginRun` 記下啟動時的 `fsm_gen`。完成事件以該 run 的身分套用；quiet arm 不是新的 login run。TX 因 `FsmMismatch` 被拒時套用 `NotReady("PROMPT_UNAVAILABLE")` 並轉 `ATTACHED`。補一支現況會失敗的測試，再 `--l1-add-only`。`login_finished_stale_fsm_gen_ignored` 仍只忽略已被新 login run 取代的舊事件。

2. [Medium] `crates/sw-core/src/login_fsm.rs:305` — `refused()` 把任何非完整 `Completed(n >= len)` 的登入 TX 收成 `NotReady("PROMPT_UNAVAILABLE")`，`login_finished`（`session_core.rs:2268-2278`）再寫成 `ATTACHED`。Python `_login_tx_refused`（`session_manager.py:83-90`）只接受 `quiet` 與 `stale_login_gen`；其餘在初始 attach 的 `:4757-4759` re-raise，外層 `:4907-4908` 得到 `DETACHED` 與 `last_error=ATTACH_FAILED:<ExcType>`。具體輸入：login probe 的 TX 結果為 `Unknown`（或 `WalUnhealthy`，原因不是 quiet）。預期：`state=DETACHED`、`last_error` 以 `ATTACH_FAILED:` 開頭。實際：`state=ATTACHED`、`last_error=PROMPT_UNAVAILABLE`。— 依 reject 原因分流：只有 quiet 與 stale login generation 走 `PROMPT_UNAVAILABLE`；其餘在 attach run 走 `Failed`，讓既有 `begin_stop(LoginFailed)` 落到 `DETACHED` 與 `ATTACH_FAILED:<ExcType>`。補對應測試。

3. [Medium] `crates/sw-core/src/session_core.rs:2173` — `choose_plan` 一律把 `self.pending_auto_login` 傳入 `choose_login_plan`。再探（`:2369`）也走這條。Python `_probe_existing_bridge`（`session_manager.py:4465`）只看 `login_regex` 與 `console_unlock_env`，不含 `pending_auto_login`。具體輸入：profile 的 `login_regex` 為空、未宣告 `console_unlock_env`、`user_env`／`pass_env` 已宣告但未解析、`pending_auto_login=true`。預期：再探走 `probe_ready`。實際：`require_login` 為真，回 `CREDENTIALS_UNRESOLVED`（`login_fsm.rs:121-129`）。帳密已解析時，Rust 走 `EnsureReady`，Python 仍走 `probe_ready`。— 再探路徑以 `pending_auto_login=false` 呼叫 `choose_login_plan`。attach 路徑維持現有旗標。補上述兩組測試。

4. [Low] `crates/sw-core/src/session_core.rs:2120` — quiet gate 使用 `quiet.quiet_active(now)`，沒有排除已宣告的 `console_unlock_env`。`choose_login_plan`（`login_fsm.rs:117-118`）在帳密判斷之前就回 `PROMPT_UNAVAILABLE`。Python `_boot_quiet_blocks_probe`（`session_manager.py:1731-1733`）在有 `console_unlock_env` 時不擋 probe，接著對未解析帳密回 `CREDENTIALS_UNRESOLVED`。具體輸入：宣告 `console_unlock_env` 且值未解析，boot quiet 進行中。預期：`CREDENTIALS_UNRESOLVED`。實際：`PROMPT_UNAVAILABLE`。— `quiet_blocks` 改為 `quiet_active && console_unlock_env 未宣告`，與 `:1733` 相同。

### 8b8a40f feat(sw-core): 命令執行與救援、agent lease、self-test 與 capture

1. [High] `crates/sw-core/src/session_core.rs:4036` — `READY` 且 `foreground` 有值時，`session.recover` 回 `{ok:false, error_code:SESSION_BUSY}`，零救援鍵。`self_test_request`（`:4432-4434`）在需要 nonce probe 時同樣拒絕，零 probe。Python `recover_session`（`session_manager.py:7528-7630`）與 `self_test`（`:7190` 起）都不看 `foreground_busy`。此差異不在 OD-21 已核准清單。具體輸入：`state=READY`、一筆前景命令仍在等 prompt，RPC `session.recover` 或 `session.self_test`。預期：recover 送 `\x03`／`\x04`（quiet 已 arm 則不送鍵並進入既有收尾）；self-test 照送 nonce probe。實際：兩者皆 `SESSION_BUSY`，UART 零寫入。— 拿掉這兩處 `foreground.is_some()` 拒絕。recover 用 `SystemRecoverKey` 送鍵；self-test 照送 probe。各補一支現況會失敗的測試並入鎖。`:2997` 的 interactive `SESSION_BUSY` 與 Python `:6222-6225` 一致，留著。

2. [Medium] `crates/sw-core/src/op_runner.rs:581` — 救援鍵除了 `Quiet`／`ReconfirmPending`（break）、`Frozen`／`Cancelled`／`StaleEpoch`（`FLUSHED_BY_RECOVERY`）之外，其餘拒絕回 `OpOutcome::Error`。`finish_command`（`session_core.rs:3929-3930`）把它映射成 `SEND_FAILED`，不看該命令帳本的 confirmed bytes，也不寫 META。Python 救援鍵的其他 `TxIncompleteError` 在 `session_manager.py:6667` raise，外層 `:6294-6304` 進入 `_map_aborted_result`。`confirmed > 0` 時（`:6351-6372`）回 `COMMAND_ABORTED_IN_FLIGHT`，並 append WAL META `command_aborted_in_flight`。具體輸入：命令已完整送出（該 `cmd_id` 的 confirmed > 0），等 prompt 時救援鍵遭 `WalUnhealthy`。預期：`error_code=COMMAND_ABORTED_IN_FLIGHT`，WAL 有對應 META。實際：`SEND_FAILED`，沒有 META。— 救援鍵的非 quiet、非 flush 拒絕改走與 `_map_aborted_result` 相同的 confirmed／unknown 分支，並寫同一筆 META。

3. [Medium] `crates/sw-core/src/session_core.rs:4545` — `BRIDGE_DOWN` 只在 `conn_epoch` 為空時出現，`recommended_action` 為 `attach`。`IoWorker` 快照的 `running`／`serial_alive`（`uart_bridge.rs:554-617`）沒有進入 self-test。Python `session_manager.py:7288-7296`：bridge 物件還在，但 snapshot 的 `running` 或 `serial_alive` 為假 → `BRIDGE_DOWN`、`recommended_action=recover`、零 probe。具體輸入：epoch 仍在、reader 已結束或 serial handle 已死。預期：`BRIDGE_DOWN`／`recover`，不送 nonce。實際：繼續 nonce probe。— pre-probe 讀 worker snapshot；`running` 或 `serial_alive` 為假時回上述分類並跳過 probe。

4. [Medium] `crates/sw-core/src/session_core.rs:4574` — passthrough 且 `ATTACHED` 時固定 `recommended_action=console_attach`。沒有讀 `profile_source`。Python `:7337-7350`：`profile_source=="fallback"` 且 RX 命中 template 時改為 `pin_profile`，並帶 `suggested_profile` 與 `hint`。具體輸入：fallback passthrough、`state=ATTACHED`、RX tail 命中某個 template。預期：`pin_profile` 與那兩個欄位。實際：`console_attach`。`selftest_passthrough_zero_tx` 只覆蓋明確 uboot。— 移植 `_suggest_profile_from_rx`，只在 `profile_source=="fallback"` 時覆寫 action。

5. [Medium] `crates/sw-core/src/session_core.rs:4415` — `RELEASED` 的 `probe_holders` 在 actor 內同步呼叫。06 計畫 `:3609` 要求 `/proc` 掃描在 actor 外的 executor thread，再以 `OpFinished(SelfTest)` 回覆。Python `:7214-7215` 同樣在 lock 外掃描。— 改成 effect：executor 掃描後把 `HolderReport` 送回，actor 再做 `:4475` 的分類。

6. [Medium] `crates/sw-core/src/session_core.rs:4556` — self-test 的 `attached_vtty` 固定 `PyValue::Null`（`:4565` 相同）。Python `:7303` 使用 bridge snapshot 的 `vtty`。具體輸入：VTTY 路徑為 `/dev/pts/7` 的 `VTTY_STALE` 或 `PASSTHROUGH` 結果。預期：`attached_vtty` 為該路徑。實際：JSON `null`。— 從 bridge／worker snapshot 填入該欄位。

7. [Medium] `crates/sw-core/src/session_core.rs:3581` — `capture_env.clock` 為 `None` 時，本地 offset 寫死 0（`capture.rs:194` 註明以 UTC 代替）。`SessionCore::new` 使用 `CaptureEnv::default()`，生產路徑沒有呼叫 `set_capture_env`。Python 檔名用 `strftime("%y%m%d-%H%M%S")` 的本地時間。具體輸入：本地 UTC+8、wall 為 `2026-10-27 00:30:00Z`，對 `COM0` 做 `log_start`。預期檔名 `COM0_261027-083000.log`。實際：`COM0_261027-003000.log`。— 預設 clock 使用 PAL 的 `local_utc_offset_s()`，檔名走 `strftime_local`。

8. [Medium] `crates/sw-core/src/session_core.rs:3998` — `conn_epoch` 為空時，還要求 `open_real_path` 與 `pending_attach` 都是空才 `REATTACH`；否則 `:4026` 回 `SESSION_NOT_READY`。Python `:7560-7567`：`bridge is None` 且 by-id 在裝置表就回 `{ok:true, action:REATTACH}`；`_spawn_attach`（`:3840-3841`）在已有 in-flight attach 時直接返回，回應仍是 `REATTACH`。具體輸入：裝置在、bridge 尚未掛上、`pending_attach` 或 `open_real_path` 已有值。預期：`ok:true`、`action=REATTACH`。實際：`SESSION_NOT_READY`。— 裝置在且沒有 bridge 時一律回 `REATTACH`；已有進行中的 open 就不要再發第二個 `OpenBridge`。

### b73da42 test(sw-core): drain／release 端到端案例

NO FINDINGS

`drain_release_e2e.rs:16-38` 把 20 ms 中間狀態放寬為 `RECOVERING` 或 `RELEASED`，同時保留 `aborted_cmd`、`close_count==1`、`COMMAND_ABORTED_IN_FLIGHT`、WAL 只記 `slee`、最終 `RELEASED`。`:41-58` 仍斷言 3001 ms 後 `DRAIN_STALLED`。這是假裝置取消時序，斷言沒有放掉 OD-14 #4。

### f97a208 merge: 同步 P5 T10～T14 至 P6

NO FINDINGS

`session_core.rs:642-644` 與 `:1480` 的 `todo!("P6 Task …")` 對應尚未交付的 Task 6／9／10。P6 parent `e650e70` 的 `on_event` 原本整段是 `s21_todo!`。生產碼沒有 `ConsoleScan`、`HumanTxCompleted`、`ConsoleAttachReq` 的建構點。

### ac46a0e fix(rust-tests): lease_selftest 讀 golden 改經 common::repo_path 以符合 cfg 邊界

NO FINDINGS

## High／Medium 摘要

| 嚴重度 | Commit | 位置 | 問題 |
|---|---|---|---|
| High | fb76460 | `serial/posix.rs:1570` | L1 的 `holder_scan_permission` 本機失敗（Unknown ≠ Permission）；unreadable proc 測試目標仍是 `/dev/pts/9`，期望值與 Python listdir→Permission 不符 |
| High | 0a187c1 | `session_core.rs:2243` | 登入中 RX `U-Boot` 使 `fsm_gen` 前進，結果被丟棄，session 停在 `ATTACHING` 且 attach 仍 `ok:true`；Python 落到 `ATTACHED`＋`PROMPT_UNAVAILABLE` |
| High | 8b8a40f | `session_core.rs:4036`、`:4432` | 前景命令進行中，`recover`／`self_test` 回 `SESSION_BUSY` 且零 TX；Python 仍送救援鍵或 nonce |
| Medium | afe4853 | `l1_required.lock.json` `s21_gate:py_parity` | 集合只有 1 筆 linux nodeid、沒有 windows；#76–#81 未入鎖 |
| Medium | fb76460 | `proc_holders_namespace.rs` | #64 的 `TestDetectMultiOpenNamespace` 3 案不存在 |
| Medium | 2d93181 | `wal.rs:1206`、`:1413` | OD-19 三鍵名在；deferred 恆 0，close／sync_dir 不計數，無 backoff；測試把 0／0 鎖死 |
| Medium | 0a187c1 | `login_fsm.rs:305` | 非 quiet／非 stale 的登入 TX 失敗變成 `ATTACHED`＋`PROMPT_UNAVAILABLE`；Python 是 `DETACHED`＋`ATTACH_FAILED:<ExcType>` |
| Medium | 0a187c1 | `session_core.rs:2173` | 再探把 `pending_auto_login` 算進 `require_login`；Python 再探只看 `login_regex` 與 `console_unlock_env` |
| Medium | 8b8a40f | `op_runner.rs:581` | 救援鍵的其他拒絕變成 `SEND_FAILED`；命令已送出時 Python 是 `COMMAND_ABORTED_IN_FLIGHT` 加 WAL META |
| Medium | 8b8a40f | `session_core.rs:4545` | serial 已死仍做 nonce probe；Python 是 `BRIDGE_DOWN`／`recover`、零 probe |
| Medium | 8b8a40f | `session_core.rs:4574` | fallback passthrough 沒有 `pin_profile`／`suggested_profile`／`hint` |
| Medium | 8b8a40f | `session_core.rs:4415` | `RELEASED` holder 掃描在 actor 內；計畫要求 executor thread |
| Medium | 8b8a40f | `session_core.rs:4556` | `attached_vtty` 恆 null |
| Medium | 8b8a40f | `session_core.rs:3581` | 預設 log 檔名用 UTC；Python 用本地 `strftime` |
| Medium | 8b8a40f | `session_core.rs:3998` | attach 進行中 recover 回 `SESSION_NOT_READY`；Python 回 `REATTACH` |
