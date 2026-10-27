我會以非實作者身份審查 `8a7986e..edfc741`，只讀計畫、規格、Python 基線與每個 commit，不改任何檔案。先把 commit 範圍與依據文件對齊。範圍內有三個 commit。我接著對照計畫、擁有者決策與 Python 基線，逐一核對差異。計畫 Task 19 與三個 commit 的自承差異已經對上。接下來核對 Python 基線、parity 修訂，以及測試斷言有沒有被放寬。計畫與 Python 的停機、PROBE 與 allowlist 路徑是主要風險。我改讀實作與基線的對應區段。Python 的 RPC 迴圈在第一筆回應寫入失敗時不會讀下一行。我接著核對 daemon 啟動、停機與測試斷言是否仍對得上這條行為。審查尚未收斂。我先把還沒讀完的測試、PROBE diff 與 Python 對照補齊，再依每個 commit 寫發現。計畫與入口分派還有幾處要對上 Python 與 diff，我接著核對這些證據。審查範圍 `8a7986e..edfc741` 共三個 commit，工作樹仍停在 `edfc741`，沒有改任何檔案。Lock 為 15 行新增、0 行刪除。這段 diff 新增的 `cfg` 只有 `test-fakes` 與 `test`，沒有 `unix`／`windows`／`target_os`。Windows 原生項目寫成 LOCAL-WINDOWS-TODO，沒有把 `s21_core.windows` 補成已完成。

### 39380326747693acbb0a1306328011e20e8a28d2 feat(sw-daemon): serialwrapd 啟動順序、有序關機與 exit code

已核對：allowlist 在建目錄與取鎖之前拒絕；空 profile 的 `no profiles loaded` 在鎖錯誤之前；`SIGTERM`／`SIGINT` 有序停機；SC-11 未收斂 exit 1 是 OD-21 已核准的 Rust-only 差異。Release 的 kill／pause 只掛在 release 持久化前後，對得上 Python `session_manager.py` 的 `_save_release_state`。`cargo build --release --target x86_64-unknown-linux-gnu` 未跑，commit 有寫明磁碟配額，沒有把該 binary 報成 PASS。

1. [High] `crates/sw-daemon/src/main.rs:14` — `implementation: rust` 的 release manifest 不會進入這次交付的 daemon。`manifest_absent` 只有指標與版本 manifest 都沒有時才是 true；只要 manifest 存在，`main` 就 `exit(launcher::run)`。`crates/sw-daemon/src/launcher.rs:65` 對任何非 `"python"` 的 implementation 回 `UnsupportedImplementation`，`launcher.rs:249` 寫明這個 Stage 1 拒絕要由 P5-T19 切換。計畫 Task 19 的 Files（`06-slice21-daemon-core.md:4388`）與 `main.rs` 規格（同檔 `:4579`）要求 `"rust"` 或無 manifest 走 `daemon_main::run`，`"python"` 才 exec。`launcher_rejects_rust_implementation_in_stage1`（`crates/sw-daemon/tests/launcher.rs:314`）仍斷言 exit 2。Lock 只凍結 nodeid，不凍結斷言本文，不能當作不改的理由。具體輸入：`release-manifest.json` 內 `components.daemon.implementation` 為 `"rust"`。現在的輸出是 stderr 一行 `serialwrapd: …`、exit 2、`signals.install()` 沒有執行。預期是進入 `daemon_main::run`。修法：manifest 存在且 implementation 為 `"rust"` 時，先 `signals.install()` 再呼叫 `daemon_main::run`；`"python"` 維持 launcher exec，且不要在 exec 前安裝會被繼承的訊號遮罩。`launcher_argv` 不再把 `"rust"` 當成 `UnsupportedImplementation`。改 `launcher_rejects_rust_implementation_in_stage1`：rust manifest 必須進到 daemon 本體，python manifest 仍 exec。測試改名才用 `regen_lock.py --l1-add-only` 追加 nodeid。

2. [Medium] `crates/sw-daemon/tests/daemon_process.rs:149` — root 下把「證據不足」跑成 cargo PASS。`endpoint_written_after_bind` 的 chmod 半段（`:149`）、`wal_dir_readonly_at_start_blocks_login_probe`（`:427`）、`wal_fault_after_ready_splits_agent_and_auto_tx`（`:460`）在 `is_root()` 時 `eprintln` 後 `return`。`unreadable_config_isolated_fail_closed` 的 `permission_denied` 列在 root 且沒有 `/usr/bin/setpriv` 時 `continue`（`:737`）。`#[test]` 正常返回就是 libtest 的 `ok`。`scripts/rust/cargo_l1_results.py:143` 只把 `ok` 記成 passed，不讀 stderr 的「非 PASS」。計畫寫明這三個 WAL／chmod 案例在 root 下是證據不足。修法：上述路徑改為 `panic!("證據不足: …")`，讓該測試在 root（或無 setpriv）時是 `FAILED`，不要 `return`／`continue`。非 root 維持現有斷言。

3. [Medium] `crates/sw-daemon/tests/daemon_process.rs:331` — `real_sigterm_to_started_daemon_exits_0_after_flush` 不再觀察 `FLUSHED_BY_SHUTDOWN`。計畫表（`06-slice21-daemon-core.md:4458`）要求 SIGTERM 之後 command 紀錄出現 `FLUSHED_BY_SHUTDOWN`，並以停機前最後一次 `command.get` 輪詢驗證。現況只在送訊號前斷言排隊命令仍是 `accepted`（`:331`），之後只斷言 exit 0、WAL 最後一行可解析、socket 消失、新 daemon 取得鎖（`:343`）。exit 0 只表示 drain 沒有回報 stalled。`sleep 20` 確實會讓假 DUT 晚 20 秒才回 prompt（`tests/contract_blackbox/bb_faketarget.py:370`），所以排隊本身不是空的。修法：從 SIGTERM 前到 socket 關閉，對那兩個 cmd_id 輪詢 `command.get`，最後一次觀察到的錯誤碼必須是 `FLUSHED_BY_SHUTDOWN`。保留 WAL 完整性、exit 0、socket 移除與新 daemon 取鎖。in-process 的 `sigterm_graceful_exit_0_and_flushes_queue` 不能取代這條真行程斷言。

### 7a8416864e246251708a6e1ad27c4fad4eb87136 fix(sw-core): 動態偵測的 PROBE TX 改經 TxWriter／Fence 送出

已核對：lock 只加 `detect_probe_tx_goes_through_fence` 一筆。沒有 platform `cfg`。凍結半邊會在改回 `parts.writer.write` 時失敗，因為 `freeze_probe_tx_for_test` 只在 `probe_send` 的私有 Fence 上生效（`crates/sw-core/src/session_manager.rs:1095`），直接寫 OS 會讓 `os_submits()==0` 不成立。送出的 payload 是 `b"\n"`（`:1144`），對得上 Python `detect_template` → `send_command("")`（`sw_core/login_fsm.py:342`、`sw_core/uart_io.py:2313`，空字串補 `\n`）。`post_login_cmd` 的候選排除不是這個 commit 新加的。

1. [Low] `crates/sw-core/tests/session_manager.rs:1555` — 未凍結半邊沒有鎖住「恰一次 OS 提交」與 payload。註解寫恰一次 OS 提交，斷言只有 PROBE TX WAL 筆數為 1 與 `open_attempts==1`。送兩次但只記一筆 WAL，或送出的不是 `b"\n"`，這半邊仍會過。修法：未凍結路徑加上 `os_submits()==1`，以及該次提交的位元組等於 `b"\n"`。

### edfc741bb9e8919e44b94f7a307492b2fb40ce09 test(sw-daemon): daemon_stop_during_force_recover_with_client_gone 改以 Python 實測語意斷言

1. [High] `crates/sw-daemon/tests/daemon_process.rs:518` — 計畫 Review Focus 5 的斷言被改成相反條件，而且替代斷言連 commit 自己宣稱的 Python 結果都沒鎖住。`06-slice21-daemon-core.md:42` 與 `:4464` 要求：同一連線先 `session.recover --force`（DUT 不回 prompt、handler 約 10 秒）再 `daemon.stop`，client 立刻斷線；handler 不被取消；`daemon.stop` 的關機要等 handler 結束再做 SC-11 drain；WAL 不得在 handler 持有 session 時關閉；以 trace 順序斷言 WAL close 在 handler 之後；行程在 15 秒內 exit 0。`3938032` 仍斷言 15 秒內 exit 0，只是把 trace 順序留成註解，該測試當時是紅的。本 commit 改成行程必須仍存活（`:524`），再用另一條新連線送 `daemon.stop` 才要求 exit 0（`:535`）。commit 訊息寫「待擁有者確認」，計畫正文沒有改。OD-14 的 1:1 重申與 OD-21 的 SC-11 都沒有核准拿掉這個計畫項。

   Python 對「第一筆回應寫入失敗」的確不會處理下一行。具體輸入：COM0 已是 `READY`，同一個 socket 寫入
   `{"id":3,"method":"session.recover","params":{"selector":"COM0","force":true}}\n`
   與 `daemon.stop` 那一行後立刻關閉。`sw_core/rpc_posix.py:68` 的 `write`／`drain` 沒有 `except`，失敗會在 `finally` 之後離開迴圈，緩衝區裡的下一行不會再被 `readline`。Rust `framing.rs:200` 在 `write_line` 失敗時回 `Close`，`serve_connection`（`:224`）就此返回。READY 路徑上 Python `force=true` 也不進 `_force_recover` 的 10 秒迴圈，而是 `_recover_after_failure`（`sw_core/session_manager.py:7621`），救援位元組是 `\x03` 再 `\x04`（`crates/sw-core/src/op_runner.rs:563`）。

   替代斷言比這份 Python 結果更鬆：狀態接受 `READY`、`ATTACHED` 或 `DETACHED`（`:518`）；WAL 只要求檔案變大且最後一行能 `json.loads`（`:529`），不要求 `\x03`／`\x04`。`target_mute()` 只改 `FakeTarget.prompt`，而 `_run_loop` 在進入時就把 `prompt_b` 快照下來（`tests/contract_blackbox/bb_faketarget.py:263`），已在跑的 READY 迴圈仍回 `root@fake:~# `。這個測試並沒有送出「DUT 靜默、handler 約 10 秒」的輸入。

   修法：把這個 nodeid 恢復成計畫斷言，維持紅燈，直到擁有者修改 `06-slice21-daemon-core.md` 的 Review Focus 5 與 Task 19 那一列；若選定的行為與 Python 不同，同時補 OD。若擁有者選定 Python 1:1，同一變更裡改計畫文字，並把斷言收緊為：handler 跑完；WAL 含 `system:recover` 的 `\x03` 與 `\x04`；最後一筆 WAL 是完整 JSON；daemon 仍存活且新連線 `health.status` 成功；管線中的 `daemon.stop` 沒有刪掉 socket；其後新連線的 `daemon.stop` 在 15 秒內 exit 0 並移除 socket。狀態必須是 Python READY 路徑的實際終態。要讓 handler 與斷線重疊，必須讓 PTY 真的不回 prompt，不能靠迴圈啟動後改 `prompt`。若擁有者選定計畫行為：`serve_connection` 在寫入失敗時仍要處理緩衝區裡已經完整的下一行；`daemon.stop` 在 recover handler 返回後才 `request_stop`；`wal.shutdown()` 不得發生在該 handler 仍持有 session 時；trace 順序與 15 秒內 exit 0 都要斷言。

### High／Medium 摘要

| 嚴重度 | Commit | 位置 | 問題 |
| --- | --- | --- | --- |
| High | `3938032` | `crates/sw-daemon/src/main.rs:14` | `implementation: rust` 的 release manifest 仍走 Stage 1 拒絕，不會執行 `daemon_main::run` |
| Medium | `3938032` | `crates/sw-daemon/tests/daemon_process.rs:149` | root／無 setpriv 時 `return` 或 `continue`，cargo 把證據不足記成 passed |
| Medium | `3938032` | `crates/sw-daemon/tests/daemon_process.rs:331` | 真行程 SIGTERM 測試不再觀察 command 紀錄上的 `FLUSHED_BY_SHUTDOWN` |
| High | `edfc741` | `crates/sw-daemon/tests/daemon_process.rs:518` | Review Focus 5 由「15 秒內 exit 0」改成「daemon 必須仍存活」，且替代斷言沒有鎖住救援 TX 與終態 |
