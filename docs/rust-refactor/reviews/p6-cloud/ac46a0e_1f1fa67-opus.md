# 審查結果：feature/rust-refactor-p6 `ac46a0e..1f1fa67`（07 Task 6）

審查 worktree 沒有任何改動（`git status` 為空）。查核方式：

- 5 支 Task 6 test binary 實跑全綠（2／11／13／23／21 passed）。
- `check_cfg_boundary.py` exit 0。`tests/test_s22_l1_lock_rust.py` 4 passed。
- lock 只做新增：+70／−0。新增程式碼沒有任何 platform cfg。
- 把 HEAD 匯出到 `/tmp`，寫了 5 個 probe 測試實跑，**4 個 FAIL，證實下面 3 個 High 和 1 個 Medium**。`/tmp` 副本已刪除。

### 8fdf62f test(p6): Task 6 ownership、human TX lane 與 Fence human 規則的失敗測試

1. **[Medium] `crates/sw-core/tests/human_tx_lane.rs:226`** — `human_tx_settles_ledger_each_chunk` 的斷言比計畫寬。
   - **問題**：計畫第 9 點要求兩件事：SettleTx 的 op_id 和各 HumanTx permit「一一對應」，以及 TxWriter 的 `ledger_gauge().live_ops` 在最後一次結算後 2 秒內歸 0。現在只檢查 `ops.contains(op_id)` 加上總數 1000。若某個 op 被結算兩次、另一個漏結算，這個測試照樣通過。帳本歸零的部分整段沒寫，也沒有在任何地方註明。
   - **修法**：
     - 改成比對 SettleTx 的 op_id 序列與 HumanTx 的 op_id 序列完全相等（`assert_eq!(settled_ops, ops)`）。
     - 另加一個 ActorRig 版本：送 1000 個按鍵後，2 秒內輪詢 `rig.tx_ledger_gauge().live_ops == 0`（`actor_liveness.rs` 已示範這個存取子）。
     - 若堅持要等 ServiceRig，就在計畫裡標成 BLOCKED-UPSTREAM，不要默默刪掉。
2. **[Low] `crates/sw-core/tests/session_core_console.rs:247`** — `release_phase1_closes_all_consoles` 分辨不出 `closed_consoles` 是怎麼算的。
   - **問題**：harness 的 `console_count` 是 0，只掛 1 個 console，結果「取 consoles 數」「取 max」「取總和」都得到 1。
   - **修法**：先 `feed(ConsoleCountChanged{count:1})` 模擬 POSIX 哨兵，再掛 c1，斷言結果等於 Python 的 `len(list_consoles())`，也就是 2。
3. **[Low] `crates/sw-core/tests/session_core_console.rs`（`queued_command_waits_for_replay_settle`）** — 只斷言 `start_ops(&fx).len()==1`，沒有確認出列的就是 B。
   - **修法**：把該 StartOp 取出來，斷言是 `OpSpec::Command(q)` 且 `q.cmd_id == b`。

### 2f4f76a feat(rust-console): 實作 raw／line ownership、human TX lane 與寫入前 input_epoch 重驗

1. **[High] `crates/sw-core/src/session_core/console.rs:467-472`（另見 `replay_point` `:201-209`）** — 延後的回放會在新的前景命令執行中途送出。
   - **問題**：被撤銷的回放還在途時，若有新回放點到達，程式記 `replay_after_revoked`。之後在途那筆結算，就無條件執行 `replay_point`，建新 hold 並設 `replay_auth`。但 revoked hold 不算前景忙，這段期間 `start_next` 可能已經讓下一個命令 C 出列。
   - **後果**：HumanReplay 在 Fence 不檢查 admission 和 owner_gen，於是舊按鍵被寫進 C 的執行中。這違反 D-22 與 §11.3。
   - **實測**：
     - 序列：c1 lease → `fg_begin(true)` → 輸入 `x` → `fg_finish`（`x` 在途）→ `fg_begin(true)`（撤銷）→ 輸入 `n` → `fg_finish` → `start_foreground_command("echo c")` → `complete_human_tx(NotSubmitted, ReplayRevoked)`。
     - 預期：這批 effects 沒有 HumanTx。
     - 實際：`txs=[(HumanReplay,"human:c1",b"xn")]`，`replay_auth=Some(2)`，`tx_order` 為 `[Replay x, Command "echo c\n", Replay "xn"]`。
   - **修法**：
     - 執行 `replay_after_revoked` 那段回放前，要求 `self.foreground.is_none() && !ownership.admission_blocked && ownership.suspend_depth == 0`。不符合時保留旗標，交給該前景的 `fg_finish`／resume 回放點處理（deferred 已經 `restore_front`，不會遺失）。
     - 用上面序列加一個回歸測試（`human_tx_lane.rs`）。
2. **[High] `crates/sw-core/src/session_core/console.rs:708`、`crates/sw-core/src/human_console/human_tx.rs:100-105, 154-166`** — console 斷線或 backlog 溢位會讓 replay hold 永遠釋放不了，session 佇列卡死。
   - **問題**：`lane.remove_client()`（斷線時呼叫，溢位時由 `push_backlog` 內部呼叫）會把 backlog 裡 `BacklogKind::Replay{hold}` 的項目一併刪掉，卻沒有呼叫 `hold_settled`。結果 `remaining` 永遠到不了 0，`foreground_busy()` 一直是 true。佇列不再出列，`interactive_open`／self-test／recover 都回 `SESSION_BUSY`，要等到 drain 才會解開。
   - **實測**：
     - 序列：TCP c1、c2 → `fg_begin(false)` → c1 輸入 `x`、`seed_deferred("c2","y")` → `fg_finish`（remaining=2）→ `disconnect("c2")` → `complete_human_tx(Completed(1))`。
     - 實際：`hold=Some{remaining:1}`、`busy=true`。
     - 真實觸發條件：raw 按鍵在途時前景結束，回放進了 backlog，接著使用者關掉 minicom。
   - **修法**：
     - 讓 `remove_client` 回傳被刪除的項目，呼叫端統計其中屬於現行 hold 的 Replay 數，呼叫 `hold_settled(n)`。
     - 若 hold 因此釋放且 `!foreground_busy()`，就 `start_next(now)`。
     - 溢位路徑比照處理。
     - 加上述回歸測試。
3. **[Medium] `crates/sw-core/src/session_core.rs:3416` 對比 `:3437`** — interactive 模式先開 lease、後關 admission，順序與 Python 相反。
   - **問題**：Python 先 `_begin_foreground_operation_locked`（關 admission），再在 `_execute_command_inner` 裡 `_open_interactive_locked` → `set_interactive_owner`。admission 已關時，human owner 只會記成 pending。Rust 是先 `open_lease`（`human_set_owner`），之後才 `command_fg_begin`。空命令的分支更是完全沒有前景開關。
   - **實測**：
     - 序列：TCP c1（owner）、TCP c2 → `input("c2", b"vi\r")`。
     - Python：owner 仍是 `human:c1`，pending 為 `human:c2`；之後提升 pending 時，因為 owner 不是 None 而不提升。
     - Rust：`owner=Some("human:c2")`。
     - 後果：c1 的按鍵改走 line editor，被當成命令送出。
   - **修法**：
     - 把 `command_fg_begin(&origin, true)` 移到 `open_lease` 之前。
     - 空命令分支補一對 begin／finish，對照 Python 的 begin＋finish foreground，包括 finish 時的 admission flush。
     - 用上面序列加回歸測試。
4. **[Low] `crates/sw-core/src/session_core.rs:1527`** — `closed_consoles = max(human.consoles.len(), console_count)` 和 Python、和計畫都不一致。
   - **問題**：Python 是 `len(bridge.list_consoles())`，哨兵和 human console 都算。計畫 #11 寫取 `consoles.len()`。用 `max` 的話，若 hub 還沒更新計數，「哨兵 1 個加 PTY 1 個」只會回 1，Python 回 2。
   - **修法**：把哨兵以 `internal:true` 登記進 `HumanConsoleState.consoles`，改取 `consoles.len()`；或明確定義 `console_count` 涵蓋全部 console 並只用它。另加上方建議的測試。
5. **[Low] `crates/sw-core/src/session_core/console.rs:732-735`** — `pty: None`（Windows 的 TCP console）回 `SESSION_NOT_READY`。
   - **問題**：Python 這條路徑回 `{"ok":true,"client_id":null,"vtty":endpoint,…}`。原本的 `todo!("P6 Task 10")` 被換成一個看起來合法、實際錯誤的回應，等於把 Windows 原生路徑包裝成已完成。
   - **修法**：恢復 `todo!("P6 Task 10")`，或回明確的未實作錯誤，並列入 LOCAL-WINDOWS-TODO。
6. **[Low] `crates/sw-core/src/session_core/console.rs:813`** — drain Phase 1 的 `lane.reset()` 丟掉在途簿記，晚到的 `HumanTxCompleted` 不會產生 `SettleTx`。
   - **問題**：D-15(6) 只豁免 `report.epoch != conn_epoch` 的情況。Phase 1 期間 `conn_epoch` 還是舊值，這筆 op 會留在舊帳本。
   - **修法**：reset 時保留在途那筆的 `(seq, op_id, epoch)`，結算時若 epoch 相符照樣送 `SettleTx`。
7. **[Low] `docs/rust-refactor/plans/07-slice22-console.md:1647`** — 實作者自己在計畫裡加「實作紀錄」，讓偏離計畫的做法合法化。
   - **問題**：Step 7 與 D-02 要求刪除 `begin_stop` 的 `owner_gen += 1`，實作保留了。另外 executor 和 PumpFull 的一格重試槽完全沒實作，也只用這段文字帶過。這些都沒有經過擁有者或審查核准，證據無從追溯。
   - **修法**：把偏離項目移到 parity-amendments 或 BLOCKED-UPSTREAM 表，讓審查者簽核。executor 接線開一個可追蹤的待辦。

### 8abd423 test(p6): reboot 命令應於送出前使 human 輸入失效

NO FINDINGS。這個測試在 2f4f76a 上確實會紅：當時只在 `reboot_tx_settled` 失效，StartOp 時 epoch 沒變。

### 1f1fa67 fix(rust-console): reboot 的 BC-02 失效點移到命令出列送出前

1. **[High] `crates/sw-core/src/session_core.rs:2539-2559`** — 這次修正刪掉了 reboot 送出後 arm quiet 時的第二次失效，與 Python 不一致。
   - **問題**：Python 的 reboot 流程會失效兩次：
     - 送出前 `invalidate_deferred("reboot")`（`session_manager.py:6062`）。
     - 送出後 `session.arm_boot_quiet()`（`:6094/6097`），裡面一定會 `invalidate_deferred("boot_quiet")`（`:590-594`）。

     commit 訊息說「reboot TX 結算只負責 arm quiet，不再重複失效」，這個前提不成立。
   - **後果**：reboot 送出期間 human 打的鍵，在命令結束時會被回放到正在重開的 DUT，可能中斷 autoboot（#130 同一類問題）。
   - **實測**：
     - 序列：c1 PTY lease → `start_foreground_command("reboot")` → 輸入 `k`（因 suspend 進 deferred，epoch 已是 e+1）→ `UartTxCompleted{confirmed_bytes:7}`。
     - Python：deferred 被清空。
     - Rust：`deferred_len("c1")==1`，epoch 沒有遞增。
   - **修法**：
     - 保留出列時的 `RebootCommand` 失效。
     - 同時在 `reboot_tx_settled` 的 `quiet.arm` 前恢復 `invalidate_input(InvalidateReason::QuietRearm, …)`，也就是和 Python `arm_boot_quiet` 對應的失效點。
     - 用上面序列加回歸測試，並確認 Python 對照結果為 0。

### High／Medium 摘要

| 等級 | Commit | 位置 | 問題 | 修法 |
|---|---|---|---|---|
| High | 2f4f76a | `session_core/console.rs:467-472` | 延後的回放在新前景命令執行中途送出，舊按鍵混進命令 | 回放前要求前景空閒、admission 開、未 suspend，否則留給下一個回放點；加回歸測試 |
| High | 2f4f76a | `session_core/console.rs:708`；`human_tx.rs:100-105,154-166` | 斷線或溢位刪掉 backlog 回放卻沒結算 hold，佇列永久卡住 | `remove_client` 回傳刪除項，依數量 `hold_settled`，必要時 `start_next`；加回歸測試 |
| High | 1f1fa67 | `session_core.rs:2539-2559` | reboot 送出後 arm quiet 不再失效，DUT 重開時會收到回放按鍵 | 恢復 arm 點的 `invalidate_input`；加回歸測試 |
| Medium | 2f4f76a | `session_core.rs:3416` 對比 `:3437` | interactive 模式先設 owner 後關 admission，第二個 console 搶走 raw owner | `command_fg_begin` 移到 `open_lease` 之前；空命令分支補 begin／finish；加回歸測試 |
| Medium | 8fdf62f | `tests/human_tx_lane.rs:226` | 只比對 contains 加總數，沒有一一對應，帳本歸零部分沒寫 | 比對完整 op_id 序列；加 ActorRig 的 `live_ops==0` 斷言，或標 BLOCKED |
