# e650e70..ac46a0e 審查報告

這個範圍有 12 個 commit：9 個 P5 commit 經 merge `f97a208` 帶入，另有 merge 本身和 `ac46a0e`。主要結論：

- **High 有 2 項**。一項是 `fb76460` 的兩支 L1 鎖定測試，結果取決於主機上有沒有 `/dev/pts/9`；另一項是 `0a187c1` 在登入中遇到 boot banner 時，session 會卡在 `ATTACHING`。兩項在 HEAD 都已實際重現。
- **merge 與 `ac46a0e`** 沒有 High／Medium。L1 鎖確認是兩邊聯集、只增不減，平台 cfg 也只在 `crates/sw-pal`。
- **Windows 原生測試沒有被假裝完成**：`0a187c1`、`8b8a40f`、`b73da42` 的 83 筆 Windows nodeid 都標了 LOCAL-WINDOWS-TODO。
- 本次審查沒有修改任何檔案。建置產物放在 `/tmp/p6rev*`。

**實跑結果（在 HEAD）**
- `cargo test --workspace --features test-fakes`：除下面 4 個 binary 外全部通過。
  - `session_manager` 0/27、`daemon_process` 0/21、`restart_after_commit` 失敗：全是 T15、T16、T19 的 `s21_todo!` 紅燈基準，不是新回歸。
  - `sw-pal --lib` 有 1 項失敗：`holder_scan_permission`，見 fb76460 第 1 項。
- 以下檢查都通過：`cargo check --workspace --target x86_64-pc-windows-gnu --tests`、`check_cfg_boundary.py`（rc=0）、`tools.contracts check`（ok）、`regen_lock --check`（`--base e650e70` 與 `--base b73da42` 都 OK）。
- L1 鎖的 merge 結果正好等於 P5 與 P6 兩邊的聯集：新增 363 筆，刪除 0 筆。
- P5 commit 已有審查（`P5-g/rev-*`、`p5-cloud-reviews/*`）。我沒有整套重審，而是逐項核對那些發現在 HEAD 是否仍存在；下面只列仍存在的項目和新發現。

### 2a89f60 test(sw-proto): 偽造 HOME 改用 /fakehome 以符合 R-21
NO FINDINGS

### 335665e feat(sw-core): IoWorker reader、RxWindow 與哨兵 fan-out
1. [Low] `crates/sw-core/tests/rx_reader.rs:475`、`crates/sw-core/src/mailbox.rs:436,452` — 問題：`OpFinished{after_rx_seq}` 不能先於對應 RX Data 被處理，這項依賴屏障沒有任何經過 `Mailbox` 的測試。把 mailbox 的 `OpFinished` 分支刪掉，全部測試仍會通過。修法：在 `tests/mailbox.rs` 加一個案例，送 Data(5) 與 `OpFinished{after_rx_seq:Some(5)}`，斷言 `recv_next` 先取出 Data(5)。

### f20bdd0 feat(sw-core): 實作 SessionCore、Mailbox 與 Actor
1. [Low] `crates/sw-core/tests/session_core.rs:934-986` — 問題：`wall_clock_jump_param` 的命令逾時部分只呼叫 `rx.wait_for_regex_from_with`，沒有走 OpRunner 的實際期限路徑。原本的 `outstanding_commands` 斷言也被刪掉了。修法：改由 ActorRig 跑真正的 `run_command`，跳動 wall clock 後斷言 9.999 s 時仍未結束、10 s 時產生 `OpFinished`。
2. [Low] `tests/acceptance/l1_required.lock.json` — 問題：這個 commit 的鎖檔變更夾帶了約 33 筆非 T11 的 nodeid（sw-pal、sw-daemon 的 Windows 補登），commit 訊息沒有說明，證據不好追。修法：在 PR 描述逐項註明這是補登 Windows lock，以及來源的收集紀錄。

### afe4853 feat(sw-core): Two-Phase Drain 核心、IoWorker 收尾與 Release／Attach
1. [Low] commit 訊息 — 問題：訊息寫「L1 Linux、Windows 各 3 項」，實際各新增 73 項，另有 `s21_gate:py_parity` 1 項。Windows 列只有 `win-runner --list-only` 的收集證據（`fix-P5-T12.log`），沒有原生 Windows 執行結果。修法：PR 描述改成各 73 項，並明寫 Windows 只有收集、尚未執行，不得計為 PASS。
2. [Low] `crates/sw-pal/src/serial/posix.rs:1054`、`crates/sw-pal/tests/serial_windows.rs:162-172` — 問題：夾帶與 T12 無關的純格式變更。修法：之後另以獨立 commit 處理，或在 PR 描述註明。

### 0fd8fa9 docs(rust-refactor): 06 計畫併入 parity-amendments P5 修訂段落
1. [Low] `docs/rust-refactor/plans/06-slice21-daemon-core.md:1002-1003` — 問題：測試表仍寫靠 `/dev/pts/9` 的 readlink 字串判定，以及「proc_root 不存在 → Unknown」，和同檔 `:1140` 的 #64 修訂矛盾。修法：改成「fd 指向實際存在、同一 inode 的暫存檔」，以及「目標存在、proc_root 不存在 → `{pids:[], holder_unknown:true, status:Permission}`」。

### fb76460 fix(sw-pal): holder 以 inode 身分判定並對齊 Python open／flock errno 映射
1. [High] `crates/sw-pal/src/serial/posix.rs:1557-1567`、`:1570-1586`（鎖檔 `l1_required.lock.json:9527-9528`）— 問題：兩支 L1 鎖定測試仍以主機上的 `/dev/pts/9` 為目標，結果取決於這個節點在不在：
   - 本機 euid 1000、`/dev/pts/9` 不存在時，`holder_scan_permission` 失敗（`posix.rs:1584`，`left: Unknown, right: Permission`）。我的全套測試與子審查各重現一次。
   - 該節點存在時，換成 `holder_scan_unreadable_proc_is_unknown` 失敗（得到 Permission）。
   - 這兩支不能當回歸測試。commit 說的「除兩項外全綠」無法重現。
   
   修法：兩支都改成以 tempdir 內實際存在的檔案為目標。缺 proc 時依 Python `probe_holders` 期望 `Permission`，並把測試名改成 `..._is_permission`。這屬於期望值變更，要在 PR 描述登記並送 §10.4 獨立審閱。
2. [Medium] `crates/sw-pal/src/proc_scan/posix.rs:146-180` — 問題：`serialwrapd_tty_holders` 仍用 `read_link` 加字串 `contains` 判斷是否同一 tty，和 #64 改成 `(st_dev, st_ino)`／`same_tty` 判定的方向不一致。Python 的 `TestDetectMultiOpenNamespace` 3 個案例也沒有移植，但 `tests/proc_holders_namespace.rs:6` 的註解說已由 proc_scan 承接。修法：改用 stat 加 `same_tty`，並移植這 3 個案例。
3. [Low] `crates/sw-pal/src/serial/posix.rs:327` 與 `:477-478` — 問題：註解說「鎖後重掃不再 finalize」，正式路徑卻仍呼叫 `finalize_holder_report`。修法：讓程式與註解一致，二擇一修正。

### 2d93181 fix(sw-core): WAL health 補齊 OD-19 三鍵為 14 鍵
1. [Medium] `crates/sw-core/src/wal.rs:1184-1209`、`:1412-1414`，以及 `crates/sw-core/tests/wal_writer.rs:1857-1870` — 問題：
   - `rotate_deferred`、`deferred_delete_pending` 寫死為 0，測試還把 0 鎖成期望值。
   - `rotate_retry` 沒有 Python 的 backoff／`next_at` 語意。
   - 這個差異沒有登記成 NC。
   
   修法：移植 `_schedule_rotate_retry_locked` 與 tick；否則登記 NC，並把這兩鍵的測試改標為證據不足，不能當成 parity 通過。
2. [Low] 本分支的 `sw_core/wal.py:756-771` 與 `contracts/rpc_schema.json` 仍是 11 鍵，Rust 輸出多出 3 鍵（ADR-07 基線不一致）。修法：併入 OD-19 的 Python 與契約變更，或在計畫中註明依賴的基線 commit。

### 0a187c1 feat(sw-core): 最小登入、帳密未解析終態與 boot quiet gate
1. [High] `crates/sw-core/src/session_core.rs:2441`（`apply_quiet_armed` 把 `fsm_gen` 加 1）與 `:2242` — 問題：attach 登入中收到 boot banner，`fsm_gen` 遞增後，這次登入的 `LoginFinished` 會被丟棄，`login_run` 也不清掉，session 永久停在 `ATTACHING`，之後 `SessionAttach` 還回 `ok:true`。
   - 輸入：SHELL_PROFILE 正在 attach 登入時，RX 收到 `U-Boot 2024.01\r\n`。
   - Python 預期：ATTACHED，`last_error` 為 `PROMPT_UNAVAILABLE`（`session_manager.py:5451-5455`）。
   - HEAD 實測：Ready、NotReady、Failed 三種結果都停在 `Attaching`。
   - 另外，`tests/session_core.rs:1213` 把「停在 ATTACHING」鎖成了斷言。
   
   修法：在 `apply_quiet_armed` 中，若 `login_run` 屬 Attach 或 Reprobe、狀態不是 READY、而且沒有宣告 `console_unlock_env`，就清掉這個 run，採用 bridge（`adopt_bridge`），設為 ATTACHED、`PROMPT_UNAVAILABLE`、detail 為 null，並回覆等待中的呼叫。之後送達的舊世代結果照舊忽略；不要採用 Opus 原建議的「接受舊世代結果」，那會違反 SC-30。`:1213` 的斷言要改，並登記到 parity-amendments。
2. [Medium] `crates/sw-core/src/login_fsm.rs:302-306` — 問題：凡是不完整的登入 TX 都被映射成 `PROMPT_UNAVAILABLE`。Python `_login_tx_refused` 只轉換 quiet 與 stale_login_gen 兩種拒絕。例如 WAL unhealthy 時，Python 是 DETACHED 加 `ATTACH_FAILED:WalUnavailableError`，Rust 卻是 ATTACHED 加 `PROMPT_UNAVAILABLE`。修法：依 `TxReport.reject` 分流，只有 Quiet 與 FsmMismatch 轉成 `PROMPT_UNAVAILABLE`。
3. [Medium] `crates/sw-core/src/session_core.rs:2172`（經 `:2381`）— 問題：再探路徑把 `pending_auto_login` 算進 `require_login`；Python `:4465` 只看 `login_regex` 與 `console_unlock_env`。修法：再探時以 `pending_auto_login=false` 呼叫 `choose_login_plan`。
4. [Low] `crates/sw-core/src/session_core.rs:2119`、`:2356` — 問題：quiet gate 沒有排除 `console_unlock_env`（Python `:1733` 有排除）。修法：條件改成 `quiet_active(now) && !declared(console_unlock_env)`。
5. [Low] `crates/sw-core/src/session_core.rs:2238-2240` — 問題：再探結束時若狀態不是 ATTACHED，直接 return 而不清掉 run，等待中的呼叫永遠收不到回覆。修法：清掉 run，並回 `STATE_CHANGED`。

### 8b8a40f feat(sw-core): 命令執行與救援、agent lease、self-test 與 capture
1. [Medium] `crates/sw-core/src/session_core.rs:4036-4038`（recover）、`:4433-4435`（self_test）— 問題：READY 且前景命令進行中時，兩者都回 `SESSION_BUSY`、零 TX。Python 不檢查前景：recover 照送 `0x03`／`0x04`，self-test 照送 nonce probe。
   - recover 這一項已登記為 parity #58（ALIGN-TO-PYTHON，排在 P8-T5），是已排程待修，不是核准差異。
   - self-test 這一項完全沒有登記。D-22(2) 只核准 `interactive_open`／`send` 回 `SESSION_BUSY`。
   
   修法：recover 改以 `SystemRecoverKey` 與前景並行送鍵；self-test 依 07 D-22 走 `human_suspend_begin`／`human_suspend_end`，不佔前景槽。各補一支會在現行程式失敗的測試。
2. [Medium] `crates/sw-core/src/op_runner.rs:582-586`，經 `session_core.rs:3929` — 問題：救援鍵遇到 Frozen、StaleEpoch、Cancelled 以外的拒絕（例如 WalUnhealthy、Flash）時，回 `SEND_FAILED` 且不寫 META。Python 走 `_map_aborted_result`，回 `COMMAND_ABORTED_IN_FLIGHT` 並寫入 META（`confirmed_tx_bytes`）。修法：用該命令已確認的 TX bytes 套用相同映射。
3. [Medium] `crates/sw-core/src/session_core.rs:4026` — 問題：attach 進行中呼叫 recover 回 `SESSION_NOT_READY`；Python 回 REATTACH、`ok:true`（`:7557-7564`）。修法：照 Python 分支處理。
4. [Medium] `crates/sw-core/src/session_core.rs:4545-4549`、`:4365`、`:4556` — 問題：self-test 有三處與 Python 不同：
   - serial 已死時仍送 probe，且建議動作是 `attach`；Python 回 `BRIDGE_DOWN` 並建議 recover（`:7288`）。
   - `attached_vtty` 恆為 null。
   - fallback passthrough 的情況缺 `pin_profile`、`suggested_profile`、`hint`。
   
   修法：移植 `_suggest_profile_from_rx` 與這幾個分類分支，並回填 vtty。
5. [Medium] `crates/sw-core/src/session_core.rs:3415` — 問題：背景 capture 的 64 筆上限按 session 計，Python 是全域共用。修法：上限移到全域的 registry 層。
6. [Medium] `crates/sw-core/src/capture.rs:194,204` — 問題：沒有注入時鐘時，log 檔名用 UTC，Python 用本地時間。修法：預設改用本地時間，或在 T16 強制注入平台 Clock 並加測試。
7. [Low] `crates/sw-core/tests/lease_selftest.rs:1333` — 問題：只斷言 committed 加 refused 的總數，不保證兩個分支都真的出現。修法：分別斷言兩者都大於 0。
8. [Low] `crates/sw-core/src/session_core.rs:2500` — 問題：lease 到期判斷用 `>=`，Python 是嚴格的 `>`。修法：改成 `>`。

### b73da42 test(sw-core): drain／release 端到端案例
1. [Low] `crates/sw-core/tests/drain_release_e2e.rs:104-123` — 問題：`in_flight_unknown_is_send_failed_no_resend` 用 `Unknown{confirmed:0}` 讓 writer 立即結算，命令在 release 之前就已經 `SEND_FAILED`，沒有驗到「release 時仍有 Unknown 在途」。修法：先用 `BlockUntilCancel` 讓命令卡在途中，release 時再讓它以 Unknown 結算；或在送 release 前斷言命令仍是 running。
2. [Low] `crates/sw-core/tests/drain_release_e2e.rs:346-351` — 問題：舊 epoch 的完成事件寫死 `epoch: 1`、`OpId(1)`，沒有從舊命令實際取得。修法：release 前先記下 `bridge_generation` 與該命令 TX 的 op_id，再用這兩個值送事件。
3. [Low] `crates/sw-core/tests/drain_release_e2e.rs:27-28` — 問題：計畫斷言「20 ms 後 state 為 RECOVERING」被放寬成「RECOVERING 或 RELEASED」，等於斷言被削弱。commit 訊息有說明理由，「drain 期間不阻塞」由 `actor_answers_get_state_while_drain_pending` 嚴格驗證。修法：在 PR 描述登記這個斷言變更，或改用 BlockForever 保留原斷言。

### f97a208 merge: 同步 P5 T10～T14 至 P6
1. [Low] `crates/sw-core/src/session_core.rs:3254` — 問題：merge 後 `SubmitOrigin` 已存在，但 execute gate 仍用 `source.starts_with("human:")` 判斷 human；D-10／OD-14 #5 規定 `Rpc` 來源一律當 agent。目前屬於 07 Task 6 尚未交付的範圍，commit 也宣稱「行為不變」。修法：在 07 Task 6 的待辦中明列這個判定點改為 `matches!(req.origin, SubmitOrigin::HumanConsole{..})`，並補 `human_timeout_promoted_only_for_internal_origin` 測試。
2. [Low] commit 訊息 — 問題：「與 P5 tip 逐 binary 比對：回歸 0」，但 `sw-pal --lib` 是否通過取決於主機狀態（fb76460 第 1 項），而且只比對 P5，沒有對 P6 基準（e650e70）跑 `compare_test_baseline.py`。修法：補做對 P6 基準的比對並附 log。

### ac46a0e fix(rust-tests): lease_selftest 讀 golden 改經 common::repo_path 以符合 cfg 邊界
1. [Low] `crates/sw-core/tests/lease_selftest.rs:9-11`、`:939-941`、`:1255-1257` — 問題：P5 早在 merge 之前就有 `b0f2853` 修了同一件事（03:33，merge 在 03:35），寫法不同（`use common::repo_path;`，沒有 `#[allow(dead_code)]`）。所以 commit 訊息說「P5 tip 本身同樣 exit 1」並不正確，下次同步 P5 時這個檔會有 3 處文字衝突。修法：改成 merge `b0f2853`（或 cherry-pick 它的版本），取代自行重寫的修正，並更正訊息。

### High／Medium 摘要表

| 嚴重度 | Commit | 位置 | 問題 |
|---|---|---|---|
| High | fb76460 | `sw-pal/src/serial/posix.rs:1557-1586` | 兩支 L1 鎖定測試依賴主機 `/dev/pts/9`，本機實測紅燈 |
| High | 0a187c1 | `sw-core/src/session_core.rs:2441`、`:2242` | 登入中遇 boot banner，永久卡在 ATTACHING，attach 卻回 ok |
| Medium | fb76460 | `sw-pal/src/proc_scan/posix.rs:146-180` | 仍用 readlink 字串判定同一 tty，Python namespace 案例未移植 |
| Medium | 2d93181 | `sw-core/src/wal.rs:1184-1209`、`:1412-1414` | 兩鍵寫死為 0、`rotate_retry` 語意不同，未登記 NC |
| Medium | 0a187c1 | `sw-core/src/login_fsm.rs:302-306` | 所有登入 TX 拒絕都映射成 `PROMPT_UNAVAILABLE` |
| Medium | 0a187c1 | `sw-core/src/session_core.rs:2172` | 再探把 `pending_auto_login` 算進 `require_login` |
| Medium | 8b8a40f | `sw-core/src/session_core.rs:4036`、`:4433` | 前景忙時 recover／self-test 回 `SESSION_BUSY`（self-test 未登記） |
| Medium | 8b8a40f | `sw-core/src/op_runner.rs:582-586` | 救援鍵遇其他拒絕回 `SEND_FAILED`，應為 `COMMAND_ABORTED_IN_FLIGHT` 並寫 META |
| Medium | 8b8a40f | `sw-core/src/session_core.rs:4026` | attach 中 recover 回 `SESSION_NOT_READY`，Python 回 REATTACH ok |
| Medium | 8b8a40f | `sw-core/src/session_core.rs:4545`、`:4365`、`:4556` | self-test 分類（BRIDGE_DOWN／vtty／pin_profile）與 Python 不符 |
| Medium | 8b8a40f | `sw-core/src/session_core.rs:3415` | 背景 capture 64 筆上限按 session 計，Python 為全域 |
| Medium | 8b8a40f | `sw-core/src/capture.rs:194,204` | log 檔名預設用 UTC，Python 用本地時間 |
