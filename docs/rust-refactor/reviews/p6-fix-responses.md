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
| 9 | Low | 8fdf62f | `release_phase1_closes_all_consoles` 分不出計數方式 | deferred：與 #12 一併於 Task 10（哨兵登記）處理 |
| 10 | Low | 8fdf62f | `queued_command_waits_for_replay_settle` 未確認出列者為 B | fixed 4460aa0（測試強化） |
| 11 | Low | 2f4f76a | `pty: None` 回 `SESSION_NOT_READY` | deferred：Task 10（TCP console 端點回應）；Windows 原生列 LOCAL-WINDOWS-TODO |
| 12 | Low | 2f4f76a | `closed_consoles = max(...)` | deferred：Task 10 把哨兵以 `internal:true` 登記進 consoles 後改取 `consoles.len()` |
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
