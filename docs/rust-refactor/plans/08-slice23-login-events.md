# P7 Slice 2.3：完整 Login FSM、Quiet／Reconfirm、Stall／Recovery、Event Engine 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 Rust daemon（`serialwrapd`）在 Slice 2.3 1:1 承接 Python 基線的完整登入與事件能力：shell／bcm／prpl／passthrough 全平台 Login FSM（prompt／login／password regex、`post_login_cmd`、`ready_probe` nonce、逐步 timeout、console unlock）、session 帳密 `env_file` 解析與 `CREDENTIALS_UNRESOLVED` 終態、Autoboot Quiet Gate 與 Ready Reconfirm（300 s／5 次、`retry_after_s`、`recommended_action`）、readiness reprobe、self-test 分類、transport stall 偵測與復原提示、`session recover`（含 `--force`）、Event Engine（schema、ReDoS 安全判定、LineBuffer 16 KiB、ANSI 剝除、cooldown、`max_fires`、dispatcher 子程序池、events.ndjson、counter、registry、10 個 `event.*` RPC），以及 agent log capture（`session.log_start/stop/status`），並通過規格 §11.6 Slice 2.3 的 L1／L1.5 出口。

**Architecture:** 本 Phase **擴充** P5（`06-slice21-daemon-core.md`，下稱 06）已實作的模組，不重做、不改名：狀態轉移仍集中在 06 T11 的 sans-IO `SessionCore::on_event(MailboxItem, now) -> Effects`；06 T13 的 `login_fsm.rs`（`LoginIo`／`LoginOutcome`／`LoginPlan`／`choose_login_plan`）、`session_auth.rs`、`quiet_gate.rs`（`QuietState`／`QuietTransition`），與 06 T14 的 `self_test.rs`、`capture.rs`、`op_runner.rs`（`OpSpec::{Recover,SelfTestProbe,RebootRecovery,DetectTemplate}`）都是本 Phase 的起點，差異見 §0.6。login 仍在 OpRunner thread 執行（經 06 T9 `TxWriter`＋`Fence` 送 bytes、經 06 T10 `RxShared` 等 RX），結果以 06 的 `ControlEvent::LoginFinished{epoch, fsm_gen, result, after_rx_seq}` 回 actor 收斂；本 Phase 補上 Slice 2.1 刻意不承接的部分（bcm、`post_login_cmd`、console unlock、reprobe、transport stall、reconfirm 有界化、force recover、可取消的 login run＝SC-30）。Event Engine 獨立於 session actor：reader thread 在 WAL 落盤後經 `RxObserver` 把 `(com, bytes, 精確 wal_seq)` 餵給 per-COM `LineBuffer`，經全域 1024 matcher queue（drop-oldest）到 matcher thread，命中後交 8 執行緒 dispatcher，子程序經 PAL `HandlerSpawner`（POSIX process group／Windows Job Object）執行。平台相依只新增在 `sw-pal`（`HandlerSpawner`、`UsbTopology`、`FsOps` 寫入側增補）。

**Tech Stack:** Rust 1.96.1（edition 2021；`crossbeam-channel =0.5.15`、`indexmap =2.11.4`、`fancy-regex =0.16.2` 經 `sw_proto::pyre`、`libc =0.2.186`／`windows-sys =0.61.2` 僅 sw-pal、dev `tempfile =3.23.0`）；Python 3.12（golden 產生器、`tests/contract_blackbox/` 黑箱，stdlib＋pytest＋PyYAML）。

**Spec:** `docs/rust-refactor/serialwrap-rust-spec-final.md` §5.1.2、§5.1.3、§5.2.1、§5.2.2、§5.3、§5.4、§5.5、§9.3、§9.4、§11.6（Slice 2.3 列）、§12.2 Stage 2 表；`docs/rust-refactor/owner-decisions-2026-10-15.md`（OD-04、OD-06、OD-08、OD-10、OD-14、OD-15）；`docs/rust-refactor/plans/rust-architecture.md`（下稱 arch）§1.3、§3.1、§3.2、§3.4、§3.6、§4、§5、§6.3、§8、§9；`docs/rust-refactor/plans/spec-reconciliation.md`（下稱調和表）；盤點 `inventory/05-session.md`（§1.6、§6、§7、§8、§9、§11、§12、§13）、`inventory/09-events-transfer.md`（§3、§8、§9）、`inventory/02-rpc.md`（§5.2、§5.4、§7.4、§7.5、§7.7、§7.12）、`inventory/03-errors.md`（§5、§6）、`inventory/10-gaps.md`（G-01、G-09、G-13）。

---

## Global Constraints

（摘自 `00-master-plan.md` Global Constraints，僅列本 Phase 相關者；全文以 master plan 為準）

- 平台相依 production code 只在 `sw-pal`；其他 crate 禁止 `#[cfg(unix)]`／`#[cfg(windows)]`／`target_os`／`cfg!`／`cfg_attr` 平台分支與 build script 間接分支（§3.1）；允許 `#[cfg(test)]` 與 `#[cfg(feature = "test-fakes")]`。`scripts/rust/check_cfg_boundary.py` 必須維持 0 命中。
- RPC／CLI `error_code` 與 session `last_error` 為兩個獨立命名空間（ADR-07、§9.3）；`CREDENTIALS_UNRESOLVED` 只屬後者。`session.attach`／`session.recover` 依 SC-34（OD-14 #1，RPC-D-02 已裁決分離）不再把 `last_error` 透傳成 `error_code`：未達 READY 一律回固定碼 `SESSION_NOT_READY`，另以回應頂層 `last_error`（`Option<SessionLastError>`，序列化為字串或 null；06 內部以 last_error 字串承載，於 RPC 邊界以 `SessionLastError::parse` 轉換）與 `last_error_detail`（`Option<String>`）回報；`RpcErrorCode::LastErrorPassthrough` 已刪除，不得新增 RPC 碼。
- 錯誤碼計數由 `contracts/error_codes.yaml` 資料產生，不得硬編碼 78（§9.2、OD-06）；本 Phase 不新增任何 `error_code` 或 `last_error` 值。
- `--retries` 只對 11 個唯讀方法重試；`session.interactive_status` 與所有 `event.*`、`file.*`、`log.*` 方法永不重試（§4.2、arch §7.5）。
- JSON：CLI／RPC 為 compact、`ensure_ascii=False`；`state.json`／WAL 以 `sort_keys=True`；events.ndjson 與 handler stdin 為 Python 預設分隔符（`", "`、`": "`）、不排序、`ensure_ascii=False`；規則檔 `indent=2, sort_keys=True`、無尾端換行；counter 檔預設分隔符、`ensure_ascii=True`（arch §3.3）。
- 背壓上限：EventEngine LineBuffer `16384` bytes、matcher queue `1024`（drop-oldest＋`event_dropped`）、dispatcher `8`（丟新＋`event_dropped`）、每條 rule 並行 1；Actor mailbox／RX／TX／WAL 待處理一律有界（§5.4、arch §5.10）。不得出現無界 channel 或無界 buffer（含 handler stdout／stderr：只保留尾端 4096 bytes）。
- Ready Reconfirm 耗盡：300 秒或 5 次失敗（§5.3）；`READY_RECONFIRM_RETRY_S=5.0`、`BOOT_QUIET_WINDOW_S=180.0`、`BOOT_BANNER_TAIL_CHARS=256`、`REPROBE_*`、`TRANSPORT_STALL_MIN_RX_AGE_S=30.0`、`RX_FLOOD_BYTES_PER_10S=20000` 一律取自 `sw_proto::constants`（arch §3.1），不得在業務碼內重打數字。
- regex 一律走 `sw_proto::pyre`（Python `re` 方言），YAML 一律走 `sw_proto::yaml11`（arch §8）；`regex` crate 語法不得直接出現在業務碼。
- Windows RPC 信任範圍＝整機本機使用者（OD-12）；event handler 子程序在 Windows 以使用者身分執行，文件不得宣稱同使用者隔離。
- repo 政策：工作分支為 `wt/rust-refactor/p7-slice23`（base `feature/rust-refactor`，符合 R-12 `wt/<slug>/<subtask>`）；不得 commit 到 `main`；每個 code commit 附 `changelog.d/rust-p7-<slug>.md` fragment（`changelog.d/` direct child）；commit 用 Conventional Commits 繁中 subject，含 trailer `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`；每個 Task 完成前跑 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`。
- 黑箱測試只經部署 artifact（CLI subprocess＋JSON）；`tests/contract_blackbox/` 不得 import `sw_core`（arch §9.2）。同一批黑箱案例先對 Python 基線綠燈，再指向 Rust artifact。測試工具一律用 P3（`04-stage1a-rust-cli.md` Task 2）的 `bb_harness`／`bb_daemon`／`bb_faketarget` API，擴充屬測試工具變更，依規格 §10.4 獨立 PR、獨立審閱（Task 18A）。
- 本切片必要案例集合以 `tests/acceptance/cases.yaml`＋`cases.lock.json` 的 `slice_2.3` 集合為準（P2 `03-stage0-test-infra.md` Task 12），新增案例只能經 `regen_lock.py --add-only` 進鎖，且在 Stage I 開始前完成（§11.3「開工前凍結」）。
- 每個 Task 的 Step 4 只要求「本 Task 的測試由紅轉綠＋既有已綠套件不退化」；其他 Task 尚在紅燈的 P7 測試 binary 以 `--test` 明列排除。整個 workspace 全綠只在 Stage I 結束（Task 17 Step 4）與 Task 19 要求。
- 在 fakes 上通過的 PAL 測試**不得**計為原生 gate（arch §4、§9.1）；需要資源的測試命名 `cap_<resource>__<name>` 並 `#[ignore = "cap:<resource>"]`。
- P5-T4 的 POSIX `SignalWait::install()` 會讓後續執行緒繼承封鎖的 `SIGINT`／`SIGTERM`；本 Phase 所有 PAL 子行程 `pre_exec` 必須先以 `pthread_sigmask(SIG_UNBLOCK, ...)` 解除這兩個訊號，再執行 `exec`，避免 handler child 無法被逾時或 daemon 停止訊號終止。

## Review Focus

1. **過期 login run 與 quiet arm 的線性化**：banner 在 login／probe 進行中 arm quiet 時，舊 `fsm_gen` 的 run 不得再送任何 byte（由 `Fence` 在 OS 寫入前以 `permit.fsm_gen >= fence.fsm_gen_floor` 擋下；`StartLogin` 與「會擋 probe 的 arm」在同一把 fence 鎖內 `fsm_gen++` 並把 floor 設為新值），也不得在完成時把 session 推成 READY；`LoginFinished` 依 `after_rx_seq` 屏障排在其觀測到的 RX 之後處理；console unlock profile（`boot_quiet_blocks_probe` 為假）不受此取消。收斂結果須符合 Task 0 登記的 SC-30（Task 7 測試覆蓋）。
2. **`CREDENTIALS_UNRESOLVED` 零 TX 與一次性警告**：宣告帳密但解析失敗時絕不對 login prompt 送空帳密、不自動重探、只改帳密檔也不自動解除、`credentials_warned` 去重 log＋WAL META 各一次，僅 attach／recover 重置（Task 7、Task 18 覆蓋；P5 已承接部分以回歸鎖定）。
3. **Reconfirm 有界性**：重臂不延長 300 s deadline、第 5 次失敗或 bootloader 證據即落 `READY_UNCONFIRMED`（不帶 `retry_after_s`），`session.list` 不評估到期而 `get_state`／tick 會（GAP-G-13），不得讓 agent 卡在永遠可重試的 `AUTOBOOT_QUIET`（Task 4、Task 5 覆蓋）。
4. **Event Engine 有界與 fsync 不阻塞 RPC**：matcher drop-oldest、dispatcher 丟新、handler 逾時殺整個 process group／Job、子程序只繼承 stdio、dispatcher 關機有上限，stdout／stderr 只留尾端；event RPC 的檔案 I/O 不得拖住其他 session 的 control 延遲（Task 12、Task 14、Task 15、Task 18 覆蓋）。
5. **逐字相容面**：events.ndjson、規則檔、counter 檔、handler stdin／env、schema 錯誤訊息、`TRANSPORT_STALL` detail、gate／self_test／recover／log 回應 key 順序須與 `contracts/golden/p7/*.ndjson` 位元組相同；DF-01／DF-08／DF-09／DF-15／DF-18 為已登記差異，其餘一律照基線（Task 1 golden、各 Task 的 golden 比對）。
6. **不重做 P5**：每個 Task 只新增 §0.6 差異表列出的項目；P5 既有測試除 §0.6 明列的更正與退役外不得修改，且必須持續綠燈。

---

## 0. 範圍、上游依賴與登記項目

### 0.1 本 Phase 範圍（In）

| 項目 | 對應 Python 基線 | Rust 承接檔（「06 Tn」＝P5 已建立，本 Phase 只擴充） |
|---|---|---|
| 帳密解析 | `auth.py`（05 §9） | `crates/sw-core/src/session_auth.rs`（06 T13） |
| Login FSM 全平台 | `login_fsm.py`（05 §8） | `crates/sw-core/src/login_fsm.rs`（06 T13；補 bcm、`post_login_cmd`、console unlock、flood 反分類、detect）。SC-35（OD-14 #2）：`platform=passthrough` 的 session 一律 `LoginPlan::None`（不登入、不送 ready_probe，含 uboot-template 等有 ready_probe 者），READY 只經 P5 的 30 s 安靜 tick；`ensure_ready` 的 passthrough 分支只作純函式回歸 |
| Login run 協調與收斂 | `session_manager._probe_existing_bridge`、`_attach_by_id` probe 段、`_attach_by_id_dynamic` detect 層、`_spawn_reboot_recovery`、`_refine_probe_failure`、`_emit_credentials_unresolved_warning` | `crates/sw-core/src/login_run.rs`（新檔，只放收斂函式）＋06 的 `session_core.rs`／`op_runner.rs` |
| Quiet Gate＋Reconfirm | `SessionRuntime` quiet／reconfirm 方法、`_update_boot_quiet_locked`、`_boot_quiet_blocks_probe`、`_agent_gate_reject_locked`、`service._resolve_session_id` gate 段（05 §1.6、§7） | `crates/sw-core/src/quiet_gate.rs`（06 T13） |
| Readiness reprobe | `reconcile_readiness`、`_prepare_reprobe_locked`、`_finish_probe_reprobe`、`_reprobe_target_still_valid_locked`、`_bootloader_evidence`（05 §6） | `crates/sw-core/src/reprobe.rs`（新檔） |
| Transport stall | `transport_stall.py`、`_refine_probe_failure`、`_mark_session_rx` 重臂（05 §8.4、10 G-09） | `crates/sw-core/src/transport_stall.rs`（新檔）＋`sw-pal` `UsbTopology` |
| Self-test | `_self_test_impl`（05 §11、02 §7.5） | `crates/sw-core/src/self_test.rs`（06 T14；補 RX_FLOOD／bootloader fallback／golden key 順序） |
| Recover／force recover | `recover_session`、`_force_recover`、`_recover_after_failure` 的 `system:recover` 分支（05 §12.1、§12.2） | `crates/sw-core/src/recover.rs`（新檔，只放回覆組裝）＋06 `OpSpec::Recover` |
| Log capture | `log_start`／`log_stop`／`log_status`、`_on_bridge_rx` capture 段（05 §13、02 §7.7） | `crates/sw-core/src/capture.rs`（06 T14；本 Phase 改為 reader 端 `RxObserver` 寫入與 golden 形狀） |
| Event Engine | `sw_core/event_engine/*`（09 §3） | `crates/sw-core/src/event_engine/{mod,schema,matcher,line_buffer,counter,registry,dispatcher,event_log}.rs`＋`crates/sw-proto/src/pyre/redos.rs`＋`sw-pal` `HandlerSpawner` |
| RPC 接線 | `service.py` 的 `session.self_test`、`session.recover`、`session.attach`、`session.log_*`（06 T18 已接線，本 Phase 補新行為）、`event.*`（02 §7.5、§7.7、§7.12） | `crates/sw-daemon/src/service.rs` |

### 0.2 非範圍（Out）

- Human console、Lease、Soft Preempt、DeferredBuffer 本體（P6）；本 Phase 只在 quiet arm 時呼叫 P6 的 `SessionCore::invalidate_human_input(InvalidateReason::BootBanner|QuietRearm, fx)`（07 D-02，以 `input_epoch` 失效，規格 §5.3「失效舊 DeferredBuffer」）。
- FileTransfer、MCU flash、完整裝置交接（P8）；`file.push/pull` 的 gate 形狀沿用 P5 SC-03。
- 命令執行與 `_recover_after_failure` 的 CTRL_C／CTRL_D 主流程（P5）；本 Phase 只補 `source=="system:recover"` 的 force 分支與 READY 路徑的 `session.recover` 入口。
- 48 小時長跑與 L2 F7（P8）。

### 0.3 上游 Consumes（實際 producer 符號；Task 0 Step 1 逐項核對，不符時先改本表與 producer 計畫／arch，不得在 08 另起別名）

本 Phase 先前版本使用的名稱一律改用下列實際符號（左欄僅供對照舊稿，**不得**出現在程式碼）：

| 舊稿名稱 | 實際符號（producer） | 備註／本 Phase 修訂 |
|---|---|---|
| `session_core::Mono`（結構體） | `sw_core::Mono = u64`（06 T0，crate root，`Clock::mono_ns()` 奈秒） | 秒換算一律經 `sw_core::mono::{from_secs_f64(f64)->Mono, as_secs_f64(Mono)->f64}`（本 Phase 新增的兩個自由函式，Task 0）；不改 `Mono` 型別 |
| `Event::{RpcAttachSession,RpcSelfTest,RpcRecover,RpcLog*}` | `MailboxItem::Control(ControlEvent::Request{req: SessionRequest::{SessionAttach, SelfTest{..}, Recover{..}, LogStart, LogStop, LogStatus}, reply})`（06 T11） | 不新增 RPC 事件 |
| `Event::LoginRunCompleted{run_id,epoch,fsm_gen,outcome}` | `ControlEvent::LoginFinished{epoch, fsm_gen, result: LoginOutcome, after_rx_seq: Option<u64>}`（06 T11）＋**本 Phase 新增欄位** `report: LoginReport`（Task 7） | run 身分以 `(epoch, fsm_gen)` 表示（`StartLogin` 前 `fsm_gen++`，故唯一；actor 以 `active_login` 比對）；`after_rx_seq` 為 OpRunner 結束時 `RxShared` 已觀測的最後 `rx_seq`，actor 依 06 `Mailbox::recv_next(processed_rx_seq, ..)` 先處理完該序號前的 data lane 才套用 |
| `Effect::StartLoginRun(plan)` | `Effect::StartLogin{epoch, fsm_gen, plan: LoginPlan}`（06 T11） | `LoginPlan` 沿用 06；**本 Phase 新增欄位** `kind: LoginRunKind`（Task 7） |
| `Effect::CancelLoginRun{run_id}` | **本 Phase 新增** `Effect::CancelLogin{epoch, fsm_gen}`（修訂 06 T11 `Effect`，Task 0） | executor 觸發該 run 的 `RunCancel` |
| `Effect::Log{level,line}` | `Effect::Warn(String)`（06 T11，GAP-G-01 WARNING） | INFO 等級不產生 Effect（GAP-G-01：INFO 被丟棄） |
| `Effect::WalMeta{meta}` | `Effect::WalMeta{cmd_id: None, meta}`（06 T11） | — |
| `Effect::SpawnAttach{by_id}` | `Effect::Notify(ManagerNote::RequestAttach{by_id})`（06 T11 `ManagerNote`；若 06 未定義此 variant，Task 0 於 06 T11 補） | manager 依 06 T16 inflight 去重 |
| `Effect::{StopBridge,NotifyReady,NotifyDetached,FlushQueue,SuspendInteractive,ResumeInteractive}` | 06 無此類 Effect：detach 走 06 T12 `SessionCore::begin_stop(StopKind::LoginFailed, None, now)`；READY 註冊為 06 `queue_registered=true`；flush 為 06 T8 `CommandRegistry::flush_session`；suspend／resume 為 P6 `SessionCore` 的 `fg_begin`／`fg_finish`（07 T6） | 測試改斷言可觀測狀態（state、`queue_registered`、命令紀錄 `error_code`、TX 次數），不斷言不存在的 Effect |
| `Generations{conn_epoch:u64,fsm_gen,owner_gen}` | `Generations{conn_epoch: Option<u64>, fsm_gen, owner_gen, bridge_generation}`（06 T11）＋`input_epoch`（07 T0 D-02） | — |
| `TxRefused::{StaleEpoch,StaleFsmGen,Quiet,..}`、`OpTx::send` | `FenceReject::{StaleEpoch, Frozen, Quiet, ReconfirmPending, WalUnhealthy, Flash, OwnerMismatch, Cancelled}`、`TxJob`／`TxReport`／`TxWriterHandle`（06 T9）；OpRunner 經 06 T13 `LoginIo.tx` 閉包送出 | **本 Phase 修訂 06 T9／arch §5.4**：`TxPermit.fsm_gen: Option<u64>`、`FenceState.fsm_gen_floor: u64`、`FenceState.auto_probe_blocked: bool`、`FenceReject::StaleFsmGen`（Task 7） |
| `RxWindowHandle::{wait_search, ..}`、`WaitOutcome::Cancelled`、`CancelToken` | `RxShared::{wait_for_regex, wait_for_regex_from, tail, text_from, snapshot_len, clear, rx_stats, total_bytes, idle_s, last_rx_mono}`（06 T10） | **本 Phase 修訂 06 T10**：新增 `RxShared::wait_for_regex_cancellable(re, from: Option<u64>, timeout, clock, cancel: &RunCancel) -> WaitOutcome{Matched, TimedOut, Cancelled}`；`RunCancel` 為 sw-core 自有型別（`Arc<AtomicBool>`＋喚醒 `RxShared` 的 condvar），**不得**使用 PAL `CancelToken`（後者綁 serial `IoWaker`，`cancel()` 會中止 writer 在途寫入） |
| `WalWriter::append(位置參數)` | `WalWriter::append(WalAppendReq{com, dir, source, payload, cmd_id, loss_flag, meta})`（06 T7） | 以 06 為準；Task 0 回改 arch §5.8 與 06 一致 |
| `SessionManager::{known_coms, profile_for, active_cmd_id_for}` | 06 T16 無此三法 | **本 Phase 新增**（Task 16，`impl BridgeQueries for SessionManager`） |
| `SessionCore::start_stop(kind, now)` | `SessionCore::begin_stop(kind: StopKind, waiter: Option<Reply>, now) -> Effects`（06 T12） | — |
| `lease::lease_context(core, now)` | `lease::lease_context(lease: Option<&InteractiveLease>, last_human_input_at: Option<Mono>, now) -> PyDict`（07） | — |
| `DeferredBuffer::invalidate_all(&mut owner_gen)` | `SessionCore::invalidate_human_input(reason: InvalidateReason, fx: &mut Effects)`（07 T6，D-02；`input_epoch++`，**不**動 `owner_gen`） | — |
| `SessionLastError::{as_str, is_prompt_timeout_family}` | `SessionLastError::{as_string(), parse()}`（P3 生成檔） | 06 的 login／quiet 介面以 `String`／`&'static str` 承載 last_error，本 Phase 沿用；`is_prompt_timeout_family` 以 `login_fsm::is_prompt_timeout_error(&str) -> bool`（`*_PROMPT_TIMEOUT` 後綴判定）實作於 sw-core（Task 3） |
| `pycompat::{py_int, py_str, py_str_strip, py_universal_lines}` | `pycompat::{py_int_parse(&str), py_strip, splitlines, universal_newlines}`（P3 T4）；`PyValue` 轉 int 用 06 T0c `Params::int_or`（P3 沒有 `py_int(&PyValue)`）；`py_str` 在 `pyfmt` | 不新增 `py_str_strip`／`py_universal_lines` |
| `pycompat::{clean_text, strip_ansi, py_round, strftime_local, iso_utc_now, uuid4_hex, uuid4_dashed, decode_utf8_replace, py_len_chars}` | `decode_utf8_replace`、`py_len_chars` 由 P3 04 Task 4 交付；其餘由 P5 06 Task 0c 交付（06 §0.3「本 Phase 補齊」列） | Task 0 Step 1 以編譯期核對；缺任一項即 **BLOCKED-UPSTREAM**：回報對應 producer（04 T4／06 T0c）補入（附 golden），P7 不自行補、不在 sw-core 另寫 |
| `sw_pal::fakes::usb::FakeUsb`、`fakes::process::FakeHandlerSpawner` | 本 Phase 新增（Task 0） | — |
| 黑箱 `bb` fixture、`bb.start_daemon`／`bb.cli`、`FakeTarget(config)`、`faketarget.py`、`.received()`／`.emit()` | P3 T2：`bb_harness.{isolated_env, run_cli(args, env=..)->CliResult(rc, stdout: bytes, stderr: bytes), is_rust, ab_label}`、`bb_daemon.PyDaemon(env, profile_dir=, socket=, lock=)`、`bb_faketarget.FakeTarget(workdir, prompt=, user=, password=).start(by_id_dir)/.rx_log()/.stop()`；conftest fixtures `env`、`py_daemon`、`faketarget` | 本 Phase 的擴充（`login_steps`、`noise`、`freeze_rx` 等）以**新增 keyword 參數**加在 `bb_faketarget.FakeTarget`，預設值維持 P3 行為（Task 18A） |

其餘沿用（簽章以 producer 為準）：`sw_proto::pyre::{compile, PyRegex, PyMatch, PyFlags, PyReError, escape}`、`pyre::parse::parse`（P3）；`sw_proto::pyjson::{PyValue, PyInt, PyDict, DumpOpts, Sep, loads, dumps, JsonDecodeError::py_message}`；`sw_proto::config_types::{SessionProfile, ProfileTemplate}`；`sw_proto::constants::*`；`sw_pal::{Platform, SysEnv, Clock, FsOps, FsRead, PalError, PalResult}`（`FsRead` 為 P3 T9 唯讀介面：`read`、`list_dir`）；06 T13 `login_fsm::{LoginIo, LoginOutcome, LoginPlan, choose_login_plan, probe_ready, ensure_ready, prompt_timeout_error}`、`session_auth::{SessionAuth, AuthResolution, parse_env_file, resolve_session_auth, credentials_declared_but_unresolved}`、`quiet_gate::{QuietState, QuietTransition, detect_boot_banner, is_reboot_command, valid_completion_regex, slice21_unsupported_feature}`；06 T14 `op_runner::{OpSpec, OpOutcome, OpRunner, OpCtx}`、`self_test::classify_pre_probe`、`capture::{BackgroundCapture, AgentLogCapture}`。

**Task 0 核對註記（2026-10-26，`scripts/rust/check_p7_prereqs.py`）**：§0.3 與實際 producer 的差異及處置（依本節規則：改本表與 producer 計畫／arch，不在業務碼起別名）：

| 項目 | 實際 producer | 處置 |
|---|---|---|
| `FenceReject::StaleFsmGen`、`TxPermit.fsm_gen: Option<u64>`、`FenceState.{fsm_gen_floor, auto_probe_blocked}` | 06 T9 已有 `TxPermit.fsm_gen: u64`、`FenceState.fsm_gen: u64`、`FenceReject::FsmMismatch`（相等比對） | 本計畫一律改稱 `FenceReject::FsmMismatch`（`p7_skeleton.rs` 同步）；`Option`／floor／`auto_probe_blocked` 由 Task 7 在 06 T9 實作後修訂，Task 0 不改 06 型別 |
| `LoginIo.tx` 回 `TxReport`、`LoginIo.{cancel, class}`、`LoginOutcome::{Aborted, Raised}` | 06 T13：`tx` 回 `sw_pal::TxResult`、無 `cancel`／`class` | 延後到 Task 3（06 T13 尚為骨架）；Task 0 只新增獨立型別 `LoginAbort` |
| `Effect::{CancelLogin, CaptureOpen, CaptureClose}`、`StartLogin.kind`、`LoginFinished.report`、`ControlEvent::{CaptureOpened, CaptureClosed}`、`ReaderCtx.observers` | 06 T10／T11 尚為骨架 | 延後到 Task 7／10／16-wire；`RxObserver` trait、`RunCancel`、`WaitOutcome`、`wait_for_regex_cancellable`、`last_rx_seq` 已於 Task 0 落地（本體 `todo!`） |
| `ManagerNote::RequestAttach` | 06 T11 未定義 | Task 0 補上 `RequestAttach { by_id: String }` |
| `LOGIN_FSM_DETAIL_ERRORS` | 06 T13 已定義（12 值） | 沿用，不重複定義 |
| `FsOps::is_file` | P3 `FsRead::is_file` 已存在 | 沿用 `FsRead::is_file`，`FsOps` 不重複定義 |
| `pyre::sre::SubPattern`、`pyre::parse::parse`、`PyReError` | P3：`pyre::parse` 為私有模組（`parse()` 回 `ParsedPattern`）、`compile` 回 `CompileError` | `redos.rs` 置於 `pyre/` 下以 `super::parse` 取用；公開 API 為 `ambiguity_score／has_backref／maybe_redos_suspicious(pattern, flags) -> Result<_, CompileError>` |
| `pyre::escape` | P3 未定義 | ~~BLOCKED-UPSTREAM（P3）~~ → **已宣告上游補件（P3 缺口，2026-10-26）**：擁有者指派於 P7 分支補 `sw_proto::pyre::escape(&str) -> String`，語意＝CPython 3.12 `re.escape(str)`；golden `contracts/golden/pyre_escape.tsv`（`scripts/rust/goldens/gen_pyre_escape.py`，新鮮度列於 `tests/test_rust_goldens_fresh.py`）＋ `crates/sw-proto/tests/pyre_escape_golden.rs`；登記於 `spec-reconciliation.md` §8 UP-P7-01 |

上游行為仍為骨架（`SKELETON`，依 P5／P6 實作進度阻擋對應 P7 Task）：06 T13 `session_auth::*`、`login_fsm::*`、`quiet_gate::*`（阻擋 Task 2～4）；06 T10 `RxShared::*`、`spawn_reader`（Task 3、10）；06 T7 `WalWriter::append`（Task 6）；06 T8／T9／T11／T12／T14 `flush_session`、`Fence::admit`、`SessionCore::on_event`／`begin_stop`、`Mailbox::recv_next`、`spawn_actor`、`OpRunner::spawn`、`classify_pre_probe`（Task 7～10）；07 T6／T7 `invalidate_human_input`、`fg_begin`／`fg_finish`、`lease_context`（Task 4、8）。

### 0.4 承接的已登記項目（不得自行擴張）

| ID | 本 Phase 行為 | Task |
|---|---|---|
| SC-03 | execute-time／file gate 回 `AUTOBOOT_QUIET` 時附 `retry_after_s`（quiet：剩餘秒數 1 位小數；pending-only：5.0）；鍵位置照 P1 凍結的 Python 輸出 | 4 |
| SC-13 | lease 到期 1 s tick 主動處理（P6 實作；本 Phase 的 reprobe tick 與其共用同一 1 s tick 事件） | 5 |
| SC-26 | Windows event handler：`exec` 走 CreateProcessW＋Job；`shell` 走 `cmd.exe /d /s /c`；逾時 TerminateJobObject；env 白名單另加 `SYSTEMROOT`、`COMSPEC`、`PATHEXT`、`TEMP`、`TMP`、`USERPROFILE`（OD-14 #6 確認原預設） | 14 |
| SC-34 | `session.attach`／`session.recover` 的 `error_code` 與 `last_error` 分離：未達 READY 回 `SESSION_NOT_READY`＋頂層 `last_error`／`last_error_detail`（OD-14 #1；P5 已實作核心，本 Phase 承接完整 login／reprobe 情境的回應） | 9、17 |
| SC-35 | `platform=passthrough` 不跑 Login FSM、不送 ready_probe、不 arm boot quiet、不跑 READY reconfirm；ATTACHED 連續 30 s 無 RX 自動轉 READY（OD-14 #2；P5 已實作 tick 與命令路徑核心，本 Phase 負責完整 Login FSM 繞過、quiet gate 排除、self-test／recover 的 Slice 2.3 深度） | 3、4、7、8、9、17 |
| DF-01 | `event.rule_delete`／`event.reset` 的 `rule_id` 必須符合 `^[a-z0-9][a-z0-9-]{0,63}\.[a-z0-9][a-z0-9-]{0,63}\Z`，否則不碰檔案（delete 回 `deleted:false`、reset 不計數） | 13、16 |
| DF-08 | `contains` 規則的 `flags` 也驗證（`unsupported regex flag: X`） | 11 |
| DF-09 | `RuleRegistry.load_all` 把 `ValueError`／`TypeError` 當 `FailedLoad` | 13 |
| DF-15 | RX observer、event engine、background capture 改用該 RX chunk 的精確 WAL seq | 16 |
| KEEP（照抄） | SES-16.4-10（reconfirm 第三條終態路徑）、SES-16.4-11（READY 確認失敗打回 ATTACHED 且不 flush）、SES-16.6-04／EVT-D-C1（login_regex 預設雙反斜線）、SES-16.6-05（nonce 可被回顯滿足）、EVT-D-S5、EVT-D-S12、EVT-D-D11、EVT-D-E1～E4、EVT-D-E7、EVT-D-E8、EVT-D-E10、EVT-D-L1、RPC-H-07、RPC-H-14、GAP-G-09、GAP-G-13、X3（`USER_ENV_MISSING` 不可達） | 各 Task |
| RPC-H-13 | `event.reload` 清單 Rust 輸出升冪，比較器以集合比對 | 16 |
| EVT-D-S15 | ReDoS 接受集合以「無 google-re2」Python 為基準 | 11 |
| EVT-D-E11 | counter 每行讀檔改為 write-through 快取（IMPL，daemon 是唯一寫入者） | 13 |

### 0.5 本 Phase 新登記（Task 0 寫入 `contracts/behavior_changes.yaml` 與調和表 §1）

ID 選擇：SC-28／SC-29 已由 P6（07 T0 D-11／D-12）占用，本 Phase 取 **SC-30**；DF-18 未被占用（DF-19 已由 P0 登記為 self-test nonce probe 的提交點 quiet 重驗、P1 T20 實作，R5-F2；本 Phase 只在 Task 8 承接其 Rust 行為，不重登記）。Task 0 Step 1 以 `contracts/behavior_changes.yaml` 現況再核對一次（P6 尚未合併時仍保留 SC-28／29 給 P6，不得搶號）。

| ID | 內容 | 規格依據 | 範圍 |
|---|---|---|---|
| SC-30 | 自動 login／probe／ready-reconfirm／reboot-recovery run 進行中偵測到 boot banner（quiet arm、`fsm_gen++`），**且 `boot_quiet_blocks_probe` 為真**（即 profile 無 `console_unlock_env`）時，Rust 立即取消該 run：`Fence` 自 arm 那一刻起以 `StaleFsmGen` 拒絕所有帶舊 `fsm_gen` 的 permit（已排入 TxWriter queue 但未進 OS 者 → `NotSubmitted`；arm 前已標 in-flight 者算已提交、照實記 WAL），RX 等待以 `Cancelled` 結束。收斂：run 為 attach probe 或既有 bridge probe 且 session 非 READY → `ATTACHED`＋`last_error=PROMPT_UNAVAILABLE`（不經 transport 精煉、`last_error_detail=null`）；session 為 READY → 狀態不變（由 reconfirm gate 接手）；reboot recovery → 回到「quiet blocks → 延長 deadline」等待迴圈。console unlock profile 不取消（Python `_boot_quiet_blocks_probe` 對其為假，FSM 設計上要在 CFE banner 後等 RX 靜默）。Python 基線（Stage 0 前）會續送剩餘 login bytes，屬違反 §5.2.1 的缺陷而非可承接行為；Stage 0 P1 T20（R2-F1）已讓 Python login FSM／`send_secret`／ready probe 帶 `system_probe` permit＋`login_gen`、於每次 OS 提交重驗 Quiet 與世代，arm 之後同樣 0 byte，被拒時同樣收斂 `ATTACHED`＋`PROMPT_UNAVAILABLE`（READY 不變、reboot recovery 回等待迴圈）。本 SC 只剩「arm 當下立即取消 run、RX 等待以 `Cancelled` 結束」為 Rust-only（Python 於下一個提交點或等待逾時才結束 run），故維持 `absolute_only` | §5.2.1「Quiet…期間禁止自動 probe」、§5.3「活躍態＋BootBannerDetected…更新 `fsm_gen`」、arch §5.3／§5.4 | Rust-only（不參與 A/B 差分，只驗絕對契約） |
| DF-18 | event registry 與 counter 的 tmp→正式檔改名改用 `FsOps::replace`（Windows `MoveFileExW(MOVEFILE_REPLACE_EXISTING)`）。Python 現況 `os.rename` 在 Windows 目的檔已存在時拋 `FileExistsError`，第二次 `event.rule_set` 同一規則回 `EXCEPTION`、counter 永遠存不進第二次。Python 側同步改 `os.replace` | §5.5「完整保留規則 Schema 之 cooldown_ms 與 max_fires」（Windows 上 counter 不可持久即違反） | 雙邊（Windows 可觀測；POSIX 行為不變） |

`behavior_changes.yaml` 條目依 P0 schema（01 T5，頂層鍵 `changes`）撰寫，欄位：`id`、`kind`、`title`、`status`、`approval{owner, date, basis: reconciliation, decision_ref}`、`owner_confirmation: not_required`、`applies_to{python, rust: {stage: slice-2.3}}`、`approved_at_revision`（catalog revision；P0 禁止 `contract_revision` 鍵）、`ab_participation`（SC-30 `absolute_only`、DF-18 `diff`）、`spec_refs`、`reconciliation_refs`、`scope`、`phases: [P7]`（DF-18 另加 `P1`）、`case_ids`、`compatibility_impact`、`approval_conditions`、`related_decisions`。SC-30：`scope: rust_only`、`applies_to.python: null`、`case_ids: [sc30-quiet-mid-login-aborts]`。DF-18：`scope: both`、`applies_to.python: {min_version: <Task 13 commit 當下 VERSION 的下一個 patch>}`、`case_ids: [df18-event-replace-overwrite]`。

**細化（不新增 ID，調和表 §1 對應列補一句）**：SC-11（有序關機總上限 5 s）延伸到 Event Engine：daemon 停止時先於 drain Phase 1 前 `EventEngine::close_intake()`（matcher 停、不再 spawn handler），drain Phase 2 之後、WAL close 之前 `EventEngine::stop(deadline)` 以 drain 的同一共用期限等 handler，逾時以 `HandlerSpawner` 終止仍在跑的 process group／Job 並最多再等 100 ms 回收，寫 WARNING `event dispatcher shutdown 逾時，終止 %d 個 handler`；仍未回收者列入 `DispatcherStopReport.unreaped` 並使 exit 1（R1-F2；完整停止序見 P8 Task 12）（Python `shutdown(wait=True)` 無上限，屬 SC-11 已核准範圍；Task 15、Task 16 覆蓋，arch §6.1 補一列）。

### 0.6 與 P5／P6 既有實作的差異表（本 Phase 唯一允許的改動範圍）

| 模組（producer） | 06／07 已有（沿用，不得重寫或改名） | 本 Phase 新增／修改 | Task |
|---|---|---|---|
| `session_auth.rs`（06 T13） | `SessionAuth`、`AuthResolution{reason: &'static str, env_file_path}`、`parse_env_file(&str)`、`resolve_session_auth(p, env, read)`、`credentials_declared_but_unresolved` | 只補 golden 回歸（env_file 解碼失敗、BOM、universal newline、Unicode 空白、警告模板逐字）；`SessionAuth` 的 `Debug` 改為遮蔽 password／console_unlock（新增行為，RED）。不新增 `Secret`／`AuthReason` 型別 | 2 |
| `login_fsm.rs`（06 T13） | `LoginIo`、`LoginOutcome{Ready, NotReady, Failed}`、`LoginPlan`、`choose_login_plan`（含 `quiet_blocks` → 合成 `PROMPT_UNAVAILABLE`）、`probe_ready`、`ensure_ready`（shell／prpl／passthrough）、`prompt_timeout_error` | `LoginIo` 加 `cancel: &RunCancel`、`class: TxClass`，`tx` 閉包改回 06 T9 `TxReport`（console unlock 的 INFO log 依 GAP-G-01 丟棄，不需 log 介面）；`LoginOutcome` 加 `Aborted(LoginAbort)`、`Raised(String)`；`ensure_ready` 補 bcm、`post_login_cmd`、console unlock、`maybe_reclassify_flood`；新增 `detect_template`、`matches_any_bootloader_prompt`、`console_at_bootloader`、`is_prompt_timeout_error`；移除 `slice21_unsupported_feature` 的呼叫點 | 3 |
| `quiet_gate.rs`（06 T13） | `QuietState`（arm／clear_quiet／confirm_ready／expire／remaining_s／reconfirm_remaining_s／on_rx_text）、`QuietTransition`、`detect_boot_banner`、`is_reboot_command`、SC-03 `retry_after_s`、DF-02 | 新增 `boot_quiet_blocks_probe`、`agent_gate_active`、`submit_gate_reject`／`execute_gate_reject` 的 golden 位元組相等、`READY_UNCONFIRMED_HINT` 常數；`quiet_rx` golden 回歸。arm 時改呼叫 07 `invalidate_human_input`（不再 `owner_gen++` 作 deferred 失效）。SC-35：`platform == "passthrough"` 的 session 一律不 arm boot-banner quiet、不設 READY reconfirm，`submit_gate_reject`／`execute_gate_reject`／`boot_quiet_blocks_probe` 對 passthrough 恆回不阻擋 | 4 |
| `tx_writer.rs`（06 T9）／arch §5.4 | `TxPermit`、`FenceState`、`FenceReject`（8 值）、`TxJob`、`TxReport` | `TxPermit.fsm_gen: Option<u64>`（只有 `LoginProbe`／`ReadyReconfirm` 帶值；`SelfTestProbe` 為 `None`、改以 `quiet_active` 擋，R5-F2）、`FenceState.fsm_gen_floor`、`FenceState.auto_probe_blocked`、`FenceReject::StaleFsmGen`；admit 規則見 Task 7 | 7 |
| `uart_bridge.rs`（06 T10） | `RxShared`、`ReaderCtx`、`spawn_reader` | `RxShared::wait_for_regex_cancellable`＋`RunCancel`；`ReaderCtx.observers: Vec<Arc<dyn RxObserver>>`（WAL append 後依序呼叫，DF-15） | 7、10、16 |
| `session_core.rs`（06 T11） | `ControlEvent::LoginFinished`、`Effect::{StartLogin, WalMeta, Warn, Notify}`、`begin_stop` | `Effect::CancelLogin`、`StartLogin.kind`、`LoginFinished.report`、`active_login`；reprobe tick；`transport_stall_warned`（`clean_text` 後非空 RX 清除）；login 收斂表；`Effect::CaptureOpen／CaptureClose` 與 `ControlEvent::CaptureOpened／CaptureClosed`；`ForceRecoverPoll` timer | 5、6、7、9、10 |
| `op_runner.rs`（06 T14） | `OpSpec::{Recover, SelfTestProbe, RebootRecovery, DetectTemplate}`、`OpOutcome` | `Recover` 補 force 輪詢（actor timer）、READY＋force 路徑；`RebootRecovery` 補 SC-30 回等待迴圈 | 7、9 |
| `self_test.rs`（06 T14） | `classify_pre_probe` 順序 0–15、`SelfTestCtx`、RELEASED `holder_unknown`（BC-04） | RX_FLOOD、bootloader fallback prompts、`BOOTLOADER_STUCK` detail、`self_test_responses` golden key 順序（`HUMAN_INTERACTIVE_ACTIVE` 的 `interactive_id` 在 `session` 前）；**更正** 06 T14 `selftest_unresponsive_counts_attempt`（Python 只在 `_finish_probe_reprobe` 遞增 `ready_reconfirm_attempts`，`session_manager.py:1483`；self-test 失敗不遞增） | 8 |
| `capture.rs`（06 T14） | `AgentLogCapture`、`BackgroundCapture`、log_start／stop／status 形狀 | agent log 寫入移到 reader 端 `RxObserver`（不經 actor 佇列、無丟棄）；open／stop 經 executor 完成事件回 actor；`log_responses` golden | 10 |
| `service.rs`（06 T18）／`slice21_scope.yaml`（06 T0） | session.* 分支、10 個 `event.*` 落入 `METHOD_NOT_FOUND` | 新增 10 個 `event.*` 分支 | 16 |

**Slice 2.1 閘退役清單**（各列於指定 Task 的 Step 3 執行，測試同 commit 更新）：

| 退役項目 | 位置 | 處置 | Task |
|---|---|---|---|
| `slice21_unsupported_feature`、`unsupported_profile_behavior` | `quiet_gate.rs`、`choose_login_plan` 呼叫點、`tests/acceptance/slice21_scope.yaml` | 移除函式與呼叫點；yaml 的 `unsupported_profile_features` 改為 `[]` 並加註「2.3 承接」 | 3 |
| 06 T13 測試 `unsupported_profile_no_auto_tx` | `crates/sw-core/tests/login_quiet.rs` | 改寫為 `post_login_cmd_profile_logs_in`（brcm-template 走完整登入到 READY） | 3 |
| 06 T16 測試 `detect_skips_passthrough_console_unlock_and_unsupported` 與 detect 實作的「unsupported（`post_login_cmd`）」排除 | `crates/sw-core/tests/session_manager.rs`、`session_manager.rs` | 改名 `detect_skips_passthrough_and_console_unlock`：只排除 passthrough 與 `console_unlock_env`（Python `detect_template`），`post_login_cmd` 模板（brcm）恢復為候選 | 3 |
| 06 T14 測試 `selftest_unresponsive_counts_attempt` | `crates/sw-core/tests/lease_selftest.rs` | 改名 `selftest_unresponsive_does_not_count_attempt`，斷言 `ready_reconfirm_attempts` 不變 | 8 |
| `deferred_methods` 的 10 個 `event.*` | `tests/acceptance/slice21_scope.yaml`、`tests/test_slice21_scope.py::test_scope_lists_exact_deferred_methods` | 自 `deferred_methods` 移入 `served_methods`；測試期望集合同步；06 T18 `deferred_methods_return_method_not_found_without_side_effects` 讀 yaml，自動只剩 file.* | 16 |
| `cross_slice_cases` 的 2.3 列（`brcm-01-login-flow`、`ev-01-spawn-handler`、F9／F10／F11） | `slice21_scope.yaml` | 保留為紀錄，Task 18／19 以實跑結果承接（不刪列） | 18、19 |
| 06 T20 黑箱 `test_slice_scope.py::test_deferred_methods` 對 event.* 的斷言 | `tests/contract_blackbox/` | 隨 yaml 自動縮小為 file.*；另加斷言「event.* 不再回 METHOD_NOT_FOUND」 | 16 |

---

## 1. 執行順序（TDD 兩段式）

1. **Stage T0（測試工具與凍結，先行且獨立審閱）**：
   - Task 18A：擴充 P3 `bb_faketarget.FakeTarget`（新 keyword 參數）與 conftest helper（`p7_profile` 等），自身測試對 P3 既有預設行為不退化；屬測試工具變更，依規格 §10.4 獨立 PR、獨立審閱後才能被其他 Task 使用。
   - Task 0：介面凍結（只為**新增**的公開項建立 `todo!("P7 Task N")` 本體；06／07 既有函式**不得**改成 `todo!`）、登記 SC-30／DF-18、`cases.yaml` 登記本 Phase 全部案例並以 `regen_lock.py --add-only` 凍結 `slice_2.3` 集合。
   - Task 1：Python golden。
2. **Stage T（測試先行）**：Task 2～Task 18 的 Step 1（寫測試）與 Step 2（確認狀態）可平行進行。Step 2 只對 §0.6「新增／修改」欄的行為要求 RED；對 06 已實作行為的 golden／回歸測試標為「回歸鎖定」，預期 PASS，若 FAIL 即為 P5 缺陷，於該 Task Step 3 修正並在 PR 說明列出。Stage T 出口：`cargo test --workspace --no-run` 成功；P7 新增行為的測試紅燈（`todo!` panic 或斷言失敗）、回歸鎖定測試綠燈；`tests/contract_blackbox/test_p7_*.py` 對 Python 基線全綠（SC-30 Rust-only 案例除外）、對 Rust artifact 紅燈。
3. **Stage I（實作）**：依下列相依順序執行各 Task 的 Step 3～Step 5：Task 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10；Event 線 11 → 12 → 13 → 14 → 15 → 16 可與 session 線平行（Task 13 另需 Task 6 的 `FsOps` 寫入側先合併）；Task 16-wire 待 10、16 完成；Task 17 待 7～10、16-wire 完成；Task 18 的 Step 3～5 最後；Task 19 為 Phase gate。
4. 每個 Task 實作完成後依 master plan §1：雙審（sonnet max＋codex gpt-6.1-sol medium）→ 修正 → 本 Task 測試＋既有已綠套件＋policy_check → commit。審查者不得為該 Task 實作者。

## 2. 模型分工總表（master plan §2；測試作者 ≠ 實作者；PAL、actor／OpRunner 整合、跨 thread 佇列與生命週期一律實作 B）

| Task | 標題 | Test author model | Implementer model |
|---|---|---|---|
| 18A | 黑箱測試工具擴充（`bb_faketarget`、conftest helper） | haiku-xhigh | agy-gemini-3.8-flash-high |
| 0 | 介面凍結、模組骨架、登記與案例凍結 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 1 | Python 基線 golden 擷取 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 2 | `session_auth.rs`：golden 回歸與 Debug 遮蔽 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 3 | `login_fsm.rs`：全平台 Login FSM | haiku-xhigh | codex-gpt-6-luna-max |
| 4 | `quiet_gate.rs`：Autoboot Quiet＋Ready Reconfirm | haiku-xhigh | codex-gpt-6-luna-max |
| 5 | `reprobe.rs`：readiness reprobe 與 bootloader 證據 | haiku-xhigh | codex-gpt-6-luna-max |
| 6 | `transport_stall.rs`＋PAL `UsbTopology`＋`FsOps` 寫入側增補 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 7 | Login run 協調、fence `fsm_gen`、帳密終態與收斂 | haiku-xhigh | codex-gpt-6-luna-max |
| 8 | `self_test.rs`：self-test 分類與 nonce probe 整合 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 9 | `recover.rs`：session recover／force recover | haiku-xhigh | codex-gpt-6-luna-max |
| 10 | `capture.rs`：agent log capture（reader 端 observer） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 11 | Event schema＋`pyre/redos.rs` | haiku-xhigh | agy-gemini-3.8-flash-high |
| 12 | `line_buffer.rs`＋`matcher.rs` | haiku-xhigh | agy-gemini-3.8-flash-high |
| 13 | `counter.rs`＋`registry.rs`＋`event_log.rs`（Rust） | haiku-xhigh | agy-gemini-3.8-flash-high |
| 13-py | Python DF-18（`os.rename` → `os.replace`） | agy-gemini-3.8-flash-high | haiku-xhigh |
| 14 | PAL `HandlerSpawner`（POSIX／Windows，SC-26） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 15 | `dispatcher.rs` | haiku-xhigh | agy-gemini-3.8-flash-high |
| 16 | EventEngine 組裝與 10 個 `event.*` RPC | haiku-xhigh | agy-gemini-3.8-flash-high |
| 16-wire | reader `RxObserver` 接線（DF-15）、`BridgeQueries`、daemon_main 啟停順序 | haiku-xhigh | codex-gpt-6-luna-max |
| 17 | session RPC 接線（self_test／recover／attach／log_*） | agy-gemini-3.8-flash-high | haiku-xhigh |
| 18 | L1.5 黑箱契約、§11.3 不變量、延遲與背景雜訊、func-test、A/B | agy-gemini-3.8-flash-high | haiku-xhigh |
| 19 | Windows 原生 L1／COM 證據與 Phase gate | agy-gemini-3.8-flash-high | haiku-xhigh |

---

## Task 0：介面凍結、模組骨架、登記與案例凍結

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p7-skeleton.md`（`### Added`：Slice 2.3 模組骨架與介面凍結；登記 SC-30、DF-18；Slice 2.3 必要案例凍結）
**BLOCKED-OWNER:** 無。**BLOCKED-UPSTREAM 條件**：§0.3 列出的 P3 `pycompat` 函式或 P6 `invalidate_human_input`／`input_epoch` 尚未合併 → 停止並回報，不在 P7 補。

**Files:**
- Modify: `docs/rust-refactor/plans/rust-architecture.md`（§1.3 新增 `reprobe.rs`、`login_run.rs`、`recover.rs`、`transport_stall.rs`；§3.1 新增 `FORCE_RECOVER_POLLS=10`、`FORCE_RECOVER_POLL_S=1.0`；§4.1 `FsOps` 寫入側增補；§4.9 增 `HandlerSpawner`；§4.3 增 `UsbTopology`；§4 `Platform` 增 `handlers`、`usb` 欄位；§5.3 刪除「(c) quiet 被清除」作為 `fsm_gen` 遞增點（理由：console unlock run 跨 quiet 清除仍須有效；清除後的過期 `BootQuietExpired` timer 由 `until` 比對自然失效），並加註 `fsm_gen` 亦進 `TxPermit`；§5.4 `TxPermit.fsm_gen`、`FenceState.{fsm_gen_floor, auto_probe_blocked}`、`FenceReject::StaleFsmGen` 與規則表（floor 規則見本計畫 Task 7）；§5.8 `append` 簽章改為 06 T7 的 `WalAppendReq`；§5.10 不新增列（capture 不經佇列）；§6.1 停止順序在 step 1 後插入「EventEngine.stop(剩餘預算)」）
- Modify: `docs/rust-refactor/plans/06-slice21-daemon-core.md`（只改介面宣告，不改既有測試語意：T9 `TxPermit`／`FenceState`／`FenceReject` 增補；T10 `RunCancel`、`RxShared::{wait_for_regex_cancellable, last_rx_seq}`、`RxObserver`、`ReaderCtx.observers`；T11 `Effect::{CancelLogin, CaptureOpen, CaptureClose}`、`StartLogin.kind`、`ControlEvent::{LoginFinished.report, CaptureOpened, CaptureClosed}`、`ManagerNote::RequestAttach`（若缺）；T13 `LoginIo`（`cancel`、`class`、`tx` 回 `TxReport`）、`LoginOutcome::{Aborted, Raised}`；並在檔首加「P7 修訂」註記指向本計畫 §0.6）
- Modify: `docs/rust-refactor/plans/spec-reconciliation.md`（§1.1 增 SC-30、§1.2 增 DF-18、SC-11 列補 Event Engine 細化、§5 統計）
- Modify: `contracts/behavior_changes.yaml`（`changes` 增 SC-30、DF-18 兩筆，依 §0.5 欄位；遞增 `revision`）
- Create: `tests/acceptance/breaker/slice_2.3.json`（凍結快照）、`tests/acceptance/breaker/slice_2.3.snapshots.ndjson`（空檔）：以 06 Task 0b `scripts/rust/freeze_slice_cases.py`（不重建）在 `slice_2.3` 集合凍結的同一 commit 凍結斷路器分母與觀測窗口起訖（規格 §12.2、OD-02／OD-13）；Task 19 Phase gate 以 `slice_breaker.py --freeze tests/acceptance/breaker/slice_2.3.json --snapshots …` 判定，觸發即停止擴大部署並退守 Stage 1
- Modify: `tests/test_contracts_catalog.py`（`test_bc_real_sc_df_ranges`：SC 期望改 `({f"SC-{i:02d}" for i in range(1, 32)} | {"SC-34", "SC-35", "SC-36"}) - {"SC-08"}`（P6 已登記 SC-28／29／31，07 Task 0 下游通知；SC-34／SC-35 由 P1-OC 登記；SC-36（OD-15 Windows 安裝選用 com0com 元件，Rust-only）由 P4 Task 19 登記、已在 rust_only 名單；SC-08 依 OD-14 #3 撤回）、DF 改 `range(1, 20)`（DF-19 已由 P0 登記、P1 T20 實作，R5-F2；本 Task 補上 DF-18 後區間連續）；若 P6 尚未合併，本 Task 只在 P6 合併後執行此項）
- Modify: `tests/acceptance/cases.yaml`、`tests/acceptance/cases.lock.json`（以 `regen_lock.py --add-only` 產生；不得手改鎖檔）
- Modify: `crates/sw-core/src/{login_fsm,quiet_gate,self_test,capture,session_core,tx_writer,uart_bridge,op_runner}.rs`（只追加新公開項，既有函式本體不動）
- Create: `crates/sw-core/src/{login_run,reprobe,transport_stall,recover,mono}.rs`
- Create: `crates/sw-core/src/event_engine/{mod,schema,matcher,line_buffer,counter,registry,dispatcher,event_log}.rs`
- Create: `crates/sw-proto/src/pyre/redos.rs`
- Modify: `crates/sw-proto/src/constants.rs`（`FORCE_RECOVER_POLLS`、`FORCE_RECOVER_POLL_S`）
- Modify: `crates/sw-pal/src/{traits.rs,process.rs,device_source.rs,fs_ops.rs,lib.rs}`、`crates/sw-pal/src/fakes/{process.rs,usb.rs}`（新增 fake）
- Create: `crates/sw-core/tests/p7_skeleton.rs`
- Create: `tests/test_p7_skeleton_boundary.py`

**Interfaces（Produces，本 Task 凍結，後續 Task 只填本體）：**

```rust
// crates/sw-pal/src/process.rs
pub struct HandlerSpec { pub argv: Vec<String>, pub env: Vec<(String, String)>, pub stdin: Vec<u8>,
                         pub timeout: std::time::Duration, pub tail_bytes: usize }
#[derive(Debug)]
pub enum HandlerOutcome { Completed { exit_code: i32, stdout_tail: Vec<u8>, stderr_tail: Vec<u8> }, TimedOut, SpawnFailed(PalError) }
pub trait HandlerSpawner: Send + Sync {
    fn shell_argv(&self, shell: &str) -> Vec<String>;            // POSIX ["/bin/sh","-c",shell]；Windows ["cmd.exe","/d","/s","/c",shell]
    fn inherited_env_keys(&self) -> &'static [&'static str];     // POSIX PATH,HOME,USER,LANG,LC_ALL；Windows 再加 SC-26 六鍵
    fn run(&self, spec: &HandlerSpec, abort: &HandlerAbort) -> HandlerOutcome;   // abort：關機時由 dispatcher 觸發，視同逾時終止
}
#[derive(Clone)] pub struct HandlerAbort(/* Arc<AtomicBool>＋喚醒 */); impl HandlerAbort { pub fn new() -> Self; pub fn trigger(&self); }
// crates/sw-pal/src/device_source/mod.rs（P4 05 Task 1 建立的目錄模組）
pub trait UsbTopology: Send + Sync { fn usb_busid(&self, real_path: &str) -> Option<String>; }
// crates/sw-pal/src/fs_ops.rs（FsOps 寫入側增補；讀取側沿用 P3 `FsRead::{read, list_dir}`，P4 05 Task 1 已交付 `mkdirs`／`write_file`／`replace`／`remove_file(..)->PalResult<()>`，皆沿用、不重複定義）
fn open_append(&self, p: &Path) -> PalResult<Box<dyn AppendFile>>; // open(p,"a"/"ab")；若 06 T14 已為 capture 加入同名方法則沿用，不重複；錯誤 py_message 照 str(OSError)，含 ": '<path>'"
fn create_truncate(&self, p: &Path, bytes: &[u8]) -> PalResult<()>; // open(p,"w") 截斷後寫入並 close（counter／registry 的 `<path>.tmp`；殘留 tmp 必須被覆寫，不得續寫）
fn is_file(&self, p: &Path) -> bool;                               // os.path.isfile
fn file_size(&self, p: &Path) -> PalResult<Option<u64>>;           // None＝不存在
fn remove_file_if_exists(&self, p: &Path) -> PalResult<bool>;      // false＝不存在（P4 `remove_file` 簽章不變）
pub trait AppendFile: Send { fn write_all_flush(&mut self, b: &[u8]) -> PalResult<()>; fn sync(&mut self) -> PalResult<()>; }
// Platform 增欄位：pub handlers: Arc<dyn HandlerSpawner>, pub usb: Arc<dyn UsbTopology>
// crates/sw-core/src/mono.rs
pub fn from_secs_f64(s: f64) -> crate::Mono;  pub fn as_secs_f64(m: crate::Mono) -> f64;
```

sw-core／sw-proto 各檔**新增**的公開簽章即本計畫 Task 2～Task 16 Interfaces 區塊中標「新增」者；Task 0 將其以 `todo!("P7 Task N")` 本體落地。06／07 既有公開項（§0.6「已有」欄）保持原實作與原測試綠燈。

**案例凍結（§11.3）：** 依 P2 `cases.schema.json` 欄位（`case_id`、`contract`、`invariants`、`layer`、`platforms`、`capabilities`、`daemon_slice: "2.3"`、`prerequisites`、`necessity`、`issues`、`behavior_changes`、`expected`、`evidence`）登記：Task 18 表的全部 L1.5 案例（id＝`p7-<檔名去 test_p7_>-<測試名>`）、Task 18 的 §11.3 不變量案例、func-test `brcm-01-login-flow`／`ev-01-spawn-handler`（外部 bridge 模式）、F9 六案／F10 兩案／F11 兩案（已在 P2 清單者只核對 `daemon_slice`）、realhw `p0-*`、`p1-rst-reboot`、`p1-rst-bootwindow`、`p1-rst-recover`、remote tier 八案、Windows `cap_windows_com__p7_*` 三案（Task 19）。SC-30 案例的 `case_id` 必須等於 `behavior_changes.yaml` 的 `sc30-quiet-mid-login-aborts`（P0：cases.yaml 與 behavior_changes 共用 case_ids），`behavior_changes: [SC-30]`；DF-18 案例 `df18-event-replace-overwrite`、`[DF-18]`。

- [ ] **Step 1：寫失敗測試（骨架存在性、邊界、登記、凍結）**

```rust
// crates/sw-core/tests/p7_skeleton.rs —— 只綁定函式指標與型別名，不呼叫任何 todo! 本體
use sw_core::{login_fsm, quiet_gate, reprobe, transport_stall, self_test, recover, capture, login_run, mono, tx_writer};
use sw_core::event_engine::{schema, matcher, line_buffer, counter, registry, dispatcher, event_log, EventEngine};

#[test]
fn p7_public_items_are_reachable() {
    let _: fn(&str) -> bool = login_fsm::is_prompt_timeout_error;
    let _: fn(&str, &[String]) -> Option<String> = login_fsm::matches_any_bootloader_prompt;
    let _: fn(&str) -> bool = transport_stall::is_transport_refinable_error;
    let _: fn(usize) -> line_buffer::LineBuffer = line_buffer::LineBuffer::new;
    let _: fn(f64) -> sw_core::Mono = mono::from_secs_f64;
    let _ = tx_writer::FenceReject::StaleFsmGen;
    let _ = std::any::type_name::<(reprobe::ReprobeState, self_test::SelfTestFinal, recover::RecoverPlan, capture::CaptureObserver,
                                   login_run::LoginRunKind, quiet_gate::QuietState, schema::Rule, matcher::PatternMatcher,
                                   counter::Counter, registry::RuleRegistry, dispatcher::Dispatcher, event_log::EventLogger, EventEngine)>();
}
```

```python
# tests/test_p7_skeleton_boundary.py
import json, pathlib, subprocess, sys, pytest, yaml
ROOT = pathlib.Path(__file__).resolve().parents[1]

@pytest.mark.skipif(not (ROOT / "crates").is_dir(), reason="crates/ 尚未建立")
def test_p7_files_exist_and_cfg_boundary_clean():
    must = ["crates/sw-core/src/login_run.rs", "crates/sw-core/src/reprobe.rs", "crates/sw-core/src/recover.rs",
            "crates/sw-core/src/transport_stall.rs", "crates/sw-proto/src/pyre/redos.rs", "crates/sw-core/src/event_engine/dispatcher.rs"]
    for rel in must:
        assert (ROOT / rel).is_file(), rel
    r = subprocess.run([sys.executable, str(ROOT / "scripts/rust/check_cfg_boundary.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr

def test_behavior_changes_registers_sc30_df18_per_p0_schema():
    data = yaml.safe_load((ROOT / "contracts/behavior_changes.yaml").read_text(encoding="utf-8"))
    rows = {e["id"]: e for e in data["changes"]}
    assert rows["SC-30"]["scope"] == "rust_only" and rows["SC-30"]["applies_to"]["python"] is None
    assert rows["SC-30"]["applies_to"]["rust"] == {"stage": "slice-2.3"}
    assert rows["DF-18"]["scope"] == "both" and rows["DF-18"]["case_ids"] == ["df18-event-replace-overwrite"]
    assert "SC-28" not in rows or "SERIALWRAP_CONSOLE_BACKEND" in rows["SC-28"]["title"] + rows["SC-28"].get("semantics", ""), "SC-28 屬 P6"

def test_slice23_cases_frozen_before_implementation():
    lock = json.loads((ROOT / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8"))
    s23 = set(lock["sets"]["slice_2.3"])
    for cid in ["sc30-quiet-mid-login-aborts", "df18-event-replace-overwrite", "p7-login-unresolved-credentials-terminal-zero-empty-secret-tx",
                "brcm-01-login-flow", "ev-01-spawn-handler", "f10-unresolved-creds-terminal", "p1-rst-recover"]:
        assert cid in s23, cid
    r = subprocess.run([sys.executable, str(ROOT / "tests/acceptance/regen_lock.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
```

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-core --test p7_skeleton`（未定義符號編譯失敗）；`python3 -m pytest -q tests/test_p7_skeleton_boundary.py`（檔案不存在、YAML 無 SC-30、鎖檔無 2.3 新案例）。另確認 `python3 -m pytest -q tests/test_contracts_catalog.py::test_bc_real_sc_df_ranges` 在登記後、改範圍前會 FAIL（證明範圍斷言有效）。
- [ ] **Step 3：實作**：
  1. 核對 §0.3 每一列的實際簽章；不一致時先改本計畫 §0.3 與 producer 計畫／arch，再繼續（不得在業務碼內做名稱別名）。
  2. 只為新增公開項建立型別與函式，本體 `todo!("P7 Task N")`；06 修訂的 struct 欄位（`TxPermit.fsm_gen` 等）以預設值落地，使 06 既有測試維持綠燈（`fsm_gen: None` 時 fence 不檢查；`auto_probe_blocked` 在 Task 4 前恆等於 `quiet_active`，保持 06 的擋法）；`LoginIo.tx` 改回 `TxReport` 等簽章變更，只同步修改 06 測試 support 中建構 `LoginIo` 的 helper 型別，不改任何 06 斷言。`event_engine/mod.rs` `pub mod` 全部子模組。
  3. PAL：`HandlerSpawner`／`UsbTopology` 的 POSIX／Windows 實作檔先放 `todo!`；`fakes/process.rs` 提供 `FakeHandlerSpawner { script: Mutex<VecDeque<HandlerOutcome>>, calls: Mutex<Vec<HandlerSpec>> }`；`fakes/usb.rs` 提供 `FakeUsb(Option<String>)`。
  4. 更新 arch、06 介面宣告、調和表、`contracts/behavior_changes.yaml`（§0.5）、`tests/test_contracts_catalog.py` 範圍；`cases.yaml` 登記後 `python3 tests/acceptance/regen_lock.py --add-only`。
- [ ] **Step 4：執行，預期 PASS**：上兩條命令；`python3 -m pytest -q tests/test_contracts_catalog.py tests/test_acceptance_cases.py tests/test_slice21_scope.py`；`cargo test --workspace --features test-fakes`（06／07 既有測試全綠，證明骨架沒有覆寫 P5 實作）；`python3 scripts/rust/check_cfg_boundary.py` exit 0；`cargo build --workspace --target x86_64-pc-windows-gnu` 成功。
- [ ] **Step 5：commit**：`chore(rust-p7): 凍結 Slice 2.3 介面、登記 SC-30 與 DF-18 並凍結案例集合`

---

## Task 1：Python 基線 golden 擷取

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p7-goldens.md`（`### Added`：Slice 2.3 golden 產生器與 `contracts/golden/p7/`）
**BLOCKED-OWNER:** 無（golden 以 Python 純函式與記憶體 fake 產生，不需實機）。
**前置條件：** P1 已完成 DF-01、DF-08、DF-09、DF-15、SC-03 的 Python 修正，且 P1-OC（`02b-stage0-owner-confirmations.md`）已完成 SC-34、SC-35 的 Python 修正（golden 必須取自修正後基線；`self_test_responses`、`recover_responses`、`gate_responses`、`login_fsm_scenarios` 含 SC-34 的 `last_error`／`last_error_detail` 欄位與 SC-35 的 passthrough 列）。

**Files:**
- Create: `scripts/rust/capture_p7_goldens.py`（可 import `sw_core`；這是基線擷取工具，不是黑箱測試）
- Create: `scripts/rust/p7_fake_bridge.py`（Python 版 scripted fake，語意與 Rust 測試支援 `LoginRig`（以 06 `LoginIo`＋`RxShared`＋`FakeClock`＋腳本化 tx 閉包組成）共用同一 DSL）
- Create: `contracts/golden/p7/{env_file,auth_resolution,login_fsm_scenarios,quiet_rx,gate_responses,reprobe_decisions,reprobe_valid,transport_stall,self_test_responses,recover_responses,log_responses,event_schema,redos,line_buffer,event_payload,event_log_tail,event_engine_order,registry_load,counter_file}.ndjson`
- Create: `tests/test_p7_goldens_fresh.py`

**Interfaces：**

```python
# scripts/rust/capture_p7_goldens.py
def main(out_dir: str = "contracts/golden/p7", check: bool = False) -> int: ...
# 每個 golden 檔：一行一筆 JSON（json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))），依案例 id 排序
# scripts/rust/p7_fake_bridge.py
class ScriptedBridge:
    """與 Rust LoginRig 同 DSL：
    {"initial_rx": str, "reactions": [{"on": "eq"|"prefix"|"secret"|"any", "match": str, "rx": str}],
     "nonces": [str], "rx_bytes_10s": int, "idle_s": float, "env": {str: str}}
    wait_for_regex：先 re.compile（無效 regex 照 Python 立即拋 re.error，golden 記 {"raised": "error"}）；對目前整個 RX 文字 re.search；未命中則虛擬時鐘 += timeout 並回 False。
    send_command(cmd)：記錄 (source, (cmd+"\\n").encode(), log=True) 並依第一個符合的 reaction 附加 rx（`{nonce}` 代換為最近一次 nonce）。
    send_secret(s)：記錄 ("system:secret", payload, log=False)，觸發 on="secret" reaction。
    """
```

DSL 擴充欄位：reaction 可帶 `"after_s": float`（RX 在虛擬時間經過該秒數後才出現，用於 console unlock idle 與延遲情境）；`rx_idle_s` 以「虛擬現在 − 最後一次 RX 出現時刻」計算。產生器以 `unittest.mock.patch.object(login_fsm.time, "monotonic"/"sleep")`、`patch.object(login_fsm.uuid, "uuid4")`、`patch.dict(os.environ, case["env"], clear=True)` 驅動 Python `login_fsm`。

- [ ] **Step 1：寫失敗測試**

```python
# tests/test_p7_goldens_fresh.py
import json, pathlib, subprocess, sys, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "contracts/golden/p7"
NAMES = ["env_file", "auth_resolution", "login_fsm_scenarios", "quiet_rx", "gate_responses", "reprobe_decisions",
         "reprobe_valid", "transport_stall", "self_test_responses", "recover_responses", "log_responses", "event_schema",
         "redos", "line_buffer", "event_payload", "event_log_tail", "event_engine_order", "registry_load", "counter_file"]

def test_goldens_exist_and_are_fresh():
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run([sys.executable, str(ROOT / "scripts/rust/capture_p7_goldens.py"), "--out", td],
                           capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 0, r.stderr
        for name in NAMES:
            fresh = (pathlib.Path(td) / f"{name}.ndjson").read_bytes()
            assert (GOLDEN / f"{name}.ndjson").read_bytes() == fresh, f"{name} 與現行 Python 基線不一致，請重新擷取並審閱差異"

def test_login_fsm_golden_covers_every_platform_and_error():
    rows = [json.loads(l) for l in (GOLDEN / "login_fsm_scenarios.ndjson").read_text(encoding="utf-8").splitlines()]
    platforms = {r["case"]["profile"]["platform"] for r in rows}
    assert {"shell", "bcm", "prpl", "passthrough"} <= platforms
    errs = {r["result"][1] for r in rows}
    assert {None, "LOGIN_REQUIRED", "PROMPT_UNAVAILABLE", "LOGIN_PROMPT_TIMEOUT", "LOGIN_USER_REQUIRED", "PASS_ENV_REQUIRED",
            "PASS_ENV_MISSING", "BCM_PROMPT_TIMEOUT", "SHELL_PROMPT_TIMEOUT", "PRPL_PROMPT_TIMEOUT", "POST_LOGIN_CMD_TIMEOUT",
            "READY_NONCE_TIMEOUT", "READY_PROMPT_TIMEOUT", "RX_FLOOD", "CREDENTIALS_UNRESOLVED",
            "CONSOLE_UNLOCK_IDLE_TIMEOUT", "CONSOLE_UNLOCK_BOOTLOADER", "CONSOLE_UNLOCK_PROMPT_TIMEOUT"} <= errs
    assert "USER_ENV_MISSING" not in errs   # X3：不可達
    assert any(r.get("raised") == "error" for r in rows), "無效 regex 須照 Python 拋 re.error"

def test_event_engine_order_golden_distinguishes_start_and_rule_set():
    rows = {r["id"]: r for r in map(json.loads, (GOLDEN / "event_engine_order.ndjson").read_text(encoding="utf-8").splitlines())}
    assert rows["event_engine_order:rule_set"]["types"][:2] == ["com_enabled", "rule_loaded"]   # engine.py:107-114
    assert rows["event_engine_order:start"]["types"][:2] == ["rule_loaded", "com_enabled"]
```

- [ ] **Step 2：執行，預期 FAIL**：`python3 -m pytest -q tests/test_p7_goldens_fresh.py`（產生器不存在）。
- [ ] **Step 3：實作**：產生器各檔案例清單（每檔至少下列案例；案例 id 為 `<file>:<nnn>`）：

| golden | 案例來源與欄位 |
|---|---|
| `env_file` | 位元組輸入（base64）：空行、`#` 註解、`export ` 前綴、無 `=`、空 key、首尾同引號（`'`／`"`）、不成對引號、長度 1 的 `"`、值含 `=`、`\r\n`、單獨 `\r`（universal newline）、行首尾 Unicode 空白（`　`、`\x1c`）、UTF-8 BOM（保留在第一個 key）、非法 UTF-8（→ `{"error":"unreadable"}`）、重複 key（後者覆蓋、保留首次位置） → `{"pairs":[[k,v],…]}` |
| `auth_resolution` | 組合：`user_env`／`pass_env`／`env_file`（存在／不存在／不可讀）／`console_unlock_env`／`username` 欄位／`os.environ` → `{"username","password_set","console_unlock_set","reason","env_file_path","warnings":[log 訊息模板]}`；相對 `env_file` 以 cwd 解析；每列帶 `layer`：`auth`（`resolve_session_auth`）或 `login_user`；`login_user` 列為 login FSM 層的使用者解析 `_resolve_login_user`：`auth=None`、`user_env` 宣告但 env 缺、`sp.username` 有值 → `(None, "USER_ENV_MISSING")`（**不** fallback `sp.username`，`login_fsm.py:97-103`） |
| `login_fsm_scenarios` | ≥60 個 DSL 案例：四平台 × 成功／各失敗碼；`post_login_cmd` 的 login guard 與分流；console unlock 全分支；`maybe_reclassify_flood` 邊界 19999／20000；`detect_template`；預設 `login_regex` 雙反斜線（EVT-D-C1）；無效 `prompt_regex`／`login_regex`（`"("`）→ `{"raised":"error"}`（`uart_io.py:1158-1160` 立即 `re.compile`） → `{"case":…,"sent":[[source,payload_b64,log]],"result":[ok,err] 或 null,"raised":null 或 exc 型別名,"virtual_elapsed_s":float}` |
| `quiet_rx` | `(t, chunk)` 序列餵 `SessionManager._update_boot_quiet_locked`（綁定在以 `SessionRuntime` 組出的 stub 上）並在各點擷取 `{"until_rel","tail","pending","deadline_rel","attempts","failed","quiet_remaining_s","reconfirm_remaining_s"}`；含跨 chunk banner、>256 字元 chunk（#162）、bootloader 尾行阻擋解除、login／prompt 解除不清 pending、過期後 prompt 仍清 until |
| `gate_responses` | `service._resolve_session_id` 與 `_agent_gate_reject_locked`（含 SC-03 後形狀）對 READY＋quiet／pending／failed、ATTACHED non-capable、RECOVERING、FLASHING、human／agent source 的完整回應 dict（保留 key 順序：以 `json.dumps` 不排序輸出到 `"response"` 字串欄位） |
| `reprobe_decisions` | 以 `SessionManager(profiles=[], wal=_NullWal(), state_path=tmp)` 注入 session／devices 後呼叫 `_prepare_reprobe_locked(session, now)` 與 `_record_reprobe_attempt_locked`、`_finish_probe_reprobe` → `{"action","attempts","next_reprobe_at_rel","exhausted","state","pending","failed","ready_reconfirm_attempts","last_error","last_error_detail"}` |
| `reprobe_valid` | `_reprobe_target_still_valid_locked(session, bridge, now)` 每個早退分支（bridge 不同、by_id 已 release／不在 devices、state∉{ATTACHED,READY}、READY 且 expire 後非 pending、READY＋bootloader 尾行（D1：force expire、`last_error` 不動）、exhausted、ATTACHED 非 reprobe 錯誤、quiet blocks、human_active）→ `{"valid","state","pending","failed","last_error"}` |
| `transport_stall` | `classify_probe_failure` 全分支表、`transport_stall_hint(real_path, rx_age_s)` 在 busid=`"1-1"`／`"8-2.1"`／None 三種（patch `resolve_usb_busid`），rx_age_s=`30.0`、`31.04`、`31.05`、`1234.56` |
| `event_schema` | `validate_rule_dict` 每一條錯誤訊息（09 §3.1 表 #1～#25＋DF-08）、`cooldown_ms:"abc"`（ValueError 型別名）、`null`（TypeError）、bool 通過的 `max_fires:true`、`schema_version:1.0`／`True` 通過、`cooldown_ms`／`timeout_ms`／`max_fires` 為 `2**128`（超出 i128，Python 接受） → `{"ok":true,"rule":{…}}` 或 `{"ok":false,"exc_type":…,"message":…}` |
| `redos` | 09 §3.1 實測樣本＋每個 AST op（MAX/MIN/POSSESSIVE_REPEAT、BRANCH、SUBPATTERN、ATOMIC_GROUP、ASSERT、ASSERT_NOT、GROUPREF、GROUPREF_EXISTS）→ `{"pattern","flags","compile_ok","error","score","has_backref","suspicious"}`（無 re2 環境擷取；產生器若偵測到 `import re2` 成功即拒絕執行並 exit 2） |
| `line_buffer` | `LineBuffer.feed` 序列：恰 16384／16385 bytes 有換行行、無換行超長塊分次 feed、`\r\n`、截斷塊不剝 `\r`、非法 UTF-8、ANSI（CSI、OSC-BEL、OSC-ST、單字元 ESC、`ESC ( B`） → `{"feeds":[b64…],"outputs":[[…],…]}` |
| `event_payload` | `build_payload`＋stdin bytes（b64）、`build_env`（patch `os.environ` 為固定集合）含 4096／4097 字元 matched_text |
| `event_log_tail` | 寫入事件序列後 `tail(rule_id, selector, since_ts, n)`：`n=None/0/3/-2/True`、`since_ts` 邊界、壞 JSON 行略過 |
| `registry_load` | 目錄內容（檔名→bytes）→ `load_all()` 的 `rules`（rule_id 清單）與 `failed`（`[basename, reason]`），含 JSONDecodeError 各訊息、`schema_version` 錯、DF-09 後 `ValueError`／`TypeError`、非法 UTF-8 |
| `counter_file` | `CounterStore.save` 後檔案位元組（b64）與 `load` 對壞檔／缺檔回 `Counter()` |
| `self_test_responses` | 以 `SessionManager`＋stub bridge／devices 驅動 `self_test` 每個分類分支（含 `HUMAN_INTERACTIVE_ACTIVE`、RELEASED 有／無 holder、passthrough fallback suggest、SC-35 passthrough ATTACHED（含 `passthrough_ready_in_s`）與 passthrough READY（`PASSTHROUGH`／`none`、零 TX）、nonce probe 三結果）→ `{"response": json.dumps(不排序)}`，保留完整 key 順序 |
| `recover_responses` | 同法驅動 `recover_session`：RELEASED、FLASHING_BUSY、REATTACH、SESSION_NOT_READY、ATTACHED REPROBE 成功／失敗（含 bootloader 分類；SC-34 `error_code:"SESSION_NOT_READY"`＋`last_error`／`last_error_detail`）、SC-35 passthrough ATTACHED REPROBE（零 TX）、ATTACHED＋force 逾時（`FORCE_RECOVER_TIMEOUT`）、READY CTRL_C 成功、READY 兩鍵失敗＋自動 force 成功／失敗（`PROMPT_TIMEOUT`、`recovery_action:"NONE"`）→ `{"response","virtual_elapsed_s"}`（patch `time.sleep`） |
| `log_responses` | `log_start`（新建／already_active／LOG_OPEN_FAILED）、`log_stop`（有／無 capture）、`log_status`（有／無）→ `{"response"}`，`capture_id`／時間欄位以固定 patch 值 |
| `event_engine_order` | 以 stub BridgeQueries 驅動 `EventEngine.start()`、`rule_set()`、`reload()` 後讀 events.ndjson 的 `type` 序列（`rule_set` 路徑為 `com_enabled` 在 `rule_loaded` 之前；`start` 為 `rule_loaded` 在前） → `{"id","types","triggered_by"}` |

- [ ] **Step 4：執行，預期 PASS**：`python3 scripts/rust/capture_p7_goldens.py --out contracts/golden/p7`；`python3 -m pytest -q tests/test_p7_goldens_fresh.py`。
- [ ] **Step 5：commit**：`test(rust-p7): 擷取 Slice 2.3 Python 基線 golden`

---

## Task 2：`session_auth.rs`：golden 回歸與 Debug 遮蔽

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p7-session-auth.md`（`### Added`：帳密解析對 Python 基線的 golden 鎖定；`SessionAuth` 除錯輸出遮蔽密碼）
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-core/src/session_auth.rs`（06 T13 既有；只加 `impl Debug`，golden 不符時修正既有本體）
- Create: `crates/sw-core/tests/session_auth_golden.rs`

**Interfaces（06 T13 既有，沿用）：** `SessionAuth { username, password, console_unlock: Option<String> }`、`AuthResolution { reason: &'static str, env_file_path: Option<String> }`、`parse_env_file(text: &str) -> IndexMap<String, String>`、`resolve_session_auth(p, env, read: &dyn Fn(&str) -> Option<std::io::Result<String>>) -> (SessionAuth, AuthResolution)`、`credentials_declared_but_unresolved`。

**新增：** `impl std::fmt::Debug for SessionAuth`（`username` 照印；`password`／`console_unlock` 有值時輸出 `Some(<redacted>)`）。

規則（逐字照 05 §9、`auth.py:53-144`，用以核對既有本體）：`env_file` 以 `SysEnv::expanduser` 展開（相對路徑以 daemon cwd 解析，不做 abspath）；`read` 回 `None`＝檔不存在 → `env_file_missing`＋WARNING `env_file 不存在: {path}（session {com}）`；回 `Some(Err(_))`（含 UTF-8 解碼失敗 `InvalidData`）→ `env_file_unreadable`＋WARNING `無法解析 env_file: {path}（session {com}）`；`parse_env_file` 以 P3 `universal_newlines` 後依 `\n` 切行（等同 Python text-mode 迭代；**不可**用 `splitlines`，後者會在 `\x1c` 等字元斷行）→ `py_strip` → 空或 `#` 開頭略過 → `strip_prefix("export ")` 後再 `py_strip` → `partition('=')`（無 `=` 略過）→ key `py_strip`（空略過）→ value `py_strip` → 長度 ≥2 且首尾同為 `'` 或 `"` 去一層 → 插入（重複 key 覆蓋值、保留首次位置）；`username = local[user_env] or environ[user_env]`（空字串視為缺），仍空且 `sp.username` 非空 → `sp.username`（此 fallback 只屬 `auth.py`；login FSM 層的 `_resolve_login_user` 不 fallback，見 Task 3）；`password`、`console_unlock` 同法（不 fallback）；`declared = user_env or pass_env or env_file or console_unlock_env`；判定順序 not_configured → ok（`console_unlock` 若宣告 console_unlock_env，否則 `username and password`）→ missing → unreadable → key_absent。

- [ ] **Step 1：寫測試**

```rust
// crates/sw-core/tests/session_auth_golden.rs
mod support; use support::{golden_rows, FakeEnv, profile_from_json, fake_read};
use sw_core::session_auth::*;

#[test]   // 回歸鎖定（06 T13 已實作；預期 PASS）
fn parse_env_file_matches_python_golden() {
    for row in golden_rows("env_file") {
        let input = base64_decode(row["input_b64"].as_str().unwrap());
        match String::from_utf8(input) {
            Ok(text) => {
                let got: Vec<(String, String)> = parse_env_file(&text).into_iter().collect();
                assert_eq!(got, row.pairs("pairs"), "case {}", row.id());
            }
            Err(_) => assert_eq!(row["error"], "unreadable", "case {}", row.id()),
        }
    }
}

#[test]   // 回歸鎖定
fn resolve_session_auth_matches_python_golden_including_warnings() {
    for row in golden_rows("auth_resolution").into_iter().filter(|r| r.str("layer") == "auth") {
        let sp = profile_from_json(&row["profile"]);
        let env = FakeEnv::from_json(&row["environ"]).with_cwd(row.str("cwd"));
        let read = fake_read(&row["files"]);                      // 非法 UTF-8 檔 → Some(Err(InvalidData))
        let (auth, res) = resolve_session_auth(&sp, &env, &read);
        assert_eq!(auth.username.as_deref(), row.opt_str("username"), "case {}", row.id());
        assert_eq!(auth.password.is_some(), row.bool("password_set"));
        assert_eq!(auth.console_unlock.is_some(), row.bool("console_unlock_set"));
        assert_eq!(res.reason, row.str("reason"));
        assert_eq!(res.env_file_path.as_deref(), row.opt_str("env_file_path"));
        assert_eq!(support::captured_warnings(), row.str_vec("warnings"));
    }
}

#[test]   // 新增行為（RED）
fn debug_output_redacts_secrets() {
    let a = SessionAuth { username: Some("u".into()), password: Some("hunter2".into()), console_unlock: Some("k3y".into()) };
    let s = format!("{a:?}");
    assert!(s.contains("\"u\"") && !s.contains("hunter2") && !s.contains("k3y"), "{s}");
}
```

- [ ] **Step 2：執行**：`cargo test -p sw-core --features test-fakes --test session_auth_golden` → `debug_output_redacts_secrets` 編譯失敗（無 `Debug`）＝RED；另兩個回歸鎖定測試在補上 `Debug` 後應 PASS，若 FAIL 記為 P5 缺陷。
- [ ] **Step 3：實作**：加 `impl Debug`；golden 回歸若 FAIL，依上段規則修正 06 既有本體（不改簽章）。
- [ ] **Step 4：執行，預期 PASS**：Step 2 命令；`cargo test -p sw-core --features test-fakes --test login_quiet`（06 T13 既有測試不退化）。
- [ ] **Step 5：commit**：`feat(rust-auth): 以 golden 鎖定帳密解析並遮蔽除錯輸出`

---

## Task 3：`login_fsm.rs`：全平台 Login FSM

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p7-login-fsm.md`（`### Added`：Rust 全平台 Login FSM（bcm、post_login_cmd、console unlock）與 RX 洪水反分類；`### Removed`：Slice 2.1 不支援 profile 能力閘）
**BLOCKED-OWNER:** 真實 Broadcom（bcm）、prplOS（prpl）、OP3（shell）、smart4R（console unlock）實機登入各一次（L2 F9／F10 的前置；見 §BLOCKED-OWNER 總表 B-01）。

**Files:**
- Modify: `crates/sw-core/src/login_fsm.rs`（06 T13 既有）、`crates/sw-core/src/quiet_gate.rs`（移除 `slice21_unsupported_feature`）、`tests/acceptance/slice21_scope.yaml`（`unsupported_profile_features: []`）、`tests/test_slice21_scope.py::test_unsupported_profile_features_are_declared`（期望改 `[]`）
- Modify: `crates/sw-core/tests/login_quiet.rs`（06 T13：`unsupported_profile_no_auto_tx` 改寫為 `post_login_cmd_profile_logs_in`，見 §0.6 退役清單）
- Modify: `crates/sw-core/src/session_manager.rs`、`crates/sw-core/tests/session_manager.rs`（06 T16：detect 不再排除 `post_login_cmd` 模板；測試改名，見 §0.6 退役清單）
- Create: `crates/sw-core/tests/support/login_rig.rs`（`support/mod.rs` re-export）
- Create: `crates/sw-core/tests/login_fsm_full.rs`

**Interfaces：**

```rust
// crates/sw-core/src/login_fsm.rs —— 06 T13 既有：LoginPlan、choose_login_plan、probe_ready、ensure_ready、prompt_timeout_error（沿用簽章）
// P7 修訂（06 T13 介面同步改，Task 0 已宣告）：
pub struct LoginIo<'a> { pub rx: &'a RxShared, pub tx: &'a dyn Fn(&[u8], TxClass, &str, bool) -> TxReport,   // 原回 TxResult；改回 06 T9 TxReport 以取得 reject
                         pub clock: &'a dyn Clock, pub nonce: &'a dyn Fn() -> String,
                         pub cancel: &'a RunCancel,          // 新增：SC-30 取消
                         pub class: TxClass }                // 新增：LoginProbe | ReadyReconfirm（呼叫端決定）
#[derive(Debug, Clone, PartialEq)]
pub enum LoginOutcome { Ready, NotReady(String), Failed(String),   // 06 既有
                        Aborted(LoginAbort),                         // 新增：TX 被拒／取消／結果不完整，之後零 TX
                        Raised(String) }                             // 新增：Python 會拋例外處（無效 regex → "error"），值為例外型別名
#[derive(Debug, Clone, PartialEq)]
pub enum LoginAbort { Refused(FenceReject), Cancelled, TxIncomplete }
// 新增
pub const LOGIN_FSM_DETAIL_ERRORS: [&str; 12];                       // 05 §8.3 的 12 個值
pub fn is_prompt_timeout_error(err: &str) -> bool;                   // ends_with("_PROMPT_TIMEOUT")
pub fn matches_login_or_password(text: &str, sp: &SessionProfile) -> bool;
pub fn maybe_reclassify_flood(io: &LoginIo, err: Option<String>) -> Option<String>;
pub fn detect_template<'t>(io: &LoginIo, templates: &'t [ProfileTemplate], probe_timeout_s: f64) -> Result<Option<&'t ProfileTemplate>, LoginOutcome>;
pub fn matches_any_bootloader_prompt(text: &str, prompts: &[String]) -> Option<String>;   // session_manager._matches_any_bootloader_prompt：pycompat::splitlines 取最後一個非空白行；無效 regex 略過
pub fn console_at_bootloader(rx_tail: &str, prompts: &[String]) -> Result<bool, LoginOutcome>; // login_fsm._console_at_bootloader：尾行＝clean_text(tail).rstrip("\r\n").split("\n")[-1]；無效 regex → Err(Raised("error"))
```

`tx` 回報的對應（§11.3「不得自動重送」）：`reject: Some(r)` → `Aborted(Refused(r))`；`NotSubmitted` 且無 reject → `Aborted(Cancelled)`；`Completed(n)` 且 `n < len` 或 `Unknown` → `Aborted(TxIncomplete)`（例如 secret 已部分寫出，該 run 不重送、也不得接著等 prompt）；`Completed(len)` → 繼續。任何 `Aborted`／`Raised` 之後不再呼叫 `tx`。

- [ ] **Step 1：寫失敗測試**

`crates/sw-core/tests/support/login_rig.rs`：`LoginRig` 以 `RxShared`＋`FakeClock`＋腳本化 tx 閉包組出 06 `LoginIo`，DSL 與 Task 1 `ScriptedBridge` 完全相同（`LoginRig::from_dsl(&PyValue)` 與 builder 兩種建法）；`.reject_after(n_sends, FenceReject::StaleFsmGen)`、`.partial_on(n)`（第 n 次 TX 回 `Completed(1)`）注入失敗。

```rust
// crates/sw-core/tests/login_fsm_full.rs
#![cfg(feature = "test-fakes")]
mod support; use support::{golden_rows, LoginRig, profile_from_json, auth_from_json, brcm_template, prpl_template, FakeEnv};
use sw_core::login_fsm::*; use sw_core::session_auth::SessionAuth; use sw_core::tx_writer::FenceReject;

#[test]   // 混合：shell／prpl／passthrough 列為回歸鎖定，bcm／post_login_cmd／console unlock／flood／raised 列為 RED
fn every_python_scenario_produces_identical_tx_and_result() {
    for row in golden_rows("login_fsm_scenarios") {
        let case = &row["case"];
        let sp = profile_from_json(&case["profile"]);
        let rig = LoginRig::from_dsl(case);
        let env = FakeEnv::from_json(&case["env"]);
        let got = match case.str("entry") {
            "ensure_ready" => ensure_ready(&rig.io(), &sp, &auth_from_json(&case["auth"]), &env),
            "probe_ready" => probe_ready(&rig.io(), &sp),
            other => panic!("unknown entry {other}"),
        };
        assert_eq!(rig.sent_triples(), row.sent_triples(), "case {} TX 序列", row.id());
        assert_eq!(got, row.expected_login_outcome(), "case {}", row.id());   // raised → Raised("error")；[true,null] → Ready；[false,e] → 依 entry 為 NotReady/Failed(e)
        assert!((rig.virtual_elapsed_s() - row.f64("virtual_elapsed_s")).abs() < 1e-6, "case {} 逾時累計", row.id());
    }
}

#[test]
fn bcm_two_stage_login_then_post_login_sh_then_nonce() {
    let sp = brcm_template();                                     // default.yaml 的 brcm-template（timeout_s 15、post_login_cmd "sh"）
    let rig = LoginRig::new().initial_rx("\r\nLogin: ")
        .on_eq("", "\r\nLogin: ").on_eq("admin", "Password: ").on_secret("CM> ")
        .on_eq("sh", "\r\n# ").on_prefix("echo __READY__", "__READY__{nonce}\r\n# ").nonces(["deadbeef"]);
    let auth = SessionAuth { username: Some("admin".into()), password: Some("pw".into()), console_unlock: None };
    assert_eq!(ensure_ready(&rig.io(), &sp, &auth, &FakeEnv::empty()), LoginOutcome::Ready);
    assert_eq!(rig.sent_sources(), ["system", "system", "system:secret", "system", "system"]);
    assert_eq!(rig.sent_payload(2), b"pw\n");
    assert!(!rig.sent_logged(2), "secret 不得進 WAL");
    assert_eq!(rig.sent_payload(4), b"echo __READY__deadbeef\n");
}

#[test]
fn flood_reclassification_threshold_is_inclusive_20000() {
    let sp = prpl_template();
    for (bytes, want) in [(19_999u64, "PROMPT_UNAVAILABLE"), (20_000, "RX_FLOOD")] {
        let rig = LoginRig::new().initial_rx("garbage").rx_bytes_10s(bytes);
        assert_eq!(probe_ready(&rig.io(), &sp), LoginOutcome::NotReady(want.into()));
    }
}

#[test]
fn login_user_env_missing_does_not_fall_back_to_profile_username() {
    let mut sp = prpl_template(); sp.user_env = Some("SW_U".into()); sp.username = Some("root".into()); sp.pass_env = Some("SW_P".into());
    let rig = LoginRig::new().initial_rx("\r\nlogin: ").on_eq("", "\r\nlogin: ");
    // auth 無 username、env 無 SW_U：_resolve_login_user 回 (None, USER_ENV_MISSING)，不用 sp.username（login_fsm.py:97-103）
    let out = ensure_ready(&rig.io(), &sp, &SessionAuth::default(), &FakeEnv::empty());
    assert_eq!(out, LoginOutcome::Failed("LOGIN_USER_REQUIRED".into()));
    assert!(rig.sent_payloads().iter().all(|p| p != b"root\n"), "不得送 sp.username");
}

#[test]
fn invalid_prompt_regex_raises_like_python_instead_of_timing_out() {
    let mut sp = prpl_template(); sp.prompt_regex = "(".into();
    let rig = LoginRig::new().initial_rx("root@prplOS:~# ");
    assert_eq!(probe_ready(&rig.io(), &sp), LoginOutcome::Raised("error".into()));
    assert!(rig.virtual_elapsed_s() < 1e-9, "Python 在 re.compile 立即拋出，不消耗 timeout");
}

#[test]
fn refusal_and_partial_write_abort_without_further_tx() {
    let sp = brcm_template();
    let auth = SessionAuth { username: Some("admin".into()), password: Some("pw".into()), console_unlock: None };
    let rig = LoginRig::new().initial_rx("\r\nLogin: ").on_eq("", "\r\nLogin: ").reject_after(1, FenceReject::StaleFsmGen);
    assert_eq!(ensure_ready(&rig.io(), &sp, &auth, &FakeEnv::empty()), LoginOutcome::Aborted(LoginAbort::Refused(FenceReject::StaleFsmGen)));
    assert_eq!(rig.sent_triples().len(), 1);
    let rig2 = LoginRig::new().initial_rx("\r\nLogin: ").on_eq("", "\r\nLogin: ").on_eq("admin", "Password: ").partial_on(3);
    assert_eq!(ensure_ready(&rig2.io(), &sp, &auth, &FakeEnv::empty()), LoginOutcome::Aborted(LoginAbort::TxIncomplete));
    assert_eq!(rig2.sent_triples().len(), 3, "secret 部分寫出後不重送、不再等 prompt");
}

#[test]
fn console_unlock_is_not_blocked_by_cfe_banner_history() {
    let mut sp = prpl_template(); sp.console_unlock_env = Some("SMART4R_CONSOLE_UNLOCK".into()); sp.bootloader_prompts = vec!["^CFE> $".into()];
    let rig = LoginRig::new().initial_rx("CFE version 1.0\r\n...\r\nLinux booting\r\n").idle_after(2.0).on_secret("root@smart4r:~# ")
        .on_prefix("echo __READY__", "__READY__{nonce}\r\n").nonces(["0a1b2c3d"]);
    let auth = SessionAuth { console_unlock: Some("k".into()), ..Default::default() };
    assert_eq!(ensure_ready(&rig.io(), &sp, &auth, &FakeEnv::empty()), LoginOutcome::Ready);
}
```

其餘家族以表格展開（同檔 `#[test]`）：

| 測試名 | 精確斷言 |
|---|---|
| `prompt_timeout_error_by_platform`（回歸） | `"bcm"`→`BCM_PROMPT_TIMEOUT`、`"shell"`→`SHELL_PROMPT_TIMEOUT`、`"prpl"`／`"passthrough"`／`""` 皆 `PRPL_PROMPT_TIMEOUT`（純函式回歸；SC-35 起 passthrough session 不進 Login FSM，`"passthrough"` 列只鎖函式映射，不代表 passthrough 會產生此 last_error） |
| `choose_login_plan_passthrough_is_none`（新增，SC-35） | 以 `platform="passthrough"` 搭配四種 profile 組合（`ready_probe` 為 `""`／`"echo __READY__${nonce}"`、`login_regex` 為 `"$^"`／`"(?mi)^login:\\s*$"`、有／無 `user_env`＋`pass_env`、有／無 `console_unlock_env`）呼叫 `choose_login_plan` → 一律 `LoginPlan::None`；`quiet_blocks=true` 時同樣 `LoginPlan::None`（**不**合成 `PROMPT_UNAVAILABLE`）；`rig.io()` 的 `write_calls()==0` |
| `is_prompt_timeout_error_suffix` | `err.endswith("_PROMPT_TIMEOUT")` 語意（`login_fsm.py:57`）：`BCM_PROMPT_TIMEOUT`、`SHELL_PROMPT_TIMEOUT`、`LOGIN_PROMPT_TIMEOUT`→true；`PROMPT_TIMEOUT`、`PROMPT_UNAVAILABLE`→false；`maybe_reclassify_flood` 可反分類集合＝`_FLOOD_MASKABLE_ERRORS` ∪ 後綴命中者 |
| `detect_boot_banner_cases`（回歸） | `"U-Boot 2024.04"`→true；`"u-boot"`→false；`"CFE version 1.0"`→true；`"CFE version"`→false；`"Hit any key to stop autoboot:  3"`→true；`""`→false |
| `matches_login_or_password_uses_cleaned_and_raw` | `"\x1b[1m(none) login:\x1b[0m"` 對 `(?mi)^.*login:\s*$` → true；無效 regex `"("` 略過、回 false（`matches_*` 有 try／except） |
| `default_login_regex_double_backslash_never_matches` | `SessionProfile::default().login_regex == r"(?mi)^login:\\s*$"`，對 `"login: "` 不匹配（EVT-D-C1 KEEP） |
| `post_login_guard_returns_login_required` | probe 假成功但 `rx.tail` 含 `login:` → `Failed("LOGIN_REQUIRED")` 且未送 `post_login_cmd` |
| `post_login_timeout_split` | `POST_LOGIN_CMD_TIMEOUT` 後 tail 命中 password → `LOGIN_REQUIRED`，否則 `POST_LOGIN_CMD_TIMEOUT` |
| `nonce_timeout_split` | 同上，nonce 逾時 → `LOGIN_REQUIRED` 或 `READY_NONCE_TIMEOUT` |
| `console_unlock_nonce_anchored_pattern` | console unlock＋預設 ready_probe 時等待樣式為 `(?m)^__READY__<escape(nonce)>\r?$`：只有回顯 → `READY_NONCE_TIMEOUT` |
| `user_env_missing_unreachable` | 任何案例皆不回 `USER_ENV_MISSING`（X3）：user_env 缺＋pass_env 宣告 → login prompt 後 `LOGIN_USER_REQUIRED`；無 login prompt → `LOGIN_PROMPT_TIMEOUT` |
| `password_resolution_errors` | 無 `pass_env` 且 auth 無密碼、見 password prompt → `PASS_ENV_REQUIRED`；`pass_env` 宣告但 env 缺 → `PASS_ENV_MISSING` |
| `console_unlock_idle_timeout_and_bootloader` | RX 持續到 deadline → `CONSOLE_UNLOCK_IDLE_TIMEOUT`；**尾行** `CFE> ` 命中 bootloader_prompts → `CONSOLE_UNLOCK_BOOTLOADER`；secret 後無 prompt → `CONSOLE_UNLOCK_PROMPT_TIMEOUT` |
| `console_at_bootloader_invalid_regex_raises` | `bootloader_prompts=["("]` → `Err(Raised("error"))`（與 `matches_any_bootloader_prompt` 略過不同） |
| `detect_template_skips_passthrough_and_unlock` | templates 依序 [passthrough, unlock, prpl]，snapshot 命中三者 prompt → 回 prpl；只命中 login_regex → 回第一個 login 候選；皆無 → None；`clock.sleep(3.0)` 被呼叫一次 |
| `bootloader_prompt_last_nonblank_line_uses_py_splitlines` | `"=> \n\n"` 對 `["^=> $"]` → `Some("^=> $")`；`"=> \nroot# "` → None；`"x\r=> "`（單獨 `\r` 斷行）→ `Some` |
| `cancel_ends_wait_and_sends_nothing_more` | 等 prompt 期間 `cancel.trigger()` → `Aborted(Cancelled)`，之後 tx 呼叫次數不變 |

`crates/sw-core/tests/login_quiet.rs` 的 `post_login_cmd_profile_logs_in`：`post_login_cmd:"sh"` 的 explicit target（brcm-template）→ 完整登入到 `READY`、`write_calls()>0`、stderr 無 `slice-2.1 不支援` 字樣。

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-core --features test-fakes --test login_fsm_full`（新增 variant／函式為 `todo!` 或不存在；回歸鎖定列若在本 Step 已 PASS 屬正常）；`--test login_quiet` 的 `post_login_cmd_profile_logs_in` FAIL。
- [ ] **Step 3：實作**：逐函式移植 `login_fsm.py`（全文見 `sw_core/login_fsm.py:1-367`，行為摘要 05 §8.2～§8.3），擴充 06 本體，要點：
  - `_probe_prompt`＝`rx.clear()`→`tx(b"\n", class, "system", true)`→`rx.wait_for_regex_cancellable(prompt_re, None, timeout_s, clock, cancel)`；regex 編譯在等待前做，失敗 → `Raised("error")`。
  - `_maybe_login`：`user = auth.username` → 否則 `user_env` 宣告時 `env.var(user_env)`（空字串視為缺 → `user=None`，**不**再 fallback `sp.username`；X3：`USER_ENV_MISSING` 不往外傳）→ `user_env` 未宣告時才用 `sp.username`；`needs_login = user.is_some() || sp.pass_env.is_some()`。
  - `_finalize_ready`：`post_login_cmd` 非空才送；nonce＝`(io.nonce)()`；`probe = ready_probe.replace("${nonce}", &nonce)`；console unlock 時先 clear；樣式依 console unlock 與 ready_probe 是否為字面預設 `echo __READY__${nonce}` 二選一（`pyre::escape`）。
  - `_ensure_console_unlocked`：`deadline = now+timeout_s`；`_wait_console_idle` 以 `clock.sleep(min(0.05, max(0.001, deadline-now)))` 輪詢、完全不 TX、每次輪詢檢查 `cancel`；bootloader 判定用 `console_at_bootloader`；INFO log 依 GAP-G-01 不輸出。
  - `maybe_reclassify_flood`：err ∈ {PROMPT_UNAVAILABLE, READY_NONCE_TIMEOUT, READY_PROMPT_TIMEOUT, POST_LOGIN_CMD_TIMEOUT, LOGIN_PROMPT_TIMEOUT} 或 `is_prompt_timeout_error`，且 `rx.rx_stats(now).0 >= RX_FLOOD_BYTES_PER_10S` → `RX_FLOOD`。
  - 退役：移除 `slice21_unsupported_feature` 與 `choose_login_plan` 中的 unsupported 分支；`slice21_scope.yaml` 與其測試同步。
- [ ] **Step 4：執行，預期 PASS**：`cargo test -p sw-core --features test-fakes --test login_fsm_full --test login_quiet`；`python3 -m pytest -q tests/test_slice21_scope.py`；06 其他 sw-core 測試 binary 不退化。
- [ ] **Step 5：commit**：`feat(rust-login): 承接全平台 Login FSM 與 console unlock`

---

## Task 4：`quiet_gate.rs`：Autoboot Quiet＋Ready Reconfirm

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p7-quiet-reconfirm.md`（`### Added`：Ready Reconfirm 有界化、gate 回應位元組相容、quiet arm 以 input_epoch 失效人類輸入）
**BLOCKED-OWNER:** 真實 U-Boot／CFE autoboot 倒數下的 quiet 時序（L2 F9 六案，B-02）。

**Files:**
- Modify: `crates/sw-core/src/quiet_gate.rs`（06 T13 既有）、`crates/sw-core/src/session_core.rs`（arm／clear 時同鎖更新 fence 的 `quiet_active`、`auto_probe_blocked`、`fsm_gen`；arm 呼叫 07 `invalidate_human_input`）
- Create: `crates/sw-core/tests/quiet_gate_p7.rs`、`crates/sw-core/tests/session_core_quiet_p7.rs`

**Interfaces：**

```rust
// 06 T13 既有（沿用）：QuietState{boot_quiet_until: Mono /*0＝無*/, boot_banner_tail, pending, deadline, attempts, failed}、
//   arm／clear_quiet／confirm_ready／expire(now, force)／quiet_active／remaining_s／reconfirm_remaining_s／on_rx_text → QuietTransition
// 新增
pub fn boot_quiet_blocks_probe(q: &QuietState, sp: &SessionProfile, now: Mono) -> bool;   // quiet_active && console_unlock_env.is_none()（session_manager.py:1193）
impl QuietState { pub fn agent_gate_active(&mut self, now: Mono) -> bool; }               // 先 expire(now,false)，再 quiet_active||pending
pub fn submit_gate_reject(public_session: &PyDict, source: &str) -> Option<PyDict>;     // service._resolve_session_id 的 gate 段
pub fn execute_gate_reject(q: &mut QuietState, com: &str, now: Mono) -> Option<PyDict>; // _agent_gate_reject_locked＋SC-03
pub const READY_UNCONFIRMED_HINT: &str = concat!("READY 已逾 300s／5 次未能經 nonce probe 再確認（可能卡在 bootloader）；",
                                                 "請 session self-test 取分類後 interactive-open 處理");   // 字面量；測試另以常數值核對 300／5
```

`source` 判定：`submit_gate_reject` 只對「非 daemon 內部 human console 路徑」呼叫；依 DF-02，RPC 帶入的 `human:` source 一律視同 agent（06 已實作，本 Task 只回歸）。

- [ ] **Step 1：寫測試**

```rust
// crates/sw-core/tests/quiet_gate_p7.rs
mod support; use support::{golden_rows, t, secs, profile_from_json};
use sw_core::quiet_gate::*;

#[test]   // 回歸鎖定（06 T13 已實作 on_rx_text；預期 PASS）
fn rx_sequences_match_python_golden() {
    for row in golden_rows("quiet_rx") {
        let sp = profile_from_json(&row["profile"]);
        let mut q = QuietState::default();
        let base = row.f64("base_mono");
        for step in row.list("steps") {
            let now = t(base + step.f64("t"));
            q.on_rx_text(step.str("chunk"), &sp, now);
            let until_rel = (q.boot_quiet_until != 0).then(|| secs(q.boot_quiet_until) - base);
            support::assert_opt_f64_close(until_rel, step.opt_f64("until_rel"), 1e-6, &format!("case {} t={}", row.id(), step.f64("t")));
            assert_eq!(q.boot_banner_tail, step.str("tail"));
            assert_eq!(q.pending, step.bool("pending"));
            assert_eq!(q.remaining_s(now), step.opt_f64("quiet_remaining_s"));            // py_round(…,1) 後精確相等
            assert_eq!(q.reconfirm_remaining_s(now), step.opt_f64("reconfirm_remaining_s"));
        }
    }
}

#[test]   // 新增（RED）
fn gate_responses_are_byte_identical_to_python() {
    for row in golden_rows("gate_responses") {
        let got = match row.str("layer") {
            "submit" => submit_gate_reject(&row.dict("public_session"), row.str("source")),
            "execute" => { let mut q = row.quiet_state("quiet"); execute_gate_reject(&mut q, row.str("com"), t(row.f64("now"))) }
            l => panic!("{l}"),
        };
        let got_s = got.map(|d| sw_proto::pyjson::dumps(&d.into(), &sw_proto::pyjson::RPC_WIRE));
        assert_eq!(got_s.as_deref(), row.opt_str("response"), "case {}", row.id());
    }
}

#[test]   // 新增（RED）
fn hint_constant_matches_limits_and_golden() {
    assert!(READY_UNCONFIRMED_HINT.contains(&format!("{:.0}s", sw_proto::constants::READY_RECONFIRM_MAX_S)));
    assert!(READY_UNCONFIRMED_HINT.contains(&format!("{} 次", sw_proto::constants::READY_RECONFIRM_MAX_ATTEMPTS)));
}

#[test]   // 新增（RED）
fn console_unlock_profile_is_not_probe_blocked() {
    let mut sp = support::prpl_template(); let mut q = QuietState::default(); q.arm(t(0.0));
    assert!(boot_quiet_blocks_probe(&q, &sp, t(1.0)));
    sp.console_unlock_env = Some("SMART4R_CONSOLE_UNLOCK".into());
    assert!(!boot_quiet_blocks_probe(&q, &sp, t(1.0)));
}
```

`crates/sw-core/tests/session_core_quiet_p7.rs`（SessionCore 事件序列＋FakeClock；`support::req(SessionRequest::…)` 包成 `MailboxItem::Control(ControlEvent::Request{..})`、`support::rx_chunk(core, text)` 包成 `MailboxItem::Data(DataEvent::UartRxChunk{..})`）：

| 測試名 | 精確斷言 |
|---|---|
| `banner_arm_bumps_fsm_gen_once_and_invalidates_human_input` | 一個 chunk 含 `U-Boot`：`fsm_gen` +1（同 chunk 不重複遞增）、`input_epoch` +1（07 D-02）、`owner_gen` **不變**、Effects 含 07 的 `Log` 行 `reason=boot_banner`；fence 鎖內同時為 `quiet_active=true`、`auto_probe_blocked=true`、`fence.fsm_gen_floor==core.fsm_gen`（會擋 probe 的 arm 推進 floor） |
| `rearm_uses_quiet_rearm_reason` | quiet 期間再見 banner → `invalidate_human_input(QuietRearm)` |
| `console_unlock_arm_keeps_auto_probe_open` | profile 有 `console_unlock_env` → `quiet_active=true` 但 `auto_probe_blocked=false` |
| `banner_arm_keeps_external_state` | READY 下 arm：`public()["state"]=="READY"`、`ready_reconfirm_pending==true`、`boot_quiet_remaining_s==180.0` |
| `quiet_rx_runs_during_foreground_busy` | 前景命令執行中 RX banner 仍 arm |
| `prompt_clears_quiet_but_not_pending_nor_fsm_gen` | arm 後 RX `root@prplOS:~# ` → `boot_quiet_remaining_s==null`、`ready_reconfirm_pending==true`、`fsm_gen` 不變（arch §5.3 (c) 已刪）、fence `quiet_active=false`、`auto_probe_blocked=false` |
| `bootloader_tail_blocks_clear` | brcm-template、tail 尾行 `CFE> ` → quiet 不解除（即使 prompt_regex 寬鬆命中） |
| `session_list_does_not_expire_get_state_does` | pending deadline 已過：`list` 輸出 `ready_reconfirm_pending:true,ready_reconfirm_failed:false,ready_reconfirm_remaining_s:0.0`；`GetState` 之後 `failed:true`（GAP-G-13） |
| `tick_expires_within_one_second` | deadline 過後下一次 1 s `Tick` 後 `list` 已翻 `failed:true` |
| `confirm_ready_clears_all` | 當前 `(epoch, fsm_gen)` 的 `LoginFinished{result: Ready}` → until=0、pending=false、failed=false、attempts=0 |
| `stale_login_finished_is_ignored` | `fsm_gen` 不符的 `LoginFinished{Ready}` → 狀態不變、不清 pending（§5.3「過期 timer／ownership 事件」） |
| `execute_time_quiet_has_retry_after_s`（回歸） | quiet 剩 37.26 s → `retry_after_s==37.3`；pending-only → `5.0`；failed → `READY_UNCONFIRMED`、無 `retry_after_s`、`recommended_action=="self_test"`（SC-03） |
| `rpc_human_source_is_treated_as_agent`（回歸） | `source="human:x"` 經 RPC 仍被 gate（DF-02） |
| `passthrough_banner_never_arms_quiet`（新增，SC-35） | `platform="passthrough"`（uboot-template 與 others-template 各一）在 ATTACHED 與 READY 下各收一個含 `U-Boot 2024.04`、`Hit any key to stop autoboot:  3` 的 chunk → `fsm_gen`、`input_epoch` 不變；`public()["boot_quiet_remaining_s"]` 為 null、`ready_reconfirm_pending==false`；fence `quiet_active==false`、`auto_probe_blocked==false`；Effects 無 `reason=boot_banner` 的 `Log` 行 |
| `passthrough_ready_never_gated`（新增，SC-35） | passthrough READY（經 30 s 安靜轉入）收到上列 banner 後立即 `Submit("ls")` → 進 Arbiter（回 `cmd_id`），**不**回 `AUTOBOOT_QUIET`／`READY_UNCONFIRMED`；`session_list` 輸出 `ready_reconfirm_failed:false`、`ready_reconfirm_remaining_s:null` |
| `passthrough_banner_restarts_quiet_window`（新增，SC-35） | passthrough ATTACHED 第 29 s 收到 banner chunk → 不 arm quiet（同上），但安靜視窗自該 chunk 起重算：之後第 29 s 的 `Tick` 仍 ATTACHED、第 30 s 的 `Tick` 轉 READY |

- [ ] **Step 2：執行**：`cargo test -p sw-core --features test-fakes --test quiet_gate_p7 --test session_core_quiet_p7` → 新增列 RED；標「回歸」者 PASS（FAIL 記 P5 缺陷）。
- [ ] **Step 3：實作**：照 05 §1.6 與 `_update_boot_quiet_locked`（`session_manager.py:2327-2368`）核對 06 本體；`QuietTransition::Armed` → 在同一次 `Fence::with` 內設 `quiet_active=true`、`auto_probe_blocked=boot_quiet_blocks_probe(..)`、`fsm_gen+=1`，且 `boot_quiet_blocks_probe` 為真時 `fence.fsm_gen_floor = fsm_gen`、清 `active_login`（線性化點，Review Focus 1；console unlock 只遞增 `fsm_gen`、不推 floor），鎖外再 `invalidate_human_input(BootBanner|QuietRearm, fx)`、對進行中且被擋的 login run 發 `Effect::CancelLogin{epoch, fsm_gen: 舊值}`（Task 7）；`Cleared` → 同鎖清 `quiet_active`、`auto_probe_blocked`，**不**動 `fsm_gen`。`submit_gate_reject`／`execute_gate_reject` 依 `gate_responses` golden 的 key 順序以 `pydict!` 建構。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2；`--test login_quiet`（06）不退化。
- [ ] **Step 5：commit**：`feat(rust-quiet): 完成 Ready Reconfirm 有界化與 Quiet gate 回應`

---

## Task 5：`reprobe.rs`：readiness reprobe 與 bootloader 證據

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p7-reprobe.md`（`### Added`：readiness reprobe 退避與 bootloader 卡死判定）
**BLOCKED-OWNER:** 實機卡 U-Boot 時 `BOOTLOADER_STUCK` 的判定（L2 `f9-attach-during-boot-reprobes`，B-02）。

**Files:**
- Modify: `crates/sw-core/src/reprobe.rs`、`crates/sw-core/src/session_core.rs`（1 s tick 處理）
- Create: `crates/sw-core/tests/reprobe.rs`

**Interfaces：**

```rust
// crates/sw-core/src/reprobe.rs
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ReprobeState { pub attempts: u32, pub next_reprobe_at: Option<Mono>, pub exhausted: bool, pub inflight: bool }
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum ReprobeAction { Probe, Attach }
pub struct ReprobeInput<'a> {
    pub state: &'a str, pub by_id: &'a str, pub released: bool, pub device_present: bool,
    pub last_error: Option<&'a str>, pub last_rx_mono: Option<Mono>, pub human_active: bool,
    pub has_bridge: bool, pub profile: &'a SessionProfile,
}
pub struct ValidInput<'a> {                                            // _reprobe_target_still_valid_locked 的輸入
    pub same_bridge: bool, pub by_id: &'a str, pub released: bool, pub device_present: bool, pub state: &'a str,
    pub last_error: Option<&'a str>, pub human_active: bool, pub rx_tail_512: &'a str, pub profile: &'a SessionProfile,
}
pub fn is_reprobe_prompt_error(state: &str, last_error: Option<&str>) -> bool;
pub fn prepare(r: &mut ReprobeState, q: &mut QuietState, inp: &ReprobeInput, now: Mono) -> Option<ReprobeAction>;  // _prepare_reprobe_locked 13 步
pub fn record_attempt(r: &mut ReprobeState, now: Mono);                // attempts+=1；>=10 → exhausted；否則 next=now+min(2*2^(attempts-1),15)
pub fn reset(r: &mut ReprobeState);
pub fn target_still_valid(r: &ReprobeState, q: &mut QuietState, inp: &ValidInput, now: Mono) -> bool;   // session_manager.py:1507-1545；READY＋bootloader 尾行 → force expire、回 false、last_error 不動（D1）
pub struct ProbeFinish { pub new_state: String, pub last_error: Option<String>, pub last_error_detail: Option<String> }
pub fn finish_probe(r: &mut ReprobeState, q: &mut QuietState, became_ready: bool, bootloader_match: Option<String>, now: Mono) -> Option<ProbeFinish>;
pub fn bootloader_evidence(rx_tail_512: &str, sp: &SessionProfile) -> Option<String>;   // clean_text 後，prompts or UBOOT_FALLBACK_PROMPTS
```

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/reprobe.rs
mod support; use support::{golden_rows, t, secs, reprobe_input_from_json, valid_input_from_json, quiet_state_from_json};
use sw_core::reprobe::*;

#[test]
fn prepare_decisions_match_python_golden() {
    for row in golden_rows("reprobe_decisions") {
        let (mut r, mut q, owned) = (row.reprobe_state("before"), quiet_state_from_json(&row["quiet"]), reprobe_input_from_json(&row["input"]));
        let got = prepare(&mut r, &mut q, &owned.as_input(), t(row.f64("now")));
        assert_eq!(got.map(|a| format!("{a:?}").to_lowercase()), row.opt_str("action").map(str::to_owned), "case {}", row.id());
        assert_eq!(r.attempts, row.u32("attempts")); assert_eq!(r.exhausted, row.bool("exhausted"));
        support::assert_opt_f64_close(r.next_reprobe_at.map(|m| secs(m) - row.f64("now")), row.opt_f64("next_reprobe_at_rel"), 1e-6, row.id());
        assert_eq!(q.failed, row.bool("failed"));
    }
}

#[test]
fn target_still_valid_matches_python_golden_for_every_early_return() {
    for row in golden_rows("reprobe_valid") {
        let (r, mut q, owned) = (row.reprobe_state("before"), quiet_state_from_json(&row["quiet"]), valid_input_from_json(&row["input"]));
        assert_eq!(target_still_valid(&r, &mut q, &owned.as_input(), t(row.f64("now"))), row.bool("valid"), "case {}", row.id());
        assert_eq!((q.pending, q.failed), (row.bool("pending"), row.bool("failed")), "case {}", row.id());
    }
}

#[test]
fn backoff_schedule_2_4_8_15_15_until_exhausted_at_10() {
    let mut r = ReprobeState::default();
    let mut gaps = vec![];
    for i in 0..10 { record_attempt(&mut r, t(100.0 * i as f64)); gaps.push(r.next_reprobe_at.map(|m| secs(m) - 100.0 * i as f64)); }
    assert_eq!(gaps, [Some(2.0), Some(4.0), Some(8.0), Some(15.0), Some(15.0), Some(15.0), Some(15.0), Some(15.0), Some(15.0), None]);
    assert!(r.exhausted);
}
```

表格家族：

| 測試名 | 精確斷言 |
|---|---|
| `reprobe_eligibility_attached` | ATTACHED：`PROMPT_UNAVAILABLE`、`PROMPT_TIMEOUT`、`SHELL_PROMPT_TIMEOUT`、`READY_PROMPT_TIMEOUT`、`RX_FLOOD`、`TRANSPORT_STALL` → true；`LOGIN_REQUIRED`、`READY_NONCE_TIMEOUT`、`BOOTLOADER_STUCK`、`CREDENTIALS_UNRESOLVED`、None → false |
| `reprobe_eligibility_detached` | DETACHED 同上但 `PROMPT_UNAVAILABLE` → false；READY／ATTACHING → 一律 false |
| `credentials_unresolved_never_reprobes` | `last_error=CREDENTIALS_UNRESOLVED`、其他條件皆滿足 → `None` 且 attempts 不變 |
| `passthrough_never_reprobes`（新增，SC-35） | `platform="passthrough"`（uboot-template，有 `ready_probe`）ATTACHED、`last_error=PROMPT_UNAVAILABLE`、RX 已安靜 ≥3 s、其他條件皆滿足 → `prepare` 回 `None`、attempts 不變；READY＋（理論上的）pending → 同樣 `None`；passthrough 的 READY 只由 P5 安靜 tick 決定 |
| `quiet_blocks_without_counting_attempt` | quiet active（無 console_unlock）→ `None`、attempts 不變；有 `console_unlock_env` → 不被 quiet 擋（`boot_quiet_blocks_probe` 為假） |
| `human_active_blocks_reprobe` | P6 lease 顯示 human active → `prepare` 回 `None`；`target_still_valid` 回 false |
| `rx_not_idle_for_3s` | `now-last_rx_mono=2.99` → None；`3.0` → Probe |
| `ready_pending_schedules_confirm_probe` | READY＋pending＋quiet 過期 → `Probe`，`next=now+min(2*2^attempts,15)` |
| `exhausted_ready_reconfirm_force_expires` | READY＋pending＋attempts>=10 → None、`q.failed=true`（SES-16.4-10 KEEP） |
| `ready_bootloader_tail_d1_keeps_last_error_null` | READY＋pending、quiet 已過期、tail 尾行 `=> ` → `target_still_valid` 回 false、`q.failed=true`、`pending=false`、state 仍 READY、`last_error` 不被設定、零 TX（D1，`session_manager.py:1521-1533`） |
| `finish_probe_ready_failure_counts_reconfirm_attempt` | pending 中的確認 probe 失敗（state 已被 probe 收斂成 ATTACHED）→ `q.attempts+=1`、`ProbeFinish.new_state=="ATTACHED"`；非 pending 的 ATTACHED reprobe 失敗 → attempts 不變（SES-16.4-11 KEEP：不 flush，佇列中 accepted 命令維持 accepted） |
| `finish_probe_bootloader_sets_stuck` | 失敗 probe 之後 `bootloader_match=Some("^=> $")` → `last_error=BOOTLOADER_STUCK`、detail `matched_prompt=^=> $`、exhausted、`q.failed=true`（僅 probe 實際跑過的 ATTACHED 路徑） |
| `bootloader_evidence_falls_back_to_uboot_prompts` | profile 無 bootloader_prompts、tail `"\r\n=> "` → `Some("^=> $")` |

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-core --features test-fakes --test reprobe`（新模組，全部 RED）。
- [ ] **Step 3：實作**：照 05 §6 的 13 步與 `_finish_probe_reprobe`、`_reprobe_target_still_valid_locked`；`human_active` 由 P6 `lease_context(...)["human_active"]` 提供；SessionCore 1 s `Tick`：每 session 呼叫 `prepare`，`Probe` → 先以 `target_still_valid` 在 actor 內再驗（等價 Python 的 worker 寫入前最終驗證；actor 單執行緒故不存在 job 收集與寫入之間的競態，但 OpRunner 取得 permit 時 fence 仍會以 `auto_probe_blocked`／`fsm_gen` 再擋一次）→ `Effect::StartLogin{epoch, fsm_gen, plan: choose_login_plan(..), kind: LoginRunKind::ExistingBridgeProbe{origin: ProbeOrigin::AutoReprobe}}`（Task 7）；`Attach` → `Effect::Notify(ManagerNote::RequestAttach{by_id})`（06 T16 inflight 去重）。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2；`--test login_quiet --test quiet_gate_p7`（不退化）。
- [ ] **Step 5：commit**：`feat(rust-reprobe): 承接 readiness reprobe 與 bootloader 卡死判定`

---

## Task 6：`transport_stall.rs`＋PAL `UsbTopology`＋`FsOps` 寫入側

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p7-transport-stall.md`（`### Added`：transport stall 分類、復原提示與一次性告警）
**BLOCKED-OWNER:** 真實 USB／usbip read-endpoint stall（`urb stopped: -32`）重現與 busid 解析實測（B-03）。Windows `UsbTopology` 的 non-COM 原生執行證據屬本環境可產出（WSL interop）；本 PR 動到 PAL，依 OD-08 另需原生 Windows COM handle 環境的回歸證據（B-06）。

**Files:**
- Modify: `crates/sw-core/src/transport_stall.rs`、`crates/sw-core/src/session_core.rs`（`transport_stall_warned` 欄位：`clean_text` 後非空的 RX chunk 時清為 false，對應 `_mark_session_rx` 的重臂，`session_manager.py:2320-2325`）
- Create: `crates/sw-pal/src/device_source/usb_posix.rs`、`crates/sw-pal/src/device_source/usb_windows.rs`
- Modify: `crates/sw-pal/src/fs_ops.rs`（Task 0 宣告的 `open_append`（若 06 未提供）、`create_truncate`、`is_file`、`file_size`、`remove_file_if_exists` 之 POSIX／Windows 實作（`mkdirs` 沿用 P4）；錯誤 `py_message` 照 Python `str(OSError)`）
- Create: `crates/sw-core/tests/transport_stall.rs`、`crates/sw-core/tests/session_core_stall.rs`、`crates/sw-pal/tests/usb_topology.rs`、`crates/sw-pal/tests/fs_ops_p7.rs`（`create_truncate_overwrites_stale_tmp`、`mkdirs_exist_ok`（P4 `mkdirs` 回歸）、`remove_file_if_exists_missing_is_false`、`open_append_error_message_has_quoted_path`；兩平台同一份）

**Interfaces：**

```rust
// crates/sw-core/src/transport_stall.rs
pub fn is_transport_refinable_error(err: &str) -> bool;    // PROMPT_UNAVAILABLE、LOGIN_PROMPT_TIMEOUT、*_PROMPT_TIMEOUT
pub fn classify_probe_failure(err: &str, rx_delta: u64, last_rx_mono: Option<Mono>, now: Mono) -> (String, bool);
pub fn transport_stall_hint(busid: Option<&str>, rx_age_s: f64) -> String;   // 10 G-09 逐字
#[derive(Clone, Debug, PartialEq)] pub struct Refined { pub err: String, pub detail: Option<String>, pub stall_warning: Option<StallWarning> }
#[derive(Clone, Debug, PartialEq)] pub struct StallWarning { pub log_line: String, pub wal_meta: PyDict }       // {"event":"transport_stall_suspected","real_path":…,"rx_age_s":round1}
// already_warned＝呼叫端傳入的 session.transport_stall_warned；本函式只決定「是否產生 warning」，去重狀態由 actor 在套用 outcome 時寫回（Task 7）
pub fn refine_probe_failure(err: String, rx_before: Option<u64>, rx_after: Option<u64>, last_rx_mono: Option<Mono>, now: Mono,
                            real_path: Option<&str>, usb: &dyn UsbTopology, already_warned: bool, rx_tail_4096: &str, com: &str) -> Refined;
```

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/transport_stall.rs
mod support; use support::{golden_rows, t};
use sw_core::transport_stall::*; use sw_pal::fakes::usb::FakeUsb;
const STALL: &str = "TRANSPORT_STALL"; const PU: &str = "PROMPT_UNAVAILABLE";

#[test]
fn hint_text_is_byte_identical() {
    for row in golden_rows("transport_stall").into_iter().filter(|r| r.str("kind") == "hint") {
        assert_eq!(transport_stall_hint(row.opt_str("busid"), row.f64("rx_age_s")), row.str("text"), "case {}", row.id());
    }
}

#[test]
fn refine_flips_only_when_zero_rx_and_frozen_30s_and_warns_once() {
    let usb = FakeUsb(Some("1-1".into()));
    let r = refine_probe_failure(PU.into(), Some(500), Some(500), Some(t(10.0)), t(40.0), Some("/dev/ttyUSB0"), &usb, false, "", "COM0");
    assert_eq!(r.err, STALL);
    assert!(r.detail.as_deref().unwrap().contains("/sys/bus/usb/devices/1-1/authorized"));
    let w = r.stall_warning.expect("首次須告警");
    assert_eq!(w.log_line, format!("session COM0 疑似 transport stall（零 RX 30.0s、probe 全程無 echo，real_path=/dev/ttyUSB0）：{}", r.detail.unwrap()));
    let again = refine_probe_failure(PU.into(), Some(500), Some(500), Some(t(10.0)), t(41.0), Some("/dev/ttyUSB0"), &usb, true, "", "COM0");
    assert!(again.stall_warning.is_none(), "transport_stall_warned 去重");
    let not = refine_probe_failure(PU.into(), Some(500), Some(500), Some(t(10.0)), t(39.99), Some("/dev/ttyUSB0"), &usb, false, "", "COM0");
    assert_eq!(not.err, PU);
}
```

表格家族：

| 測試名 | 精確斷言 |
|---|---|
| `classify_golden_table` | 對 golden `kind=="classify"` 每列 `(err, rx_delta, last_rx_mono, now) → (out, flipped)` 相等 |
| `login_detail_attached_when_not_flipped` | `READY_NONCE_TIMEOUT`、rx_delta=5 → detail＝`clean_text(tail)` 最後 300 字元；tail 為空 → None |
| `flood_value_not_in_detail_set` | `RX_FLOOD` 未翻轉 → detail None |
| `never_rx_is_not_stall` | `last_rx_mono=None` → 不翻轉 |
| `rx_before_none_returns_err_without_detail` | `rx_before=None` → `(err, None)` |
| `wal_meta_rx_age_rounded` | `rx_age_s=31.05` → META `rx_age_s` 為 `py_round(31.05,1)`（與 golden 一致） |
| `busid_from_sysfs_tempdir`（sw-pal，`#[cfg(unix)]` 一般測試，**不** `#[ignore]`；只用 tempdir symlink、不需 PTY） | 以 tempdir 模擬 `<root>/class/tty/ttyUSB0/device` symlink → `"1-1"`；深層 `8-2.1:1.0` → `"8-2.1"`；無 symlink → None |
| `usb_windows_is_always_none`（sw-pal，`#[cfg(windows)]`，Windows 原生執行） | 任意 path → None |
| `rx_rearms_stall_warning`（`session_core_stall.rs`） | 套用帶 `stall_warning` 的失敗 outcome 後 `transport_stall_warned==true`；收到非空 RX chunk → false；再次 stall → 再產生一次 WAL META（共 2 筆） |
| `ansi_only_chunk_does_not_rearm` | `clean_text(decode_utf8_replace(data))` 為空的 chunk（例：純 ANSI 控制序列）不重臂、不更新 `last_rx`（`_on_bridge_rx` 在 `_mark_session_rx` 之前 `if not chunk: return`，`session_manager.py:2400-2407`） |

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-core --features test-fakes --test transport_stall --test session_core_stall`；`cargo test -p sw-pal --test usb_topology --test fs_ops_p7`。
- [ ] **Step 3：實作**：移植 `transport_stall.py` 與 `_refine_probe_failure`（`session_manager.py:1267-1330`）；POSIX `UsbTopology`：`basename(real_path)` → `realpath(<sysfs_root>/class/tty/<name>/device)`（`sysfs_root` 預設 `/sys`，測試可注入）→ 由深至淺第一個 fullmatch `\d+-\d+(\.\d+)*` 的段；任何錯誤 None。Windows：恆 None。log 由 actor 以 `Effect::Warn`（GAP-G-01 模板）輸出，`refine_probe_failure` 本身不寫 log。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2；`scripts/rust/win-test.sh --package sw-pal --test usb_topology --test fs_ops_p7`（以 cargo `--test` 選 binary，**不得**用 `-- <filter>`，後者以測試名過濾會零案例假綠；Windows 原生、WSL interop）；核對 `target/win-test-report/<run_id>.json` 中該 exe `passed >= 1`。
- [ ] **Step 5：commit**：`feat(rust-stall): 承接 transport stall 分類與 host 層復原提示`

---

## Task 7：Login run 協調、fence `fsm_gen`、帳密終態與收斂

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p7-login-run.md`（`### Added`：Login run 協調、quiet 中止 login run（SC-30）、fence 以 fsm_gen 擋過期 run、attach／reprobe 收斂與 transport stall 告警）
**BLOCKED-OWNER:** 實機 F10 兩案（帳密修正後 READY、未解析終態零空帳密 TX；B-04；交 bench 前先以 `scripts/docker/run-l15.sh` 跑 `L1.5-docker`，不計 L2）。原生 Windows COM 上的登入（OD-08，B-06）OD-15 起為擁有者 host 可執行 gate（`COM20:COM21`）。

**Files:**
- Create: `crates/sw-core/src/login_run.rs`（收斂純函式）
- Modify: `crates/sw-core/src/tx_writer.rs`（06 T9：`TxPermit.fsm_gen`、`FenceState.{fsm_gen_floor, auto_probe_blocked}`、`FenceReject::StaleFsmGen`）、`crates/sw-core/src/uart_bridge.rs`（06 T10：`RunCancel`、`wait_for_regex_cancellable`、`last_rx_seq`）、`crates/sw-core/src/session_core.rs`（06 T11：`LoginFinished` 收斂、`Effect::CancelLogin`）、`crates/sw-core/src/op_runner.rs`（06 T14：login／reboot recovery 執行體）、`crates/sw-core/src/session_manager.rs`（動態 attach detect 層、reboot recovery 起動）
- Create: `crates/sw-core/tests/login_run.rs`、`crates/sw-core/tests/session_core_login.rs`、`crates/sw-core/tests/fence_fsm_gen.rs`

**Interfaces：**

```rust
// tx_writer.rs（修訂 06 T9；06 既有欄位不動）
pub struct TxPermit { /* 06 欄位 */ pub fsm_gen: Option<u64> }        // LoginProbe／ReadyReconfirm 帶 Some(啟動時的 fsm_gen)，其他 class（含 SelfTestProbe，R5-F2）None
pub struct FenceState { /* 06 欄位 */ pub fsm_gen_floor: u64, pub auto_probe_blocked: bool }
pub enum FenceReject { /* 06 的 8 值 */ StaleFsmGen }
// admit 規則（Fence 鎖內，依序）：epoch → frozen → permit.fsm_gen==Some(g) && g < st.fsm_gen_floor → StaleFsmGen
//   → LoginProbe／ReadyReconfirm：auto_probe_blocked → Quiet（取代原「quiet_active 即擋」，使 console unlock 可運作）
//   → SystemRecoverKey／SelfTestProbe：quiet_active → Quiet（不變；R5-F2：SelfTestProbe 不改用 auto_probe_blocked——self-test
//     pre-check 對任何 profile 都以 quiet_active 回 AUTOBOOT_QUIET，提交點必須同義，console unlock 的 arm 也擋）→ 其餘 06 規則不變
// uart_bridge.rs（修訂 06 T10）
#[derive(Clone)] pub struct RunCancel { /* Arc<AtomicBool>＋對所屬 RxShared condvar 的 notify_all */ }
impl RunCancel { pub fn new(rx: &Arc<RxShared>) -> Self; pub fn trigger(&self); pub fn is_triggered(&self) -> bool; }
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum WaitOutcome { Matched, TimedOut, Cancelled }
impl RxShared {
    pub fn wait_for_regex_cancellable(&self, re: &PyRegex, from: Option<u64>, timeout: Duration, clock: &dyn Clock, cancel: &RunCancel) -> WaitOutcome;
    pub fn last_rx_seq(&self) -> u64;                                      // 最後一個 append 的 rx_seq（LoginFinished.after_rx_seq 來源）
}
// login_run.rs（新增）
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum ProbeOrigin { AttachSession, RecoverReprobe, AutoReprobe }
#[derive(Clone, Debug, PartialEq)]                                         // 不 derive Eq：含 f64
pub enum LoginRunKind { AttachProbe { gen_before: u64 }, ExistingBridgeProbe { origin: ProbeOrigin }, DynamicAttach, RebootRecovery { hard_timeout_s: f64 } }
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum EntryUsed { Ensure, Probe }   // 實際採用的入口；只有 Ensure 失敗會拆 bridge
#[derive(Clone, Debug, PartialEq)]
pub struct LoginReport { pub kind: LoginRunKind, pub entry: Option<EntryUsed>, pub detail: Option<String>,
                         pub stall_warning: Option<StallWarning>, pub credentials: Option<AuthResolution> }
pub fn apply_login_finished(core: &mut SessionCore, epoch: u64, fsm_gen: u64, result: LoginOutcome, report: LoginReport, now: Mono, fx: &mut Effects);
// session_core.rs（修訂 06 T11）
// ControlEvent::LoginFinished { epoch, fsm_gen, result: LoginOutcome, after_rx_seq: Option<u64>, report: LoginReport }   // 新增 report 欄位
// Effect::CancelLogin { epoch, fsm_gen }                                                                              // 新增
// LoginPlan 沿用 06；Effect::StartLogin{epoch, fsm_gen, plan} 新增 kind：Effect::StartLogin{epoch, fsm_gen, plan, kind: LoginRunKind}
```

**世代規則（actor 內）：** `SessionCore` 持 `active_login: Option<(epoch, fsm_gen)>`。`StartLogin` 前在**同一次** `Fence::with` 內 `fsm_gen += 1`、`fence.fsm_gen_floor = fsm_gen`，並設 `active_login`（arch §5.3 (b)）。quiet arm 一律 `fsm_gen += 1`（arch §5.3 (a)，隔離 quiet timer）；只有 `boot_quiet_blocks_probe` 為真時才在同鎖內把 `fsm_gen_floor` 推到新值並清 `active_login`——console unlock 的 arm 不推 floor，進行中的 unlock run 以原 `fsm_gen` 繼續取得 permit、完成時仍與 `active_login` 相符。`LoginFinished` 只在 `(epoch, fsm_gen) == active_login` 時套用。因此會擋 probe 的 arm 之後，該 run 尚未進 OS 的 job（含已排進 64 深 TxWriter queue 者）在 OS 寫入前檢查時一律 `StaleFsmGen` → `NotSubmitted`；arm 前已標 in-flight 者照實記 WAL（arch §5.4 線性化）。OpRunner 每個 run 建一個 `RunCancel`；`Effect::CancelLogin` → executor `trigger()`，等待中的 `wait_for_regex_cancellable` 立即回 `Cancelled` → `LoginOutcome::Aborted(Cancelled)`。`CancelLogin` 只在 `boot_quiet_blocks_probe` 為真時發出（console unlock 不取消，SC-30）。

**`choose_login_plan` 的呼叫（沿用 06）：** `quiet_blocks` 參數一律傳 `boot_quiet_blocks_probe(q, sp, now)`；06 已有的 `(LoginPlan::None, Some("PROMPT_UNAVAILABLE"))` 即「QuietBlocked」入口：零 TX、不經 transport 精煉。既有 bridge probe（`_probe_existing_bridge`，`session_manager.py:2474-2494`）走同一判定，但 **state 為 READY 時不覆寫 `last_error`**，回 `{"ok":true,"session":…}`，且不清 `recovering`／`pending_auto_login`。既有 bridge 路徑 `pending_auto_login` 一律傳 false（Python 該路徑只看 `login_regex or console_unlock_env`）。

**收斂規則**（`apply_login_finished`；逐字照 `session_manager.py:2496-2547`（existing bridge）與 `:2626-2729`（attach）、05 §3 T3～T6、T10～T12、T17～T18；新增 SC-30）：

| 條件 | 結果 |
|---|---|
| `epoch` 或 `fsm_gen` 不符（含已被 quiet 取消的 run） | 不改狀態；I/O 證據已由 TxWriter 落 WAL |
| 處理時機 | actor 以 06 `Mailbox::recv_next(processed_rx_seq, ..)`：`after_rx_seq` 之前的 data lane chunk 先處理完才套用（banner 若已在 data lane，先 arm → `fsm_gen` 變動 → 本列第一條丟棄） |
| state ∈ {FLASHING, RELEASED} | 不覆寫（#69）；ExistingBridgeProbe 回覆 RPC `{"ok":false,"error_code":"STATE_CHANGED","session":…}`；AttachProbe 在 RELEASED 時關閉剛開的 bridge、保留 RELEASED |
| AttachProbe／DynamicAttach：裝置已移除、`real_path` 變更、state 已 DETACHED 或 `bridge_generation != gen_before` | `begin_stop(StopKind::Removed)` 保留 consoles、`DETACHED`＋`last_error=DEVICE_REMOVED_DURING_ATTACH` |
| AttachProbe／DynamicAttach：`entry == Some(Ensure)` 且失敗（`Failed`／`NotReady`） | `begin_stop(StopKind::LoginFailed)`（06 T13 既有路徑，保留 consoles）、`DETACHED`、`last_error=err`、`last_error_detail=detail`、`detached_at`；**不**遞增 `bridge_generation` |
| AttachProbe／DynamicAttach：其餘（含 `entry == Some(Probe)` 失敗、`CREDENTIALS_UNRESOLVED`、QuietBlocked 合成 `PROMPT_UNAVAILABLE`、`!command_capable`） | 接納 bridge：`bridge_generation += 1`、`attached_real_path`、`vtty`；成功 → `READY`、`confirm_ready()`、`last_error=None`、`last_ready_at`、清 `recovering`／`recovery_started_at`／`pending_auto_login`、reset reprobe、`queue_registered=true`；失敗 → `ATTACHED`、`last_error=err`、`last_error_detail=detail`、清 `recovering`／`recovery_started_at`（**不**清 `pending_auto_login`） |
| ExistingBridgeProbe（QuietBlocked 以外） | 一律清 `recovering`／`recovery_started_at`／`pending_auto_login`；成功 → `READY`、`confirm_ready()`、`last_error=None`、`last_ready_at`、reset reprobe、`queue_registered=true`；失敗 → `ATTACHED`、`last_error=err`、`last_error_detail=detail`（READY→ATTACHED 不 flush，SES-16.4-11）；`bridge_generation` 不變 |
| `CREDENTIALS_UNRESOLVED`（任一 kind） | 06 T13 既有：`credentials_warned==false` 才 `Effect::Warn(…)`＋`Effect::WalMeta{cmd_id:None, meta:{"event":"credentials_unresolved",…}}` 並設 `credentials_warned=true`；結果列依上兩列（attach → ATTACHED 保橋） |
| `report.stall_warning == Some(w)` 且 `transport_stall_warned == false` | `Effect::Warn(w.log_line)`＋`Effect::WalMeta{cmd_id:None, meta:w.wal_meta}`，設 `transport_stall_warned=true`（去重以 actor 狀態為準，run 端只負責產生） |
| `Aborted(Refused(StaleFsmGen|Quiet))` 或 `Aborted(Cancelled)` 且 `fsm_gen` 仍相符（理論上不會發生，防禦） | 視同 SC-30：非 READY → `ATTACHED`＋`PROMPT_UNAVAILABLE`、detail=None；READY → 不變；RebootRecovery → 回等待迴圈 |
| `Aborted(TxIncomplete)` | `ATTACHED`＋`last_error=PROMPT_UNAVAILABLE`、`last_error_detail="tx_incomplete"`；**不**自動排 reprobe（`reprobe.exhausted=true` 直到 attach／recover 重置；§11.3-5 不得自動重送） |
| `Aborted(Refused(StaleEpoch|Frozen|Flash|WalUnhealthy|OwnerMismatch))` | 不改狀態 |
| `Raised(exc)` | AttachProbe／DynamicAttach → 拆 bridge（保留 consoles）、`DETACHED`＋`last_error="ATTACH_FAILED:{exc}"`（`:2749`）；ExistingBridgeProbe{AutoReprobe} → `ATTACHED`＋`last_error="REPROBE_FAILED:{exc}"`＋`record_attempt`（`:1584`）；ExistingBridgeProbe{AttachSession／RecoverReprobe} → RPC 回 framing `EXCEPTION`（Python 例外穿出 manager）；RebootRecovery → WARNING `reboot-recovery ensure_ready 例外（session=%s），續迴圈待逾時收尾`、本輪 `RECOVERY_ERROR` 並續迴圈（`:3435-3441`） |

**SC-30 收斂（banner 到達時，actor 在 arm 的同一事件內決定，不等 run 回報）：** run 為 AttachProbe／ExistingBridgeProbe 且 state 非 READY → `ATTACHED`＋`last_error=PROMPT_UNAVAILABLE`、`last_error_detail=None`；READY → 不變；RebootRecovery → 回「quiet blocks → 延長 deadline」等待迴圈。之後抵達的 `LoginFinished` 因 `fsm_gen` 不符被丟棄。

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/fence_fsm_gen.rs（真 Fence＋TxWriter＋FakeSerial，線性化）
#![cfg(feature = "test-fakes")]
mod support; use support::*;
use sw_core::tx_writer::*;

#[test]
fn job_queued_before_arm_is_not_submitted_after_arm() {
    let h = TxRig::new();                                      // FakeSerial writer 先阻塞在第 1 個 job
    let g = h.fence_bump_fsm_gen();                            // 模擬 StartLogin
    let r1 = h.submit(TxClass::LoginProbe, Some(g), b"\n");    // 已經 FenceSubmitGate 提交 OS、於 gate 外卡住等完成（in-flight）
    let r2 = h.submit(TxClass::LoginProbe, Some(g), b"root\n");// 排在 queue
    h.wait_in_flight();
    h.fence_arm_quiet(/*blocks_probe*/ true);                  // 同鎖：quiet_active、auto_probe_blocked、fsm_gen+1
    h.release_writer();
    assert_eq!(r1.recv().result.outcome, TxOutcome::Completed(1), "arm 前已 in-flight → 已提交、照實記 WAL");
    let rep2 = r2.recv();
    assert_eq!((rep2.result.outcome, rep2.reject), (TxOutcome::NotSubmitted, Some(FenceReject::StaleFsmGen)));
    assert_eq!(h.wal_tx_payloads(), vec![b"\n".to_vec()]);
    assert_eq!(h.dev.written(), b"\n");
}

#[test]
fn login_probe_authorized_then_quiet_arm_before_os_submit_is_zero_tx() {   // R0-F1：出列檢查通過後、OS 提交前 arm
    let h = TxRig::new(); let g = h.fence_bump_fsm_gen();
    h.dev.script_write(vec![sw_pal::fakes::serial::FakeWriteStep::PauseBeforeSubmit]);
    let r1 = h.submit(TxClass::LoginProbe, Some(g), b"root\n");
    assert!(h.dev.wait_paused(std::time::Duration::from_secs(2)));       // 尚未進 FenceSubmitGate 臨界區
    h.fence_arm_quiet(/*blocks_probe*/ true);                             // 不得阻塞（writer 不持鎖）
    h.dev.release_pause();
    let rep = r1.recv();
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::StaleFsmGen)));
    assert_eq!(h.dev.os_submits(), 0);
    assert!(h.dev.written().is_empty(), "帳號不得在 quiet arm 後送出");
    assert!(h.wal_tx_payloads().is_empty());
}

#[test]
fn console_unlock_probe_passes_while_quiet_active() {
    let h = TxRig::new(); let g = h.fence_bump_fsm_gen();
    h.fence_arm_quiet(/*blocks_probe*/ false);                 // console unlock profile：core fsm_gen+1，但 floor 不動
    let rep = h.submit(TxClass::LoginProbe, Some(g), b"k\n").recv();   // 原 run 世代，floor 未推進
    assert!(rep.reject.is_none());
    assert_eq!(h.fence_floor(), g);
}

#[test]
fn self_test_probe_gated_by_quiet_active_even_for_console_unlock() {   // R5-F2，DF-19：舊「SelfTestProbe 看 auto_probe_blocked」必紅
    for blocks_probe in [true, false] {
        let h = TxRig::new();
        h.dev.script_write(vec![sw_pal::fakes::serial::FakeWriteStep::PauseBeforeSubmit]);
        let r1 = h.submit(TxClass::SelfTestProbe, None, b"echo __READY__ab12cd34\n");
        assert!(h.dev.wait_paused(std::time::Duration::from_secs(2)));    // pre-check（出列）已過、尚未進 FenceSubmitGate
        h.fence_arm_quiet(blocks_probe);                                    // console unlock（false）：quiet_active=true、auto_probe_blocked=false
        h.dev.release_pause();
        let rep = r1.recv();
        assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::Quiet)), "blocks_probe={blocks_probe}");
        assert_eq!(h.dev.os_submits(), 0);
        assert!(h.wal_tx_payloads().is_empty());
        if !blocks_probe {                                                  // 對照：同狀態的 console unlock login run 仍放行
            let g = h.fence_floor();
            assert!(h.submit(TxClass::LoginProbe, Some(g), b"k\n").recv().reject.is_none());
        }
    }
}
```


```rust
// crates/sw-core/tests/session_core_login.rs
#![cfg(feature = "test-fakes")]
mod support; use support::{core_attached_prpl_with_creds, core_ready_prpl, t, req, rx_chunk, start_login_of, login_finished, report};
use sw_core::{login_run::*, login_fsm::LoginOutcome, session_core::*};

#[test]
fn quiet_arm_mid_login_cancels_run_and_late_ready_is_ignored_sc30() {
    let mut core = core_attached_prpl_with_creds("/tmp/ok.env", &[("SW_OPI_U", "root"), ("SW_OPI_P", "pw")]);
    let fx = core.on_event(req(SessionRequest::SessionAttach), t(0.0));
    let (epoch, g, _plan, _kind) = start_login_of(&fx).expect("attach 須起 login run");
    let fx_rx = core.on_event(rx_chunk(&core, "U-Boot 2024.04\r\n"), t(0.5));
    assert!(fx_rx.any(|e| matches!(e, Effect::CancelLogin { epoch: e2, fsm_gen } if *e2 == epoch && *fsm_gen == g)));
    let p = core.public_default(t(0.5));
    assert_eq!((p.str("state"), p.str("last_error")), ("ATTACHED", "PROMPT_UNAVAILABLE"));
    assert!(p.is_null("last_error_detail"));
    core.on_event(login_finished(epoch, g, LoginOutcome::Ready, report(LoginRunKind::ExistingBridgeProbe { origin: ProbeOrigin::AttachSession }, Some(EntryUsed::Ensure))), t(2.0));
    assert_eq!(core.public_default(t(2.0)).str("state"), "ATTACHED", "過期 fsm_gen 的 Ready 不得推成 READY");
}

#[test]
fn login_finished_waits_for_earlier_banner_in_data_lane() {
    let mut core = core_attached_prpl_with_creds("/tmp/ok.env", &[("SW_OPI_U", "root"), ("SW_OPI_P", "pw")]);
    let fx = core.on_event(req(SessionRequest::SessionAttach), t(0.0));
    let (epoch, g, _, _) = start_login_of(&fx).unwrap();
    let mut mb = support::mailbox_for(&core);
    mb.push_data(rx_chunk(&core, "U-Boot 2024.04\r\n"), /*rx_seq*/ 7);
    mb.push_control(login_finished(epoch, g, LoginOutcome::Ready, report(LoginRunKind::AttachProbe { gen_before: 0 }, Some(EntryUsed::Ensure))).with_after_rx_seq(7));
    support::drain_mailbox(&mut core, &mut mb, t(1.0));        // recv_next 依 processed_rx_seq 先處理 banner
    assert_eq!(core.public_default(t(1.0)).str("state"), "ATTACHED");
}
```

表格家族（`session_core_login.rs`；06 T13 已覆蓋的「帳密終態零 TX＋一次告警」以 `credentials_unresolved_is_terminal_attached_and_sends_no_credentials`（06）回歸，不重寫）：

| 測試名 | 精確斷言 |
|---|---|
| `attach_ensure_failure_detaches_preserving_consoles` | AttachProbe、`entry=Ensure`、`Failed("SHELL_PROMPT_TIMEOUT")` → `DETACHED`、06 `begin_stop(LoginFailed)` 被觸發、consoles 保留、`bridge_generation` 不變 |
| `attach_probe_without_creds_failure_stays_attached` | prpl-template（無帳密、`login_regex` 非空）→ `entry=Probe`、`NotReady("PRPL_PROMPT_TIMEOUT")` → `ATTACHED`、bridge 保留、`bridge_generation==1` |
| `credentials_unresolved_attach_stays_attached` | 帳密宣告但未解析 → `ATTACHED`＋`CREDENTIALS_UNRESOLVED`、bridge 保留、`bridge_generation==1`、零 TX |
| `quiet_blocked_attach_zero_tx_and_ready_exception` | quiet 期間 `SessionAttach`：非 READY → `last_error=PROMPT_UNAVAILABLE`、`write_calls()==0`、不經 refine；READY 中的既有 bridge reprobe 被 quiet 擋 → `last_error` 仍為 null、回 `{"ok":true,"session":…}`；`session.recover`（REPROBE）與動態 attach 同樣零 TX |
| `attach_failure_keeps_pending_auto_login` | AttachProbe Probe 失敗 → `pending_auto_login` 維持原值；ExistingBridgeProbe 失敗 → 清為 false |
| `verified_attach_promotes_ready_and_bumps_bridge_generation` | `state=="READY"`、`bridge_generation==1`、`ready_reconfirm_pending==false`、`queue_registered==true` |
| `device_removed_during_attach` | 完成時 `bridge_generation != gen_before` → `DETACHED`＋`DEVICE_REMOVED_DURING_ATTACH` |
| `failure_sets_detail_from_refine` | `report.detail=Some(x)` → `last_error_detail==x` |
| `stall_warning_deduped_by_actor` | 連續兩個帶 `stall_warning` 的失敗 → WAL META `transport_stall_suspected` 1 筆、Warn 1 次 |
| `tx_incomplete_marks_and_stops_auto_reprobe` | `Aborted(TxIncomplete)` → `ATTACHED`、`last_error_detail=="tx_incomplete"`、400 s tick 內無 `StartLogin`；`SessionAttach` 後恢復 |
| `raised_maps_like_python_exceptions` | AttachProbe → `DETACHED`＋`ATTACH_FAILED:error`；AutoReprobe → `ATTACHED`＋`REPROBE_FAILED:error`＋attempts+1；RecoverReprobe → reply `Err(PyErr)` |
| `ready_reconfirm_failure_demotes_without_flush` | READY＋pending 的 AutoReprobe 失敗 → `ATTACHED`、佇列中 accepted 命令維持 accepted（未 `FLUSHED_BY_RECOVERY`） |
| `flashing_or_released_not_overwritten` | 完成時 state=FLASHING → state 不變；RecoverReprobe 回 `STATE_CHANGED` |
| `not_command_capable_stays_attached_no_error`（回歸） | others-template → `ATTACHED`、`last_error==null`、零 TX |
| `reboot_recovery_extends_deadline_while_quiet` | RebootRecovery、quiet active：deadline=`min(max(deadline, quiet_until+timeout), start+180+timeout)`；逾時 → bridge 在 `ATTACHED`＋`last_error or RECOVERY_TIMEOUT`，無 bridge `DETACHED`＋`RECOVERY_TIMEOUT` |
| `reboot_recovery_banner_returns_to_wait_loop_sc30` | RebootRecovery 登入中 banner → run 取消、迴圈繼續、最終 quiet 清除後 READY |
| `reboot_recovery_credentials_break` | RebootRecovery 帳密未解析 → 一次告警後 break，最終 `ATTACHED`＋`CREDENTIALS_UNRESOLVED` |
| `dynamic_attach_detect_layer_sets_profile_source_detected`（回歸 06 T16） | DynamicAttach detect 命中 op3-template → `profile_source=="detected"`、READY 時寫 sticky（`profile_detected[by_id]`） |
| `console_unlock_not_cancelled_by_banner` | console unlock profile 登入中見 `CFE version ` → 無 `CancelLogin`、`fence.fsm_gen_floor` 不變、`active_login` 保留，run 完成後 READY |
| `creds_file_fixed_does_not_auto_probe` | `CREDENTIALS_UNRESOLVED` 後改寫 FakeFs 的 env_file 內容，推進 2 個 reprobe 週期（≥ 30 s 虛擬時間）→ 零 `StartLogin`、`write_calls()` 不變、`last_error` 仍為 `CREDENTIALS_UNRESOLVED`（§9.3）；之後 `SessionAttach` 才重新解析並 READY |

```rust
// crates/sw-core/tests/login_run.rs（OpRunner＋真 TxWriter＋FakeSerial 整合）
#![cfg(feature = "test-fakes")]
#[test]
fn op_login_routes_secret_with_log_false_and_login_probe_class() {
    let h = support::op_harness_prpl();                        // FakeSerial＋TxWriter＋RxShared＋FakeClock
    h.target_script(&[("\n", "login: "), ("root\n", "Password: "), ("pw\n", "root@prplOS:~# "), ("echo __READY__", "__READY__{nonce}\r\nroot@prplOS:~# ")]);
    let (out, rep) = h.run(LoginRunKind::AttachProbe { gen_before: 0 }, support::auth("root", "pw"));
    assert_eq!((out, rep.entry), (sw_core::login_fsm::LoginOutcome::Ready, Some(EntryUsed::Ensure)));
    assert!(h.wal_tx_records().iter().all(|r| r.payload != b"pw\n"), "secret 不得進 WAL");
    assert_eq!(h.permit_classes(), vec![TxClass::LoginProbe; 4]);
    assert!(h.permit_fsm_gens().iter().all(|g| g.is_some()));
}
```

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-core --features test-fakes --test fence_fsm_gen --test login_run --test session_core_login`。
- [ ] **Step 3：實作**：OpRunner 執行體：記 `rx_total_before = rx.total_bytes()` → 依 `LoginPlan` 呼叫 `ensure_ready`／`probe_ready`（`LoginIo.class`：AttachProbe／ExistingBridgeProbe{AttachSession,RecoverReprobe}／DynamicAttach 用 `LoginProbe`，AutoReprobe 的 READY 確認與 RebootRecovery 用 `ReadyReconfirm`）→ 失敗且非 QuietBlocked 時 `refine_probe_failure`（Task 6，`already_warned` 取自 OpCtx 快照）填 `report.detail`／`stall_warning` → 送 `LoginFinished{.., after_rx_seq: Some(rx.last_rx_seq()), report}`。SessionCore 依上表收斂；`Aborted` 之後 run 不再送 TX（Task 3 保證）。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2；`--test login_quiet --test drain_release`（06 既有，不退化）。
- [ ] **Step 5：commit**：`feat(rust-login): 承接 login run 收斂、SC-30 取消與 fence fsm_gen`

---

## Task 8：`self_test.rs`：self-test 分類與 nonce probe 整合

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p7-self-test.md`（`### Added`：self-test 補 RX_FLOOD／bootloader fallback 分類與 Python 位元組相容回應；`### Fixed`：self-test 探測失敗不再計入 reconfirm 次數；self-test nonce probe 在送出前一刻偵測到開機 banner（含 console unlock profile）時不送出、回 `AUTOBOOT_QUIET`（DF-19））
**BLOCKED-OWNER:** 實機 F11 `f11-flood-probe-classified`（RX_FLOOD 分類，B-05）。

**Files:**
- Modify: `crates/sw-core/src/self_test.rs`（06 T14 既有）、`crates/sw-core/src/op_runner.rs`（`OpSpec::SelfTestProbe`：`TxPermit.fsm_gen: None`（R5-F2）、`SelfTestRefused`、`RunCancel`）、`crates/sw-core/src/session_core.rs`
- Modify: `crates/sw-core/tests/lease_selftest.rs`（06 T14：`selftest_unresponsive_counts_attempt` 改名並更正，見 §0.6 退役清單）
- Create: `crates/sw-core/tests/self_test_p7.rs`

**Interfaces：**

```rust
// 06 T14 既有（沿用）：classify_pre_probe(core: &SessionCore, ctx: &SelfTestCtx) -> Result<PyDict, NeedProbe>、SelfTestCtx、NeedProbe
// 新增
pub struct SelfTestFinal;                                     // 組裝器命名空間
impl SelfTestFinal {
    /// 依 `self_test_responses` golden 的分支模板插入 session 與 lease ctx：每個分支各自的 key 序（例：HUMAN_INTERACTIVE_ACTIVE 為
    /// ok, classification, interactive_id, session, recommended_action, <lease ctx 4 鍵>；DEVICE_MISSING 為 ok, classification, session, recommended_action, <lease ctx>）
    pub fn assemble(classification: &str, extra: PyDict, session_public: PyDict, lease_ctx: PyDict, command_capable: bool) -> PyDict;
}
pub fn nonce_probe_result(probe_ok: bool, rx_bytes_last_10s: Option<u64>, attached_real_path: Option<&str>, current_real_path: Option<&str>,
                          attached_vtty: Option<&str>, bridge_generation: u64) -> PyDict;     // 順序 16（含 flood → RX_FLOOD／wait）
pub fn released_result(released_by: Option<&str>, released_at: Option<&str>, reason: Option<&str>, holder_pids: &[u32], holder_unknown: bool) -> PyDict;
// holder_unknown=true（Windows 或掃描失敗）→ external_holder:"none"、reclaimable:false、recommended_action:"wait_external_flash"（BC-04）
```

- [ ] **Step 1：寫測試**

```rust
// crates/sw-core/tests/self_test_p7.rs
#![cfg(feature = "test-fakes")]
mod support; use support::{golden_rows, core_for_selftest, t}; use sw_core::self_test::*;

#[test]   // 新增（RED）：位元組與 key 序相同
fn every_branch_matches_python_golden_bytes() {
    for row in golden_rows("self_test_responses") {
        let got = support::run_self_test(&row["setup"]);             // CoreHarness＋FakeSerial；nonce 分支依 setup 腳本回應
        assert_eq!(support::wire(&got), row.str("response"), "case {}", row.id());
    }
}

#[test]   // 新增（RED）
fn bootloader_wins_over_flood_and_uses_fallback_prompts() {
    let mut h = core_for_selftest("prpl-template-without-bootloader-prompts"); h.to_attached();
    h.rx(b"\r\n=> "); h.set_rx_bytes_last_10s(25_000);
    let d = h.self_test(2.0);
    assert_eq!((d.str("classification"), d.str("recommended_action"), d.str("matched_prompt")), ("BOOTLOADER", "recover_interactive", "^=> $"));
    assert_eq!(h.dev.write_calls(), 0);
}

#[test]   // 新增（RED）
fn bootloader_stuck_last_error_keeps_classification_after_tail_moves() {
    let mut h = core_for_selftest("prpl-template"); h.to_attached_with_error("BOOTLOADER_STUCK", Some("matched_prompt=^CFE> $"));
    h.rx(b"noise");
    assert_eq!(h.self_test(2.0).str("matched_prompt"), "^CFE> $");
}

#[test]   // 新增（RED）：§11.3-3
fn banner_during_self_test_suspend_does_not_replay_stale_human_input() {
    let mut h = core_for_selftest("prpl-template"); h.to_ready(); h.attach_human_raw("c1");
    let pending = h.self_test_async(2.0);                          // fg_begin：human 進 deferred
    h.human_input("c1", b"reboot\r");                              // 進 DeferredBuffer（帶目前 input_epoch）
    h.rx(b"U-Boot 2024.04\r\n");                                    // arm → invalidate_human_input → input_epoch+1
    h.finish(pending);                                             // fg_finish／resume
    assert!(!h.dev.written().windows(7).any(|w| w == b"reboot\r"), "失效上下文的人類輸入不得回放");
}
```

表格家族（`self_test_p7.rs`；每列一個 `#[test]`；鍵順序以 golden 為準）：

| 測試名 | 輸入差異 | 斷言 |
|---|---|---|
| `st_released_no_holder`（回歸） | RELEASED、holders `[]`、`holder_unknown=false` | `external_holder:"none"`、`reclaimable:true`、`device_attach` |
| `st_released_holder_unknown`（新增） | RELEASED、`[]`、`holder_unknown=true` | `external_holder:"none"`、`reclaimable:false`、`wait_external_flash`（BC-04） |
| `st_released_with_pid`（回歸） | `[123]` | `[123]`、`wait_external_flash` |
| `st_human_strict_key_order`（新增） | strict＋human lease | `HUMAN_INTERACTIVE_ACTIVE`／`wait_or_detach_console`；`interactive_id` 位於 `session` 之前（`session_manager.py:4349-4356`） |
| `st_passthrough_fallback_suggest`（新增） | profile_source fallback、suggested `prpl-template` | `pin_profile`＋`suggested_profile`＋`hint` 逐字（02 §7.5）；另含 `passthrough_ready_in_s`（SC-35，數值為剩餘安靜秒數 1 位小數、≥0） |
| `st_passthrough_attached_ready_in`（新增，SC-35） | others-template ATTACHED、自進入 ATTACHED 起已安靜 12.34 s | `classification:"PASSTHROUGH"`、`recommended_action:"console_attach"`、`passthrough_ready_in_s:17.7`；`write_calls()==0` |
| `st_passthrough_ready_zero_tx`（新增，SC-35） | passthrough READY（uboot-template 與 others-template 各一；uboot-template 有 `ready_probe`） | `classification:"PASSTHROUGH"`、`recommended_action:"none"`；不送 nonce probe：`write_calls()==0`、無 `system:self_test` TX WAL；不呼叫 `confirm_ready`；state 維持 READY |
| `st_rx_flood_attached`（新增） | ATTACHED、rx 20000 | `RX_FLOOD`／`wait`＋`rx_bytes_last_10s:20000` |
| `st_attached_family_extra_keys`（新增） | ATTACHED 族 | `attached_real_path`,`current_real_path`,`attached_vtty`,`bridge_generation` 位置照 golden |
| `st_autoboot_quiet_zero_tx`（回歸） | READY＋quiet_active | `AUTOBOOT_QUIET`／`wait`；`write_calls()==0` |
| `st_flashing_goes_to_probe`（新增） | state FLASHING | 進 nonce probe（RPC-H-07 KEEP）；fence `Flash` 擋 TX → `TARGET_UNRESPONSIVE`、不 confirm_ready |
| `st_probe_flood`（新增） | probe 失敗、20000 | `RX_FLOOD`／`wait`＋`probe_ok:false` |
| `st_probe_ok_confirms_ready`（回歸） | probe 成功 | `OK`／`none`＋`probe_ok:true`；`ready_reconfirm_pending:false` |
| `st_probe_quiet_arm_mid_probe_cancels`（新增） | probe 已提交 OS、等待 nonce 中 banner | probe 照實計為已提交（TX WAL 1 行 `system:self_test`）；nonce 未回 → `TARGET_UNRESPONSIVE`、`probe_ok:false`；不 `confirm_ready`、`quiet_active` 維持；之後無任何 `system:self_test` TX |
| `st_probe_quiet_armed_after_precheck_before_submit_zero_tx`（回歸：06 T14 `selftest_quiet_armed_after_precheck_before_submit_zero_tx` 已實作，本列改以 golden 位元組斷言；R5-F2，DF-19） | READY、quiet 未啟動；probe job 停在 `FakeWriteStep::PauseBeforeSubmit` 時 RX `U-Boot 2024.04\r\n` | `write_calls()==1`、`os_submits()==0`、無 `system:self_test` TX WAL；回應位元組等於 golden `self_test_responses` 的 AUTOBOOT_QUIET 列（鍵序 `ok,classification,session,recommended_action,<lease ctx>,command_capable`）、無 `probe_ok`；不推進 FakeClock 即回覆；`ready_reconfirm_pending:true` |
| `st_probe_console_unlock_banner_after_precheck_zero_tx`（新增，R5-F2） | profile 有 `console_unlock_env`；同上窗口內 RX `CFE version 1.0\r\n` | fence `quiet_active=true`、`auto_probe_blocked=false`；probe `reject==Some(Quiet)`、`os_submits()==0`；回應 `AUTOBOOT_QUIET`／`wait`（同上列位元組形狀）；`fsm_gen_floor` 不變（console unlock login run 不受影響） |

另加 SessionCore 整合 `self_test_nonce_probe_uses_offset_and_suspends_human`（新增）：nonce 等待以 `wait_for_regex_cancellable(.., from: Some(offset), ..)`（非整窗）；human raw owner 存在時 P6 `fg_begin`／`fg_finish` 依序發生；成功時 `confirm_ready`。

`lease_selftest.rs` 更正：`selftest_unresponsive_does_not_count_attempt` —— READY＋pending、nonce 不回 → `TARGET_UNRESPONSIVE`、`ready_reconfirm_attempts` **不變**（Python 只在 `_finish_probe_reprobe` 遞增，`session_manager.py:1483`）。

- [ ] **Step 2：執行**：`cargo test -p sw-core --features test-fakes --test self_test_p7 --test lease_selftest` → 新增列 RED；更正後的 `selftest_unresponsive_does_not_count_attempt` RED（06 實作會遞增）；回歸列 PASS。
- [ ] **Step 3：實作**：照 `session_manager.py:4305-4580` 分支順序與鍵順序擴充 06 本體；nonce probe：`last_probe_at`、`mark_tx`、送 `ready_probe.replace("${nonce}")`（source `system:self_test`、`TxClass::SelfTestProbe`、`fsm_gen: None`——R5-F2：以 `epoch`＋`quiet_active` 在提交點重驗，與 pre-check 同義；不與 login run 的 `fsm_gen_floor` 掛鉤）；probe `TxReport.reject == Some(Quiet)` → 06 T14 `OpOutcome::SelfTestRefused(Quiet)`，actor 以 `SelfTestFinal::assemble("AUTOBOOT_QUIET", …)` 產生與 pre-check 同位元組形狀的回應、不等 nonce、不 `confirm_ready`（Python 對應 02 T20 行為規格第 6 點）；否則 `wait_for_regex_cancellable(nonce, Some(offset), timeout_s)`、成功後再等 prompt（結果忽略）、`confirm_ready`；移除 06 中 self-test 失敗遞增 `ready_reconfirm_attempts` 的程式；RELEASED 的 holder 掃描沿用 06（actor 外 executor）。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2；`--test command_exec`（06）不退化。
- [ ] **Step 5：commit**：`feat(rust-selftest): 承接 self-test 完整分類與 Python 相容回應`

---

## Task 9：`recover.rs`：session recover／force recover

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p7-recover.md`（`### Added`：session recover 的完整分支順序、force recover 輪詢與 bootloader 分類回覆）
**BLOCKED-OWNER:** 實機 `p1-rst-recover`（L3，B-07）。

**Files:**
- Create: `crates/sw-core/src/recover.rs`（計畫與回覆組裝純函式）
- Modify: `crates/sw-core/src/op_runner.rs`（06 T14 `OpSpec::Recover`）、`crates/sw-core/src/session_core.rs`（`ForceRecoverPoll` timer）
- Create: `crates/sw-core/tests/recover_p7.rs`

**Interfaces：**

```rust
// 06 T14 既有（沿用）：OpSpec::Recover{timeout_s, force}、READY 的 CTRL_C／CTRL_D 路徑、06 測試 recover_* 五項
// crates/sw-core/src/recover.rs（新增）
pub struct RecoverRequest { pub timeout_s: f64, pub force: bool }
#[derive(Debug, PartialEq)]
pub enum RecoverPlan {
    Reply(PyDict),        // RELEASED／FLASHING_BUSY／SESSION_NOT_READY 等立即回覆
    Reattach,             // 無 bridge 且裝置在、且無 drain 進行中：ATTACHING＋RequestAttach
    AwaitDrain,           // drain（RECOVERING）進行中：加入 06 StopOp waiters，回條到達後再重新 plan（不得在 handle 關閉前開新 bridge，§11.3-4、arch §5.5）
    Reprobe,              // ATTACHED 非 force：ExistingBridgeProbe{RecoverReprobe}（與 auto reprobe 互斥：auto 跳過、recover 等待）
    Force,                // ATTACHED／非 READY 的 force：begin_stop(StopKind::Clear) 後輪詢 FORCE_RECOVER_POLLS×FORCE_RECOVER_POLL_S
    ReadyKeys,            // READY（force 與否皆同）：06 recover_after_failure(source="system:recover", cmd_id="")，兩鍵失敗後自動 Force
}
/// 順序逐字照 recover_session（session_manager.py:4582-4667）：SESSION_NOT_FOUND → RELEASED 早退 → FLASHING_BUSY 早退
/// → （此時才）reset reprobe、credentials_warned=false → drain 進行中 → 無 bridge（裝置在→Reattach／否則 SESSION_NOT_READY）
/// → ATTACHED（force→Force／否則 Reprobe）→ 非 READY（force→Force／否則 SESSION_NOT_READY）→ READY → ReadyKeys
pub fn plan_recover(state: &str, has_bridge: bool, draining: bool, device_present: bool, released: bool, flash_busy: bool, req: &RecoverRequest) -> RecoverPlan;
pub fn plan_resets_progress(plan: &RecoverPlan) -> bool;   // RELEASED／FLASHING_BUSY 的 Reply 為 false，其餘 true
pub fn reprobe_reply(recovered: bool, last_error: Option<&str>, last_error_detail: Option<&str>, bootloader_match: Option<&str>, session: PyDict) -> PyDict;   // SC-34：last_error／detail 取自同一 session 快照
pub fn force_reply(recovered: bool, session: Option<PyDict>) -> PyDict;
// 常數取自 sw_proto::constants::{FORCE_RECOVER_POLLS, FORCE_RECOVER_POLL_S}（Task 0 新增），不在業務碼定義
```

規則：`reprobe_reply` 未恢復時依 SC-34（OD-14 #1）回 `{"ok":true,"recovering":false,"action":"REPROBE","recovered":false,"session":…,"error_code":"SESSION_NOT_READY","last_error":<str|null>,"last_error_detail":<str|null>}`（`error_code` 固定，不再透傳 last_error；`LastErrorPassthrough` 已刪除），bootloader 證據或 `last_error == "BOOTLOADER_STUCK"` 再附 `classification:"BOOTLOADER"`、`recommended_action:"recover_interactive"`、命中時 `matched_prompt`；reprobe 失敗且 force → 轉 Force。Force 失敗 `{"ok":false,"error_code":"FORCE_RECOVER_TIMEOUT","action":"FORCE_CLEAR_REATTACH","recovered":false,"session":<dict 或 {}>}`（約 10 s 虛擬時間）。READY 路徑（不看 `force`）：CTRL_C／CTRL_D 各 `min(timeout_s, 2.0)`；成功 `PROMPT_TIMEOUT_RECOVERED`；兩鍵失敗 → `ATTACHED` → 自動 Force：成功 `{"ok":true,"error_code":"PROMPT_TIMEOUT_FORCE_RECOVERED","partial":true,"recovery_action":"FORCE_CLEAR_REATTACH","session":…}`，失敗 `{"ok":false,"error_code":"PROMPT_TIMEOUT","partial":true,"recovery_action":"NONE"}`（約 2×2 s＋10 s＝14 s 虛擬時間，`session_manager.py:3905-3929`）。force 的 clear 走 06 Two-Phase Drain；drain 逾時時仍以輪詢結果回覆，不冒用 `FORCE_RECOVER_TIMEOUT` 作內部事件名（§5.2.5）。重複 recover 加入同一 StopOp waiters（06 去重）。

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/recover_p7.rs
#![cfg(feature = "test-fakes")]
mod support; use support::{golden_rows, t, core_attached_silent, core_ready_prpl_with_fake_serial, core_attached_with_error};
use sw_core::recover::*;

#[test]
fn recover_responses_match_python_golden_bytes_and_virtual_time() {
    for row in golden_rows("recover_responses") {
        let (got, elapsed) = support::run_recover(&row["setup"]);
        assert_eq!(support::wire(&got), row.str("response"), "case {}", row.id());
        assert!((elapsed - row.f64("virtual_elapsed_s")).abs() < 0.5, "case {} 耗時 {elapsed}", row.id());
    }
}

#[test]
fn attached_force_times_out_after_ten_polls_with_exact_shape() {
    let mut h = core_attached_silent();                            // 前置：ATTACHED、target 靜默
    let reply = h.rpc_recover(RecoverRequest { timeout_s: 2.0, force: true });
    h.advance_until_reply(10.5);
    let r = reply.take().expect("10 s 內須回覆");
    assert_eq!(r.keys(), ["ok", "error_code", "action", "recovered", "session"]);
    assert_eq!(r.str("error_code"), "FORCE_RECOVER_TIMEOUT");
    assert!(h.elapsed_s() >= 10.0 && h.elapsed_s() < 10.5);
}

#[test]
fn ready_force_runs_keys_then_auto_force_and_returns_prompt_timeout() {
    let mut h = core_ready_prpl_with_fake_serial().target_silent();     // 前置：READY
    let reply = h.rpc_recover(RecoverRequest { timeout_s: 2.0, force: true });
    h.advance_until_reply(15.0);
    let r = reply.take().unwrap();
    assert_eq!(support::wire_without_version(&r), r#"{"ok":false,"error_code":"PROMPT_TIMEOUT","partial":true,"recovery_action":"NONE"}"#);
    assert!(h.elapsed_s() >= 14.0 && h.elapsed_s() < 14.5);
    assert_eq!(h.wal_tx_payloads(), vec![b"\x03".to_vec(), b"\x04".to_vec()]);
}

#[test]
fn recover_during_drain_does_not_open_before_receipt() {
    let mut h = support::core_draining();                            // RECOVERING、Phase 2 等待 handle 關閉回條
    let reply = h.rpc_recover(RecoverRequest { timeout_s: 2.0, force: false });
    assert_eq!(h.open_bridge_calls(), 0, "回條前不得重開同一端點（§11.3-4）");
    h.deliver_worker_stopped_receipt();
    assert_eq!(h.open_bridge_calls(), 1);
    assert_eq!(reply.take().unwrap().str("action"), "REATTACH");
}

#[test]
fn reprobe_unrecovered_reports_last_error_field_with_ok_true() {   // SC-34（OD-14 #1）
    let mut h = core_attached_with_error("TRANSPORT_STALL");
    let reply = h.rpc_recover(RecoverRequest { timeout_s: 2.0, force: false });
    h.complete_login(sw_core::login_fsm::LoginOutcome::NotReady("TRANSPORT_STALL".into()));
    let r = reply.take().unwrap();
    assert_eq!((r.bool("ok"), r.str("action"), r.bool("recovered"), r.str("error_code")), (true, "REPROBE", false, "SESSION_NOT_READY"));
    assert_eq!(r.keys(), ["ok", "recovering", "action", "recovered", "session", "error_code", "last_error", "last_error_detail"]);
    assert_eq!(r.str("last_error"), "TRANSPORT_STALL");
    assert_eq!(r["last_error_detail"], r["session"]["last_error_detail"]);   // transport_stall_hint 文字，與 session 快照同值
}
```

表格家族：

| 測試名 | 精確斷言 |
|---|---|
| `released_recover_does_not_reset_credentials_warned` | RELEASED、`credentials_warned=true` → 回 `{"ok":true,"released":true,…}` 且 `credentials_warned` 仍 true、reprobe 進度不變 |
| `flashing_busy_does_not_reset_progress` | FLASHING → `FLASHING_BUSY`＋`selector`＋`session`；`reprobe_attempts` 不變 |
| `recover_no_bridge_device_absent` | `{"ok":false,"error_code":"SESSION_NOT_READY","session":…}` |
| `recover_reprobe_bootloader_adds_classification` | tail `=> ` → `error_code:"SESSION_NOT_READY"`、`last_error:"BOOTLOADER_STUCK"`、`classification:"BOOTLOADER"`、`matched_prompt:"^=> $"`（SC-34：BOOTLOADER 判定看 `last_error`，不看 `error_code`） |
| `recover_reprobe_unrecovered_null_last_error`（新增，SC-34） | ATTACHED、`last_error=None`、reprobe 未達 READY → `error_code:"SESSION_NOT_READY"`、`last_error:null`、`last_error_detail:null` |
| `recover_passthrough_attached_never_probes`（新增，SC-35） | passthrough（uboot-template，有 `ready_probe`）ATTACHED、非 force → `{"ok":true,"recovering":false,"action":"REPROBE","recovered":false,"session":…,"error_code":"SESSION_NOT_READY","last_error":null,"last_error_detail":null}`；`write_calls()==0`；不建立 `ExistingBridgeProbe` op |
| `recover_passthrough_ready_never_sends_keys`（新增，SC-35） | passthrough READY、非 force → `recovered:true`（state 維持 READY）、不送 `\x03`／`\x04`、`write_calls()==0` |
| `recover_passthrough_force_times_out`（新增，SC-35） | passthrough ATTACHED＋force → 走 Force（clear＋reattach）；10 次輪詢內未滿 30 s 安靜 → `FORCE_RECOVER_TIMEOUT`（形狀同 `attached_force_times_out_after_ten_polls_with_exact_shape`）；reattach 後 ATTACHED、零 TX |
| `recover_reprobe_failure_with_force_goes_to_force` | ATTACHED reprobe 失敗＋force → Force 回覆形狀 |
| `recover_resets_credentials_warned` | 先 CREDENTIALS_UNRESOLVED 已告警 → recover 後再遇終態會再告警一次 |
| `recover_non_ready_non_force` | ATTACHING → `SESSION_NOT_READY`＋session |
| `recover_ready_force_success` | 自動 force 在第 3 次輪詢 READY → `PROMPT_TIMEOUT_FORCE_RECOVERED` |
| `duplicate_force_recover_joins_same_stop` | 兩個並發 force → 只有一個 StopOp、兩個回覆 |
| `stale_force_recover_poll_is_ignored` | 第一次 force 的 `ForceRecoverPoll{stop_id:1, n}` 在第二次 force（stop_id 2）之後才到 → 不回覆、不影響第二次的輪詢計數 |
| `force_clear_invalidates_human_input` | force 期間 human 輸入進 deferred → drain Phase 1 `invalidate_human_input(Recovery)`；reattach 後不回放（§11.3-3） |
| `recover_ready_quiet_skips_keys`（回歸 06 `quiet_rechecked_before_each_recover_key`） | quiet active → 不送任何 key → ATTACHED → 自動 Force |

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-core --features test-fakes --test recover_p7`（06 既有 `command_exec` 的 recover_* 五項為回歸，維持 PASS）。
- [ ] **Step 3：實作**：`plan_recover` 照上方順序；`plan_resets_progress` 為真時才 reset reprobe 與 `credentials_warned`；`AwaitDrain` 經 06 StopOp waiters；force 輪詢以 actor timer `ForceRecoverPoll{stop_id, n}` 每 `FORCE_RECOVER_POLL_S` 檢查 READY（不阻塞 mailbox；過期 `stop_id` 丟棄；呼叫端 RPC 經 reply channel 等待，LONG_RPC 上限 600 s）；Reprobe 與 auto reprobe 互斥（auto 遇 recover 進行中跳過、recover 遇 auto 進行中等待其完成事件）。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2；`--test command_exec --test drain_release`（06）不退化。
- [ ] **Step 5：commit**：`feat(rust-recover): 承接 session recover 完整分支與 force recover`

---

## Task 10：`capture.rs`：agent log capture（reader 端 observer）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p7-log-capture.md`（`### Added`：agent log 寫入移至 reader 端、不經 actor、不丟資料；log RPC 回應與 Python 位元組相容）
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-core/src/capture.rs`（06 T14 `AgentLogCapture` 保留為 actor 端中繼資料）、`crates/sw-core/src/session_core.rs`（log_start／stop／status 改為 Effect＋完成事件）、`crates/sw-core/src/uart_bridge.rs`（`ReaderCtx.observers`，與 Task 16-wire 共用）
- Create: `crates/sw-core/tests/capture_p7.rs`

**設計（取代先前「1024 佇列＋滿 50 ms 丟棄」）：** Python 在 RX callback（reader 執行緒）內同步 `fp.write(chunk); fp.flush()`，從不丟資料。Rust 對應：`CaptureObserver` 實作 `RxObserver`（Task 16-wire 定義），掛在該 session 的 reader 上，於 WAL append 之後在 reader 執行緒同步寫檔（與 WAL 同步 append 同屬 reader 熱路徑既有的阻塞 I/O，不經 actor、不經佇列、無丟棄、不需 arch §5.10 新列）。actor 只發 Effect，檔案開關由 executor thread 執行並以完成事件回 actor，actor 永不阻塞於檔案 I/O。

**Interfaces：**

```rust
// crates/sw-core/src/capture.rs（新增；06 的 AgentLogCapture／BackgroundCapture 沿用）
pub struct CaptureStats { pub line_count: AtomicU64, pub byte_count: AtomicU64 }
pub struct CaptureObserver { /* Mutex<Option<ActiveFile{capture_id: String, file: Box<dyn AppendFile>, stats: Arc<CaptureStats>}>>, foreground_busy: AtomicBool */ }
impl CaptureObserver {
    pub fn new() -> Arc<Self>;
    pub fn set_foreground_busy(&self, busy: bool);                                   // actor 在前景開始／結束時發布（EVT-D-L1）
    pub fn install(&self, capture_id: String, file: Box<dyn AppendFile>) -> Arc<CaptureStats>;   // executor 呼叫
    pub fn remove(&self, capture_id: &str) -> Option<(u64, u64)>;                    // executor 呼叫；取鎖保證先前寫入完成；回 (line_count, byte_count)
}
impl RxObserver for CaptureObserver { fn on_rx(&self, com: &str, data: &[u8], wal_seq: u64); }  // clean_text(decode_utf8_replace) 非空且 !foreground_busy 才寫；寫入失敗吞掉（Python except: pass）
// session_core.rs（新增 Effect／事件；修訂 06 T11）
// Effect::CaptureOpen { capture_id: String, path: PathBuf }      → executor：FsOps::mkdirs(dir)（失敗 → reply framing EXCEPTION）→ open_append → install
// ControlEvent::CaptureOpened { capture_id, result: Result<(), PalError> }
// Effect::CaptureClose { capture_id: String }                    → executor：remove → CaptureClosed
// ControlEvent::CaptureClosed { capture_id, line_count: u64, byte_count: u64 }
pub fn log_path_for(log_dir: &str, com: &str, unix_s: i64, offset_s: i32) -> String;        // "{log_dir}/{com}_{%y%m%d-%H%M%S}.log"（本地時間）
pub fn log_start_reply(cap: &AgentLogCapture, already_active: bool, session: PyDict) -> PyDict;
pub fn log_open_failed_reply(err: &PalError) -> PyDict;                                      // {"ok":false,"error_code":"LOG_OPEN_FAILED","detail":<str(OSError)>}
pub fn log_stop_reply(cap: &AgentLogCapture, line_count: u64, byte_count: u64) -> PyDict;    // 無 session
pub fn no_active_capture_reply(session: PyDict) -> PyDict;
pub fn log_status_reply(cap: Option<(&AgentLogCapture, u64, u64)>, session: PyDict) -> PyDict;
```

規則：`log_dir = profile.log_dir or LOG_DIR`（動態 session 不套 defaults.log_dir，EVT-D-D9 KEEP）；RX 寫入僅在 `!foreground_busy`（EVT-D-L1 KEEP）、寫 `clean_text` 後文字、`byte_count += len(utf8)`、`line_count += count('\n')`；session detach（drain Phase 1）時 actor 發 `CaptureClose`；`started_at` 用 `iso_utc_now`；capture_id 用 `uuid4_dashed(SysEnv::random_bytes)`；各回覆的 key 順序以 `log_responses` golden 為準。

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/capture_p7.rs
#![cfg(feature = "test-fakes")]
mod support; use support::{golden_rows, tempdir, core_ready_prpl_with_capture_dir};
#[test]
fn foreground_rx_is_not_captured_and_counts_match_python_rules() {
    let dir = tempdir();
    let mut h = core_ready_prpl_with_capture_dir(dir.path());
    let start = h.rpc_log_start();                               // executor 完成 CaptureOpened 後才回覆
    assert_eq!(start.keys(), ["ok", "capture_id", "log_path", "session"]);
    h.rx(b"hello\r\n\xe4\xb8\x96\xe7\x95\x8c\n");                // clean_text → "hello\n世界\n"
    h.set_foreground_busy(true); h.rx(b"SECRET-FG\n"); h.set_foreground_busy(false);
    let stop = h.rpc_log_stop();
    assert_eq!(stop.keys(), ["ok", "capture_id", "log_path", "line_count", "byte_count", "started_at"]);
    assert_eq!((stop.u64("line_count"), stop.u64("byte_count")), (2, "hello\n世界\n".len() as u64));
    assert_eq!(std::fs::read_to_string(stop.str("log_path")).unwrap(), "hello\n世界\n");
}

#[test]
fn log_responses_match_python_golden_bytes() {
    for row in golden_rows("log_responses") {
        assert_eq!(support::wire(&support::run_log_rpc(&row["setup"])), row.str("response"), "case {}", row.id());
    }
}

#[test]
fn slow_capture_fs_does_not_block_actor_control_lane() {
    let mut h = support::core_with_blocking_capture_fs();        // FakeFs：open／write 卡 2 s
    h.rpc_log_start_async();
    let t0 = std::time::Instant::now();
    let _ = h.rpc_get_state();                                   // actor 不等檔案 I/O
    assert!(t0.elapsed() < std::time::Duration::from_millis(50));
}
```

表格家族：

| 測試名 | 精確斷言 |
|---|---|
| `log_path_uses_local_time` | `log_path_for("/x","COM2", 1_700_000_000, 28800)=="/x/COM2_231115-061320.log"` |
| `log_start_already_active` | 第二次 → `{"ok":true,"already_active":true,"capture_id","log_path","session"}` |
| `log_open_failed_detail_is_python_oserror_str` | 目錄唯讀 → `detail` 以 `[Errno 13] Permission denied: '` 開頭（POSIX） |
| `log_stop_without_capture` | `{"ok":false,"error_code":"NO_ACTIVE_CAPTURE","session":…}` |
| `log_status_inactive_and_active` | 無 → `{"ok":true,"active":false,"session"}`；有 → 鍵順序 `ok,active,capture_id,log_path,line_count,byte_count,started_at,session` |
| `detach_auto_stops_capture` | release 後 `log_status.active==false`、檔案已關閉 |
| `stop_waits_for_inflight_write` | reader 正寫入大 chunk 時 stop → `byte_count` 含該 chunk（mutex 保證） |
| `no_drop_under_burst` | 10000 個 chunk 連續 RX → 檔案位元組數＝`byte_count`＝輸入 clean_text 總長（無丟棄） |

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-core --features test-fakes --test capture_p7`；06 `lease_selftest::log_capture_start_stop_status`／`log_capture_open_failure` 為回歸。
- [ ] **Step 3：實作**：照上方設計；06 actor 端直接寫檔的程式移除，改為 Effect／完成事件。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2；`--test lease_selftest`（06）不退化。
- [ ] **Step 5：commit**：`feat(rust-capture): agent log 改由 reader 端寫入並對齊 Python 回應`

---

## Task 11：Event schema＋`pyre/redos.rs`

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p7-event-schema.md`（`### Added`：事件規則 schema 驗證與無 re2 ReDoS 判定；`### Fixed`：DF-08 contains 規則 flags 驗證）
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-core/src/event_engine/schema.rs`、`crates/sw-proto/src/pyre/redos.rs`
- Create: `crates/sw-core/tests/event_schema.rs`、`crates/sw-proto/tests/redos.rs`

**Interfaces：**

```rust
// crates/sw-proto/src/pyre/redos.rs
pub fn ambiguity_score(sp: &crate::pyre::sre::SubPattern) -> u32;
pub fn has_backref(sp: &crate::pyre::sre::SubPattern) -> bool;
pub fn maybe_redos_suspicious(pattern: &str, flags: PyFlags) -> Result<bool, PyReError>;   // score>=2 || has_backref
// crates/sw-core/src/event_engine/schema.rs
pub struct Pattern { pub kind: String, pub value: String, pub flags: String }
pub struct Handler { pub exec: Option<Vec<String>>, pub shell: Option<String> }
pub struct Rule { pub schema_version: i64, pub owner: String, pub name: String, pub rule_id: String, pub kind: String,
                  pub selectors: Vec<String>, pub profile: String, pub level: String, pub pattern: Pattern, pub scope: String,
                  pub max_fires: Option<PyValue /* int 或 bool，原值 */>, pub cooldown_ms: PyInt, pub timeout_ms: PyInt,   // PyInt 含 Big，保留 Python 任意精度
                  pub handler: Handler, pub auto_enable_com_on_load: bool, pub debug: bool, pub raw: PyDict }
impl Rule { pub fn cooldown_ms_sat(&self) -> i128; pub fn timeout(&self) -> std::time::Duration; }   // 執行時轉換：超出 i128 飽和；timeout 夾到 Duration::MAX（比較語意與 Python 大整數一致：永不到期／永遠冷卻）
pub enum RuleError { Schema(String) /* RuleSchemaError */, Value(String) /* 其他 ValueError */, Type(String) /* TypeError */ }
pub fn validate_rule_dict(obj: &PyValue) -> Result<Rule, RuleError>;
pub fn flags_from_string(s: &str) -> Result<PyFlags, RuleError>;   // i/s/m；其他 → Schema("unsupported regex flag: {ch}")
pub const NAME_RE: &str = r"^[a-z0-9][a-z0-9-]{0,63}$";
```

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/event_schema.rs
mod support; use support::golden_rows; use sw_core::event_engine::schema::*;
#[test]
fn every_python_validation_outcome_matches() {
    for row in golden_rows("event_schema") {
        match (validate_rule_dict(&row["input"]), row.bool("ok")) {
            (Ok(rule), true) => assert_eq!(sw_proto::pyjson::dumps(&rule.raw.clone().into(), &support::RULE_FILE), row.str("raw_dump"), "case {}", row.id()),
            (Err(RuleError::Schema(m)), false) => { assert_eq!(row.str("exc_type"), "RuleSchemaError"); assert_eq!(m, row.str("message"), "case {}", row.id()); }
            (Err(RuleError::Value(_)), false) => assert_eq!(row.str("exc_type"), "ValueError", "case {}", row.id()),
            (Err(RuleError::Type(_)), false) => assert_eq!(row.str("exc_type"), "TypeError", "case {}", row.id()),
            (got, want) => panic!("case {} 期望 ok={want} 實得 {:?}", row.id(), got.map(|r| r.rule_id)),
        }
    }
}
#[test]
fn contains_with_bad_flag_is_rejected_df08() {
    let v = support::rule_json(r#"{"pattern":{"kind":"contains","value":"x","flags":"q"}}"#);
    assert!(matches!(validate_rule_dict(&v), Err(RuleError::Schema(m)) if m == "unsupported regex flag: q"));
}
```

```rust
// crates/sw-proto/tests/redos.rs
#[test]
fn redos_golden() {
    for row in support::golden_rows("redos") {
        let flags = sw_proto::pyre::PyFlags::from_rule_flags(row.str("flags")).unwrap();
        match sw_proto::pyre::redos::maybe_redos_suspicious(row.str("pattern"), flags) {
            Ok(s) => { assert!(row.bool("compile_ok"), "case {}", row.id()); assert_eq!(s, row.bool("suspicious"), "case {}", row.id()); }
            Err(_) => assert!(!row.bool("compile_ok"), "case {}", row.id()),
        }
    }
}
```

表格家族（`event_schema.rs`）：

| 測試名 | 精確斷言 |
|---|---|
| `schema_version_python_eq` | `1`、`1.0`、`true` 通過；`"1"`、`2` → `schema_version must be 1` |
| `name_re_allows_trailing_newline` | `name:"abc\n"` 通過（Python `$` 允許結尾 `\n`），`rule_id` 衍生為 `owner.abc\n`。已知限制（照抄 Python）：此 rule_id 因 DF-01 的 `\Z` 無法經 `event.rule_delete`／`event.reset` 操作，只能刪檔；調和表 DF-01 列補註，不另修 |
| `rule_id_must_equal_derived` | `rule_id:"x.y"` 與衍生不符 → `rule_id must equal 'o.n'` |
| `regex_len_512_chars` | 512 字元通過、513 → `pattern.value regex 過長（上限 512 字元，避免 ReDoS）` |
| `redos_rejects_a_plus_b_plus` | `a+b+` → 16 號長訊息逐字；`temp=(\d+)C`、`Kernel panic`、`(?=x)abc` 通過 |
| `cooldown_python_int` | `"5000"`→5000、`1.5`→1、`true`→1；`"abc"` → `RuleError::Value`；`null` → `RuleError::Type` |
| `big_ints_beyond_i128_accepted` | `cooldown_ms`／`timeout_ms`／`max_fires` 為 `2**128` → 通過（與 golden 相同）、`raw` 原值寫回；`cooldown_ms_sat()==i128::MAX` |
| `max_fires_bool_passes` | `max_fires:true` 通過且 `rule.max_fires==Some(Bool(true))`；`-1`、`"3"` → `max_fires must be null or non-negative int` |
| `handler_exactly_one` | 兩者皆有、皆空 → `handler must have exactly one of 'exec' (list[str]) or 'shell' (str)` |
| `truthiness_flags` | `auto_enable_com_on_load:"false"` → true（EVT-D-C3 KEEP） |
| `raw_preserves_unknown_keys` | 未知鍵 `x_note` 原樣保留於 `raw` |

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-core --test event_schema`；`cargo test -p sw-proto --test redos`。
- [ ] **Step 3：實作**：移植 `schema.py:114-342`；`re.compile` 等價為 `pyre::compile(value, flags)`（Untranslatable 視同 `re.error`，訊息 `pattern.value is not a valid regex: <PyReError 的 Python str>`）；ReDoS 檢查一律執行（Rust 無 re2 路徑，EVT-D-S15）；Python `int()` 經 `py_int`，非 RuleSchemaError 的失敗映射 `Value`／`Type`；`re.match(_NAME_RE, s)` 以 `pyre` 的 match 語意（錨定開頭、`$` 可匹配結尾 `\n` 前）。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2。
- [ ] **Step 5：commit**：`feat(rust-event): 承接事件規則 schema 與 ReDoS 判定`

---

## Task 12：`line_buffer.rs`＋`matcher.rs`

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p7-event-matcher.md`（`### Added`：LineBuffer 16 KiB 切行、ANSI 剝除與有界 matcher）
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-core/src/event_engine/{line_buffer,matcher}.rs`
- Create: `crates/sw-core/tests/event_line_buffer.rs`、`crates/sw-core/tests/event_matcher.rs`

**Interfaces：**

```rust
// line_buffer.rs
pub struct LineBuffer { /* buf: Vec<u8>, max: usize */ }
impl LineBuffer { pub fn new(max_line_bytes: usize) -> Self; pub fn feed(&mut self, data: &[u8]) -> Vec<String>; }
// matcher.rs
pub struct MatchResult { pub matched_text: String, pub groups: Vec<Option<String>> }
pub struct PatternMatcher { /* PyRegex */ }
impl PatternMatcher { pub fn new(p: &Pattern) -> Result<Self, RuleError>; pub fn eval(&self, line: &str) -> Option<MatchResult>; }  // regex：只看前 4096 字元
pub fn apply_cooldown(rule: &Rule, c: &Counter, now_ms: i64) -> bool;
pub fn apply_max_fires(rule: &Rule, c: &Counter) -> bool;
pub fn apply_scope(rule: &Rule, active_cmd_id: Option<&str>) -> bool;
pub fn apply_profile(rule: &Rule, com_profile: Option<&str>) -> bool;
pub trait MatcherContext: Send + Sync {
    fn active_cmd_id(&self, com: &str) -> Option<String>; fn com_profile(&self, com: &str) -> Option<String>; fn now_ms(&self) -> i64;
    fn emit_fire(&self, fire: MatcherFire); fn emit_event(&self, ev: PyDict); fn counter_for(&self, rule_id: &str) -> Counter;
}
pub struct MatcherFire { pub rule: Arc<Rule>, pub selector: String, pub m: MatchResult, pub wal_seq: u64, pub matched_at: i64, pub active_cmd_id: Option<String> }
pub struct MatcherWorker { /* thread "event-matcher"；crossbeam bounded(1024) 但 drop-oldest 由 feed_line 實作 */ }
impl MatcherWorker { pub fn start(rules: Vec<Arc<Rule>>, ctx: Arc<dyn MatcherContext>, queue_max: usize) -> Result<Self, RuleError>;
                     pub fn feed_line(&self, selector: &str, line: String, wal_seq: u64);
                     pub fn replace_rules(&self, rules: Vec<Arc<Rule>>) -> Result<(), RuleError>;
                     pub fn stop(self); }
```

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/event_line_buffer.rs
mod support; use support::golden_rows; use sw_core::event_engine::line_buffer::LineBuffer;
#[test]
fn line_buffer_matches_python_golden() {
    for row in golden_rows("line_buffer") {
        let mut lb = LineBuffer::new(16384);
        for (i, feed) in row.b64_list("feeds").iter().enumerate() {
            assert_eq!(lb.feed(feed), row.str_vec_at("outputs", i), "case {} feed {}", row.id(), i);
        }
    }
}
#[test]
fn exactly_16384_byte_line_not_truncated_16385_is() {
    let mut lb = LineBuffer::new(16384);
    let ok = vec![b'a'; 16384]; let mut d = ok.clone(); d.push(b'\n');
    assert_eq!(lb.feed(&d), vec!["a".repeat(16384)]);
    let mut d2 = vec![b'b'; 16385]; d2.push(b'\n');
    assert_eq!(lb.feed(&d2), vec![format!("{} ...truncated", "b".repeat(16384))]);
}
```

```rust
// crates/sw-core/tests/event_matcher.rs
#[test]
fn queue_overflow_drops_oldest_and_emits_event_dropped() {
    let ctx = support::RecordingMatcherCtx::blocking();     // emit_fire 會阻塞直到 release，用來塞滿 queue
    let w = MatcherWorker::start(vec![support::rule_contains("x", &["ALL"])], ctx.clone(), 4).unwrap();
    w.feed_line("COM0", "x0".into(), 0);                    // 被 worker 取出並卡在 emit_fire
    ctx.wait_blocked();
    for i in 1..=5 { w.feed_line("COM0", format!("x{i}"), i); }   // 容量 4：第 5 筆把 seq=1 擠掉
    let dropped = ctx.events_of_type("event_dropped");
    assert_eq!(dropped.len(), 1);
    assert_eq!(support::dumps_default(&dropped[0]), r#"{"type": "event_dropped", "reason": "matcher_queue_overflow", "selector": "COM0", "wal_seq": 1}"#);
    ctx.release(); w.stop();
}
```

表格家族（`event_matcher.rs`）：

| 測試名 | 精確斷言 |
|---|---|
| `regex_input_capped_4096_chars` | 目標字在第 4097 字元 → 不命中；`contains` 不受限 → 命中 |
| `contains_flags_i` | `contains`＋`flags:"i"` 大小寫不敏感 |
| `groups_none_for_unparticipating` | `(a)|(b)` 對 `b` → `groups==[None, Some("b")]` |
| `selector_filter_com_name_only` | 規則 selectors `["COM1"]` 對 `COM0` 不評估；`ALL` 全評估 |
| `gate_order_and_debug_skipped` | scope→profile→max_fires→cooldown 順序；`debug:true` 時發 `match_skipped` 與四種 reason |
| `cooldown_from_last_fire` | `cooldown_ms=1000`、`last_fire_ts=5000`：`now=5999` false、`6000` true |
| `max_fires_zero_never` | `max_fires=0` 永不觸發；`exhausted=true` → false |
| `scope_active_foreground_string` | `active_cmd_id=Some("foreground")` 時 `spontaneous` 不過、`command_output` 過（EVT-D-E3 KEEP） |
| `multiple_rules_fire_same_line` | 兩條規則都 emit_fire |
| `replace_rules_failure_keeps_old_compiled` | 新規則集合含編譯失敗 → `Err`、舊規則仍生效 |
| `evaluate_panic_is_contained` | ctx.counter_for panic → WARNING `event matcher 求值失敗，丟棄此 item（selector=COM0）`、worker 繼續 |
| `stop_join_2s_warning` | worker 卡住 → stop 2 s 後 WARNING `event-matcher thread did not exit within 2s; context may receive late callbacks` |

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-core --test event_line_buffer --test event_matcher`。
- [ ] **Step 3：實作**：LineBuffer 照 09 §3.3 偽碼（每次 feed 最多切一段無換行超長塊、`idx > max` 嚴格、截斷塊不剝 `\r`、`decode_utf8_replace`→`strip_ansi`→截斷追加 `" ...truncated"`）；matcher 照 09 §3.2；queue 以 `Mutex<VecDeque>`＋`Condvar` 實作 drop-oldest（容量 1024），worker `get(timeout=0.1)`。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2。
- [ ] **Step 5：commit**：`feat(rust-event): 承接 LineBuffer 與有界 matcher`

---

## Task 13：`counter.rs`＋`registry.rs`＋`event_log.rs`（含 Python DF-18）

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high（Rust 部分）；**Task 13-py**（Python DF-18 兩行改動與其測試）：Test author agy-gemini-3.8-flash-high、Implementer haiku-xhigh，獨立 commit
**Changelog fragment:** `changelog.d/rust-p7-event-store.md`（`### Added`：事件規則、counter 與 events.ndjson 的 Rust 承接；`### Fixed`：DF-01 rule_id 路徑穿越、DF-09 載入不崩潰、DF-18 Windows 覆寫改名）
**BLOCKED-OWNER:** 無（本 Task 不改 PAL；`FsOps` 寫入側由 Task 6 實作。Windows 覆寫改名以 WSL interop 原生執行取得證據；Python 側 Windows 行為以 Windows Python 3.12 經 WSL interop 執行 `tests/test_event_replace_df18.py` 取得）。

**Files:**
- Modify: `crates/sw-core/src/event_engine/{counter,registry,event_log}.rs`
- Modify（13-py）: `sw_core/event_engine/counter.py:57`、`sw_core/event_engine/registry.py:98`（`os.rename` → `os.replace`，DF-18）、`changelog.d/rust-p7-df18-python.md`（`### Fixed`：Windows 上第二次寫入同一事件規則或 counter 不再失敗）、`docs/design-event-trigger.md`（若描述到 rename 語意則同步；無則於 PR 說明 R-18 免同步理由並上 `policy-exempt:docs-sync`）
- Create: `crates/sw-core/tests/event_store.rs`、`tests/test_event_replace_df18.py`（13-py）

**Interfaces：**

```rust
// counter.rs
#[derive(Clone, Debug, Default, PartialEq)] pub struct Counter { pub fires: PyInt, pub last_fire_ts: Option<PyInt>, pub exhausted: bool }   // PyInt：counter 檔可被手改成超出 i128 的值，Python 照收；遞增經 PyInt::add_small(1)
impl Counter { pub fn to_json(&self) -> PyDict; pub fn from_json(v: &PyValue) -> Counter; }
pub struct CounterStore { /* root, FsOps, write-through cache（EVT-D-E11） */ }
impl CounterStore { pub fn new(root: PathBuf, fs: Arc<dyn FsOps>) -> PalResult<Self>; pub fn load(&self, rule_id: &str) -> Counter;
                    pub fn save(&self, rule_id: &str, c: &Counter) -> PalResult<()>; pub fn clear(&self, rule_id: &str); }
pub fn valid_rule_id(rule_id: &str) -> bool;    // DF-01 正規式
// 寫檔：`FsOps::create_truncate(<path>.tmp, bytes)` → `FsOps::replace(tmp, path)`（Python `open(tmp,"w")` 截斷語意：殘留 tmp 被覆寫，不得續寫成壞 JSON）
// 讀檔／列目錄：沿用 P3 `FsRead::{read, list_dir}`（排序在呼叫端做 `sorted()`），不在 FsOps 重複定義
// registry.rs
pub struct FailedLoad { pub path: String, pub reason: String }
pub struct LoadResult { pub rules: Vec<Arc<Rule>>, pub failed: Vec<FailedLoad> }
pub struct DiffResult { pub added: Vec<String>, pub removed: Vec<String>, pub changed: Vec<String> }   // 升冪（RPC-H-13）
pub struct RuleRegistry { /* root, FsOps */ }
impl RuleRegistry { pub fn new(root: PathBuf, fs: Arc<dyn FsOps>) -> PalResult<Self>; pub fn load_all(&self) -> LoadResult;
                    pub fn upsert(&self, obj: &PyValue) -> Result<Arc<Rule>, UpsertError>; pub fn delete(&self, rule_id: &str) -> bool;
                    pub fn diff_against(&self, prev: &[Arc<Rule>]) -> DiffResult; }
pub enum UpsertError { Rule(RuleError), Io(PalError) }   // Rule → INVALID_RULE_SCHEMA（Schema 與 Value）；Type／Io → EXCEPTION
// event_log.rs
pub struct EventLogger { /* path, rotate_bytes=10485760, backup_count=3, Mutex */ }
impl EventLogger { pub fn new(path: PathBuf, fs: Arc<dyn FsOps>, clock: Arc<dyn Clock>) -> PalResult<Self>; pub fn write(&self, ev: PyDict);
                   pub fn tail(&self, rule_id: Option<&PyValue>, selector: Option<&PyValue>, since_ts: Option<&PyValue>, n: Option<&PyValue>) -> Result<Vec<PyValue>, PyErr>; }
```

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/event_store.rs
mod support; use support::{golden_rows, tempdir, native_fs, fake_clock};
use sw_core::event_engine::{counter::*, registry::*, event_log::*};

#[test]
fn counter_file_bytes_match_python() {
    let d = tempdir(); let store = CounterStore::new(d.path().into(), native_fs()).unwrap();
    store.save("o.n", &Counter { fires: 3.into(), last_fire_ts: Some(1_700_000_000_123i64.into()), exhausted: false }).unwrap();
    assert_eq!(std::fs::read_to_string(d.path().join("o.n.counter.json")).unwrap(),
               r#"{"fires": 3, "last_fire_ts": 1700000000123, "exhausted": false}"#);
    store.save("o.n", &Counter { fires: 4.into(), ..Default::default() }).unwrap();   // 第二次覆寫（DF-18）
    assert_eq!(store.load("o.n").fires, 4.into());
}

#[test]
fn rule_id_traversal_is_refused_df01() {
    let d = tempdir(); let outside = d.path().join("victim.json"); std::fs::write(&outside, "{}").unwrap();
    let reg = RuleRegistry::new(d.path().join("events.d"), native_fs()).unwrap();
    assert!(!reg.delete("../victim"));
    assert!(outside.exists());
}

#[test]
fn registry_load_matches_python_golden_reasons() {
    for row in golden_rows("registry_load") {
        let d = tempdir(); row.materialize_dir(d.path());
        let res = RuleRegistry::new(d.path().into(), native_fs()).unwrap().load_all();
        assert_eq!(res.rules.iter().map(|r| r.rule_id.clone()).collect::<Vec<_>>(), row.str_vec("rules"), "case {}", row.id());
        let failed: Vec<(String, String)> = res.failed.iter().map(|f| (basename(&f.path), f.reason.clone())).collect();
        assert_eq!(failed, row.pairs("failed"), "case {}", row.id());
    }
}

#[test]
fn tail_n_semantics_match_python_slicing() {
    for row in golden_rows("event_log_tail") {
        let d = tempdir(); let log = EventLogger::new(d.path().join("events.ndjson"), native_fs(), fake_clock(row.i64("ts0"))).unwrap();
        for ev in row.dict_list("events") { log.write(ev); }
        let got = log.tail(row.opt("rule_id"), row.opt("selector"), row.opt("since_ts"), row.opt("n"));
        match got { Ok(v) => assert_eq!(support::dumps_default_list(&v), row.str("entries"), "case {}", row.id()),
                    Err(_) => assert_eq!(row.str("entries"), "EXCEPTION", "case {}", row.id()) }
    }
}
```

表格家族：

| 測試名 | 精確斷言 |
|---|---|
| `event_line_format_default_separators_ts_last` | `write({"type":"rule_loaded","rule_id":"o.n"})` 檔案內容 `{"type": "rule_loaded", "rule_id": "o.n", "ts": <ms>}\n` |
| `rotate_when_size_plus_incoming_exceeds` | 預設 10485760：恰等於不輪替、超過 1 byte 輪替；`.1..3` 遞移、`.4` 不存在 |
| `tail_reads_current_file_only` | `.1` 內容不出現在 tail |
| `upsert_rule_file_indent2_sorted_no_newline` | 規則檔位元組等於 Python `json.dump(raw, ensure_ascii=False, indent=2, sort_keys=True)` |
| `upsert_overwrites_existing_df18` | 同 rule 連續 upsert 兩次皆成功（Windows 原生同測） |
| `load_all_valueerror_typeerror_are_failed_df09` | `cooldown_ms:"abc"`、`null` → 進 `failed`，不 panic |
| `counter_load_bad_file_is_default` | 壞 JSON／不存在 → `Counter::default()` |
| `stale_tmp_is_overwritten` | 預先放一個內容為 `{"fires": 99, "garbage` 的 `o.n.counter.json.tmp` → `save(fires=1)` 後正式檔內容逐字為 Python 輸出、`load().fires==1`（未續寫） |
| `reset_with_invalid_rule_id_counts_zero_df01` | `valid_rule_id("a/b")==false`；`valid_rule_id("o.n")==true`；`"o.n\n"` → false（`\Z`） |

```python
# tests/test_event_replace_df18.py（Python 側 DF-18）
from sw_core.event_engine.counter import CounterStore, Counter
from sw_core.event_engine.registry import RuleRegistry
import unittest.mock as m, os

def test_counter_save_uses_replace(tmp_path):
    store = CounterStore(str(tmp_path))
    with m.patch("sw_core.event_engine.counter.os.replace", wraps=os.replace) as rep, \
         m.patch("sw_core.event_engine.counter.os.rename", side_effect=AssertionError("must not use os.rename")):
        store.save("o.n", Counter(fires=1)); store.save("o.n", Counter(fires=2))
    assert rep.call_count == 2 and store.load("o.n").fires == 2

def test_registry_upsert_uses_replace(tmp_path):
    reg = RuleRegistry(str(tmp_path))
    rule = {"schema_version": 1, "owner": "o", "name": "n", "kind": "tool", "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "x"}, "handler": {"exec": ["true"]}}
    with m.patch("sw_core.event_engine.registry.os.rename", side_effect=AssertionError("must not use os.rename")):
        reg.upsert(rule); reg.upsert(rule)
```

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-core --test event_store`；`python3 -m pytest -q tests/test_event_replace_df18.py`。
- [ ] **Step 3：實作**：照 09 §3.4、§3.5、§3.8；tmp→正式檔一律 `FsOps::replace`；JSON 讀取 `read_bytes`→UTF-8 解碼（失敗依 DF-09 golden 的 reason 格式）→`pyjson::loads`（失敗 reason＝`"read/parse failed: " + py_message()`）；OSError reason 用 `PalError.py_message`；counter write-through cache（同一 daemon 唯一寫入者）。Python：兩處 `os.rename` 改 `os.replace`，同步更新 `tests/test_event_counter.py`／`test_event_registry.py` 不受影響。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2；`python3 -m pytest -q tests/`；`scripts/rust/win-test.sh --package sw-core --test event_store`（以 `--test` 選 binary；核對報告該 exe `passed >= 1`）；13-py 另以 Windows Python 執行 `py -3.12 -m pytest -q tests/test_event_replace_df18.py`（WSL interop，於 Windows 本機目錄）。
- [ ] **Step 5：commit**：`feat(rust-event): 承接規則、counter 與事件 log 儲存並修正 Windows 覆寫改名`

---

## Task 14：PAL `HandlerSpawner`（POSIX／Windows，SC-26）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p7-handler-spawner.md`（`### Added`：跨平台事件 handler 子程序執行（Windows Job Object、cmd.exe shell））
**BLOCKED-OWNER:** 測試團隊 Windows 工作站（`windows-task` 監管下、第三方 EDR 環境）實際觸發 handler 的行為與延遲（B-08）；本 PR 動到 PAL，依 OD-08 需原生 Windows COM handle 環境的回歸證據（B-06）。本環境可產出 Windows 原生 L1（WSL interop，non-COM）。

**Files:**
- Create: `crates/sw-pal/src/process/handler_posix.rs`、`crates/sw-pal/src/process/handler_windows.rs`
- Modify: `crates/sw-pal/src/process.rs`、`crates/sw-pal/src/lib.rs`（`native()` 組裝）
- Create: `crates/sw-pal/tests/handler_spawner.rs`、`crates/sw-pal/src/conformance/handler_suite.rs`
- Modify: `docs/design-event-trigger.md`（新增「Windows handler 語意」段：SC-26 的 `exec`／`shell`（`cmd.exe /d /s /c`）、Job Object 逾時終止、env 白名單六鍵；以及 OD-12 信任範圍：handler 以 daemon 使用者身分執行、Windows RPC 信任範圍為整機本機使用者，**不**宣稱同使用者隔離）、`README.md` 事件章（中英兩段同步一句指向該段；R-18）

**Interfaces：** Task 0 凍結之 `HandlerSpawner`／`HandlerSpec`／`HandlerOutcome`。補充語意：
- POSIX：`fork/exec` 等價（`std::process::Command`＋`pre_exec(setsid＋close_range(3, ~0, CLOSE_RANGE_CLOEXEC)`；不支援時 fallback 對 `/proc/self/fd`（或 `/dev/fd`）列出的 ≥3 fd 設 `FD_CLOEXEC`）——只**標記** CLOEXEC 而非直接關閉，使 exec 時一併關閉，且不破壞 std 自身用來回報 exec 失敗的 CLOEXEC pipe（直接關閉會讓 `ENOENT` 被誤判為成功）；對應 Python `close_fds=True`；`std::process::Command` 預設**不會**關閉未設 CLOEXEC 的 fd，例如 openpty 的 master／slave、accept 出的 socket；`env_clear()` 後只放 spec.env，cwd 繼承），stdin／stdout／stderr 皆 pipe；寫 stdin 與讀 stdout／stderr 以兩條 reader thread＋主執行緒 `poll` 完成（避免 pipe 死鎖），stdout／stderr 各只保留最後 `tail_bytes`；逾時 `killpg(pgid, SIGTERM)`（ESRCH／EPERM 忽略）→ 等 1.0 s → `killpg(SIGKILL)` → 收尾讀取 → `TimedOut`；被訊號終止的 `exit_code` 為 `-signum`；spawn 失敗 `SpawnFailed(py_message)`，`py_message` 照 Python `str(OSError)`（例：`[Errno 2] No such file or directory: 'nonexist'`）。
- Windows：`CreateProcessW`（`CREATE_NO_WINDOW|CREATE_SUSPENDED|CREATE_UNICODE_ENVIRONMENT|EXTENDED_STARTUPINFO_PRESENT`，`bInheritHandles=TRUE` 但以 `STARTUPINFOEXW`＋`PROC_THREAD_ATTRIBUTE_HANDLE_LIST` 只列 stdin／stdout／stderr 三個 pipe handle，環境區塊只含 spec.env）→ `AssignProcessToJobObject`（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`）→ `ResumeThread`；逾時或 `abort` 觸發時 `TerminateJobObject(job, 1)` 後等待 → `TimedOut`；`exec` argv 以 MSVC 引號規則組 command line；`shell` 路徑的 command line 逐字組為 `cmd.exe /d /s /c "<shell>"`（外層加一對引號，**不得**對 `<shell>` 套 MSVC 跳脫——`/s` 規則只剝除最外層引號，內部引號原樣交給 cmd）；spawn 失敗 `py_message` 照 Python `str(OSError)` Windows 形式 `[WinError 2] <FormatMessageW>`。

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-pal/src/conformance/handler_suite.rs（POSIX 與 Windows 共用斷言；只用兩平台皆有的命令）
pub fn completes_with_stdin_echo_and_tail(s: &dyn HandlerSpawner) {
    let spec = HandlerSpec { argv: s.shell_argv(echo_stdin_then_stderr_cmd()), env: minimal_env(s), stdin: b"{\"a\": 1}\n".to_vec(),
                             timeout: Duration::from_secs(10), tail_bytes: 4096 };
    match s.run(&spec, &HandlerAbort::new()) {
        HandlerOutcome::Completed { exit_code, stdout_tail, stderr_tail } => {
            assert_eq!(exit_code, 3);
            assert!(String::from_utf8_lossy(&stdout_tail).contains("{\"a\": 1}"));
            assert!(String::from_utf8_lossy(&stderr_tail).contains("ERR"));
        }
        o => panic!("unexpected {o:?}"),
    }
}
pub fn timeout_kills_whole_tree(s: &dyn HandlerSpawner) {
    let marker = unique_marker();
    let spec = HandlerSpec { argv: s.shell_argv(&spawn_grandchild_sleep_cmd(&marker)), env: minimal_env(s), stdin: vec![],
                             timeout: Duration::from_millis(300), tail_bytes: 4096 };
    let t0 = Instant::now();
    assert!(matches!(s.run(&spec, &HandlerAbort::new()), HandlerOutcome::TimedOut));
    // 相對量測：逾時＋POSIX SIGTERM→SIGKILL 寬限 1.0 s＋排程餘裕 3 s（EDR 環境延遲由 B-08 實測，不在此收緊）
    assert!(t0.elapsed() < Duration::from_millis(300) + Duration::from_secs(1) + Duration::from_secs(3));
    assert!(wait_until(|| !process_with_marker_alive(&marker), Duration::from_secs(5)), "孫行程必須一併終止");
}
pub fn abort_terminates_tree(s: &dyn HandlerSpawner) {
    let marker = unique_marker(); let abort = HandlerAbort::new();
    let spec = HandlerSpec { argv: s.shell_argv(&spawn_grandchild_sleep_cmd(&marker)), env: minimal_env(s), stdin: vec![],
                             timeout: Duration::from_secs(60), tail_bytes: 4096 };
    let a2 = abort.clone(); std::thread::spawn(move || { std::thread::sleep(Duration::from_millis(200)); a2.trigger(); });
    assert!(matches!(s.run(&spec, &abort), HandlerOutcome::TimedOut));
    assert!(wait_until(|| !process_with_marker_alive(&marker), Duration::from_secs(5)));
}
pub fn stdout_tail_is_bounded(s: &dyn HandlerSpawner) {   // 產生 1 MiB 輸出，只回最後 4096 bytes
    let spec = HandlerSpec { argv: s.shell_argv(emit_1mib_cmd()), env: minimal_env(s), stdin: vec![], timeout: Duration::from_secs(20), tail_bytes: 4096 };
    let HandlerOutcome::Completed { stdout_tail, .. } = s.run(&spec, &HandlerAbort::new()) else { panic!() };
    assert_eq!(stdout_tail.len(), 4096);
}
pub fn spawn_failure_message_is_python_like(s: &dyn HandlerSpawner) {
    let spec = HandlerSpec { argv: vec!["sw-p7-nonexistent-binary".into()], env: minimal_env(s), stdin: vec![], timeout: Duration::from_secs(1), tail_bytes: 4096 };
    let HandlerOutcome::SpawnFailed(e) = s.run(&spec, &HandlerAbort::new()) else { panic!() };
    assert!(e.py_message.starts_with("[Errno 2] ") || e.py_message.starts_with("[WinError 2] "), "{}", e.py_message);
}
pub fn env_is_cleared_except_spec(s: &dyn HandlerSpawner) {
    let _g = EnvSentinel::set("SW_P7_PARENT_SENTINEL", "leak");             // 父行程環境注入 sentinel（測試結束還原）
    let mut env = minimal_env(s); env.push(("SW_P7_ALLOWED".into(), "1".into()));
    let spec = HandlerSpec { argv: s.shell_argv(print_env_cmd()), env: env.clone(), stdin: vec![], timeout: Duration::from_secs(10), tail_bytes: 65536 };
    let HandlerOutcome::Completed { stdout_tail, .. } = s.run(&spec, &HandlerAbort::new()) else { panic!() };
    let keys = parse_env_dump(&stdout_tail);
    assert!(!keys.contains("SW_P7_PARENT_SENTINEL"), "父環境不得滲入");
    for (k, _) in &env { assert!(keys.contains(k.as_str()) || is_shell_injected(k), "{k} 必須存在"); }
    // 陰性對照：把 sentinel 放進 spec.env，必須看得到（證明本測試能失敗）
    let mut env2 = env; env2.push(("SW_P7_PARENT_SENTINEL".into(), "x".into()));
    let spec2 = HandlerSpec { env: env2, ..spec };
    let HandlerOutcome::Completed { stdout_tail, .. } = s.run(&spec2, &HandlerAbort::new()) else { panic!() };
    assert!(parse_env_dump(&stdout_tail).contains("SW_P7_PARENT_SENTINEL"));
}
pub fn handler_inherits_only_stdio(s: &dyn HandlerSpawner) {
    let leaked = open_inheritable_probe_handle();                            // POSIX：無 CLOEXEC 的 pipe fd；Windows：bInheritHandle=TRUE 的 pipe handle
    let spec = HandlerSpec { argv: probe_handle_argv(&leaked), env: minimal_env(s), stdin: vec![], timeout: Duration::from_secs(10), tail_bytes: 4096 };
    let HandlerOutcome::Completed { exit_code, stdout_tail, .. } = s.run(&spec, &HandlerAbort::new()) else { panic!() };
    assert_eq!(exit_code, 0, "{}", String::from_utf8_lossy(&stdout_tail));   // 子程序回報該 fd／handle 無效
}
pub fn cmd_shell_preserves_inner_quotes(s: &dyn HandlerSpawner) {   // 只在 shell_argv[0]=="cmd.exe" 時執行
    let spec = HandlerSpec { argv: s.shell_argv(r#"echo "a  b" & exit /b 3"#), env: minimal_env(s), stdin: vec![], timeout: Duration::from_secs(10), tail_bytes: 4096 };
    let HandlerOutcome::Completed { exit_code, stdout_tail, .. } = s.run(&spec, &HandlerAbort::new()) else { panic!() };
    assert_eq!(exit_code, 3); assert!(String::from_utf8_lossy(&stdout_tail).contains("\"a  b\""));
}
```

```rust
// crates/sw-pal/tests/handler_spawner.rs
#[test] fn native_completes() { sw_pal::conformance::handler_suite::completes_with_stdin_echo_and_tail(&*sw_pal::native().handlers); }
#[test] fn native_timeout_kills_tree() { sw_pal::conformance::handler_suite::timeout_kills_whole_tree(&*sw_pal::native().handlers); }
#[test] fn native_tail_bounded() { sw_pal::conformance::handler_suite::stdout_tail_is_bounded(&*sw_pal::native().handlers); }
#[test] fn native_spawn_failure() { sw_pal::conformance::handler_suite::spawn_failure_message_is_python_like(&*sw_pal::native().handlers); }
#[test] fn native_env_cleared() { sw_pal::conformance::handler_suite::env_is_cleared_except_spec(&*sw_pal::native().handlers); }
#[test] fn native_abort_terminates_tree() { sw_pal::conformance::handler_suite::abort_terminates_tree(&*sw_pal::native().handlers); }
#[test] fn native_inherits_only_stdio() { sw_pal::conformance::handler_suite::handler_inherits_only_stdio(&*sw_pal::native().handlers); }
#[test] fn native_cmd_shell_quotes() { let h = &*sw_pal::native().handlers; if h.shell_argv("x")[0] == "cmd.exe" { sw_pal::conformance::handler_suite::cmd_shell_preserves_inner_quotes(h); } }
#[test] fn shell_argv_and_env_keys_by_platform() {
    let h = &*sw_pal::native().handlers;
    let argv = h.shell_argv("x");
    assert!(argv == ["/bin/sh", "-c", "x"] || argv == ["cmd.exe", "/d", "/s", "/c", "x"]);
    let keys = h.inherited_env_keys();
    assert!(keys.starts_with(&["PATH", "HOME", "USER", "LANG", "LC_ALL"]));
    if argv[0] == "cmd.exe" { assert!(keys.ends_with(&["SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE"])); } else { assert_eq!(keys.len(), 5); }
}
```

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-pal --test handler_spawner`；`scripts/rust/win-test.sh --package sw-pal --test handler_spawner`（`--test` 選 binary；`-- handler_spawner` 會以測試名過濾、零案例假綠，禁止使用）。
- [ ] **Step 3：實作**：照上方補充語意；所有 `libc::`／`windows_sys::` 只在 `handler_posix.rs`／`handler_windows.rs`；`native()` 依平台組裝。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2（Linux 與 Windows 原生兩份報告皆 PASS；Windows 報告存 `target/win-test-report/<run_id>.json`，且 `handler_spawner` exe 的 `passed` 等於本檔 `#[test]` 數（9）、`ignored==0`）；`python3 scripts/rust/check_cfg_boundary.py`。
- [ ] **Step 5：commit**：`feat(rust-pal): 新增跨平台事件 handler 子程序執行器`

---

## Task 15：`dispatcher.rs`

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p7-event-dispatcher.md`（`### Added`：事件 dispatcher 子程序池與 fire 事件）
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-core/src/event_engine/dispatcher.rs`
- Create: `crates/sw-core/tests/event_dispatcher.rs`

**Interfaces：**

```rust
pub fn build_payload(fire: &MatcherFire, fire_count: &PyInt, matched_line: &str) -> PyDict;   // bridge_generation 恆 0（EVT-D-E2 KEEP）
pub fn build_env(fire: &MatcherFire, fire_count: &PyInt, inherited: &[(String, String)]) -> Vec<(String, String)>;
pub fn stdin_bytes(payload: &PyDict) -> Vec<u8>;                                            // Default 分隔符、ensure_ascii=false、+"\n"
pub struct Dispatcher { /* 8 條 worker thread "event-dispatch-N"；busy_rules: HashSet；inflight */ }
impl Dispatcher {
    pub fn start(per_daemon_max: usize, spawner: Arc<dyn HandlerSpawner>, counters: Arc<CounterStore>, emit: Arc<dyn Fn(PyDict) + Send + Sync>,
                 env: Arc<dyn SysEnv>, clock: Arc<dyn Clock>) -> Self;
    pub fn dispatch(&self, fire: MatcherFire, matched_line: String);
    pub fn close_intake(&self);                                 // 冪等；之後 dispatch() 直接丟棄（不 spawn、不計 fire_count），已在跑的 handler 不受影響（R1-F2）
    pub fn stop(self, deadline: std::time::Instant) -> DispatcherStopReport;   // 先 close_intake；等 handler 結束直到 deadline；逾時對仍在跑者 `HandlerAbort::trigger()`（終止整個 process group／Job），之後最多再等 100 ms 讓 worker 的 `run` 回收子行程樹返回；寫 WARNING `event dispatcher shutdown 逾時，終止 %d 個 handler`（SC-11 細化，§0.5）
}
pub struct DispatcherStopReport { pub aborted: usize, pub unreaped: Vec<String> /* 100 ms 寬限後 run 仍未返回的 rule_id，依 rule_id 排序 */ }
```

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/event_dispatcher.rs
mod support; use support::{golden_rows, fire_from_json, RecordingEmit, fake_clock_ms};
use sw_core::event_engine::dispatcher::*; use sw_pal::fakes::process::FakeHandlerSpawner; use sw_pal::HandlerOutcome;

#[test]
fn payload_and_env_bytes_match_python() {
    for row in golden_rows("event_payload") {
        let fire = fire_from_json(&row["fire"]);
        let payload = build_payload(&fire, &row.py_int("fire_count"), row.str("matched_line"));
        assert_eq!(base64(&stdin_bytes(&payload)), row.str("stdin_b64"), "case {}", row.id());
        assert_eq!(build_env(&fire, &row.py_int("fire_count"), &row.pairs("inherited")), row.pairs("env"), "case {}", row.id());
    }
}

#[test]
fn per_rule_busy_then_saturation_drop_new() {
    let sp = FakeHandlerSpawner::blocking();                    // run() 阻塞直到 release_all()
    let emit = RecordingEmit::default();
    let d = Dispatcher::start(2, sp.clone(), support::counters(), emit.as_fn(), support::env(), fake_clock_ms(1000));
    d.dispatch(support::fire("o.a", "COM0", 1), "x".into());
    d.dispatch(support::fire("o.a", "COM0", 2), "x".into());   // 同 rule 忙
    d.dispatch(support::fire("o.b", "COM0", 3), "x".into());
    d.dispatch(support::fire("o.c", "COM0", 4), "x".into());   // inflight==2 → 飽和丟新
    let ev = emit.types();
    assert_eq!(ev, ["event_dropped", "event_dropped"]);
    assert_eq!(emit.reason(0), "per_rule_busy"); assert_eq!(emit.reason(1), "per_daemon_saturated");
    sp.release_all(HandlerOutcome::Completed { exit_code: 0, stdout_tail: b"ok".to_vec(), stderr_tail: vec![] });
    d.stop(std::time::Instant::now() + std::time::Duration::from_secs(5));
    assert_eq!(emit.count_type("fire_completed"), 2);
}
```

表格家族：

| 測試名 | 精確斷言 |
|---|---|
| `fire_completed_shape` | 鍵順序 `type,rule_id,selector,exit_code,duration_ms,stdout_tail,stderr_tail,fire_count,wal_seq`；非 0 exit 仍為 `fire_completed` |
| `fire_timeout_shape` | `type,rule_id,selector,duration_ms,fire_count,wal_seq` |
| `fire_failed_on_spawn_error_still_updates_counter` | SpawnFailed → `{"type":"fire_failed","rule_id","selector","reason":<py_message>}`；counter `fires+1`、`last_fire_ts` 為完成時刻 |
| `last_fire_ts_is_end_time` | handler 跑 2 s（虛擬）→ `last_fire_ts==start+2000`（EVT-D-E4 KEEP） |
| `matched_text_env_truncation_chars` | 4097 字元（含 CJK）→ 前 4096 字元＋`" ...truncated"` |
| `stdout_tail_decoded_replace` | tail 切在多位元組中間 → 解碼以 U+FFFD 取代 |
| `shell_handler_uses_platform_argv` | `handler.shell` → `spawner.shell_argv(shell)` 的結果被傳入 |
| `inherited_env_only_whitelisted_present` | SysEnv 有 `PATH`、`SECRET` → env 只含 `PATH` 與 11 個 `SERIALWRAP_EVENT_*` |
| `stop_deadline_aborts_running_handlers` | `FakeHandlerSpawner::blocking()` 不 release；`stop(now+200ms)` 在 ≤ 300 ms 內返回、回傳 `DispatcherStopReport{aborted:1, unreaped:[]}`、該 handler 的 `HandlerAbort` 已 trigger、WARNING 逐字 |
| `close_intake_rejects_new_dispatch`（R1-F2） | 一個 handler 執行中 → `close_intake()` → 再 `dispatch` 同 rule 與另一 rule 各一次 → `FakeHandlerSpawner.spawn_count()` 不增、`fire_count` 不增；原 handler 不被 abort（`HandlerAbort` 未 trigger），release 後正常完成 |
| `stop_reports_unreaped_when_run_ignores_abort`（R1-F2） | `FakeHandlerSpawner::ignore_abort()`（本 Task 於 Task 0 的 fake 增補：abort 後 `run` 仍不返回）→ `stop(now+200ms)` 在 ≤ 300 ms 內返回 `{aborted:1, unreaped:["r1"]}`，不無界等待 |

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-core --features test-fakes --test event_dispatcher`。
- [ ] **Step 3：實作**：照 09 §3.6 與 `dispatcher.py:150-246`；`duration_ms = (wall_now_ms - start_ms)`（`time.time()` 語意，整數截斷）；worker 為固定 8 執行緒＋有界 job channel（容量 8，`dispatch` 前已以 inflight 檢查保證不阻塞）。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2。
- [ ] **Step 5：commit**：`feat(rust-event): 承接事件 dispatcher 子程序池`

---

## Task 16：EventEngine 組裝與 10 個 `event.*` RPC

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p7-event-engine.md`（`### Added`：Rust Event Engine 與 event.* RPC）
（reader 熱路徑接線、`BridgeQueries` for `SessionManager` 與 daemon 啟停順序屬 **Task 16-wire**，由實作 B 負責，見本 Task 之後。）
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-core/src/event_engine/mod.rs`、`crates/sw-daemon/src/service.rs`（10 個分支）
- Modify（退役 Slice 2.1 閘，§0.6）：`tests/acceptance/slice21_scope.yaml`（10 個 `event.*` 自 `deferred_methods` 移到 `served_methods`）、`tests/test_slice21_scope.py`（`test_scope_lists_exact_deferred_methods`、`test_scope_served_methods_cover_rpc_catalog` 期望同步）、`tests/contract_blackbox/test_slice_scope.py`（加 `test_event_methods_served`）
- Create: `crates/sw-core/tests/event_engine.rs`、`crates/sw-daemon/tests/event_rpc.rs`

**Interfaces：**

```rust
// crates/sw-core/src/event_engine/mod.rs
pub trait BridgeQueries: Send + Sync { fn active_cmd_id_for(&self, com: &str) -> Option<String>; fn profile_for(&self, com: &str) -> Option<String>; fn known_coms(&self) -> Vec<String>; }
pub struct EngineDeps { pub events_dir: PathBuf, pub runtime_dir: PathBuf, pub log_path: PathBuf, pub bridge: Arc<dyn BridgeQueries>,
                        pub queue_max: usize, pub per_daemon_max: usize, pub platform: Arc<Platform>, pub warn: Arc<dyn Fn(String) + Send + Sync> }   // 與 06 ManagerDeps.warn 同型
pub struct EventEngine;
impl EventEngine {
    pub fn new(d: EngineDeps) -> PalResult<Self>;
    pub fn start(&self) -> Result<(), PyErr>;
    pub fn close_intake(&self);                                // 冪等：matcher.stop → dispatcher.close_intake；之後 feed_rx 為 no-op、不再 spawn handler（daemon 停止序於 Phase 1 前呼叫，R1-F2）
    pub fn stop(&self, deadline: std::time::Instant) -> DispatcherStopReport;   // close_intake（若未呼叫）→ dispatcher.stop(deadline)（SC-11 細化）；daemon 停止序於 drain Phase 2 之後、WAL close 之前呼叫
    pub fn feed_rx(&self, com: &str, data: &[u8], wal_seq: u64);   // per-COM LineBuffer（永不重設，EVT-D-S5）→ enabled 才 feed_line
// EventEngine 以 `impl RxObserver`（Task 16-wire 定義的 trait）轉呼 feed_rx
    pub fn rule_set(&self, obj: &PyValue) -> Result<Arc<Rule>, UpsertError>;
    pub fn rule_delete(&self, rule_id: &str) -> bool;
    pub fn rule_list(&self, selector: Option<&PyValue>, owner: Option<&PyValue>) -> Vec<PyDict>;
    pub fn rule_get(&self, rule_id: &str) -> Option<PyDict>;
    pub fn reload(&self) -> Result<DiffResult, PyErr>;
    pub fn com_enable(&self, com: &str) -> PyDict; pub fn com_disable(&self, com: &str) -> PyDict;
    pub fn com_status(&self, com: Option<&PyValue>) -> PyDict;
    pub fn reset(&self, rule_id: Option<&PyValue>, selector: Option<&PyValue>) -> i128;
    pub fn tail(&self, rule_id: Option<&PyValue>, selector: Option<&PyValue>, since_ts: Option<&PyValue>, n: Option<&PyValue>) -> Result<Vec<PyValue>, PyErr>;
}
// crates/sw-daemon/src/service.rs（dispatch 新增分支，順序照 service.py:1133-1172）
"event.rule_set" | "event.rule_delete" | "event.rule_list" | "event.rule_get" | "event.com_enable" | "event.com_disable"
| "event.com_status" | "event.reset" | "event.reload" | "event.tail"
```

RPC 規則（02 §7.12、09 §3.10）：`rule_set` 成功 `{"ok":true, **raw}`（raw 的 `ok` 覆寫值但位置第一，RPC-H-14 KEEP）；`UpsertError::Rule(Schema|Value)` → `{"ok":false,"error_code":"INVALID_RULE_SCHEMA","error":<msg>}`；`Rule(Type)`／`Io` → framing `EXCEPTION`；`rule_delete` 參數 `str(params.get("rule_id") or "")`、DF-01 不合法回 `deleted:false`；`com_enable/disable` 參數 `str(… or "")`（空字串也進集合）；`com_status`／`rule_list`／`reset`／`tail` 原樣傳值；`reload` 回升冪清單；所有 event 方法經 service 包裝 `setdefault("version")`。emit_fire 在 dispatch **之前**設 exhausted（EVT-D-E1 KEEP）；`ALL` 在 `start`、`rule_set`、`reload` 三處（`auto_enable_com_on_load` 為真時）展開到**當下** `known_coms()`，`triggered_by` 分別為 `auto`／`rule_set`／`reload`，之後才出現的 COM 不會自動啟用（EVT-D-D11 KEEP）；事件順序：`rule_set` 與 `reload` 在鎖內先寫 `com_enabled` 再於鎖外寫 `rule_loaded`，只有 `start` 是 `rule_loaded` 在前（`engine.py:101-114`，以 `event_engine_order` golden 鎖定）；`reload` 不 disable COM（EVT-D-E10 KEEP）；RX 精確 wal_seq：reader 對每個 chunk 以 `WalAppend.seq` 呼叫 `feed_rx`（DF-15）。

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/event_engine.rs
#[test]
fn end_to_end_fire_uses_exact_wal_seq_and_writes_ndjson() {
    let h = support::engine_harness(&["COM0"]);                  // FakeHandlerSpawner 回 Completed exit 0
    h.engine.rule_set(&support::rule_json(r#"{"selectors":["COM0"],"pattern":{"kind":"contains","value":"Kernel panic"}}"#)).unwrap();
    h.engine.feed_rx("COM0", b"noise\nKernel pa", 41);
    h.engine.feed_rx("COM0", b"nic - not syncing\n", 42);       // 第二個 chunk 完成該行
    h.wait_fire_completed(1);
    let lines = h.ndjson_types();
    assert_eq!(lines, ["com_enabled", "rule_loaded", "match_recorded", "fire_completed"], "rule_set 路徑：com_enabled 先於 rule_loaded");
    assert_eq!(h.ndjson(2).i64("wal_seq"), 42, "DF-15：取完成該行之 chunk 的精確 seq");
    assert_eq!(h.spawner_calls()[0].env_get("SERIALWRAP_EVENT_WAL_SEQ"), Some("42"));
}
#[test]
fn event_order_matches_python_golden_for_start_rule_set_reload() {
    for row in support::golden_rows("event_engine_order") {
        let h = support::engine_harness_from(&row["setup"]);
        assert_eq!(h.ndjson_types(), row.str_vec("types"), "case {}", row.id());
    }
}
#[test]
fn exhausted_set_before_dispatch_keep_e1() {
    let h = support::engine_harness_saturated();                 // dispatcher 飽和
    h.engine.rule_set(&support::rule_json(r#"{"max_fires":1,"selectors":["COM0"],"pattern":{"kind":"contains","value":"x"}}"#)).unwrap();
    h.engine.feed_rx("COM0", b"x\n", 1);
    h.flush();
    let c = h.engine.rule_get("o.n").unwrap();
    assert_eq!(c.dict("counter").bool("exhausted"), true);
    assert_eq!(c.dict("counter").i64("fires"), 0);
}
```

```rust
// crates/sw-daemon/tests/event_rpc.rs
#[test]
fn rule_set_response_spreads_raw_with_ok_first() {
    let s = support::service_with_engine();
    let r = s.rpc("event.rule_set", &support::dict(r#"{"schema_version":1,"owner":"o","name":"n","kind":"tool","selectors":["COM0"],
        "pattern":{"kind":"contains","value":"x"},"handler":{"exec":["true"]},"ok":"spoof"}"#)).unwrap();
    assert_eq!(support::wire(&r), format!(r#"{{"ok":"spoof","schema_version":1,"owner":"o","name":"n","kind":"tool","selectors":["COM0"],"pattern":{{"kind":"contains","value":"x"}},"handler":{{"exec":["true"]}},"version":"{}"}}"#, s.version()));
}
```

表格家族（`event_rpc.rs`；每列斷言整個 wire 字串或關鍵欄位）：

| 測試名 | 精確斷言 |
|---|---|
| `rule_set_invalid_schema` | `{"ok":false,"error_code":"INVALID_RULE_SCHEMA","error":"name must match [a-z0-9-]{1,64}","version":…}` |
| `rule_set_type_error_is_exception` | `cooldown_ms:null` → `Err(PyErr)`（framing `EXCEPTION`，無 version） |
| `rule_delete_traversal` | `rule_id:"../x"` → `{"ok":true,"deleted":false,…}` |
| `rule_list_selector_filter` | `selector:"COM1"` 只回 selectors 含 COM1 或 ALL 者；欄位 `rule_id,owner,kind,selectors,fires,exhausted,last_fire_ts` |
| `rule_get_not_found` | `{"ok":false,"error_code":"RULE_NOT_FOUND","version":…}` |
| `com_enable_empty_string` | `{}` → `selector:""` 被加入；`com_status` 無參數 → `coms` 含 `""` |
| `com_disable_resets_counters_and_logs` | events.ndjson 依序 `counter_reset`（`triggered_by:"com_disable"`）、`com_disabled`（即使原本未啟用） |
| `reset_rule_id_priority` | 同時給 rule_id 與 selector → 只清 rule_id、`cleared:1`（不檢查存在） |
| `reload_sorted_sets` | added／removed／changed 升冪 |
| `tail_n_and_since` | `n:-2` → 跳過前 2 筆；`n:"3"` → `EXCEPTION`；`since_ts:"x"` 與 int ts 比較 → `EXCEPTION` |
| `event_methods_not_retryable` | `Method::EventTail.is_retryable_readonly()==false`（全部 10 個） |
| `start_failed_load_written` | 啟動時壞檔 → `rule_load_failed`（`path`,`reason`）且 engine 正常啟動（DF-09） |
| `all_selector_expands_on_start_rule_set_reload` | start 時 known_coms=[COM0] → `com_enabled COM0 auto`；之後 COM1 出現不自動啟用；再 `rule_set` 同規則 → `com_enabled COM1 rule_set`；`reload` → `triggered_by:"reload"` |
| `event_rpc_does_not_block_other_sessions` | 測試直接寫入一個 10 MiB（`rotate_bytes` 上限，`event_log.py:44-58`）的 events.ndjson，並以 FakeFs 讓讀取每 MiB 延遲 50 ms；`event.tail` 讀檔期間，另一 session 的 `session.get_state` 回覆 ≤50 ms |
| `event_methods_served_not_method_not_found` | 10 個 `event.*` 皆不回 `METHOD_NOT_FOUND`；`file.push`／`file.pull` 仍回（06 T18 deferred 測試讀 yaml 自動縮小） |

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-core --features test-fakes --test event_engine`；`cargo test -p sw-daemon --features test-fakes --test event_rpc`；`python3 -m pytest -q tests/test_slice21_scope.py`（期望已改、yaml 未改 → FAIL）。
- [ ] **Step 3：實作**：照 09 §3.7、§3.9、§3.10；event RPC 在 RPC 連線 thread 上執行（不經 session actor），engine 內部鎖只保護規則集合與 enabled set，檔案 I/O 在鎖外；退役 `slice21_scope.yaml` 的 event.* deferred 列。測試以 stub `BridgeQueries` 驅動，不依賴 16-wire。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2；06 T18 `deferred_methods_return_method_not_found_without_side_effects` 仍綠（只剩 file.*）。
- [ ] **Step 5：commit**：`feat(rust-event): 組裝 Event Engine 並接線 event RPC`

---

## Task 16-wire：reader `RxObserver` 接線（DF-15）、`BridgeQueries` 與 daemon 啟停順序

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-p7-event-wiring.md`（`### Fixed`：DF-15 事件、capture 與 observer 取用該 RX chunk 的精確 WAL seq；`### Added`：Event Engine 隨 daemon 啟停）
**BLOCKED-OWNER:** 原生 Windows COM 上的事件端到端（B-06）。

**Files:**
- Modify: `crates/sw-core/src/uart_bridge.rs`（06 T10 `ReaderCtx.observers`；reader 在 `WalWriter::append` 回傳後以 `WalAppend.seq` 依序呼叫每個 observer，observer panic 以 `catch_unwind` 吞掉，等價 Python `except Exception: pass`）
- Modify: `crates/sw-core/src/session_manager.rs`（`impl BridgeQueries for SessionManager`：語意逐字照 `session_manager.py:1007-1030`：`active_cmd_id_for`＝該 com 的 session `foreground_busy` 時回字面 `"foreground"`、否則 None（永不回真實 cmd_id，EVT-D-E3 KEEP）；`profile_for`＝`profile.profile_name`；`known_coms`＝`sorted(com)`（升冪）；值由 actor 在狀態改變時發布到 manager 的唯讀快照（`ArcSwap` 等價的 `Mutex<Arc<..>>`），查詢不經 actor mailbox；manager 在建立每個 reader 時注入 `[EventEngine, CaptureObserver]` 兩個 observer）
- Modify: `crates/sw-daemon/src/daemon_main.rs`（start：engine.start → 註冊 observer 工廠 → initial scan／watcher／bootstrap；stop：server.stop → `manager.begin_shutdown()` → watcher.stop → `engine.close_intake()` → session drain（P5 `manager.shutdown(5 s)`；P8 拆為 Phase 1／2）→ `engine.stop(drain 共用期限)` → WAL close（handler 與 drain 並行、共用 SC-11 的 5 s 總上限；`report.unreaped` 非空記入 `incomplete`；P8 Task 12 為完整停止序權威，R1-F2））、`docs/rust-refactor/plans/rust-architecture.md` §6.1（同步，Task 0 已登記）
- Create: `crates/sw-core/tests/rx_observer_wiring.rs`、`crates/sw-daemon/tests/event_lifecycle.rs`

**Interfaces：**

```rust
// crates/sw-core/src/uart_bridge.rs（修訂 06 T10）
pub trait RxObserver: Send + Sync { fn on_rx(&self, com: &str, data: &[u8], wal_seq: u64); }   // 不得阻塞於跨 thread 等待、不得回呼 SessionManager
pub struct ReaderCtx { /* 06 欄位 */ pub observers: Vec<Arc<dyn RxObserver>> }
// crates/sw-core/src/session_manager.rs
impl sw_core::event_engine::BridgeQueries for SessionManager { /* known_coms, profile_for, active_cmd_id_for */ }
```

- [ ] **Step 1：寫失敗測試**

| 測試名（檔） | 精確斷言 |
|---|---|
| `observer_receives_exact_wal_seq_per_chunk`（`rx_observer_wiring.rs`） | FakeSerial 依序吐 3 個 chunk，期間另一 session 推進 WAL seq → observer 收到的 `wal_seq` 與各 chunk 的 WAL RX 記錄 seq 逐一相等（DF-15） |
| `observer_called_after_wal_append` | observer 被呼叫時 `wal.current_seq()` 已含該 chunk |
| `observer_panic_is_contained` | 一個 observer panic → 下一個 observer 仍收到、reader 不中止 |
| `bridge_queries_reflect_manager_state` | 兩個 session、一個前景命令中 → `known_coms`、`profile_for` 值正確；前景中的 com → `active_cmd_id_for == Some("foreground")`、閒置 com → None；不存在的 com → None |
| `engine_started_before_bootstrap_attach`（`event_lifecycle.rs`） | 啟動時 bootstrap attach 產生的第一個 RX chunk 也被 engine 看到 |
| `shutdown_aborts_long_handler_within_budget` | handler `sleep 60` 執行中送 `daemon.stop` → daemon 在 5.1 s 內結束（5 s 共用期限＋100 ms 回收寬限）、handler 行程樹已不存在、stderr 有 `event dispatcher shutdown 逾時` WARNING |
| `shutdown_closes_event_intake_before_drain`（R1-F2，修正前必 FAIL） | in-process `run_with_hooks`、一條 rule 的 handler 執行中送 stop，以 test-fakes hook 在 `EngineCloseIntake` 之後、drain 開始前令 FakeSerial 再吐一行命中同 rule 與另一 rule 的 RX → trace 後段恰為 `[ServerStopped, ShutdownGate, WatcherStopped, EngineCloseIntake, SessionsDrained{..}, EngineStop{aborted:1, unreaped:[]}, WalShutdown{ok:true}, LockReleased]`（P5 trace 名稱；P8 拆 Phase 1／2 後 `EngineStop` 位於 `drain.phase2`／`flush_shutdown` 之後）；`FakeHandlerSpawner.spawn_count()` 自 `EngineCloseIntake` 起不再增加；`WalShutdown` 之後 events log 與 WAL 皆無新 append |

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-core --features test-fakes --test rx_observer_wiring`；`cargo test -p sw-daemon --features test-fakes --test event_lifecycle`。
- [ ] **Step 3：實作**：照上方；observer 清單在 reader 建立時固定（不在熱路徑加鎖）。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2；06 `--test rx_reader`／`drain_release`、P6 console 測試不退化（reader 熱路徑變更）。
- [ ] **Step 5：commit**：`feat(rust-event): 接線 reader observer、BridgeQueries 與 Event Engine 啟停`

---

## Task 17：session RPC 接線（self_test／recover／attach／log_*）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p7-session-rpc.md`（`### Added`：session RPC 承接 Slice 2.3 行為（reprobe 欄位、帳密重置、force 字串語意））
**前提：** 06 T18 已為 `session.self_test`／`recover`／`attach`／`log_*` 建立分支與轉型；本 Task 只補 2.3 新行為並以回歸鎖定既有轉型。
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `crates/sw-daemon/src/service.rs`（06 T18 既有分支）、`crates/sw-core/src/session_core.rs`（`public()` 的 reprobe 欄位改為實值，06 恆 null）
- Create: `crates/sw-daemon/tests/session_rpc_p7.rs`

**Interfaces：** service 分支參數強制轉型（02 §5.2）：`session.self_test`：selector 別名鏈、`timeout_s = py_float(params.get("timeout_s") or 2.0)`、`strict_human_lock = coerce_rpc_bool(...)`；`session.recover`：`timeout_s` 同上、`force = py_truthy(params.get("force"))`；`session.attach`：selector；`session.log_start/stop/status`：selector。轉型在 selector 驗證之前（非法數字字串即使 selector 空也回 `EXCEPTION`）。`session.self_test` 的 `INVALID_ARGS` 在 service 層早退且**無** `command_capable`；其他分支一律補 `command_capable`。長 RPC（`session.recover`、`session.attach`、`session.self_test`）在 service 端 reply 等待上限 600 s（arch §6.3）。

- [ ] **Step 1：寫失敗測試**（表格；每列斷言整個回應 wire 或指定鍵）

| 測試名 | 精確斷言 |
|---|---|
| `self_test_invalid_args_has_no_command_capable`（回歸） | `{}` → `{"ok":false,"error_code":"INVALID_ARGS","version":…}` |
| `self_test_bad_timeout_before_selector_is_exception`（回歸） | `{"timeout_s":"x"}` → `Err(PyErr)` |
| `self_test_not_found_has_lease_ctx_and_command_capable_false`（回歸） | `SESSION_NOT_FOUND`＋`selector`＋4 個 lease 鍵＋`command_capable:false` |
| `recover_force_string_false_is_true` | `force:"false"` → 走 force 路徑（`bool("false")` 為真） |
| `attach_credentials_unresolved_last_error_field`（SC-34，OD-14 #1） | ATTACHED＋CREDENTIALS_UNRESOLVED、probe 再失敗 → wire 去掉 `version` 後等於 `{"ok":false,"error_code":"SESSION_NOT_READY","last_error":"CREDENTIALS_UNRESOLVED","last_error_detail":null,"session":…}`，鍵序 `ok, error_code, last_error, last_error_detail, session`；`error_code` 不得為 `CREDENTIALS_UNRESOLVED` |
| `attach_resets_credentials_warned` | attach 後終態再告警一次（WAL META 計數 +1） |
| `attach_state_result_mapping` | FLASHING → `FLASHING_BUSY`；RECOVERING → `SESSION_RECOVERING`；last_error `"READY_NONCE_TIMEOUT"`（detail 為 rx tail）→ `SESSION_NOT_READY`＋`last_error:"READY_NONCE_TIMEOUT"`＋`last_error_detail` 等於 `session.last_error_detail`；last_error None → `SESSION_NOT_READY`＋`last_error:null`＋`last_error_detail:null`（不再回 `NOT_READY`，SC-34） |
| `attach_human_lease_branch_last_error_field`（新增，SC-34） | ATTACHED＋human lease＋`last_error:"PROMPT_UNAVAILABLE"` → 不 probe（`write_calls()==0`）、`{"ok":false,"error_code":"SESSION_NOT_READY","last_error":"PROMPT_UNAVAILABLE","last_error_detail":null,"session":…}` |
| `attach_passthrough_attached_ok_no_probe`（新增，SC-35） | passthrough（uboot-template，有 `ready_probe`）ATTACHED → `{"ok":true,"session":…}`，`write_calls()==0`，state 仍 ATTACHED |
| `log_rpcs_shapes` | 與 `log_responses` golden 一致，且 `log_status` 在 `RETRYABLE_READONLY_METHODS` 內 |
| `session_public_fields_p7` | `session.get_state` 含 `boot_quiet_remaining_s`、`ready_reconfirm_pending`、`ready_reconfirm_failed`、`ready_reconfirm_remaining_s`、`last_error_detail`、`rx_bytes_last_10s`、`last_rx_age_s`、`last_tx_age_s`（§9.4）、`reprobe_attempts`、`next_reprobe_at`（monotonic 秒原值，2.3 起為實值）、`reprobe_exhausted`，位置照 02 §6.1 |

- [ ] **Step 2：執行，預期 FAIL**：`cargo test -p sw-daemon --features test-fakes --test session_rpc_p7`。
- [ ] **Step 3：實作**：沿用 06 平面 `match` 分支（不得 registry 化），route 為 `SessionManager::route(sid, SessionRequest::{SelfTest{..}, Recover{..}, SessionAttach, LogStart, LogStop, LogStatus}, 600 s／60 s)`；只補新行為。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2；**Stage I 全綠點**：`cargo test --workspace --features test-fakes`（含 06／07 全部既有測試與所有 P7 測試）、`python3 -m pytest -q tests/`、`python3 scripts/rust/check_cfg_boundary.py`。
- [ ] **Step 5：commit**：`feat(rust-daemon): 接線 Slice 2.3 session RPC`

---

## Task 18A：黑箱測試工具擴充（`bb_faketarget`、P7 helper）——Stage T0，獨立 PR

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-p7-bb-tooling.md`（`### Added`：黑箱 FakeTarget 支援自訂登入步驟、延遲、背景雜訊、RX 凍結、開機腳本與 console lock）
**審閱：** 規格 §10.4 測試工具變更須獨立審閱——本 Task 以**獨立 PR** 合併（雙審同 master plan §1），合併前其他 Task 不得依賴其新參數。
**BLOCKED-OWNER:** 無。

**Files:**
- Modify: `tests/contract_blackbox/bb_faketarget.py`（P3 T2；只新增 keyword 參數與方法，預設值完全維持 P3 行為）
- Create: `tests/contract_blackbox/bb_p7.py`（P7 helper；stdlib＋PyYAML；不 import `sw_core`）
- Create: `tests/contract_blackbox/p7_profiles/{shell,bcm,prpl,passthrough_uboot,passthrough_others,console_unlock}.yaml`
- Modify: `tests/contract_blackbox/conftest.py`（新增 fixture `p7`）
- Create: `tests/contract_blackbox/test_bb_faketarget_p7_selftest.py`
- Modify: `scripts/rust/win-blackbox.ps1`（P3 `04` Task 16：新增選用參數 `-Tests <逗號分隔的 pytest 路徑>`，未給時維持 P3 行為＝整個 `tests\contract_blackbox`；供 B-06／B-08 程序選擇 P7 案例）

**Interfaces：**

```python
# tests/contract_blackbox/bb_faketarget.py（擴充）
class FakeTarget:
    def __init__(self, workdir: Path, *, prompt: str = "root@fake:~# ", user: str = "root", password: str = "pw",
                 login_steps: list[dict] | None = None,   # [{"expect": str, "respond": str}]；None＝P3 既有 login 流程
                 commands: dict | None = None,            # {"overrides": {"sh": {"response": "\r\n# "}}}
                 delay_ms: int = 0,                       # 每個回應延遲
                 noise: dict | None = None,               # {"interval_ms": int, "pattern": str}
                 console_lock: dict | None = None,        # {"secret": str, "idle_s": float}
                 boot_on_start: list[tuple[float, str]] | None = None) -> None: ...
    def emit(self, text: str) -> None: ...               # 立即輸出
    def freeze_rx(self) -> None: ...                     # 之後不再輸出任何 byte（模擬 read-endpoint stall），仍吞下 TX
    def thaw_rx(self) -> None: ...
    def boot(self, script: list[tuple[float, str]]) -> None: ...   # 依相對時間輸出 banner／倒數／login
    def rx_log_since(self, mark: int) -> bytes: ...      # rx_log()[mark:]；mark 由 rx_mark() 取得
    def rx_mark(self) -> int: ...
# tests/contract_blackbox/bb_p7.py
def p7_profile_dir(tmp: Path, name: str, **subst: str) -> Path          # 複製 p7_profiles/<name>.yaml，代換 ${BY_ID}、${ENV_FILE} 等
def cli_json(args: Sequence[str], env: Mapping[str, str], timeout_s: float = 60.0) -> tuple[int, dict | None]   # run_cli＋json.loads(stdout)
def wait_state(env, sel: str, want: str, timeout_s: float = 30.0) -> dict
class P7Rig:                                                            # fixture `p7` 的回傳
    env: dict[str, str]
    def start(self, profile: str, targets: list[FakeTarget], extra_env: Mapping[str, str] = {}) -> None   # bb_daemon.PyDaemon（daemon_prefix() 決定 Python 或 Rust）
    def cli(self, *args: str, timeout_s: float = 60.0) -> tuple[int, dict | None]
    def wal_records(self, sel: str, limit: int = 500) -> list[dict]     # 經 CLI `log tail-raw`
    def stop(self) -> None
```

- [ ] **Step 1：寫測試**：`test_bb_faketarget_p7_selftest.py`——(a) 不帶新參數時，以 P3 既有 `test_bb_harness_selftest.py` 的同一組互動驗證行為逐位元組不變；(b) `login_steps` 依序回應；(c) `freeze_rx` 後 2 s 內 `rx_log` 照常增加但 target 輸出 0 byte；(d) `boot` 腳本時間誤差 ≤ 100 ms；(e) `noise` 間隔誤差 ≤ 20%；(f) `console_lock` 未見 secret 前不輸出 prompt。
- [ ] **Step 2：執行，預期 FAIL**：`python3 -m pytest -q tests/contract_blackbox/test_bb_faketarget_p7_selftest.py --confcutdir=tests/contract_blackbox -p no:cacheprovider`。
- [ ] **Step 3：實作**：純 stdlib、PTY（`posix_only`）；新參數全部 keyword-only、預設值不改變 P3 行為。
- [ ] **Step 4：執行，預期 PASS**：同 Step 2；`python3 -m pytest -q tests/contract_blackbox/test_bb_harness_selftest.py tests/contract_blackbox/test_bb_no_product_import.py`（P3 不退化）。
- [ ] **Step 5：commit**：`test(rust-p7): 擴充黑箱 FakeTarget 以支援登入與 quiet 情境`

---

## Task 18：L1.5 黑箱契約、§11.3 不變量、延遲與背景雜訊、func-test、A/B

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh（案例撰寫與 A/B 執行；Rust 端缺陷由對應 Task 實作者修）
**Changelog fragment:** `changelog.d/rust-p7-blackbox.md`（`### Added`：Slice 2.3 黑箱契約案例、bridge 模式不變量案例與 A/B 執行紀錄）
**BLOCKED-OWNER:** L2 F9（6 案）、F10（2 案）、F11（2 案）以 Rust daemon 在擁有者 bench 執行（destructive 須授權，B-02、B-04、B-05）；L3 realhw P0＋登入／Recovery／事件 P1 與 remote（B-07）。
**前提：** Task 18A 已合併；Task 0 已把下列全部案例登記進 `cases.yaml` 並凍結於 `slice_2.3`。

**Files:**
- Create: `tests/contract_blackbox/test_p7_login.py`、`test_p7_quiet_reconfirm.py`、`test_p7_selftest_recover.py`、`test_p7_stall.py`、`test_p7_events.py`、`test_p7_log_capture.py`、`test_p7_latency_noise.py`、`test_p7_invariants.py`
- Create: `tests/acceptance/l15/slice23/{s23-login-wire-equals-wal,s23-quiet-mid-login-zero-tx,s23-login-no-resend,s23-interleave-login-agent-human,s23-reboot-recovery-no-replay,s23-recover-force-handoff}.yaml`、`tests/acceptance/l15/test_slice23_l15.py`（P2 l15 harness，bridge 模式外部 byte 證據）
- Create: `scripts/rust/run_p7_ab.sh`
- Create: `docs/rust-refactor/evidence/p7-ab-<YYMMDD>.md`（由 `run_p7_ab.sh` 產生；檔名日期為執行當日）

- [ ] **Step 1：寫測試**（黑箱；以下為兩個關鍵案例全文，其餘以表格展開）

```python
# tests/contract_blackbox/test_p7_login.py
import time, pytest
from bb_faketarget import FakeTarget
from bb_p7 import wait_state

pytestmark = [pytest.mark.posix_only, pytest.mark.cap_posix_pty]

def test_unresolved_credentials_terminal_zero_empty_secret_tx(p7, tmp_path):
    tgt = FakeTarget(tmp_path / "t0", prompt="root@op3:~# ", login_steps=[{"expect": "", "respond": "\r\nop3 login: "}])
    p7.start("shell", [tgt])                                   # profile：env_file 指向不存在的 OPI.env、環境無 SW_OPI_U/P
    s = wait_state(p7.env, "COM0", "ATTACHED")
    assert s["last_error"] == "CREDENTIALS_UNRESOLVED"
    assert tgt.rx_log() == b"", "宣告帳密但未解析時 attach 不得送任何 probe／login byte"
    mark = tgt.rx_mark()
    time.sleep(35.0)                                           # 跨過多個 reprobe 週期
    assert tgt.rx_log_since(mark) == b"", "終態期間不得自動重探或送任何 byte"
    metas = [r for r in p7.wal_records("COM0") if r["dir"] == "META" and r.get("meta", {}).get("event") == "credentials_unresolved"]
    assert len(metas) == 1

def test_creds_file_fixed_does_not_auto_probe_until_attach(p7, tmp_path):
    env_file = tmp_path / "OPI.env"
    tgt = FakeTarget(tmp_path / "t0", prompt="root@op3:~# ")
    p7.start("shell", [tgt], extra_env={"P7_ENV_FILE": str(env_file)})
    wait_state(p7.env, "COM0", "ATTACHED")
    env_file.write_text("SW_OPI_U=root\nSW_OPI_P=pw\n", encoding="utf-8")   # §9.3：只改帳密檔不得自動解除
    mark = tgt.rx_mark()
    time.sleep(35.0)
    assert tgt.rx_log_since(mark) == b""
    rc, j = p7.cli("session", "list")
    assert [x for x in j["sessions"] if x["com"] == "COM0"][0]["last_error"] == "CREDENTIALS_UNRESOLVED"
    p7.cli("session", "attach", "--selector", "COM0")
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
```

| 檔案／測試名 | 情境 | 精確斷言（Python 基線與 Rust 皆須通過；SC-30 案例只對 Rust） |
|---|---|---|
| `test_p7_login.py::test_platform_matrix[shell|prpl|bcm]` | 各平台標準登入（bcm：`login_steps` Login→Password→`CM> `、`commands.overrides.sh`） | 進 READY；`last_error` null；WAL TX 不含密碼 bytes；bcm 之後 `cmd submit` 成功 |
| `test_p7_login.py::test_passthrough_uboot_quiet_ready_no_login`（SC-35） | passthrough_uboot（有 `ready_probe`、FakeTarget 停在 `=> ` 後不再輸出） | 不跑登入、不送 ready_probe：首個 `cmd submit` 之前 target 收到 0 byte；進入 ATTACHED 後 30～32 s 內轉 READY、`last_error` null；之後 `cmd submit --cmd version` 依 prompt `=> ` 完成（`ok:true`、`partial:false`） |
| `test_p7_login.py::test_passthrough_others_quiet_ready`（SC-35） | others-template；FakeTarget 先於第 10 s 輸出一行雜訊再靜默 | 第 10 s 前後仍 ATTACHED（RX 重新起算安靜視窗）；自該行起 30～32 s 內轉 READY；全程 `last_error` null、target 收到 0 byte |
| `test_p7_login.py::test_passthrough_banner_no_quiet_gate`（SC-35） | passthrough READY 後 FakeTarget 輸出 `U-Boot 2024.04`／`Hit any key to stop autoboot:  3` | `session list` 的 `boot_quiet_remaining_s` 為 null、`ready_reconfirm_pending:false`；state 維持 READY；立即 `cmd submit` 不回 `AUTOBOOT_QUIET`（進 Arbiter 並送出 bytes） |
| `test_p7_login.py::test_creds_fixed_then_attach_ready` | 補 env_file 後 `session attach` | READY；WAL META `credentials_unresolved` 共 1 筆（修正前那筆） |
| `test_p7_login.py::test_console_unlock_smart4r_like` | `console_lock` secret、開機先輸出 `CFE version ` banner | READY（banner 不中止 unlock，SC-30 例外）；secret 不在 WAL；INFO log 不出現在 stderr |
| `test_p7_quiet_reconfirm.py::test_spontaneous_reboot_gates_agent` | READY 後 `boot()` 輸出 U-Boot 倒數 | `cmd submit` → `AUTOBOOT_QUIET`＋`retry_after_s` 浮點；倒數期間 target 收到 0 byte |
| `test_p7_quiet_reconfirm.py::test_attach_during_quiet_zero_tx` | quiet 期間 `session attach`、`session recover`（REPROBE） | 皆 0 byte；ATTACHED 時 `last_error=PROMPT_UNAVAILABLE`；READY 時 `last_error` 維持 null |
| `test_p7_quiet_reconfirm.py::test_ready_bootloader_tail_terminal_d1` | READY 後 `boot()` 輸出 banner 並停在 `=> `（常數不可由 env 調整，quiet 180 s 後才評估） | 等待 ≤ 180 s＋2 s：`session list` 顯示 `ready_reconfirm_failed:true`、state 仍 `READY`、`last_error` 為 null；`cmd submit` → `READY_UNCONFIRMED`、`recommended_action:"self_test"`、無 `retry_after_s`；banner 之後 target 收到 0 byte（D1 不對 U-Boot 送 probe）。標 `slow`（> 180 s），屬必要案例、不得 SKIP；快速時間邊界由 Task 5 `ready_bootloader_tail_d1_keeps_last_error_null`（FakeClock）覆蓋 |
| `test_p7_quiet_reconfirm.py::test_attached_bootloader_probe_sets_stuck` | ATTACHED（無 READY 歷史）、tail 停在 `=> `、quiet 已過期或從未 arm、手動 `session recover` | 回 `classification:"BOOTLOADER"`、`matched_prompt:"^=> $"`；之後自動 reprobe 失敗一次 → `last_error:"BOOTLOADER_STUCK"`、`reprobe_exhausted:true` |
| `test_p7_quiet_reconfirm.py::test_rust_quiet_mid_login_aborts_sc30`（case_id `sc30-quiet-mid-login-aborts`） | attach 登入途中輸出 banner | Rust：banner 之後 target 收到 0 byte、`ATTACHED`＋`PROMPT_UNAVAILABLE`（`is_rust()` 為假時 `pytest.skip("SC-30 Rust-only")`，屬 cases.yaml 標註的平台限定、非執行時 SKIP 取綠） |
| `test_p7_selftest_recover.py::test_selftest_classifications` | READY、ATTACHED bootloader、flood、passthrough ATTACHED、passthrough READY、released | `classification` 分別 `OK`、`BOOTLOADER`、`RX_FLOOD`、`PASSTHROUGH`、`PASSTHROUGH`、`RELEASED` 與對應 `recommended_action`（passthrough ATTACHED 為 `console_attach` 且帶 `passthrough_ready_in_s`；passthrough READY 為 `none`，self-test 期間 target 收到 0 byte，SC-35） |
| `test_p7_selftest_recover.py::test_attach_recover_last_error_field`（SC-34，case_id `sc34-attach-recover-last-error-field`） | ATTACHED＋`CREDENTIALS_UNRESOLVED`；再以 `READY_NONCE_TIMEOUT` 情境各呼叫 `session attach`、`session recover` | attach：`ok:false`、`error_code:"SESSION_NOT_READY"`、`last_error` 等於 `session.last_error`；recover：`ok:true`、`action:"REPROBE"`、`recovered:false`、`error_code:"SESSION_NOT_READY"`、`last_error` 同值；兩者皆有 `last_error_detail` 鍵且等於 `session.last_error_detail`；CLI exit code 與 Python 基線相同 |
| `test_p7_selftest_recover.py::test_recover_force_timeout_attached` | 前置：ATTACHED、target 靜默；`session recover --force` | `FORCE_RECOVER_TIMEOUT`、`action:"FORCE_CLEAR_REATTACH"`，耗時 10～13 s |
| `test_p7_selftest_recover.py::test_recover_force_on_ready_runs_keys_first` | 前置：READY、target 靜默；`session recover --force` | `{"ok":false,"error_code":"PROMPT_TIMEOUT","partial":true,"recovery_action":"NONE"}`，耗時 14～17 s；target 依序收到 `\x03`、`\x04` |
| `test_p7_selftest_recover.py::test_recover_ready_ctrl_c` | 命令 hang、CTRL_C 後 prompt | `PROMPT_TIMEOUT_RECOVERED`、`recovery_action:"CTRL_C"` |
| `test_p7_stall.py::test_transport_stall_flip` | READY 後 `freeze_rx()` 35 s、`session recover` | `last_error=="TRANSPORT_STALL"`；`last_error_detail` 以 `零 RX ` 開頭；WAL META `transport_stall_suspected` 1 筆；`thaw_rx()` 並收到非空 RX 後再 stall → 共 2 筆 |
| `test_p7_events.py::test_rule_fire_handler_payload` | `event add`（rule_set）規則 contains `Kernel panic`、handler `exec` python stdin 落檔 | handler 收到的 stdin 與 env 鍵集合與 09 §3.6 一致；`event tail` 依序 `com_enabled`,`rule_loaded`,`match_recorded`,`fire_completed`（rule_set 路徑；`engine.py:107-114`） |
| `test_p7_events.py::test_start_load_order` | 規則檔預先放在 events.d、daemon 啟動 | `event tail` 前兩筆依序 `rule_loaded`,`com_enabled` |
| `test_p7_events.py::test_rule_crud_and_reload` | add／show／list／rm／reload | 回應形狀與 02 §7.12 一致；reload 以集合比對（RPC-H-13） |
| `test_p7_events.py::test_handler_timeout_kills_group` | handler `sleep 30 & sleep 30` timeout 500 ms | `fire_timeout`；無殘留行程 |
| `test_p7_events.py::test_handler_no_inherited_fds` | handler 以 `os.listdir("/proc/self/fd")` 列出自身 fd | 除 0、1、2 與 listdir 自身的目錄 fd 外無其他 fd（與 `close_fds=True` 一致；特別是不得含 PTY master／slave 與 RPC socket） |
| `test_p7_events.py::test_redos_rejected_no_re2` | `event add` `a+b+` | exit 2、stdout `INVALID_RULE_SCHEMA`（Python 基線若可 import re2 → `pytest.skip("baseline_variant_re2")`，cases.yaml 已登記此變體） |
| `test_p7_log_capture.py::test_capture_skips_foreground` | log-start、背景 RX、`cmd submit` 輸出 | 檔案含背景 RX、不含前景命令輸出 |
| `test_p7_latency_noise.py::test_login_with_delay_and_noise` | 每步 800 ms 延遲＋每 50 ms 一行 kernel noise | 仍進 READY（timeout_s 15） |
| `test_p7_latency_noise.py::test_control_latency_under_flood_with_events` | 20000 B/10 s ×10 洪水＋一條 ALL 規則 | 另一 session `session get-state` p99 ≤ `contracts/resource_limits.yaml` 的 `control_latency_flood_p99_ms`（06 T21 凍結值，200 次取樣）；matcher `event_dropped` 有界出現。daemon RSS 增量只記錄為**診斷值**（OD-09：RSS 預算待 Python 48h 基線後凍結），不作 gate |

`test_p7_invariants.py`（§11.3 六項，外部 CLI＋FakeTarget 觀測）：

| 測試名 | §11.3 | 斷言 |
|---|---|---|
| `test_rejected_requests_zero_tx` | 1 | quiet gate submit、`CREDENTIALS_UNRESOLVED`、`READY_UNCONFIRMED` 被拒時 target 0 byte、WAL 無 TX |
| `test_login_bytes_not_interleaved_with_agent_or_human` | 2 | reprobe 登入進行中並發 `cmd submit`＋human console 輸入：target 收到的 login 序列（`\n`、user、secret、nonce 命令）為連續片段，agent 命令只在 READY 之後出現；human bytes 不夾在 login 序列內 |
| `test_recovery_and_reboot_do_not_replay_stale_input` | 3 | human 在 `recover --force`／reboot recovery 期間輸入 → reattach／READY 後 target 不收到該輸入 |
| `test_release_during_login_waits_for_handle_close` | 4 | 登入進行中 `device release`：回成功的當下 daemon 行程 `/proc/<pid>/fd` 已無任何指向該 FakeTarget PTY slave 的 fd（以 `os.readlink` 比對，屬外部行程觀測、非產品 import）；`device attach` 之前不出現新的 open；`session recover` 的 REATTACH 同樣只在舊 fd 消失後發生 |
| `test_no_auto_resend_after_partial_or_timeout` | 5 | 命令逾時救援、login 被 SC-30 中止後，target 收到的每個 payload 恰一次 |
| `test_evidence_insufficient_is_not_pass` | 6 | `run_p7_ab.sh` 在任何案例缺 evidence 檔時該列為 `INCOMPLETE`、整體 exit 非 0（以注入缺檔驗證） |

`tests/acceptance/l15/slice23/*.yaml`：沿用 P2 l15 harness 與 06 T21 的 bridge 模式（wire 證據＝`tools/uart_stdio_bridge` 的 `bridge.v1`），斷言 wire 與 WAL 一致、SC-30 中止後 wire 零 byte、login 不重送、三來源不交錯、reboot recovery 不回放、force recover 回條後才重開；每個 case 的 `manifest.json` 為 `l15-manifest.v1`。

**先前切片必要回歸（§11.5、§11.6「先前必要回歸」）：** Rust artifact 上重跑 `tests/contract_blackbox/slice21/`（06 T20）、`tests/acceptance/l15/slice21/`（06 T21）、P6 的 `tests/contract_blackbox/test_console_*.py`、`test_interactive_lease.py`、`test_slice22_family_mirrors.py`，以及 func-test 外部 bridge 模式 `brcm-01-login-flow`、`ev-01-spawn-handler`（06 `cross_slice_cases` 交給 2.3 者）。

- [ ] **Step 2：執行，預期（Python PASS／Rust FAIL）**：`python3 -m pytest -q tests/contract_blackbox/test_p7_*.py`（預設 Python 基線，須全綠；SC-30 案例 skip）；`SERIALWRAP_DAEMON_EXE=target/x86_64-unknown-linux-gnu/release/serialwrapd SERIALWRAP_AB_LABEL=rust-p7 python3 -m pytest -q tests/contract_blackbox/test_p7_*.py`（Stage T 時紅燈）；`python3 -m pytest -q tests/acceptance/l15/test_slice23_l15.py`（Python 基線綠，SC-30 case 除外）。
- [ ] **Step 3：實作**：`scripts/rust/run_p7_ab.sh` 依規格 §15.9.1：先跑 Python（A）並確認 daemon 結束、PTY 釋放，再以新 FakeTarget 實例跑 Rust（B），兩輪結果以 `contracts/` 正規化規則比對；接著跑上段「先前切片必要回歸」與 func-test 兩案。輸出 evidence md，每列含：`case_id`、結果（PASS／FAIL／SKIP＋原因／INCOMPLETE）、evidence 路徑與 sha256；表頭含 `git rev-parse HEAD`、`contracts_revision`（`contracts/` tree sha256）、`cases_yaml_sha256`、`cases.lock.json` 的 `slice_2.3` 集合 sha256、測試工具 revision（`tests/contract_blackbox/` 與 `tools/uart_stdio_bridge/` tree sha256）、CLI／daemon artifact sha256、平台、傳輸模式（`faketarget_pty` 或 `virtual_pty`）、run_id。
- [ ] **Step 4：執行，預期 PASS**：`scripts/rust/run_p7_ab.sh`（A、B 兩輪全綠，SKIP 僅限 cases.yaml 已登記的平台限定；先前切片回歸全綠）。
- [ ] **Step 5：commit**：`test(rust-p7): 新增 Slice 2.3 黑箱契約、不變量案例與 A/B 驗證`

---

## Task 19：Windows 原生 L1／COM 證據與 Phase gate

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-p7-phase-gate.md`（`### Added`：Slice 2.3 Windows 原生 COM 案例、出口證據產生器與驗證器）
**擁有者 host 可執行 gate（OD-15）：** B-06（原生 Windows COM handle 下的登入、quiet 與事件，OD-08）：由 orchestrator 經 WSL interop 在擁有者 host 以 `SW_TEST_COM_PAIR=COM20:COM21`（Spike 1 已通過的 com0com DUT-side pair）執行，結果依實跑判 PASS／FAIL；只有該 host 不可用（`caps_missing` 含 `windows_com`）時才為 BLOCKED-OWNER。
**BLOCKED-OWNER:** B-08（測試團隊工作站）。

**Files:**
- Create: `crates/sw-daemon/tests/cap_windows_com_p7.rs`（`#[ignore = "cap:windows_com"]`；三案 `cap_windows_com__p7_login_ready`、`cap_windows_com__p7_quiet_banner_zero_tx`、`cap_windows_com__p7_event_fire`）
- Create: `crates/sw-daemon/tests/support/com_peer.rs`（測試端腳本化 DUT：以 sw-pal serial backend 開 `SW_TEST_COM_PAIR` 的 peer 端（COMb），DSL 與 Task 1 `ScriptedBridge` 相同，記錄收到的全部 bytes）
- Create: `scripts/rust/p7_gate.sh`、`scripts/rust/p7_gate_report.py`、`tests/test_p7_gate_report.py`
- Modify: `tests/acceptance/l1_required.lock.json`（只以 P2 `regen_lock.py --l1-add-only` 登記 `p7_gate:*` 各測試列的平台集合；R5-F3）
- Create: `docs/rust-refactor/evidence/p7-gate-<YYMMDD>.md`（由 `p7_gate_report.py` 產生）、`docs/rust-refactor/evidence/p7-b01-procedure.md`（B-01 擁有者程序）

**Windows COM 案例規格（OD-08、OD-11、OD-15）：** daemon 以 throwaway 隔離實例啟動（獨立 state／run／wal 目錄），`windows.test_device_allowlist`（或 `SERIALWRAP_TEST_DEVICE_ALLOWLIST`）只列 COMa（OD-11：僅隔離實例、明確列名）；peer 端 COMb 由測試開啟並扮演 DUT。擁有者 host 上 COMa＝`COM20`（`CNCA0`）、COMb＝`COM21`（`CNCB0`）。byte 證據＝peer 收到的全部 bytes 與 daemon WAL 的 TX 記錄逐筆比對（相同順序、相同內容，secret 只出現在 peer 端、不在 WAL）。三案驗收：(1) prpl 腳本登入到 READY、`cmd submit` 成功；(2) READY 後 peer 輸出 U-Boot banner → submit 回 `AUTOBOOT_QUIET`，banner 之後 peer 收到 0 byte；(3) contains 規則命中 → handler（`cmd.exe` shell 路徑）執行、`fire_completed`、Job 結束後無殘留行程。缺 `SW_TEST_COM_PAIR` 時三案保持 ignored，報告標「證據不足」（arch §9.4），gate 對應列為 BLOCKED-OWNER。

**Interfaces：**

```python
# scripts/rust/p7_gate_report.py
def build_report(results_dir: Path, *, l1_lock: Path | None = None) -> str: ...   # l1_lock 預設 repo tests/acceptance/l1_required.lock.json；讀 p7_gate.sh 寫入的 results.json（每列：name、argv、rc、started_at、ended_at、log_path、log_sha256、
                                                  #   test_counts{passed,failed,ignored}、evidence[]，測試列另有 required_set、l1_results），計算每列狀態並輸出 md
def verify_report(md_path: Path, results_dir: Path) -> list[str]: ...   # 回傳不一致清單：git HEAD、artifact sha256、run_id、log sha256、列狀態重算
TEST_ROWS: frozenset[str]   # 執行測試的列（cargo test --workspace、win-test.sh (non-COM)、win-test.sh --caps windows_com、contract_blackbox *、l15 *、python3 -m pytest -q tests/）
# 狀態規則（R5-F3：原「rc==0 且 passed>0 且 failed==0」會讓只跑一個測試、或必要測試全被 ignored 的列 PASS，已取代）：
#   測試列 PASS ⇔ rc==0、required_set 非 null（`p7_gate:<列名 slug>`，nodeid 清單只在 P2 l1_required.lock.json）、l1_results（P2 l1-results.v1；
#          pytest 列帶 SW_RESOURCE_REPORT＋--junitxml、cargo 列為 P3 cargo_l1_results.py、Windows 列取 win-test 報告的 l1_results）經 P2 l1_judge.judge
#          判定為 PASS（每個凍結 nodeid 恰一列 passed、無重複、無必要案例 skip／ignored／deselected／未執行、exit_code==0、finished）、test_counts 與
#          l1_results 彙總相符，且所有 evidence 檔存在且 sha256 相符；l1_judge FAIL 或任一列 failed → FAIL；INSUFFICIENT／INVALID／缺 l1_results → INCOMPLETE；
#   非測試列（check_cfg_boundary、regen_lock --check、cases.lock slice_2.3 coverage、policy_check）PASS ⇔ rc==0 且 evidence 相符；
#   能力缺失（記錄於 results.json 的 caps_missing）→ BLOCKED-OWNER＋B 編號（該列的 cap 測試不屬其 required_set）；
#          其他任何情況 → FAIL 或 INCOMPLETE；B-01～B-08 只有在 owner 提供的證據檔（路徑＋sha256，經 verify）存在時才可為 PASS，否則 BLOCKED-OWNER
```

- [ ] **Step 1：寫失敗測試**（只驗證產生器與驗證器本身，以合成 fixture，不讀「最新報告」）

```python
# tests/test_p7_gate_report.py
import json, pathlib, subprocess, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/rust"))
import p7_gate_report as g

def _results(tmp, rows, caps_missing=()):
    d = tmp / "run"; d.mkdir(); (d / "results.json").write_text(json.dumps({"run_id": "r1", "git_head": "abc", "artifacts": {},
        "caps_missing": list(caps_missing), "rows": rows}), encoding="utf-8"); return d

def _row(name, rc=0, passed=3, failed=0, ev=()):
    return {"name": name, "argv": [name], "rc": rc, "test_counts": {"passed": passed, "failed": failed, "ignored": 0}, "evidence": list(ev),
            "log_path": "x.log", "log_sha256": None}

def test_zero_tests_is_not_pass(tmp_path):
    md = g.build_report(_results(tmp_path, [_row("win-test.sh (non-COM)", passed=0)]))
    assert "| win-test.sh (non-COM) | INCOMPLETE |" in md          # R5-F3：缺 l1_results／0 測試＝證據不足，永不 PASS

def test_missing_com_pair_makes_com_row_blocked_not_pass(tmp_path):
    md = g.build_report(_results(tmp_path, [_row("win-test.sh --caps windows_com", passed=0)], caps_missing=["windows_com"]))
    assert "| win-test.sh --caps windows_com | BLOCKED-OWNER | B-06 |" in md

def test_com_row_with_pair_judged_by_run_not_blocked(tmp_path):
    # OD-15：擁有者 host 提供 COM20:COM21 時，COM 列依實跑結果判定，不得為 BLOCKED-OWNER
    md = g.build_report(_results(tmp_path, [_row("win-test.sh --caps windows_com", rc=1, passed=2, failed=1)]))
    assert "| win-test.sh --caps windows_com | FAIL |" in md

def test_owner_items_blocked_without_evidence_and_pass_with_verified_evidence(tmp_path):
    md = g.build_report(_results(tmp_path, []))
    assert "| B-02 | BLOCKED-OWNER |" in md

def test_verify_detects_tampering(tmp_path):
    d = _results(tmp_path, [_row("cargo test --workspace")]); md = tmp_path / "gate.md"; md.write_text(g.build_report(d), encoding="utf-8")
    md.write_text(md.read_text(encoding="utf-8").replace("| INCOMPLETE |", "| PASS |").replace("abc", "zzz"), encoding="utf-8")
    assert g.verify_report(md, d), "竄改後必須驗證失敗"

def test_required_rows_complete():
    assert set(g.REQUIRED_ROWS) >= {"check_cfg_boundary", "cargo test --workspace", "win-test.sh (non-COM)", "win-test.sh --caps windows_com",
        "contract_blackbox p7 A", "contract_blackbox p7 B", "contract_blackbox slice21 B", "contract_blackbox slice22 B",
        "l15 slice21 B", "l15 slice23 B", "func-test brcm-01-login-flow", "func-test ev-01-spawn-handler",
        "regen_lock --check", "cases.lock slice_2.3 coverage", "python3 -m pytest -q tests/", "policy_check"}

# ---- R5-F3：測試列以凍結 nodeid 清單逐案判定（failing-first） ----
REQ = ["cargo:sw-core::login_fsm::a", "cargo:sw-core::login_fsm::b", "cargo:sw-core::quiet_gate::c"]

def _lock(tmp, sid="p7_gate:cargo-test-workspace", plat="linux", ids=REQ):
    p = tmp / "l1.lock.json"
    p.write_text(json.dumps({"schema_version": 1, "sets": {sid: {plat: {"selector": {"paths": [], "markexpr": ""}, "nodeids": sorted(ids)}}}}), encoding="utf-8")
    return p

def _l1_row(tmp, rows, *, exit_code=0, finished=True, plat="linux", name="cargo test --workspace", sid="p7_gate:cargo-test-workspace"):
    rep = {"schema_version": 2, "format": "l1-results.v1", "runner": "cargo", "platform": plat, "argv": [], "markexpr": "",
           "collected": [n for n, _ in rows], "deselected": [], "exit_code": exit_code, "finished": finished, "core_portable_platform_skips": [],
           "results": [{"nodeid": n, "outcome": o, "skip_kind": "capability" if o == "skipped" else None, "phases": {}} for n, o in rows]}
    f = tmp / f"{abs(hash(name))}.json"; f.write_text(json.dumps(rep), encoding="utf-8")
    r = _row(name, passed=sum(o == "passed" for _, o in rows), failed=sum(o == "failed" for _, o in rows))
    r.update({"required_set": sid, "l1_results": str(f)}); return r

def test_positive_count_without_required_nodeids_is_not_pass(tmp_path):
    lock = _lock(tmp_path)
    row = _l1_row(tmp_path, [("cargo:sw-core::other::x", "passed")])          # passed>0、failed==0：舊規則會給 PASS
    md = g.build_report(_results(tmp_path, [row]), l1_lock=lock)
    assert "| cargo test --workspace | INCOMPLETE |" in md and "missing" in md

def test_required_ignored_is_not_pass(tmp_path):
    lock = _lock(tmp_path)
    row = _l1_row(tmp_path, [(REQ[0], "passed"), (REQ[1], "passed"), (REQ[2], "skipped")])
    assert "| cargo test --workspace | INCOMPLETE |" in g.build_report(_results(tmp_path, [row]), l1_lock=lock)

def test_duplicate_or_unfinished_is_not_pass(tmp_path):
    lock = _lock(tmp_path)
    dup = _l1_row(tmp_path, [(REQ[0], "passed"), (REQ[0], "passed"), (REQ[1], "passed"), (REQ[2], "passed")])
    assert "| cargo test --workspace | INCOMPLETE |" in g.build_report(_results(tmp_path, [dup]), l1_lock=lock)
    unf = _l1_row(tmp_path, [(n, "passed") for n in REQ], exit_code=101, finished=False)
    (tmp_path / "run").rename(tmp_path / "run_dup")
    assert "| cargo test --workspace | INCOMPLETE |" in g.build_report(_results(tmp_path, [unf]), l1_lock=lock)

def test_test_row_without_l1_results_is_incomplete(tmp_path):
    md = g.build_report(_results(tmp_path, [_row("cargo test --workspace", passed=500)]), l1_lock=_lock(tmp_path))
    assert "| cargo test --workspace | INCOMPLETE |" in md

def test_full_required_set_passes(tmp_path):
    lock = _lock(tmp_path)
    row = _l1_row(tmp_path, [(n, "passed") for n in REQ] + [("cargo:sw-core::extra::y", "passed")])
    assert "| cargo test --workspace | PASS |" in g.build_report(_results(tmp_path, [row]), l1_lock=lock)
    bad = _l1_row(tmp_path, [(REQ[0], "failed"), (REQ[1], "passed"), (REQ[2], "passed")])
    (tmp_path / "run").rename(tmp_path / "run_ok")
    assert "| cargo test --workspace | FAIL |" in g.build_report(_results(tmp_path, [bad]), l1_lock=lock)
```

- [ ] **Step 2：執行，預期 FAIL**：`python3 -m pytest -q tests/test_p7_gate_report.py`；`cargo test -p sw-daemon --test cap_windows_com_p7 --no-run --target x86_64-pc-windows-gnu`（檔案不存在）。
- [ ] **Step 3：實作**：
  1. `cap_windows_com_p7.rs`＋`com_peer.rs` 照上方規格。
  2. `p7_gate.sh`：建立 `target/p7-gate/<run_id>/`，依序執行並把每個子程序的 argv、rc、時間、log（落檔並算 sha256）、測試計數寫入 `results.json`；`TEST_ROWS` 每列另寫 `required_set`（`p7_gate:<列名 slug>`）與 `l1_results`（cargo 列改以 P3 `cargo_l1_results.py --out <run>/<slug>/resource-report.json -- <原 cargo 參數>` 執行；pytest／`run_p7_ab.sh` 內的 pytest 帶 `SW_RESOURCE_REPORT` 與 `--junitxml`；Windows 列取 `win-test` 報告的 `l1_results`），各 `p7_gate:*` 集合以 P2 `regen_lock.py --l1-add-only` 凍結（R5-F3；時點依 P2 Task 13：P7 自 Task 0 起每個 Task 在自己的紅燈 commit 登記新增 nodeid，與 `cases.lock` 於 Task 0 凍結同一原則，本 Task 只登記 gate 自身測試，R5 審查後續）：`check_cfg_boundary.py`；`cargo test --workspace --features test-fakes`；`scripts/rust/win-test.sh --package sw-pal --package sw-core --package sw-daemon`（non-COM 列）；`scripts/rust/win-test.sh --caps windows_com --package sw-daemon --test cap_windows_com_p7`（未設 `SW_TEST_COM_PAIR` → `caps_missing` 加 `windows_com`）；`scripts/rust/run_p7_ab.sh`（p7 A／B、slice21／slice22／l15 回歸、func-test 兩案各自成列，取其 evidence md 的逐案結果）；`python3 tests/acceptance/regen_lock.py --check`；`cases.lock slice_2.3 coverage`（逐一核對 `slice_2.3` 集合每個 `case_id` 在本次結果中為 PASS，或為 BLOCKED-OWNER 且附 B 編號；任何缺漏 → FAIL）；`python3 -m pytest -q tests/`；`python3 -m policy_check --repo .`。全部子程序結束後才呼叫 `p7_gate_report.py build`，寫出綁定本次 `run_id`、`git rev-parse HEAD`、artifact sha256 的 md，最後 `p7_gate_report.py verify` 必須回空清單。
- [ ] **Step 4：執行，預期 PASS**：`python3 -m pytest -q tests/test_p7_gate_report.py`；`scripts/rust/p7_gate.sh`（exit 0 ⇔ 所有 REQUIRED_ROWS 為 PASS 或計算得出的 BLOCKED-OWNER，且 verify 無不一致）。
- [ ] **Step 5：commit**：`chore(rust-p7): 產出 Slice 2.3 出口證據與 Windows 原生 COM 案例`

---

## BLOCKED-OWNER 總表（本環境無法產出；交付可執行程序與判定器，證據到位前不得標 PASS）

| ID | 項目 | 需要的資源／授權 | 本 Phase 提供的程序 | 對應 Task |
|---|---|---|---|---|
| B-01 | 四平台實機登入（bcm、prpl、op3 shell、smart4R console unlock）各至少一次成功與一次帳密錯誤 | 擁有者 bench 實板、真實帳密檔 | 擁有者手動程序 `docs/rust-refactor/evidence/p7-b01-procedure.md`（Task 19 產生）：每平台以 Rust bundle 執行 `serialwrap session attach`→`session list`（READY）→`cmd submit`、再以錯誤帳密檔重跑得 `CREDENTIALS_UNRESOLVED` 且 WAL 無空帳密 TX，逐步列 CLI 命令與判定欄位；另以 TestPilot（`serialwrap_exe` 指 Rust bundle）逐一 `testpilot run serialwrap_regression --case <id>` 執行 B-02／B-04 中涵蓋該平台的案例 | 3、7 |
| B-02 | L2 F9 六案（`f9-attach-during-boot-reprobes`、`f9-brcm-spaced-prompt-reboot-login`、`f9-quiet-window-agent-passthrough`、`f9-reboot-autoboot-unmolested`、`f9-spontaneous-reboot-agent-gated`、`f9-uboot-readonly-and-console-kept`），皆 destructive；含唯一 Windows 可跑案 `f9-brcm-spaced-prompt-reboot-login` 的 Windows 執行 | bench、destructive 授權、U-Boot 唯讀護欄（§15.10） | TestPilot 無 family 選擇器，逐一執行：`testpilot run serialwrap_regression --case <id>`（上列六個 ID 各一次；`testbed.yaml` 的 `serialwrap_exe` 指 pinned Rust bundle、`allow_destructive: true`）；Windows 案依 `docs/regression-plugin.md` 的 Windows 限定執行程序 | 4、5、18 |
| B-03 | 真實 USB／usbip read-endpoint stall 重現與 busid 解析 | 實體 USB-UART 經 usbipd、可製造 stall 的手段 | `test_p7_stall.py` 的實機版本程序（以 `usbipd` detach 模擬）與判定欄位 | 6 |
| B-04 | L2 F10 兩案（`f10-creds-fixed-then-ready`、`f10-unresolved-creds-terminal`），destructive | bench、ThrowawayDaemon 授權 | `testpilot run serialwrap_regression --case f10-creds-fixed-then-ready`、`--case f10-unresolved-creds-terminal`（Rust bundle） | 7、18 |
| B-05 | L2 F11 兩案（`f11-flood-probe-classified` destructive、`f11-last-rx-age-sane`） | bench | `testpilot run serialwrap_regression --case f11-flood-probe-classified`、`--case f11-last-rx-age-sane`（Rust bundle） | 8、18 |
| B-06 | 原生 Windows COM handle 證據（OD-08：動到 PAL 的 PR 與切片出口必備）：登入 FSM、quiet、事件端到端，及 Task 6／13／14 的 PAL 變更在 COM handle 環境下的回歸 | OD-15 起為擁有者 host 可執行 gate：Spike 1 已通過的 com0com DUT-side pair `COM20:COM21`（實體 USB-UART 直連 Windows 為備援）；orchestrator 經 WSL interop 代跑 | `SW_TEST_COM_PAIR=COM20:COM21 scripts/rust/win-test.sh --caps windows_com --package sw-daemon --test cap_windows_com_p7`（Task 19 三案）；`scripts/rust/win-test.sh --package sw-pal --package sw-core`（PAL 回歸）；Windows 黑箱 `scripts/rust/win-blackbox.ps1 -Tests tests\contract_blackbox\test_p7_events.py,…`（`-Tests` 於 Task 18A 新增，預設值維持 P3 行為） | 6、7、13、14、16-wire、19 |
| B-07 | L3 realhw P0＋登入／Recovery／事件相關 P1 與 remote 案例 | bench、`two_boards`、`deployed_recent`、remote 容器環境 | `realhw` 以 Rust bundle 為 `serialwrap_exe`，分開執行並各自留報告：(1) `python3 -m realhw --tier p0`（P0 全部 8 案）；(2) `python3 -m realhw --only p1-rst-reboot`、`--only p1-rst-bootwindow`、`--only p1-rst-recover`（`--only` 一次一個精確 ID，`realhw/__main__.py:19`、`harness.select_cases`）；(3) `python3 -m realhw --tier remote`（8 案：`rm-topo-*`、`rm-live-*`）；(4) 先前切片必要回歸：06 Task 20／22 與 07 出口表列出的 realhw ID，逐一 `--only`。執行後以 `realhw --list` 對照 `cases.lock.json` 的 `slice_2.3` 集合確認無漏案 | 9、18 |
| B-08 | 測試團隊 Windows 工作站（`windows-task`、第三方 EDR）上 event handler 實際觸發、Job 終止與逾時延遲 | 測試團隊工作站 | `scripts/rust/win-blackbox.ps1 -Tests tests\contract_blackbox\test_p7_events.py` 程序與判定欄位 | 14 |

## Slice 2.3 出口對照（規格 §11.6）

| 出口條件 | 本計畫證據 | 狀態判定 |
|---|---|---|
| L1／L1.5：完整 Login FSM | Task 3、7 L1；Task 18 `test_p7_login.py` A/B、`l15/slice23` | 本環境可產出 |
| L1／L1.5：Quiet Gate／Reconfirm | Task 4、5 L1；Task 18 `test_p7_quiet_reconfirm.py` | 本環境可產出 |
| L1／L1.5：Event Engine | Task 11～16-wire L1；Task 18 `test_p7_events.py`；Task 14 Windows 原生 L1（non-COM） | 本環境可產出 |
| L1／L1.5：Stall／Recovery | Task 6、9 L1；Task 18 `test_p7_stall.py`、`test_p7_selftest_recover.py` | 本環境可產出 |
| §11.3 六項不變量 | Task 18 `test_p7_invariants.py`、`l15/slice23`；Task 7 `fence_fsm_gen.rs` | 本環境可產出 |
| 延遲與背景雜訊案例 | Task 18 `test_p7_latency_noise.py` | 本環境可產出 |
| 先前切片必要回歸（L1.5） | Task 18 slice21／slice22／l15 slice21 於 Rust artifact；func-test `brcm-01-login-flow`、`ev-01-spawn-handler` | 本環境可產出 |
| 必要案例集合完整 | Task 0 凍結；Task 19 `cases.lock slice_2.3 coverage` 列 | 本環境可產出 |
| L2／L3 前置：同一批情境於 docker 兩容器拓樸（P2 Task 17 `scripts/docker/run-l15.sh`） | `L1.5-docker` 報告 | 本環境可產出（OD-15；不計為 L2／L3） |
| L2：F9、F10、F11 切片清單＋先前必要回歸 | B-02、B-04、B-05 | BLOCKED-OWNER |
| Windows 原生 COM | B-06（擁有者 host `COM20:COM21`） | 擁有者 host 可執行（OD-15） |
| L3／平台：P0＋登入／Recovery／事件 P1、適用 remote | B-07 | BLOCKED-OWNER |
