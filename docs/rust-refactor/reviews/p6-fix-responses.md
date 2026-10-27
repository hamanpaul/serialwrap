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
| R3-2 | High | 1fb9612 | tcp backend 的 `console_attach` 回 SESSION_NOT_READY | deferred：TCP console 端點回應屬 Task 10 後段，Linux 上 SC-28 tcp 後端為 rust_only、Windows 為原生路徑（LOCAL-WINDOWS-TODO）；需 hub StartListener 與端點經 RPC 回填，下一輪實作 |
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
