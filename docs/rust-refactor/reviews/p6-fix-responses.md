# P6 審查發現處理紀錄（feature/rust-refactor-p6）

來源：`origin/docs/parity-amendments` 的 `docs/rust-refactor/reviews/p6-cloud/*.md`（grok-4.7＋Claude Opus 雙審）與
`sol-cloud-p6-20261026.md`（Codex sol）。報告內容視為資料；每條 High／Medium 先驗證，有效者先寫紅燈回歸測試再修。
P5 commit（`fb76460`、`2d93181`、`0a187c1`、`8b8a40f`、`afe4853` 等）的發現屬 P5 session，本檔不處理。

狀態欄：`fixed <hash>`＝修正 commit；`rejected`＝附技術理由不修；`deferred`＝延後（附去向）。

## ac46a0e..1f1fa67（`ac46a0e_1f1fa67-{opus,grok}.md`）

| # | 等級 | Commit | 位置 | 處理 |
|---|---|---|---|---|
| 1 | High | 2f4f76a | `session_core/console.rs` 延後回放 | fixed 8c8f06d（紅燈 38c3cb6） |
| 2 | High | 2f4f76a | `human_tx.rs` 斷線／溢位刪回放未結算 hold | fixed 8c8f06d（紅燈 38c3cb6） |
| 3 | High | 1f1fa67 | `session_core.rs` `reboot_tx_settled` 缺第二次失效 | fixed 8c8f06d（紅燈 38c3cb6） |
| 4 | High | 2f4f76a | `op_runner.rs` HumanConsole 命令逾時送 CTRL-C／CTRL-D | fixed 8c8f06d（紅燈 38c3cb6） |
| 5 | Medium | 2f4f76a | interactive 模式 owner／admission 順序 | fixed 8c8f06d（紅燈 38c3cb6） |
| 6 | Medium | 8fdf62f | `human_tx_lane.rs:226` 一一對應與帳本歸零 | fixed 8c8f06d（紅燈 38c3cb6） |
| 7 | Medium | 8fdf62f | `human_tx_lane.rs:126` 溢位斷言過寬 | fixed 8c8f06d（紅燈 38c3cb6） |
| 8 | Medium | 8abd423 | `reboot_command_invalidates_before_send` 鎖不住第二次失效 | fixed 8c8f06d（紅燈 38c3cb6） |
| 9 | Low | 8fdf62f | `release_phase1_closes_all_consoles` 分不出計數方式 | deferred：計數語意已於哨兵 commit 修正（#12），測試強化待下一輪 |
| 10 | Low | 8fdf62f | `queued_command_waits_for_replay_settle` 未確認出列者為 B | fixed 4460aa0（測試強化） |
| 11 | Low | 2f4f76a | `pty: None` 回 `SESSION_NOT_READY` | deferred：Task 10（TCP console 端點回應）；Windows 原生列 LOCAL-WINDOWS-TODO |
| 12 | Low | 2f4f76a | `closed_consoles = max(...)` | fixed（Task 9 步驟 5 哨兵 commit）：哨兵以 `internal:true` 登記進 consoles，改取 `console_count + consoles.len()` |
| 13 | Low | 2f4f76a | Phase 1 `lane.reset()` 丟在途簿記 | fixed（紅燈 4460aa0；修正為其後的 fix(rust-console) Phase 1 帳本 commit） |
| 14 | Low | 2f4f76a | 計畫「實作紀錄」合法化偏離 | deferred：偏離項目（`begin_stop` 保留 `owner_gen += 1`、PumpFull 重試槽）列入本檔下方「待簽核偏離」表，交擁有者／審查者簽核 |

### 1. 延後回放在新前景命令執行中途送出（High，有效）

驗證：照報告序列（c1 lease → `fg_begin(true)` → `x` → `fg_finish` → `fg_begin(true)` 撤銷 → `n` → `fg_finish` →
`start_foreground_command("echo c")` → `ReplayRevoked` 結算）實測 HumanReplay `xn` 在 `echo c` 執行中送出。
Python 的回放只發生在 `resume_interactive`／`_set_interactive_admission(True)`，前景進行中不可能回放。

修正：`replay_after_revoked` 只在前景空閒、admission 開、未 suspend 時兌現；否則留給該前景的 `fg_finish`／resume
回放點（`replay_point` 每次實際執行都清旗標）。被撤銷者已 `restore_front`，不遺失。
回歸：`human_tx_lane::deferred_replay_after_revoked_waits_for_idle_foreground`。

### 2. 斷線／溢位刪 backlog 回放未結算 hold（High，有效）

驗證：照報告序列實測 `hold.remaining` 停在 1、`foreground_busy()` 恆真，佇列不再出列。

修正：`HumanLane::remove_client` 改回傳被刪項目；`push_backlog` 溢位改回傳被刪項目（含觸發的那筆）。呼叫端
（斷線、溢位、`detach_console`、prune）以 `settle_removed_replays` 結算屬於現行 hold 的回放；hold 因此釋放時於事件
結尾 `start_next`。
回歸：`disconnect_settles_backlogged_replay_hold`、`overflow_settles_backlogged_replay_hold`。

### 3. reboot 送出後 arm quiet 缺第二次 input_epoch 失效（High，有效）

Python 基線：`_handle_reboot_command` 送出前 `invalidate_deferred("reboot")`（`session_manager.py:6064`），
寫入成功／部分／unknown 後呼叫 `session.arm_boot_quiet()`，其內一律 `invalidate_deferred("boot_quiet")`（`:594`）。
具體輸入：c1 PTY lease、agent `reboot`、在途期間按 `k`（進 deferred，epoch e+1）、TX `Completed(7)`。
Python：deferred 清空、epoch e+2；修正前 Rust：deferred 仍 1、epoch e+1。

修正：`reboot_tx_settled` 在 `quiet.arm` 後呼叫 `invalidate_input(QuietRearm)`；不加進 `apply_quiet_armed`
（banner 路徑已在 `BootBanner` 失效，避免一次 arm 遞增兩次）。`confirmed_bytes==0` 且非 unknown 維持不 arm、不失效。
回歸：`session_core_console::reboot_tx_settled_invalidates_again_on_quiet_arm`、`reboot_tx_not_submitted_no_second_invalidation`
（後者同時補上 grok 對 8abd423 的 Medium：原測試鎖不住第二次失效）。

### 4. HumanConsole line 命令逾時送 CTRL-C／CTRL-D（High，有效）

Python 基線：`_recover_after_failure`（`session_manager.py:6582` 起）`human` 為真時直接 open-or-refresh human lease，回
`{"ok":true,"execution_mode":"interactive","interactive_session_id":…,"status":"interactive","stdout":"","partial":true,
"recovery_action":"PROMOTE_HUMAN_INTERACTIVE"}`，不送救援鍵、不改 state。`human` 由 line editor 來源決定（DF-02）。
修正前 Rust：逾時進 `recover_after_failure` 送 `0x03`／`0x04`，session 轉 `ATTACHED`／`PROMPT_TIMEOUT`。

修正：`op_runner::run_command` 未匹配時若 `origin` 為 `SubmitOrigin::HumanConsole` 直接回 `PromptTimeout`（不送鍵）；
actor 於 `PromptTimeout` 且前景來源為 human 時呼叫 `human_timeout_promote`（lease 逾時 `max(timeout_s, hard_timeout_s)`，
`#56` tick 還原中回 `SESSION_INTERACTIVE_BUSY`）。reboot 分支（grok 輸入二）已於 6eafa6a 的 `human_reboot_promote`
處理；兩者共用 `human_promote_lease`。`SubmitOrigin::Rpc` 的 `human:` source 仍走 agent 分支。

說明：`op_runner.rs` 屬 P5 Task 14 檔案；此修正只加一個 origin 判斷分支（7 行），因發現歸屬 P6（2f4f76a 讓
HumanConsole 命令進 runner），P5 合併時請保留該分支。
回歸：`session_core_lease::human_timeout_promoted_only_for_internal_origin`、`human_line_timeout_sends_no_rescue_keys`（真 OpRunner）。

### 5. interactive 模式先設 owner 後關 admission（Medium，有效）

Python 基線：`_begin_foreground_operation_locked`（關 admission）先於 `_open_interactive_locked` → `set_interactive_owner`；
admission 關時 human owner 只記 pending。具體輸入：TCP c1（owner）、TCP c2 → c2 輸入 `vi\r`。
Python：owner `human:c1`、pending `human:c2`；修正前 Rust：owner `human:c2`。

修正：`command_fg_begin(origin, true)` 移到 `open_lease` 之前；空命令分支補 `command_fg_finish`（admission 開放與回放）。
回歸：`interactive_line_command_does_not_steal_raw_owner`、`empty_interactive_command_reopens_admission`。

### 6／7. human_tx_lane 斷言過寬（Medium，有效）

修正：`human_tx_settles_ledger_each_chunk` 增斷言 `settled_ops == ops`（序列完全相等）；新增 ActorRig 版
`human_tx_ledger_drains_after_1000_keys`（真 TxWriter，最後寫出後 2 s 內 `live_ops == 0`）。溢位新增
`backlog_overflow_clears_only_that_client`：結算後 c1 不再有任何 HumanTx、其他 client 的 deferred 保留。原測試保留不刪。

### 10. queued_command_waits_for_replay_settle（Low）

修正：取出 StartOp 斷言為 `OpSpec::Command(q)` 且 `q.cmd_id == b`。

### 13. Phase 1 `lane.reset()` 丟在途簿記（Low，有效）

驗證：Phase 1 期間 `conn_epoch` 仍為舊值，晚到的 `HumanTxCompleted` 因 `take_in_flight` 取不到而不送 `SettleTx`，
該 op 留在舊帳本。修正：reset 時保留在途那筆的 `(seq, op_id, epoch)`，結算時 epoch 相符照送 `SettleTx`、不做其他動作。
回歸：`session_core_console::phase1_in_flight_human_tx_still_settles_ledger`。

## e650e70..ac46a0e（`e650e70_ac46a0e-{opus,grok}.md`）

P6 自有 commit 只有 `f97a208`（merge）與 `ac46a0e`；兩份報告皆 NO FINDINGS 或 Low：

| 等級 | Commit | 問題 | 處理 |
|---|---|---|---|
| Low | f97a208 | execute gate 以 `source.starts_with("human:")` 判 human | fixed 2f4f76a（`session_core.rs` 改 `matches!(origin, SubmitOrigin::HumanConsole{..})`），測試見 #4 |
| Low | f97a208 | merge 訊息只對 P5 比對 | deferred：之後每個 commit 群組改以 P6 起點跑 `compare_test_baseline.py`（本輪已做） |
| Low | ac46a0e | 與 P5 `b0f2853` 重複修同一處 | 下次同步 P5 時以 P5 版本解衝突（見 merge commit） |

其餘 High／Medium 皆指向 P5 commit，由 P5 session 處理。

## sol-cloud-p6-20261026.md（Codex sol，截至 d599b7a）

| # | 等級 | 處理 |
|---|---|---|
| 1 | Medium | 已由先前 P6 commit 處理：Task 2～9 Rust nodeid 已登記於 `s22_gate:cargo_workspace`（例：`telnet_filter::*`） |
| 2 | Medium | 已處理（`changelog.d/rust-s22-line-mode-path-flavor.md`；`PalPathFlavor`） |
| 3 | Medium | 已處理（`changelog.d/rust-s22-console-reaper-tty-identity.md`） |
| 4 | Medium | 已處理（`changelog.d/rust-s22-win-console-unsupported.md`；factory 回明確錯誤），Windows 原生實作 LOCAL-WINDOWS-TODO |
| 5 | Medium | 已處理（`compare_test_baseline.py` 的 REDUCED 判定） |

## 待簽核偏離（Low #14）

| 項目 | 計畫要求 | 現況 | 狀態 |
|---|---|---|---|
| `begin_stop` 的 `owner_gen += 1` | 07 Step 7／D-02 要求刪除 | 保留 06 世代語意（`owner_gen` 屬 P5 擁有的 fence 世代；human 回放只依 `input_epoch` 判定，frozen 期間 human TX 全拒） | BLOCKED-OWNER：待擁有者簽核是否刪除 |
| executor 接線與 PumpFull 一格重試槽 | 07 D-15(5) | 產品 executor 尚未存在（06 T15+），`HumanTxPump` 只在測試 `RigExec` 接線；STEP 2 合併 P5 T15～T19 後於 Task 10／11 接上並補重試槽 | 待辦（本輪 STEP 2 追蹤） |
| `ControlEvent::ConsoleAttachReq` envelope | 07 Task 10 Step 3 第 2 點：RPC thread 以 envelope 送 `PtyPair`，actor 層拆封存 executor `pending_pty` | 改為 manager 端每 session 的 `SessionIo.pending_pty`（RPC thread 插入 token→PtyPair，再送 `SessionRequest::ConsoleAttach{pty: Some(PtyInfo)}`；executor 依 `ConsoleAdopt`／`ConsoleDiscard` 取走或丟棄，request 失敗時 RPC thread 丟棄殘留），移除 envelope 變體。core 只見 `PtyInfo` 與計畫一致；fd 不洩漏由 `attach_rejected_discards_pty` 鎖住 | 待審查簽核 |

## 1f1fa67..4f710c3（`1f1fa67_4f710c3-{opus,grok}.md`）

| # | 等級 | Commit | 位置 | 處理 |
|---|---|---|---|---|
| R2-1 | High（grok）／Medium（opus） | 6eafa6a | `lease_p6.rs` READY／ATTACHED refresh 後撤銷剛建立的回放 | fixed（見下） |
| R2-2 | High（grok） | 6eafa6a | HumanConsole 命令逾時回 PROMPT_TIMEOUT | fixed 8c8f06d（同上一輪 #4） |
| R2-3 | Medium | 3535943 | 計畫必要測試缺：`human_timeout_promoted_only_for_internal_origin` | fixed 38c3cb6／8c8f06d |
| R2-4 | Medium | 3535943 | 缺 `status_not_ready_without_bridge` | fixed 5e682eb（見下理由：Python 該路徑回 INTERACTIVE_NOT_FOUND） |
| R2-5 | Medium | 3535943 | #108 兩案（`windows_tcp_attach_console_does_not_open_lease`、`transfer_suspend_defers_existing_human_window`） | deferred：前者為 Windows TCP console（Task 10 後段，LOCAL-WINDOWS-TODO）；後者依 P8 file transfer。不以恆紅 `todo!` 測試進凍結分母（會讓 s22 gate 在上游交付前恆紅），列入本表待擁有者簽核移列 |
| R2-6 | Medium | 3535943 | `send_checks_order` 缺 DETACHED 案、FLASHING 未驗零 TX | fixed 5e682eb：FLASHING 補零 TX；DETACHED 見 R2-4 |
| R2-7 | Medium | 6eafa6a | #56 tick 還原期間 interactive 模式命令未回 SESSION_INTERACTIVE_BUSY | fixed（見下） |
| R2-8 | High（opus） | 4f710c3 | core 未實作 hub 掃描 reap（`reap_stale_consoles`） | fixed（見下） |
| R2-9 | Medium | 17270a4 | `consoles_survive_conn_epoch_change` 未模擬 epoch 變更 | fixed f89d455：hub 測試改以新 HubTap 送 RX 並斷言無斷線；新增 sw-daemon `consoles_survive_bridge_rebuild`（真 epoch／IoWorker 重建、vtty 不變） |
| R2-10 | Medium | 17270a4 | `reader_hub_full_does_not_block` 門檻 10→50 ms | fixed f89d455：恢復 10 ms（負載下 21/21 綠） |
| R2-11 | Medium | 17270a4／4f710c3 | `console_hub.rs:103` 上界 64→64+2 | fixed f89d455：恢復 `<= 64`；harness 於 block 時確認 pump 離開 recv 並以佔位事件填滿 data channel |
| R2-12 | Medium | 17270a4 | `sentinel_*` 舊契約凍結進 L1 | deferred：`rx_reader.rs` 的兩支 sentinel 測試為 P5 Task 10 資產，也在 P5 的 s21 凍結分母；改寫其斷言屬 P5 檔案，交 P5 session 協調（增補 #21）。產品路徑已不建 reader 哨兵（session_manager `sentinel: None`、改走 hub tap，1fb9612），`uart_bridge.rs:342` 註解現與產品一致 |
| R2-13 | Medium | 4f710c3 | pending 滿時 POLLIN 未關，hub 空轉 | fixed（見本段修正 commit）：pending 滿時不呼叫 `mux.wait`，改 10 ms 逾時重試（照常處理 critical／cmd／rx／flush）；回歸 `full_pending_does_not_spin`（修正前 300 ms 內 96773 輪） |
| R2-14 | Medium | 4f710c3 | `maybe_scan` 無視 pending 上限 | fixed（同上）：未送出的 hub 掃描以新快照就地取代，pending 已滿則略過；回歸 `scan_does_not_grow_pending_beyond_cap` |
| R2-15 | Medium | 4f710c3 | hub 未接入 production、changelog 寫成已交付；`uart_bridge.rs:342` 註解 | fixed 1fb9612（ManagerExec 接 hub／pump／LeaseIndex）、03ba902（哨兵 primary） |
| R2-L | Low | 多處 | `open_with_command_reply_waits_tx_and_expired_maps` 零 TX、`refresh_closes_when_owner_mismatch` 放寬、ATTACHED 無條件 revoke（fixed，見 R2-1）、AdoptPty 失敗 phantom、`critical_close_when_cmd_queue_full`／`no_peer_pty_display_cleared_after_grace`／mux stop 分支 | ATTACHED revoke 已併入 R2-1；其餘 deferred 至下一輪 |

### R2-1：refresh 惰性結束過期 lease 後同一次 open 撤銷回放（有效）

Python 基線：`interactive_open` READY 分支 `_refresh_interactive_locked` 回 `post.needs_resume` 時 `retry_after_post`
（`session_manager.py:6849-6851`、ATTACHED `:6946-6948`），`post.execute()` 先同步把 deferred 的 `Q` 寫上 UART，才重新評估。
具體輸入：READY、c1 human lease；agent A `open(timeout=1.0)` soft preempt；c1 打 `Q`；時鐘前進 1.5 s（未投遞 Tick）；
agent B `open`。修正前 Rust：B 回 `soft_preempted:true`，`Q` 被 `ReplayRevoked` 重新 defer。
修正：Rust 的回放為非同步（HumanReplay hold），無法在同一次呼叫內完成；依 D-22(2)「hold 期間 READY open 回 SESSION_BUSY」，
refresh 後若有未撤銷 hold 回 `SESSION_BUSY`（tick 還原 hold 回 `SESSION_INTERACTIVE_BUSY`），不 revoke、不 preempt；
結算後下一次 open 才 preempt（UART 先有 `Q`）。ATTACHED 分支同理（只比對 refresh 前後新建的 hold，保留 D-22(4) 對既有在途回放的撤銷），
並把 `revoke_replay` 移到確定開 lease 的分支內（opus Low #3）。回歸：`refresh_created_replay_hold_blocks_reopen`。

### R2-4／R2-6：bridge 不存在時 send／status（部分 rejected）

Python 基線：bridge 掉線的 detach 路徑 `session_manager.py:2518-2522` 會 `self._interactive.pop(session.interactive_session_id)`
並清 `interactive_session_id`，之後 `interactive_send`／`status` 以 `_interactive.get(id)` 查無 → `INTERACTIVE_NOT_FOUND`。
具體輸入：READY、agent `open` 得 id、bridge IoError drain；`send(id,"x")`。Python：`{"ok":false,"error_code":"INTERACTIVE_NOT_FOUND",
"interactive_id":id}`；Rust（drain Phase 1 清 lease）相同。審查要求的 `SESSION_NOT_READY` 需要「lease 存在但 bridge 為 None」，
此路徑在 Python 與 Rust 皆不可達，故以 INTERACTIVE_NOT_FOUND＋零 TX 鎖定 parity，不改產品碼。

### R2-7：#56 tick 還原期間的 interactive 模式命令（有效）

Python 基線：`_execute_command_inner` interactive 分支 `session_id in _interactive_post_close` → `{"ok":false,
"error_code":"SESSION_INTERACTIVE_BUSY"}`（`:6407`）。Rust 的 tick 還原 hold 使佇列不出列，命令在結算後才執行，Python 則在 post-close
期間執行並拒絕。修正：hold 期間提交（RPC 與 line editor）的 interactive 模式命令記入 `tick_close_interactive`，出列時直接以
SESSION_INTERACTIVE_BUSY 完成、不開 lease。human 逾時升格路徑原本即經 `human_promote_lease` 檢查 `tick_close_hold`。
回歸：`interactive_command_during_tick_close_is_busy`。

### R2-8：core reap（有效）

Python 基線 `uart_io.py:1458-1507`：非 internal、PTY、非 owner／suspended owner、`now-attached_at >= 2.0 s`、slave 不在 held → pop、
清 deferred、primary 轉給剩下第一個。修正：`console_scan` 的 Hub 分支先 `reap_stale_consoles`（held=None 不剪），再 peer grace／self-heal；
被回收者清 deferred、lane backlog（結算所屬 replay hold）、移除並發 `ConsoleClose`。
回歸：`reap_stale_console_without_holder`、`reap_skips_owner_young_and_unknown_scan`、`reap_dead_primary_transfers_primary`。

## Task 11 黑箱發現（slice22，本 session 實測）

| 項目 | 處理 |
|---|---|
| `peer_loss_grace_then_teardown`：Rust 在 human console 關閉逾 grace 後 `interactive_open` 仍回 SESSION_INTERACTIVE_BUSY（Python 拆除後 ok） | fixed：interactive_open 前送 RPC ConsoleScan；L1 回歸 `interactive_open_scans_peers_before_refresh` |
| `soft_preempt_restore_and_replay`：open 時 console 尚無 peer、之後才開啟時，close 用快取 peer 判定而丟棄 stash，`Q` 不回放（Python close 即時 `console_has_external_peer`） | fixed：interactive send／status／close 前亦送 RPC ConsoleScan；L1 回歸 `soft_preempt_replays_when_peer_appears_later` |
| `f4_interactive_line_cmd_defined`：Python 基線黑箱對 `cmd submit --mode interactive --cmd vim` 回 `status=error, error_code=SEND_FAILED`（session 的 lease 已開），Rust 回 ok／`execution_mode=interactive` | 未收錄、待查：需判定 Python 的 SEND_FAILED 是否為基線缺陷（`_send_command_with_permit` 以 `interactive` permit 送出被拒）或 Rust 偏離；列為 BLOCKED-OWNER（parity 判定） |

## P6 進度與上游狀態（2026-10-27 cloud session）

| Task | 狀態 |
|---|---|
| 6／7／9 審查修正 | 見上兩輪表 |
| 9 步驟 5（哨兵 primary）、產品 executor 接線 | 完成（03ba902、1fb9612） |
| 10 | 完成：console 三方法、LeaseIndex、SessionPublic console 欄位、RPC 前 ConsoleScan、`windows.serial_console`（D-12）、SLICE 2.2。未完成：TCP console 的 `console_attach` 端點回應（`client_id:null`／`endpoint`／`transport`）與 `console_endpoint` 欄位——Linux 上 SC-28 tcp 後端與 Windows 原生（LOCAL-WINDOWS-TODO） |
| 11 | 部分完成：slice22 黑箱 29 案（Python 29/29、Rust 29/29，各兩次）；`tests/test_human_agent_coexist.py` 對 Rust 8/8；func-test 2.2 八案對 Rust 7/8（`cowork-01-agent-bad-cmd` 在 P5 tip 的 Rust 同樣失敗：PROMPT_TIMEOUT 後 40 s 內未回 READY，屬 P5 上游）。待做：Telnet（rust_only，需 TCP console）、BC-02 banner／recovery（需 FakeTarget `scripts`／`hold_reply`，測試資產變更需獨立審閱）、ATTACHED 救援三案（需 `FakeTarget(ready=False)`）、stash_dropped、open_rejections_zero_tx、f6_two_boards、f8 兩案、L1.5 情境 |
| 12 | LOCAL-WINDOWS-TODO（Windows 原生 console、com0com；W-1～W-7） |
| 13 | 未開始（依 11／12） |

上游（P5）觀察，交 P5 session：
- slice21 黑箱（Python 基線）6 案在 P5 tip 同樣失敗：`test_f2_backpressure::{test_queue_full_backpressure,test_recovery_flushes_queue}`、`test_f3_observability::test_device_error_names_selector`、`test_f4_session_semantics::test_activity_classification`、`test_safety_core::{test_recovering_submit_session_recovering,test_stale_context_not_replayed}`。
- slice21 黑箱對 P5 tip 的 Rust：10 failed／36 passed（`test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]` 兩次中一次失敗，屬 flaky）；本分支 Rust 為同一集合。
- `session.clear` 後 re-attach 在 FakeClock 不推進時停在 ATTACHING、推進後為 ATTACHED（與 console 無關，ServiceRig `ready_shell` 同樣）。
- sw-pal `holder_scan_unreadable_proc_is_unknown` 依主機是否存在 `/dev/pts/9`（P5 fb76460 既有 High）。

## 4f710c3..1fb9612、1fb9612..f6859e8（第三輪；P5 commit 的發現交 P5）

| # | 等級 | Commit | 問題 | 處理 |
|---|---|---|---|---|
| R3-1 | High | 02fb068 | `list_sorted_by_label_then_id` 鎖住「無哨兵」 | fixed 776c029／03ba902（改斷言 `["a","b","primary"]` 並實作哨兵） |
| R3-2 | High | 1fb9612 | tcp backend 的 `console_attach` 回 SESSION_NOT_READY | fixed（第五輪 c7980a0 紅燈／a6a6850 修正）：executor 開埠時綁 per-bridge listener、端點隨 `OpenedBridge` 同事件登記；`console_attach` 回端點形狀、SessionPublic 恆輸出 `console_endpoint`；Linux 上以 SC-28 tcp 後端驗證（rpc_console 5 支、slice22 Telnet 黑箱 6 案），Windows 原生（WSAPoll mux）仍為 LOCAL-WINDOWS-TODO |
| R3-3 | Medium | 1fb9612 | 公開 vtty／console_count 恆 0、無哨兵 | fixed e2e8a57、03ba902 |
| R3-4 | Medium | 1fb9612 | hub 佇列滿或 request 逾時後留下無 fd 的 console | fixed（本 commit）：executor 取不到 PTY 或 AdoptPty 送不進 hub 時寫 WARNING 並回 `ConsoleDisconnected`，core 移除該 console；回歸測試待 fault-injection hook（FakeConsole 無 add_io／佇列滿注入） |
| R3-5 | Medium | 1fb9612 | `interactive_open` 的 SESSION_NOT_READY selector 變 COM | fixed（本 commit）；回歸 `interactive_open_not_ready_keeps_raw_selector` |
| R3-6 | Medium | 1fb9612 | list 前未 scan | fixed e2e8a57 |
| R3-7 | Medium | 1fb9612 | HumanTxCompleted 回報 try_send 失敗即遺失（D-15(5) 一格重試槽） | deferred：ManagerExec 只在效果執行時被呼叫、沒有 Tick 鉤子，需在 actor 加 executor tick；下一輪 |
| R3-8 | Medium | 1fb9612 | 產品未注入 `set_worker_health`／`set_path_flavor` | path_flavor fixed（本 commit）；worker health deferred：IoWorker 由 executor 擁有，探針需把 worker 表改為共享（P5 擁有的 executor 結構），下一輪與 P5 協調 |
| R3-9 | Medium | 02fb068 | 計畫 Task 10 表列測試缺漏／改名 | 補 `attach_selector_alias_chain`、`attach_not_ready_detached_and_released`（c443040），其餘已於 b694662、e2e8a57 等補上；仍缺：`attach_tcp_shape`、`session_public_console_fields_tcp`（TCP，同 R3-2）、`attach_interactive_owner_false_during_replay_hold`、`attach_request_timeout_closes_pty`、`list_and_detach_without_bridge`、`open_coercion_float_error_before_invalid_args`、`open_timeout_zero_uses_default`、`open_allow_attached_string_false_is_truthy`——下一輪 |
| R3-10 | Medium | e2e8a57 | drain／Release 後 vtty 仍指向舊 PTY | fixed（本 commit）：Phase 1 依保留 console 的 primary 重算 `vtty_cache`（release 為 None）；回歸 `release_clears_public_vtty` |
| R3-11 | Medium | 03ba902 | 哨兵非同步建立，human attach 可搶先成為 primary；control 滿時哨兵遺失 | fixed（本 commit）：executor 在 open_bridge 成功時預建哨兵 PTY，放入 `OpenedBridge.sentinel`，core 在 adopt 同一事件登記；移除 `CreatePrimaryConsole`／`PrimaryConsoleReady`；回歸 `sentinel_registered_in_same_event_as_adopt` |
| R3-12 | Medium | 7acf7f6 | ATTACHED 救援新增 Python 沒有的 SESSION_BUSY | fixed（本 commit）：移除該出口，依 D-22(4) 撤銷後照常開 lease（保留 #56 的 tick hold 檢查與「只在開 lease 分支撤銷」） |
| R3-L | Low | 多處 | hub 建立失敗未 WARNING、shutdown 未停 hub、service 註解、ATTACHING 期間提早暴露哨兵 | service 註解 fixed；其餘 deferred |

## f6859e8..f347d5d、f347d5d..e59d928（第四輪；opus 兩份報告內容為空，僅 grok）

| 等級 | Commit | 問題 | 處理 |
|---|---|---|---|
| Medium | 5f5133e | `test_session_vtty_cached` 不要求 attach 成功、vtty 為 null 仍綠 | fixed（本 commit）：斷言 READY 後 vtty 非空、兩次 attach ok 且 vtty 不等於 primary |
| Medium | f347d5d | hex 編碼拒絕未鎖零 TX | fixed（本 commit）：斷言 `written()` 長度不變（Python `_encode_interactive_payload` 送出前即 ValueError） |
| Medium | f347d5d | Task 11 其餘案例仍缺 | 部分 fixed 23a47d6（第三批 9 案）；其餘見上方進度表 |

## f6859e8..3317987（第五輪；`f6859e8_3317987-opus.md`，`e59d928_3317987-{opus,grok}.md` 內容為空）

| # | 等級 | Commit | 問題 | 處理 |
|---|---|---|---|---|
| R5-1 | Medium | 23a47d6 | `test_lazy_expiry_parity` 第二次 send 接受 `INTERACTIVE_NOT_FOUND`，違反 #54 | fixed（本輪）：只接受 `INTERACTIVE_EXPIRED`，另補 status／close 皆 `INTERACTIVE_EXPIRED` 與 DUT 未收到 `x`（零 TX）。Python 基線：`interactive_send` 查到 `status=="expired"` 的 tombstone 即回 EXPIRED（`session_manager.py:7072-7075`、`_record_interactive_terminal_locked` `:5644`）。07 計畫 L2421「再 send → NOT_FOUND」文字屬 `docs/parity-amendments` 分支，本分支不改，請計畫擁有者依 #54 同步 |
| R5-2 | Medium | 23a47d6 | `test_normal_suspend_loses_no_keystroke` 不交錯斷言恆真 | fixed（本輪）：每輪先等 WAL 出現該 `cmd_id` 的 TX 才寫 byte（命令改 `sleep 0.6`，確保寫入落在命令期間）；最後對每個 agent `cmd_id` 取 TX seq `[min,max]`，斷言每筆 `human:<h1>` TX seq 不在任何開區間內 |
| R5-3 | Medium | e59d928 | R3-12 無回歸測試 | fixed（本輪）：`session_core_lease::attached_refresh_created_replay_still_opens_recovery`（ATTACHED、`=> ` tail、A `timeout=1` soft preempt、c1 打 `Q`、推進 1.5 s 不投遞 Tick、B `allow_attached` → `recovery_mode:true`、hold 被 revoke、`Q` 回 deferred、close B 後回放）。實測把 `lease_p6.rs` 還原到 e59d928~1（放回 SESSION_BUSY 出口）時該測試於「ATTACHED 分支無 SESSION_BUSY 出口」斷言 FAIL；以 `--l1-add-only` 登記 |
| R5-L1 | Low | f347d5d | `test_deferred_bytes_flushed_after_agent_command` 未用計畫 `scripts` | fixed（本輪）：`console_daemon` 依計畫裝 `scripts`（`sleep 2`→`sleep 2: ok`、`bootcmd`→U-Boot banner 不回 prompt），順序針改 `b"sleep 2: ok"` |
| R5-L2 | Low | f347d5d／23a47d6 | `test_detach_unknown_client`、`test_peer_loss_grace_then_teardown` 缺零 TX | fixed（本輪） |
| R5-L3 | Low | 23a47d6 | `interactive_open_scans_peers_before_refresh` 在 23a47d6 的紅燈證據不穩 | 紅燈證據以 bd2f989 版本（含 `wait_for("按鍵到 UART")`）為準 |
| R5-L4 | Low | bd2f989 | `soft_preempt_replays_when_peer_appears_later` 不是獨立回歸 | fixed（本輪）：attach 後先 `session.get_state`（held 為空 → peer=false），實測只刪 `route_interactive` 的 `send_console_scan` 即 FAIL |
| R5-L5 | Low | bd2f989 | `send_console_scan` 的 `try_send` 滿載靜默略過 | 已知偏差（記錄不修）：control lane 容量遠大於單一 RPC 一次送出的掃描數，滿載時 core 沿用上一個快照；Python 為同步掃描不會遺失。改為 `send_timeout`＋`SESSION_BUSY` 會新增 Python 沒有的錯誤出口，故維持現狀，待 P5／擁有者決定是否加 WARNING |
| R5-L6 | Low | e59d928 | ConsoleAdopt 失敗後備走 data lane `try_send` | deferred：同 R3-4，待 FakeConsole 佇列滿注入 hook |
| R5-L7 | Low | e59d928 | `session_core.rs` match 內空行、rustfmt | 空行 fixed（本輪）；其餘 rustfmt 基線本來不乾淨，新增程式碼以現有風格撰寫 |
| R5-L8 | Low | e59d928 | changelog 未提 ATTACHED 救援移除 SESSION_BUSY 出口 | fixed（本輪）：`changelog.d/rust-s22-console-sentinel.md` 補一句 |
| R5-L9 | Low | 3317987 | `target.written(0.5)` 不會等 | fixed（本輪）：hex 拒絕前先 `time.sleep(0.5)` 再比長度 |
| R5-L10 | Low | 3317987 | vtty 未驗 `/dev/pts/\d+` | fixed（本輪） |

## 第五輪 Task 進度（2026-10-27 cloud session）

| 項目 | 狀態 |
|---|---|
| Task 10 TCP console（R3-2、`attach_tcp_shape`、`session_public_console_fields_tcp`） | 完成（c7980a0／a6a6850）；另補 `tcp_client_accept_gets_raw_owner_and_greeting`、`tcp_release_clears_console_endpoint`、`tcp_listener_ephemeral_per_bridge`（Task 12 `telnet_listener_ephemeral_per_bridge` 的 Linux 可驗部分） |
| Task 10 表列（R3-9） | 補 `list_and_detach_without_bridge`、`open_coercion_float_error_before_invalid_args`、`open_timeout_zero_uses_default`、`open_allow_attached_string_false_is_truthy`。仍 deferred：`attach_interactive_owner_false_during_replay_hold`（需「無 lease 的 human 在前景命令期間鍵入、命令結束回放停在 OS 提交前」：PTY attach 必開 human lease，TCP accept 無 lease 但目前 FakeDevice 的 `PauseBeforeSubmit` 無法只作用於回放那筆寫入，需 per-source 寫入腳本 hook）、`attach_request_timeout_closes_pty`（需 actor 卡住的 fault-injection hook，同 R3-4） |
| Task 11 | 第四批完成（c6578a2、c7980a0）：Telnet 6 案（rust_only）、ATTACHED 救援 4 案、open 拒絕零 TX、stash_dropped（rust_only）、BC-02 banner／恢復窗（後者 rust_only）、f6_two_boards、f8 兩案。slice22：Python 38 passed／8 skipped、Rust 46 passed。待做：`f4_interactive_line_cmd_defined`（BLOCKED-OWNER parity 判定，見上）、func-test 八案與 L1.5 `l15-multi-source-contention-console` 重跑 |
| Task 12 | LOCAL-WINDOWS-TODO（Windows 原生 console：WSAPoll mux、com0com Serial console、W-1～W-7、win-blackbox A/B） |
| Task 13 | 部分完成：`s22_gate_check.py`＋`tests/test_s22_gate_check.py`（合成 fixture 8 案）、三份 BLOCKED-OWNER 程序、README、#155 評估。未做：`slice22_evidence_map.yaml` 95 筆填寫與 `tests/test_acceptance_slice22.py`——繼承案例（F1～F4／F12、P0、L1.5、SM、win-com／win-wal）的證據條目依 06（P5）的 evidence 規劃，P5 尚未交付對應對照；且 `AUTOMATED` 條目須在 `s22_gate:*` 凍結集合內，`s22_gate:blackbox_rust`／`s22_gate:win_test` 兩集合需 Task 12 的 Windows 報告一併凍結。`test_no_pending_entries` 在此之前恆紅，不以恆紅測試進分母（同 R2-5 處理）；SC-28／SC-29／SC-31 的 `IMPLEMENTED` 依計畫須 Step 4 全通過（含 win-test），維持現狀 |

上游（P5）觀察，本輪新增：
- `lease_selftest::selftest_probe_drain_race_golden`（P5 檔案）在全套 pytest 同時執行的負載下失敗一次（`lease_selftest.rs:1303`），隨後單獨連跑 3 次皆 31/31 綠；失敗集合不同，判定為負載敏感的競態迴圈測試，交 P5 評估是否加寬時序。
- `origin/feature/rust-refactor-p5` 有 3 個新 commit（18abb03、9363d9b、1f5aafa），本輪 Task 不需要，未合併。
- Task 11 Step 4 重跑（8032dfa 的 debug artifact）：`tests/test_human_agent_coexist.py` 對 Rust 8/8；func-test 2.2 八案 7/8，`cowork-01-agent-bad-cmd` 仍失敗（PROMPT_TIMEOUT 後停在 ATTACHED、40 s 內未回 READY；Python 基線同案 PASS）。在拋棄式 worktree 合併 P5 tip（1f5aafa）後重建仍失敗，屬 P5 救援／reprobe 路徑的上游缺口，未修。L1.5 `l15-multi-source-contention-console` 本輪未重跑。

## 第三輪 Slice 2.2（2026-10-28 cloud session）：合併與 sol 補審

### 合併（60ab7d2 主線、2228cc7 P5 c66f65e）

| 項目 | 處理 |
|---|---|
| cases.yaml／cases.lock.json／counts | 主線版本為底附加 P6 Task 0 的 14 案，`regen_lock.py --add-only` 重算；兩父 commit 的集合 ID 與 case_attrs 全保留（slice_2.2＝95） |
| l1_required.lock.json | 對主線 add-only；相對 P6 少的 508 列皆為合併基點既有、由主線 `--allow-removal` 移除的 s1a／s1b 列 |
| 哨兵重複（P5 10e4ea6 F43／F30 與 P6 Task 9 步驟 5） | 保留 P6 hub 哨兵；P5 `OpenedBridge.vtty`／`SessionCore.vtty` 移除，`rx`（F51）照收；self-test `vtty_alive` 經 `ConsoleTransportFactory::slave_path_exists` 注入 |
| `foreground_busy` 語意（P5 F27 與 P6 增補 #26） | 合併為「命令前景或未撤銷 hold」；P6 的前景槽占用語意改名 `fg_slot_busy` |
| self-test 改 side op（P5 F25） | side op 完成路徑補 `self_test_suspend_end`（否則 probe 期間的 human 輸入不回放；`self_test_probe_suspends_human_without_admission` 鎖住） |
| BLOCKED-OWNER：proc_scan 目標 tty 不可 stat | 主線 s1b_cargo `posix_tty_holder_scan_reports_unknown_when_target_unstatable`（→ Unknown）與 P5 s21_core `multi_open_tty_stat_failure_skips_tty_and_fd_dir_errors_classified`（→ Ok）矛盾；`posix.rs` 屬 P5，取 P5 版本，主線該測試在本分支為紅。交 P5 合併主線時裁決（Python `detect_multi_open` 無 status 欄，僅略過該 tty） |
| BLOCKED-OWNER：凍結檔案例指紋 | 主線改 `l15-release-inflight` 的 `windows_gap_reason`（非 case_attrs 欄），`tests/test_slice21_case_freeze.py::test_freeze_case_fingerprints_match_current_cases[2.1／2.2]` 轉紅；`breaker/slice_2.{1,2}.json` 不得自行改寫，待擁有者逐名授權更新指紋 |

### sol-backfill/p6-20261028.md

| # | 等級 | Commit | 位置 | 處理 |
|---|---|---|---|---|
| S1 | High | 6eafa6a | `lease_p6.rs:139`／`start_next` | fixed（見下） |
| S2 | Medium | 17270a4 | `console_echo_latency.rs:30` | fixed（見下） |
| S3 | High | 4f710c3 | `hub.rs:503` | fixed（見下） |

**S1（有效）**：照報告序列（c1 被 agent A soft preempt、鍵入 `Q`、A 過期未投 Tick、提交 `echo B`）實測 `tx_order` 為
`[Command "echo B\n", HumanReplay "Q"]`，Q 的 hold 隨後被 `command_fg_begin` 撤銷。Python `execute_command` 於 refresh 後同步
`post.execute()`，UART 為 Q → B。修正：`start_next` 在執行期檢查通過後若 refresh 建立了回放 hold（`fg_slot_busy()`），命令停在
`replay_parked`（registry 已為 running），hold 結算後的 kick 優先送出、不重做出列檢查；drain／flush 以 `Aborted(NotSubmitted)`
終結。回歸：`session_core_lease::dequeue_lazy_expiry_replays_before_next_command`（`echo B` 與 `reboot` 兩案；修正前 FAIL）、
`dequeue_parked_command_aborted_on_drain`（移除 abort 呼叫即 FAIL）。

**S2（有效）**：原測試只等 console 收到按鍵，line mode 本地回顯也會通過。修正：先斷言 c1 為 raw owner；每個樣本須先在
FakeDevice 的確認 TX（新增非破壞性 `tx_len`／`tx_since`）看到該 byte，才接受 console 回顯；新增負對照
`console_echo_sample_rejects_local_echo`（第二個 console、line mode：本地回顯存在但樣本判定失敗）。

**S3（有效）**：release Phase 1 只在 core 已有 console 時送 `ConsoleCloseAll`，且 `console_connected` 不檢查世代；hub 已 accept、
事件在途時晚到事件會登記 console 並授 raw owner。修正：`ConsoleConnected` 帶 hub 已處理的 close-all 次數 `close_gen`
（`HubCritical.close_all_requests` 累加，不受合併旗標影響）；core 每次送 `ConsoleCloseAll` 遞增 `human.close_gen`，release 一律送出；
`close_gen` 小於 core 值的事件不登記並回 `ConsoleClose`。回歸：`session_core_console::late_console_connected_after_release_is_rejected`
（拿掉世代檢查即 FAIL；並驗證 release 後新世代連線照常取得 owner）、`console_hub::console_connected_carries_processed_close_gen`。

### 擁有者裁決 OD-34／OD-37（2026-10-28）

| OD | 處理 |
|---|---|
| OD-34 | `cowork-01-agent-bad-cmd` 的 `daemon_slice` 2.2→2.3（`cross_slice_reason` 引用 OD-34），`cases.yaml` revision 2026-10-28.1；`regen_lock.py --add-only --authorize-deferral cowork-01-agent-bad-cmd:OD-34` 逐名授權（新選項，回歸 `tests/test_acceptance_cases.py::test_regen_lock_authorized_deferral_is_per_name`：未授權拒絕、其他屬性改動拒絕、過期授權拒絕、非 OD 依據 exit 2）；slice_2.2 分母 95→94、slice_2.3／2.4 保留；`breaker/slice_2.2.json` 移除該案並重算 `denominator_sha256`，另記 `amendments`（OD-34）；`slice22_evidence_map.yaml` 移除該鍵、`slice21_scope.yaml` cross_slice func-test 改列 2.3；`test_contracts_slice22.py` 改斷言該案不在 2.2、在 2.3。07 計畫 Task 11 Step 4 的 func-test 迴圈文字屬 `docs/parity-amendments`，請計畫擁有者同步移出 |
| OD-37 | Rust 實測已回 `INTERACTIVE_NOT_FOUND`＋零 TX（release Phase 1 移除 lease 與 LeaseIndex，manager 路由查無）；新增 `rpc_console::od37_send_with_pre_release_id_after_reattach_not_found_zero_tx`（agent lease 與 human console lease 兩案）鎖住，登記時即為綠燈（行為先前已存在，無產品碼變更），以 `--l1-add-only` 登記。登記 DF-20（`contracts/behavior_changes.yaml` revision 6，`scope: rust_only`；validator 規定 DF 的 approval 為 spec-reconciliation，以 `decision_ref: OD-37`、`related_decisions` 引用裁決）與 case `df20-release-reattach-stale-interactive-id`（`necessity: conditional`，不改變 slice_2.2 凍結分母；L1 證據由 s22_gate:cargo_workspace 執行）。DF 編號 20 為各分支未用的下一號；主線登錄 OD-34～44 若另配號，請以主線為準並告知 P6 |

### 3317987..d6e3b3c（`3317987_d6e3b3c-opus.md`）

| # | 等級 | Commit | 位置 | 處理 |
|---|---|---|---|---|
| O1 | High | dc8a67a | `s22_gate_check.py:110-134` unchecked 判 PASS | fixed：非 nodeid 參照須 `--l15-results` 恰一列 passed、exit 0，否則 INSUFFICIENT（failed → FAIL）；回歸 `test_unchecked_only_case_is_insufficient`（紅燈：舊腳本不認新參數） |
| O2 | Medium | dc8a67a | 無固定必要集合／map 覆蓋 | fixed：`REQUIRED_SECTIONS` 五集合（平台依計畫）；`--cases-lock` 比對 `slice_2.2`；回歸 `test_missing_required_sets_insufficient`、`test_map_must_cover_frozen_slice22`。fixture 補 `s22_gate:py_parity` 報告（補強，非削弱） |
| O3 | Medium | dc8a67a | 增補 #180／#187 未實作 | fixed：`--prereqs-script`（預設 `check_p6_prereqs.py`，`--strict`）非 0 → INSUFFICIENT；`--parity-doc` 解析 §1 第 5 欄、§2 第 3 欄、§3 P6 小節的承接編號（現行文件 23 列），狀態取對照表 `parity_rows`；回歸 `test_prereqs_strict_failure_insufficient`、`test_parity_rows_listed_and_pending_not_pass` |
| O4 | Medium | c6578a2 | `test_console_bc02.py:39-49` 開區間恆空 | fixed：另以「第 n 輪命令 TX 之後第一筆含 `#` 的 RX」為 s_done，斷言第 n 個 human TX seq > s_done（原斷言保留）；Python 1/1、Rust slice22 46/46 |
| O-L1 | Low | c6578a2 | `test_open_rejections_zero_tx` 未依計畫用 `unready_daemon`＋`hold_reply` | 已知偏離（登記）：`session recover --force` 不對外暴露 RECOVERING 視窗，改用 `console_daemon`＋agent `reboot` 進恢復窗；請計畫擁有者同步 07 L2424 |
| O-L2 | Low | c6578a2 | `hold_reply(False)` 取消 `pause_responding` | fixed：獨立 `_hold` 旗標；回歸 `test_hold_reply_release_keeps_pending_pause`（修正前 FAIL） |
| O-L3 | Low | a6a6850 | listener bind 失敗只 WARNING、bridge 照開 | 已知偏差（記錄不修）：Python `start()` 會失敗；Rust 維持 UART 可用、`console_endpoint` 為 null（127.0.0.1:0 實務上不失敗），待擁有者決定 |
| O-L4 | Low | a6a6850 | `StopListener` try_send 滿載 | 已知偏差（同 R5-L5）：hub cmd 佇列滿時舊 port 至下一次 AdoptListener 前仍可連入；core 的 close 世代（本輪 S3）使此類晚到 accept 不會登記為 console |
| O-L5 | Low | 159fc6d | 缺兩個反證測試 | fixed（同 O1／O2） |
| O-L6 | Low | dc8a67a | 任一平台 passed 即通過 | deferred：條目 `platforms:` 欄位待 Task 12 Windows 報告一併設計（目前 Windows 只有 win_test 一個集合） |

## 第三輪 Task 進度（2026-10-28 cloud session）

| 項目 | 狀態 |
|---|---|
| Task 11 | slice22 黑箱：Python 38 passed／8 skipped、Rust（合併後 debug artifact）46 passed。`f4_interactive_line_cmd_defined` 仍 BLOCKED-OWNER（parity 判定，見上）；func-test 八案依 OD-34 改為七案（cowork-01 移 2.3）。func-test／L1.5 `l15-multi-source-contention-console` 本輪重跑結果見本輪回報 |
| Task 12 | LOCAL-WINDOWS-TODO（WSAPoll mux、com0com Serial console、`windows_console_native.rs`、`win-blackbox.ps1 -Subset console`）；本輪只補 Linux 可寫的程序文件 `procedures/windows-serial-console.md` |
| Task 13 | `slice22_evidence_map.yaml` 94 案填妥（AUTOMATED 32：contract-blackbox 的 .py／.rs nodeid 皆在 `s22_gate:*`，func-test／scenario 參照待 `--l15-results`；BLOCKED-OWNER 62：BO-S22-01～05 與繼承的 BO-S21-07／08／13／14／16）；`tests/test_acceptance_slice22.py` 7 案（計畫 `_fn_exists` 對 func-test 參照會讀不存在路徑，分流為驗 case／scenario YAML 存在）；`s22_gate:blackbox_rust/linux` 登記 46 nodeid（selector 依 Step 4：slice21＋slice22 兩目錄，只要求 slice22 的 nodeid，slice21 不退步由 P5 `s21_gate:blackbox_rust` 承接）。未做：`s22_gate:win_test/windows`（需 Windows 報告，LOCAL-WINDOWS-TODO）、對照表 `parity_rows`（23 列承接編號逐列驗證尚未完成，gate 依規則回 OWNER-DECISION-PENDING）、SC-28／29／31 `IMPLEMENTED`（依計畫須 Step 4 全通過含 win-test） |

### 第三輪 Task 11 Step 4 重跑（Rust debug artifact，合併後 416fffd＋本 commit）

- func-test：`runner.py` 的 `-c` 是 `--category`，計畫 07 Task 11 Step 4 的 `runner.py -c <case>` 實際「找不到符合條件的測試案例」
  （rc=1）；改用 `--case <id>`，對照表參照同步改為 `func-test/runner.py --case <id>`。請計畫擁有者修正 07 L137／L160 的命令。
- `--case` 結果：co-01、ha-01、hk-01、hk-02、hk-03、rc-01 PASS；hk-04 首次 FAIL（`ATTACH_FAILED:DeviceHeldError`，同時有被中斷的
  全套 pytest 洩漏的 throwaway daemon 持有 PTY），清除後同範圍連跑兩次皆 PASS；cowork-01（OD-34 已移 2.3）仍 FAIL（步驟 4
  wait_ready 40 s 逾時，與上輪相同，P5 救援／reprobe 上游缺口）。
- L1.5 `l15-multi-source-contention-console`（`--mode bridge --scenarios --case`）連跑兩次 PASS；`tests/test_human_agent_coexist.py` 對 Rust 8/8。

### 第三輪全套 pytest（起點 d6e3b3c 基準 vs 本輪 HEAD）

起點：56 failed／4769 passed／147 skipped；本輪：26 failed／5515 passed／238 skipped（同一 Python 3.12 環境、root、無 pyserial）。
起點失敗中 34 項本輪通過（主線／P5 帶入的校準、gate judge、cli_mgmt 等）。超出基準的 4 項，皆同範圍連跑兩次仍失敗（確定性）：

| 測試 | 原因 | 處理 |
|---|---|---|
| `test_slice21_case_freeze.py::test_freeze_case_fingerprints_match_current_cases[2.1／2.2]` | 主線改 `l15-release-inflight` 文字欄 | BLOCKED-OWNER（見合併表） |
| `test_l1_judge.py::test_l1_lock_cargo_sets_exclude_ignored_tests` | 主線 OD-24 測試禁止 cargo 凍結集合含 `#[ignore]` 列；P5 `s21_core` 與 P6 `s22_gate:console_conformance_pty`（計畫 07 Task 13 Step 4 以 `--include-ignored cap_posix_pty__` 執行的 7 列）皆命中 | BLOCKED-OWNER：OD-24 與 07 Task 13 的 cap 集合設計衝突；移除須擁有者以 `--allow-removal` 逐名裁決（或裁定 `--include-ignored` selector 的集合豁免），P6 不自行移除 |
| `test_bb_l15_holder_observer.py::test_observer_does_not_ignore_zombie_leader_with_live_thread` | 主線新測試（4ad9b39）預期 PermissionError，root 下不會發生 | 環境（root），與 P6 無關；非 root 環境應重跑確認 |

## 第四輪 Slice 2.2（2026-10-29 cloud session）

### 合併（320329a 主線 8ef93f4、73897df P5 cca5ba8）

| 項目 | 處理 |
|---|---|
| DF-20 編號 | 主線已依 OD-37 登錄 DF-20（scope both、case `df20-stale-interactive-id-no-replay`）→ 依「以主線為準」改用主線條目；P6 先前自配的 DF-20 條目撤回。已凍結的 P6 案例 `df20-release-reattach-stale-interactive-id`（lock add-only 不得刪）保留，`behavior_changes: [DF-20]` 仍指同一裁決 |
| P5 開埠移出 actor（`OpenCtx`／`JobQueue`） | P6 哨兵 PTY 與 TCP listener 改為 `OpenCtx` 方法、隨 `BridgeOpened` 同一事件交給 core；listener epoch 於 `AdoptBridge` 接手，未接手即 `BeginDrain` 亦停止 |
| `regen_lock.py` 兩種延後核准 | P5 `--allow-defer case:from:to:OD` 與 P6 `--authorize-deferral case:OD` 並存，兩支測試皆保留 |
| OD-34 `l15-delayed-reply`（P5 自 2.1 延至 2.3） | `cases.yaml` revision 2026-10-29.1；slice_2.2 分母 94→93；`breaker/slice_2.2.json` 還原凍結本體（c2924a5），兩筆 OD-34 延後以 P5 的 `freeze_slice_cases.py --amend` 寫入 `slice_2.2.amendments.json`（凍結本體不再改寫）；`slice22_evidence_map.yaml` 移除該鍵 |
| `breaker/slice_2.1.json` | 還原 P5 凍結本體位元組（P5 修訂鏈 `base_freeze_sha256` 以之為錨）；#183 當時寫入的 `case_fingerprints` 原值移到 `slice_2.1.case_fingerprints.json`；指紋檢查改對有效分母逐案比對 |
| proc_scan 目標 tty 不可 stat（上輪 BLOCKED-OWNER） | P5 已改為 `tty_holders_fail_closed_with`（PermissionDenied → Permission、其他 → Unknown），與主線 s1b_cargo 語意一致；本分支取 P5 版本，該矛盾解除 |

### Codex sol `d6e3b3c_e9b5c61-sol.md`

| # | 等級 | 位置 | 處理 |
|---|---|---|---|
| P6-e9b5c61-1 | High | `session_core.rs` 共用 `self_test_suspended` | fixed 7370b8f（紅燈 0123679）：旗標存入各自 `PendingOp::SelfTest.suspended`（帶 drain 世代），Recover 等其他 op 完成不再 resume；daemon 級 `rpc_console::overlapping_self_tests_replay_human_input_end_to_end`（fcbb564；還原修正前原始碼時 FAIL） |
| P6-e9b5c61-2 | High | `session_core.rs` 取消停放命令 | fixed 7370b8f（紅燈 0123679）：`settle_parked_terminal` 以 `registry.complete` 清在途指標、保留 canceled、零 TX；`start_next` 與 `abort_parked_command` 兩條退出路徑皆結算 |
| P6-e9b5c61-3 | Medium | `cases.yaml` `l15-release-inflight` 指紋 | OWNER-BATCH：該欄位變更出自主線擁有者 commit dbbc1f7（`fix(bridge): 收緊 Windows 序列埠期限與 peer gate`），P5 亦已帶入；sol 的修法 1（本分支還原文字）會與主線／P5 分歧並於下次合併再衝突，修法 2 須逐名核准。合併帶入的另一個指紋差異（`l15-delayed-reply`）已依 OD-34 修訂鏈排除 |
| P6-e9b5c61-4 | Medium | `test_l1_judge.py` OD-24 與 `s22_gate:console_conformance_pty` | OWNER-BATCH：OD-24 (2) 字面禁止 cargo 凍結集合含 `#[ignore]` 列；該集合的 selector 以 `--ignored cap_posix_pty__` 實跑，與 OD-24 的立論（官方 selector 不執行 ignored）不同。移列須 `--allow-removal` 逐名核准；P5 的 `s21_core` 同類列屬 P5 |

### 接線＋端到端驗收（`p6-wiring-e2e.md`）

- R3-8（worker health 未注入）：fixed 30475b8（紅燈 d2a6e67），ATTACHED 救援改取 manager 注入的現行 epoch IoWorker 快照。
- M1～M11 no-op 變異皆 RED；新增 daemon 級測試 4 支（fcbb564）。

### 第四輪全套 pytest 與環境

- 本輪改以一般使用者 `ubuntu`（非 root）執行全套與 cargo 測試；起點 e9b5c61 於獨立 clone 跑基準。
- 上輪「`test_bb_l15_holder_observer.py::test_observer_does_not_ignore_zombie_leader_with_live_thread` 在 root 下不成立」：非 root 重跑 17/17 passed，確認為 root 環境因素。
- `test_l1_judge.py::test_l1_lock_nodeids_subset_of_collect_only` 的 `COM9` 參數 id：本機 pytest 9.2.0 對長字串參數改產 `msg2` id；以 CI 釘定的 pytest 9.0.3 重跑即通過（環境因素；起點與本輪同環境比較）。

### 第四輪 Slice 2.2 出口 gate（`s22_gate_check.py` 非 dry-run，`target/s22-gate/261029-r4b`，HEAD 24b8012）

result = FAIL。cargo_workspace PASS（2123 passed）、console_conformance_pty PASS（需 `cargo_l1_results.py --caps posix_pty`）、
py_parity PASS、parity_py FAIL（指紋 2 案，OWNER-BATCH）、blackbox_rust FAIL（slice21 `test_stale_context_not_replayed`、
`test_two_daemons_same_tty`，P5）、win_test INSUFFICIENT（LOCAL-WINDOWS-TODO）。逐案 93：PASS 23／BLOCKED-OWNER 62／FAIL 8
（Slice 2.1 繼承 L1.5 情境，非 root 下 Rust holder 掃描 fail-closed，root 下 PASS，交 P5）。parity_rows：PASS 17／FAIL 1／INSUFFICIENT 5。
首次實跑另抓到 `console_methods_no_longer_deferred` 依賴 cwd（24b8012、a8c57f0 修正）。

### 第四輪全套 pytest（非 root、同一環境）

起點 e9b5c61：25 failed／5528 passed／226 skipped；終點 24b8012：20 failed＋1 error／5561 passed／223 skipped。超出基準 8 項：
`tests/test_rust_cfg_boundary.py::test_repo_clean` 為 24b8012 引入的真實回歸（執行期讀檔被判 test-data-path），a8c57f0 修正
（33 passed）；其餘 7 項（`test_mgmt_stubs::test_live_daemon_fixture_serves_attached_console_list`、slice21
`test_queue_full_backpressure`、`test_reject_paths_zero_tx[CMD_TOO_LONG]`、`test_cli_mgmt_l15::test_restart_no_stale_console_replay`、
`test_functest_bridge_smoke::test_bridge_smoke_sm01`、`test_l15_smoke::test_{reject_zero_tx,release_inflight}_runs_on_python`）皆為
Python daemon 側 `ATTACH_FAILED:DeviceHeldError`：起點基準執行時洩漏的 throwaway Python daemon（pid 19168，持有 /dev/pts/1、/dev/pts/2）
於整段終點全套期間存活。清除後同範圍連跑兩次：第一次 7/7 passed、第二次僅 `test_queue_full_backpressure` 失敗（失敗集合不同）。
## 第四輪續（2026-10-29 第二個 cloud session）

### 合併（e14b4c8 主線 62ffec7、7faa3c6 P5 3084388）

| 項目 | 處理 |
|---|---|
| 主線 62ffec7 | 只登錄 OD-45～OD-47（`owner-decisions-2026-10-15.md` 三列），無衝突 |
| P5 3084388（OD-45 event_latency L1 結構 gate、OD-46／OD-47 WAL unlink 黑箱 A/B、折行回顯修正） | 無衝突；`l1_required.lock.json` 只增 5 列（`command_exec` 3、`drain_release_e2e` 1、`dispatch` 1） |
| OD-45～OD-47 | 皆為 P5 承接（event_latency、`s21-wal-unlink-under-traffic`、`s21-exclusive-conflict*`），P6 無對應工作；合併後 P6 檔案不受影響 |

### sol 報告

| 報告 | # | 處理 |
|---|---|---|
| `d6e3b3c_e9b5c61-sol.md`／`e9b5c61_73897df-sol.md` | P6-e9b5c61-1、-2（High） | 已於 7370b8f 修正（紅燈 0123679）；`e9b5c61_73897df` 審到 73897df（修正之前）故標「未修」，`73897df_24b8012-sol.md` 已核對為「已修 7370b8f」 |
| 同上 | P6-e9b5c61-3、-4（Medium） | 維持 OWNER-BATCH（見下），累計未修 2 輪 |
| `73897df_24b8012-sol.md` | B 段 | NO FINDINGS |

### 本輪自行發現並修正

| 項目 | 紅燈 | 修正 | 說明 |
|---|---|---|---|
| `check_cfg_boundary.py` test-data-path 違規（24b8012 自己引入） | `check_cfg_boundary.py` exit 1 | 4e3cd8a（與並行 session 的 a8c57f0 重複；合併時取 a8c57f0 的 `include_str!`） | `rpc_console.rs` 直接用 `CARGO_MANIFEST_DIR` |
| console_attach 逾時後留下幽靈 human lease（07 Task 10 `attach_request_timeout_closes_pty`） | 9979795 | 52d49d2 | 0 逾時取得 handler 逾時出口；遲到的 `ConsoleAttach` 在 executor 找不到 PTY 時原送 `ConsoleDisconnected`（斷線語意保留 lease），之後 attach 的 console 拿不到 lease 與 raw ownership。改送 control lane `ConsoleDetach`（ERR-7.5-b）。Python attach 同步、無此出口，Python 行為不受影響。計畫原文「actor 卡住」所需的 fault hook 須改 P5 `actor.rs`，列 SPEC-GAP |
| s22 出口 `l15-wal-interference` 恆 FAIL | 實跑 r4 | 76c78b2 | OD-40 已裁決移出 L1.5 判定；對照表改指 `l15_ab_excluded` 所列 Rust 側 WAL 黑箱 3 案與 `wal_writer` L1 3 案（add-only 入鎖） |
| s22_gate_check 跨集合重疊 nodeid 誤判證據不足（parity #40／#109） | 237b1c2 | cd80448 | 「恰一列 passed」改逐份報告（集合×平台）；單一報告內重複照舊不計 |

### 07 Task 10 兩個 deferred 案例

- `attach_request_timeout_closes_pty`：完成（上表）。
- `attach_interactive_owner_false_during_replay_hold`：**SPEC-GAP**。計畫前置狀態「回放 hold 存在、前景已清、且 attach 會開 lease」在 Python 與 Rust 都不會發生：`console_attach` 只在無 lease 時開 lease；鍵入而產生回放的 human console 本身必已由 attach 開了 lease（PTY），soft preempt／lazy expiry 後 PostClose 也會還原它的 lease；TCP 後端的 attach 不開 lease、哨兵 primary 的輸入被丟棄。實測 ATTACHED 救援 lazy expiry 變體：c2 attach 時 c1 的 lease 已還原，回應無 `interactive_session_id`。D-22(2) 的「hold 期間 attach 回 owner=false」在程式上為 `!fg_slot_busy()`，但無可達的前置狀態可驗；請計畫擁有者改寫案例或撤列。
