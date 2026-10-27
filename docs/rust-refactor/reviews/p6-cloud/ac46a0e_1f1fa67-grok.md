我會以非實作者身分審查 `ac46a0e..1f1fa67`，先對齊計畫、規格與 commit 範圍，再逐筆對照 Python 基線與測試斷言。不改任何檔案。範圍是四個 commit。我先讀計畫、parity 修訂與 Python 對照面，再逐筆核對實作與測試是否真的 1:1。小 commit 已對上。接著對 Python 的 reboot 失效點、ownership 與 human TX 結算，核對實作有沒有把斷言做弱或把時機放錯。審查結論需要對到確切行號。我先核對四個 commit 的關鍵路徑，再依 commit 寫發現。範圍與工作樹已確認。接著核對四個發現的行號與 Python 對照。範圍 `ac46a0e..1f1fa67`（HEAD `1f1fa67`，工作樹乾淨）共四個 commit。未改檔、未跑測試；紅燈與否依控制流推得。Lock 為 add-only（70 條新增 `cargo:` nodeid，diff 裡唯一的 `-` 行是標頭）。`crates/sw-core`、`sw-daemon`、`sw-cli` 的 diff 沒有 `cfg(unix)`、`cfg(windows)` 或 `target_os`。沒有 commit 宣稱 Windows 原生、com0com 或實體 UART 已完成；無 PTY 的 `ConsoleAttach` 仍回 `SESSION_NOT_READY`，TCP 端點留在 Task 10。

### 8fdf62f test(p6): Task 6 ownership、human TX lane 與 Fence human 規則的失敗測試

1. [Medium] `crates/sw-core/tests/human_tx_lane.rs:226` — `human_tx_settles_ledger_each_chunk` 只在 `CoreHarness` 裡數 1000 個 `Effect::SettleTx`，沒有把這些 effect 套進 worker 的 TX ledger。計畫 Step 3 第 9 點要求：同一 epoch、c1 送 1000 個 1-byte chunk 後，`SettleTx` 的 `op_id` 與各 `HumanTx` permit 一一對應，而且最後一個 `HumanTxCompleted` 之後 2 秒內 `tx().ledger_gauge().live_ops == 0`（R1-F8，`TX_LEDGER_SATURATED`）。現在 core 只要發出 `SettleTx` 就綠，executor 從未 `settle` 也綠。樹裡沒有 `ServiceRig`；`ActorRig` 的 `RigExec` 已會處理 `Effect::SettleTx` 並提供 `tx_ledger_gauge()`。修法：用 `ActorRig` 跑這 1000 筆，斷言 `op_id` 對應，並在最後一個 `HumanTxCompleted` 之後 2 秒內 `tx_ledger_gauge().live_ops == 0`。

2. [Medium] `crates/sw-core/tests/human_tx_lane.rs:126` — `backlog_overflow_clears_client`（自 `:112`）的斷言是結算在途的 `a` 之後，下一批 `HumanTx` 的 payload 總長 `< HUMAN_TX_BACKLOG_MAX_BYTES`（262144）。計畫表要求溢位時清空該 client 的 backlog（含觸發的那一筆）並寫 WARNING，其他 client 不受影響。`push_backlog` 的單位是 8192-byte chunk，下一批只要還有一筆 8192 就仍小於上限，所以「有 WARNING 但 backlog 沒清空」會綠。c2 只送了 `q\r`（line，不進 lane），清掉別人的 backlog 也看不出來。修法：結算 `a` 後斷言 c1 的 `human_txs` 為空且 c1 backlog 長度為 0；事先讓 c2 留一筆 backlog 或 deferred byte，斷言它還在。

其餘計畫表上的 Task 6 測試名稱都在 lock 與測試函式裡。抽查的 fence HumanRaw／HumanReplay 順序、單筆 in-flight、partial／unknown 不重送、replay hold 與 ownership 斷言，和計畫表一致。這支 commit 以 `todo!("P6 Task 6")` 讓套件在實作前失敗，這一點成立。

### 2f4f76a feat(rust-console): 實作 raw／line ownership、human TX lane 與寫入前 input_epoch 重驗

1. [High] `crates/sw-core/src/op_runner.rs:408` — line editor 已把命令以 `SubmitOrigin::HumanConsole` 送進 arbiter（`session_core/console.rs:639`），`execute_time_reject`（`session_core.rs:3339`）只在閘門把這個 origin 當 human。`run_command` 不看 origin：reboot 進 `run_reboot`（`:408`），其餘逾時進 `recover_after_failure`（`:474`）。沒有 lease 時閘門放行。TCP `ConsoleConnected` 會把第一個 client 做成 raw owner，但不開 lease（`console.rs:692`）；第二個 client 走 `InputRoute::Line`（`ownership.rs:203`）。

   輸入一：READY、非 quiet。c1 經 `ConsoleConnected` 成為 raw owner（無 lease），c2 送 `ls\r`，30 秒內沒有 prompt。Python `session_manager.py:6582` 的 human 分支回 `recovery_action=PROMOTE_HUMAN_INTERACTIVE`、`partial=true`，並開或刷新 human lease，UART 沒有 `0x03`／`0x04`。Rust 在 `op_runner.rs:570` 送出這兩個 byte，`finish_command`（`session_core.rs:3993`）把 session 轉成 `ATTACHED`、`PROMPT_TIMEOUT`。

   輸入二：同一拓撲，c2 送 `reboot\r`，2 秒內沒有 prompt。Python `session_manager.py:6124`：state `ATTACHED`、`last_error=REBOOTING`、human lease、`recovery_action=PROMOTE_HUMAN_INTERACTIVE`，不啟動 reboot-recovery。Rust `run_reboot` 回 `RebootExpected`（`op_runner.rs:550`）；`finish_command`（`session_core.rs:4000`）設 `RECOVERING`、`pending_auto_login`、`EXPECT_REBOOT`；quiet 到期後 `reboot_recovery_tick`（`session_core.rs:4352`）清掉 `quiet_active` 並發 `StartLogin(EnsureReady)`，登入 probe 會寫上 UART。

   計畫把對應測試 `human_timeout_promoted_only_for_internal_origin`、`human_reboot_opens_lease` 列在 Task 7（`07-slice22-console.md:1826`）。這條路徑在本 commit 已經會寫出 Python 不會寫的 bytes。修法：`origin` 為 `SubmitOrigin::HumanConsole` 時，`run_command` 不呼叫 CTRL-C／CTRL-D，`run_reboot` 不回 agent 的 `RebootExpected`。Actor 依上述兩個 Python 位置開或刷新 human lease、state 維持 `ATTACHED`、`recovery_action=PROMOTE_HUMAN_INTERACTIVE`；reboot 另設 `last_error=REBOOTING`。已有 lease 時維持現有的 `SESSION_INTERACTIVE_BUSY`。補上這兩個輸入的測試。`SubmitOrigin::Rpc` 即使 source 以 `human:` 開頭仍走 agent 分支。

本 commit 把 `InvalidateReason::RebootCommand` 只放在 `reboot_tx_settled`（當時 `session_core.rs:2561`）。送出前失效已由 `1f1fa67` 補上；quiet arm 少掉的第二次失效記在該 commit。

### 8abd423 test(p6): reboot 命令應於送出前使 human 輸入失效

1. [Medium] `crates/sw-core/tests/session_core_console.rs:435` — `reboot_command_invalidates_before_send` 只斷言 `start_foreground_command("reboot")` 返回時 `input_epoch == e0+1`，以及以舊 epoch 送入的 `k` 沒有 `HumanTx`。`start_foreground_command`（`tests/support/mod.rs:407`）在 `StartOp` 就返回，不會送 `UartTxCompleted`。因此它在 `2f4f76a` 上會紅（當時失效在 TX 結算），也鎖得住「出列當下還沒遞增」。它鎖不住 `1f1fa67` 刪掉 quiet arm 那次失效：送出之後、arm 之前以新 epoch 擷取的按鍵仍會在 resume 時被回放，這支測試保持綠色。修法：`StartOp` 之後以當時的 epoch 送入 `k`，再依序餵 `UartTxCompleted { Completed(n≥1) }` 與 `OpFinished`，斷言 `input_epoch == e0+2`、deferred 為空、沒有 payload `k` 的 `HumanReplay`。另補 `NotSubmitted` 且 `confirmed_bytes==0`：不 arm quiet、不做第二次遞增。

### 1f1fa67 fix(rust-console): reboot 的 BC-02 失效點移到命令出列送出前

1. [High] `crates/sw-core/src/session_core.rs:2558` — 出列後、`StartOp` 前的 `invalidate_input(RebootCommand)`（`:3463`）對應 Python `session_manager.py:6062` 的 `invalidate_deferred("reboot")`，這一半是對的。`reboot_tx_settled` 在 `Completed(n≥1)` 或 `Unknown` 時只做 `quiet.arm` 與 `apply_quiet_armed`，沒有再遞增 `input_epoch`，也沒有清空 deferred。Python `arm_boot_quiet`（`session_manager.py:583`）每次都會 `invalidate_deferred("boot_quiet")`；`_handle_reboot_command` 在寫入成功、部分寫入或結果不明之後才呼叫它（`:6091`）。parity-amendments #55 只禁止在出列時 arm quiet，沒有拿掉這次失效。`Fence::admit` 的 `HumanReplay`（`tx_writer.rs:205`）不看 `quiet_active`。

   輸入：c1 為 raw owner，epoch 為 N。Agent 命令 `reboot` 出列。`command_fg_begin` 暫停 human 並關閉 admission，`RebootCommand` 把 epoch 變成 N+1 並清掉舊 deferred，接著 `StartOp`。reboot 尚在途時，human 按下 `k`（admission 關閉，進 deferred，擷取 epoch 為 N+1）。TX 為 `Completed`。`OpFinished` 走到 `command_fg_finish`（`session_core.rs:3849`），把 `k` 以 `HumanReplay` 寫上 UART。quiet 窗內的 UART 是 `reboot\n` 之後還有 `k`。Python 預期：arm 時再失效一次，epoch 變成 N+2，`k` 被清掉，resume 不送任何 human byte；UART 只有 `reboot\n`。`confirmed_bytes==0` 且非 `Unknown` 的路徑維持現況：不 arm、不做第二次失效。

   修法：在 `reboot_tx_settled` 確定要 arm 的路徑上，`quiet.arm(now)` 之後呼叫 `invalidate_input(InvalidateReason::QuietRearm, …)`，把 effects 放進回傳值。不要加進 `apply_quiet_armed`。Banner 路徑已在 `:2499` 做過 `BootBanner`，再加一次會讓一次 `arm_boot_quiet` 變成兩次遞增。回歸斷言用上一節那支測試的第二段。

### High／Medium 摘要

| 嚴重度 | Commit | 位置 | 問題 |
|---|---|---|---|
| High | `1f1fa67` | `session_core.rs:2558` | reboot 寫入成功後的 quiet arm 沒有做 Python `arm_boot_quiet` 的第二次 `input_epoch` 失效；在途期間擷取的 human byte 會在 quiet 窗內被回放 |
| High | `2f4f76a` | `op_runner.rs:408` | `HumanConsole` 的 line 命令已進 agent runner；逾時會送 CTRL-C／CTRL-D，reboot 會進 `RECOVERING` 並在 quiet 結束後送登入 probe |
| Medium | `8abd423` | `session_core_console.rs:435` | 回歸只鎖「`StartOp` 時 epoch 已 +1」，鎖不住 arm 時的第二次失效 |
| Medium | `8fdf62f` | `human_tx_lane.rs:226` | 1000-chunk 測試沒有斷言 worker `live_ops == 0` |
| Medium | `8fdf62f` | `human_tx_lane.rs:126` | backlog 溢位只斷言下一批小於 262144 bytes，清空與其他 client 隔離都沒鎖住 |
