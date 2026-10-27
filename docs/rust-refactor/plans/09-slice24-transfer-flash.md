# Slice 2.4（P8）：FileTransfer、MCU Flash、完整裝置交接與監管整合 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Rust daemon 承接 Slice 2.4 的全部範圍：`file push/pull`（板端 encoder／decoder 探測、echo-ACK 節流、checksum、分段、逾時與錯誤形狀）、POSIX `/dev/ttyMCU` MCU flash 端點（未 bridge 時沉默、flasher sync bytes 認線與 ACK 回放、holder-probe 斷線偵測、baud 鏡射、FLASHING 仲裁）與 `mcu patterns/status`、與外部工具的完整 `device release/attach` 交接、`systemd-user/system` 與 `windows-task` 監管下的端到端行為，以及全部凍結契約的最終覆蓋判定。

**Architecture:** 業務邏輯放 `sw-core`（`file_transfer`、`mcu_patterns`、`flash_endpoint`、`session_core` 的 Flash 事件），平台能力（PTY symlink、`tty_setraw`、`tcgetattr_speed`、master 寫入分身、serial line control）只在 `sw-pal`；所有 TX 都經 P5 的單一 `TxWriter` 與 `Fence`（`TxClass::FileTransfer`／`FlashProbe`／`Flash`），RX 證據先落 WAL 再分送。黑箱驗證一律經部署 artifact（`SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE`），同一批案例先對 Python 基線、再對 Rust artifact 執行。

**Tech Stack:** Rust 1.96.1（edition 2021；`x86_64-unknown-linux-gnu`、`x86_64-pc-windows-gnu`）、crossbeam-channel `=0.5.15`、libc `=0.2.186`（僅 sw-pal）、windows-sys `=0.61.2`（僅 sw-pal）、新增 `md-5 = "=0.10.6"`（sw-core，對應 Python `hashlib.md5`）；Python 3.12 pytest（黑箱、golden 擷取、覆蓋判定）、`systemd-run --user`、Windows `schtasks.exe`（經 WSL interop，需擁有者授權旗標）。

**Spec:** `docs/rust-refactor/serialwrap-rust-spec-final.md` §4.1（承接表 2.4 列）、§5.1.2、§5.1.3、§5.2.1、§5.2.2、§5.2.4、§5.2.6、§5.3（Flash／Release 列）、§6.1、§6.2「交接」、§7.1、§9.4、§11.3、§11.4、§11.5、§11.6（Slice 2.4 列）、§12.1、§12.2（Stage 2 表 2.4 列）、§13（Spike 2）、§15.10、§15.11；`owner-decisions-2026-10-15.md` OD-03、OD-08、OD-09、OD-10、OD-11、OD-12、OD-15；`plans/rust-architecture.md` §4.2、§4.7、§4.8、§5.4～§5.10、§6.1、§6.3、§9；`plans/spec-reconciliation.md` SC-02、SC-03、SC-04、SC-05、SC-11、SC-12、SC-14、SC-23、SC-24、DF-02、DF-04、UIO-12.9-a～f、UIO-12.10-f、EVT-D-S7、EVT-D-F1～F6；盤點 `inventory/09` §4、`inventory/06` §5.3、§9、`inventory/05` §3、§12、`inventory/07` §5、§6、`inventory/02` §7.2、§7.3、§7.11、`inventory/10` G-03。

---

## Global Constraints

- 平台相依 production code 只在 `sw-pal`；其他 crate 禁止 `#[cfg(unix)]`／`#[cfg(windows)]`／`target_os`／`cfg!`／`cfg_attr` 平台分支與 build script 間接分支（§3.1）；允許 `#[cfg(test)]` 與 `#[cfg(feature = "test-fakes")]`。`/dev/ttyMCU` 是否存在只看 `Capabilities.flash_endpoint`。
- 需要 `sw_pal::fakes` 的 integration test 一律在該 crate `Cargo.toml` 以 `[[test]] name=… required-features=["test-fakes"]` 宣告；本計畫所有 `cargo test` 命令對這些測試一律帶 `--features test-fakes`（紅燈與綠燈步驟相同）。
- RPC／CLI `error_code` 與 session `last_error` 為兩個獨立命名空間（ADR-07、§9.3）。本切片新增輸出的碼全部是既有碼：`TRANSFER_*`、`TARGET_*`、`CHECKSUM_*`、`MOVE_*`、`PULL_PARSE_FAILED`、`BASE64_DECODE_FAILED`、`ECHO_ACK_UNSUPPORTED`、`CONSOLE_LINE_LIMIT_TOO_SMALL`、`LOCAL_FILE_NOT_FOUND`、`FLASHING_BUSY`、`DEVICE_STILL_HELD`、`DEVICE_NOT_PRESENT`、`SESSION_BUSY`、`AUTOBOOT_QUIET`（傳輸中途拒絕，SC-33）；更新／回滾守衛沿用 P4 的 `SESSION_BUSY`／`FLASHING_BUSY`；不得新增錯誤碼。
- 錯誤碼計數由 `contracts/error_codes.yaml` 資料產生，不得硬編碼 78（§9.2、OD-06）。
- `--retries` 只對 11 個唯讀方法重試：`health.ping`、`health.status`、`session.list`、`session.get_state`、`session.activity`、`session.console_list`、`session.log_status`、`device.list`、`alias.list`、`mcu.patterns`、`mcu.status`（§4.2）；`file.push`／`file.pull`／`device.release`／`device.attach` 絕不重試。
- CLI JSON：`json.dumps(..., ensure_ascii=False, separators=(",", ":"))` 等價輸出（`CLI_STDOUT`）；`state.json`／WAL 以 `sort_keys=True`（`DISK_STABLE`）。
- 背壓上限：`CMD_PENDING_MAX=256`、`CMD_HISTORY_MAX=512`、`BG_CAPTURE_MAX_COUNT=64`、`DEFERRED_INPUT_MAX_BYTES=262144`、EventEngine LineBuffer `16384`、每 Console 出向 Ring `1 MiB`（§5.4）。本切片新增：flash probe RX 緩衝每 COM `4096` bytes（保留最後 4096）、FlashEndpoint 首讀 `4096`、pump chunk `4096`；不得新增無界 buffer 或無界 channel。
- Drain timeout 3,000 ms → `RECOVERING`＋`drain_stalled:true`＋`drain_timeout_epoch`（§5.2.5）；`device.release` 遇此情況回 `DRAIN_STALLED`（OD-14 #4，修訂 SC-04；取代原 `SESSION_RECOVERING`），drain 中的 `device.attach`／`clear`／`bind`／`recover` 維持 `SESSION_RECOVERING`＋drain 欄位。
- Windows 序列埠：`CreateFileW(OPEN_EXISTING, dwShareMode=0)`；WAL handle：`FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE`（§6.2、§8.2）。
- Windows RPC 預設 `tcp://127.0.0.1:48700`；信任範圍＝整機本機使用者（OD-12），文件不得宣稱「同使用者隔離」。
- Windows 監管預設 `windows-task`，`ExecutionTimeLimit=PT0S`（OD-03、§6.1）；`windows-service` 本切片不採用（OD-12 條件未成立）。
- OD-15 驗證環境：(1) 擁有者 host 已通過 Spike 1 基本資格（DUT-side pair `CNCA0=COM20`／`CNCB0=COM21`、HVCI 執行中），需原生 Windows COM 的案例（OD-08）改為可執行 gate：orchestrator 經 WSL interop 在該 host 以 `SW_TEST_COM_PAIR=COM20:COM21`、OD-11 隔離實例＋`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20` 執行；P4 Task 19 的 SC-36 選用 com0com 元件（`setup --with-com0com`）安裝的 pair 亦可作為 Windows 原生 flash 前置／交接測試的 COM pair（報告記錄 pair 來源：`owner-preinstalled` 或 `sc36-component`）。Spike 1 剩餘項（控制線隔離、peer-close 語意）結果未知，不得預標 PASS。(2) systemd 行為（journald stderr、autostart hold drop-in、有序停止）先以 P2 Task 17 `scripts/docker/run-systemd.sh` 在 systemd container 驗證；L2／L3 交 bench 前先跑 `scripts/docker/run-l15.sh` 兩容器拓樸，報告標 `L1.5-docker`，**不得**計為真板 L2、實機 L3 或 OD-10 bench 長跑。(3) Linux 監管 >72h 以 `scripts/docker/run-supervision.sh --hours 72` 在 docker 與 48h docker 長跑並行（不同 compose project／volume）；`windows-task` >72h 以 P2 Task 18 throwaway 排程工作（`serialwrap-throwaway-<run_id>`、隔離目錄）在擁有者 host 執行（Linux docker 無法承載 Task Scheduler）。
- 長跑門檻唯一來源 `realhw/acceptance/longrun_acceptance.yaml`；OD-09 三項已凍結，其餘延後凍結（§14.2、§15.11.4）。
- 本切片固定常數（逐字取自盤點，改值等同改契約）：`DEFAULT_CHUNK_SIZE=512`、`DEFAULT_ECHO_SLICE_SIZE=64`（字元）、`DEFAULT_ECHO_TIMEOUT_S=5.0`、`MIN_FILE_CHUNK_TIMEOUT_S=5.0`、`MIN_FILE_PULL_TIMEOUT_S=30.0`、`MIN_FILE_ECHO_TIMEOUT_S=5.0`、`ACK_MODES=["auto","echo","none"]`、`RX_MAX_CHARS=131072`（字元）、FlashEndpoint `grace=0.3`、pump select `0.2`、probe poll `0.02`、holder watch `poll=0.5,max=1800.0`、McuPattern 預設 `baud=115200,timeout_ms=500`、預設 pattern `ti-cc26xx`／`5555`／`00cc`／`115200`、`TTYMCU_PATH=$SERIALWRAP_TTYMCU_PATH 或 RUN_DIR/dev/ttyMCU`。
- repo 政策：不得 commit 到 `main`；本 Phase 於 `wt/rust-refactor/p8-slice24`（base `feature/rust-refactor`，符合 R-12 的 `wt/<slug>/<subtask>`）進行；每個 code PR 附 `changelog.d/<slug>.md` fragment（本 repo 無對應 issue 時以 slug 命名，frontmatter 省略 `issue`；`type` 只能取 policy engine `TYPE_TO_SECTION` 的 `feat／fix／refactor／perf／change／docs／remove／deprecate／security`，一個 fragment 只有一個 `type`）；commit 用 Conventional Commits 繁中 subject，含 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` trailer；完成前跑 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`。
- R-18：動到 `code_paths` 的 PR 同步更新 `README.md`／`docs/**` 的對外契約，或上 `policy-exempt:docs-sync` 並在 PR 描述寫理由（純內部 Rust 模組）。#155：PR 描述記錄回歸 case 評估結論（pytest／cargo 已覆蓋，或需實機者列入 `regression/` 或下方 BLOCKED-OWNER）。
- 本 Phase **不修改** Python `sw_core`（Stage 1 驗收的退守 artifact 必須保持不變，規格 §0.2、§12.2「校準與斷路器」）；與 Python 不同的行為一律以 Rust-only 的 SC 登記（Task 1）。
- 黑箱測試（`tests/contract_blackbox/`）不得 import `sw_core`；golden 擷取腳本（`scripts/rust/capture_*_golden.py`）可 import `sw_core`，但產出的 `contracts/golden/*.ndjson` 是 Rust 唯一讀取來源，Rust 測試不呼叫 Python。
- 在記憶體替身（`sw_pal::fakes`）上通過的測試**不得**計為原生 gate；需資源的 Rust 測試命名 `cap_<resource>__<name>` 並加 `#[ignore = "cap:<resource>"]`；需資源的 pytest 加對應 marker（`posix_pty`、`windows_com`、`physical_uart`、`supervisor_runtime`、`interactive_desktop`、`process_rpc`）。

## Review Focus

1. Flash 認線只送 flasher 自己的 sync bytes、命中後原樣回放 probe 期間 RX、不重送 `first_bytes`；端點未 bridge 時對 master 零寫入（CLAUDE.md MCU 坑 1、2）。
2. Probe／FLASHING 期間 fence：非 `Flash`／`FlashProbe` class 的 TX 一律 `NotSubmitted` 且不落 WAL；`FlashProbe` 只在 `Probe(id)` 且 permit 的 `probe_id` 相符時放行；未命中與 `FlashHolderGone` 只解除同一 `probe_id`；`FlashSyncMatched` 依 BC-02 呼叫 P6 `invalidate_human_input(InvalidateReason::Flash)`（`input_epoch++`，D-02），Probe→Matched→Gone 為同 handle 模式切換（無 `OpenBridge`／`BeginDrain`、`conn_epoch` 不變）。
3. holder-probe：armed 之後持有者歸零才結束 pump；`/proc` 不可讀與「無 holder」同樣不會 armed，最長 1800 s；pump 任何結束路徑都必須 `FlashHolderGone`→exit flashing（CLAUDE.md MCU 坑 3）；`FlashEndpoint::stop` 必須喚醒 detection／pump／holder watcher 全部等待。
4. File transfer 中止與證據：drain／release／shutdown 中止時不自動重送、只記錄 OS 確認的 bytes（SC-07），且 echo-paced 已確認 slice 的 TX 紀錄必須在 drain 回條之前落 WAL（§5.2.4）；echo stall 只送一次 `\x15\n`；`MOVE_TIMEOUT`／`MOVE_FAILED` 不 cleanup（EVT-D-F3）；Quiet／Reconfirm 期間尚未開始的寫入回 `AUTOBOOT_QUIET`（SC-33）。
5. 交接與監管：release 回條前不回成功、外部 holder 存在時 attach 不得清除 released 保護、監管停止須走有序 drain 且不得被進行中的長 RPC（600 s `file.push`）卡死，更新／回滾期間新舊版本不得同時持有 UART（以連續取樣證明，不以單次快照）。
6. 黑箱 oracle 一律是外部 peer（shelltarget／fakemcu 的 `rx_log()` 或 P2 bridge `wire.ndjson`），WAL 只作關聯；「零 TX」「不重送」不得以 WAL 無紀錄或 `cmd_id` 不重複證明（ADR-07、§15.6.4）。

---

## 0. 執行協定

### 0.1 前置交付（本 Phase 只消費，不重做）

| 來源 Phase | 消費的介面（名稱以 `rust-architecture.md` 為準） |
|---|---|
| P0 | `contracts/error_codes.yaml`（含 `reachable`、`contract_tests`）、`session_last_errors.yaml`、`behavior_changes.yaml`（`id` 符合 `^(BC|SC|DF)-\d{2}$`；必填 `kind`、`status`、`approval`、`applies_to`、`approved_at_revision`（catalog revision；`contracts/` 內禁止 `contract_revision` 鍵，P0 `test_bc_real_no_contract_revision_key`）、`ab_participation`（`rust_only`→`absolute_only`）、`spec_refs`、`phases`、`case_ids`、`owner_confirmation`…；P6 已用 SC-28／29／31、P7 已用 SC-30，本 Phase 自 SC-32 起（跨計畫審查改號：原稿 SC-31／SC-32 與 P6 SC-31 撞號，已改為 SC-32／SC-33））、`rpc_schema.json`（`x-methods`，54 個）、`cli_schema.json`、`wal_record.schema.json`、`state_file.schema.json`、`contracts/golden/` 目錄慣例 |
| P1 | Python 基線已實作 BC-01～05、SC-02/03/04/05/14、DF-02/04（Stage 0）；`_resolve_session_id` 的 SC-02 由 `command.submit`、`file.push`、`file.pull` 共用（RECOVERING → `SESSION_RECOVERING`、FLASHING → `FLASHING_BUSY`，皆附 `session`）；`sw_core` 為 A/B 的 A 側 |
| P1-OC | `02b-stage0-owner-confirmations.md`：Python 基線已實作 OD-14 的 SC-34（attach／recover 改回 `SESSION_NOT_READY`＋`last_error`／`last_error_detail`）、SC-35（passthrough 30 s 安靜自動 READY；`file.push`／`file.pull` 對 `platform=passthrough` 任何 state 回 `PROFILE_NOT_COMMAND_CAPABLE`）、`DRAIN_STALLED`（release drain 逾時）、SC-08 撤回（socket 維持 `0660`）；`contracts/error_codes.yaml` 已含 `DRAIN_STALLED`、`pending_owner_confirmations` 已清空且驗證器 `owner_item` 放寬為 ≥1 |
| P2 | `tests/acceptance/cases.yaml`（Task 12 schema：`case_id`、`suite`、`source_ref`、`family`、`title`、`contract`、`invariants`、`layer`、`platforms`、`windows_status`、`windows_gap_reason`、`capabilities`、`cli_stage`、`daemon_slice`、`stage0_required`、`prerequisites`、`necessity`、`issues`、`behavior_changes`、`destructive`、`expected`、`sentinel`、`evidence`、`implemented_by`、`cross_slice_reason`；頂層 `invariant_ids` 六項與 `features` 表）、`cases.schema.json`、`cases.lock.json`（`sets["slice_2.4"]`，只能以 `regen_lock.py --add-only` 更新）、已登記的 `f7-*`、`f10-*`、`p1-cmd-file`、`p1-ho-cycle`、`p1-ho-persist`、`p1-rst-daemon`、`lr-mixed`、`win-task-*`、`p2-1-mcu-flash-manual`；`tools/uart_stdio_bridge/`（`wire.ndjson`）、DUT 模擬器、`func-test/scenarios/`（§15.7.1）、`realhw/acceptance/{longrun_acceptance.yaml,gate.py,observer.py}` |
| P3 | 黑箱 harness `tests/contract_blackbox/{conftest.py,bb_harness.py,bb_stubd.py,bb_faketarget.py,bb_daemon.py,bin/py-serialwrapd}`（`run_cli(args, env=…) -> CliResult(rc, stdout: bytes, stderr: bytes)`、`isolated_env`、`StubDaemon`、`FakeTarget.rx_log()`、`PyDaemon`；fixtures `env`／`stubd`／`py_daemon`／`faketarget`；markers `rust_only` 等）；`sw_proto::pyjson::{PyValue, PyDict, loads, dumps, DumpOpts, Sep}`、`pydict!`、`sw_proto::pypath::{PathFlavor, basename}`、`sw_proto::pycompat::{decode_utf8_replace, splitlines, py_len_chars, py_int_parse, py_truthy}`、`sw_proto::pyfmt::py_str`、`sw_proto::pyre::{compile, PyRegex, PyFlags}`、`sw_proto::runtime_paths::RuntimePaths`、CLI `file push/pull`、`mcu patterns/status`、`device release/attach` 子命令與 params 組裝。**注意**：`pycompat::{shlex_quote, strip_ansi, a2b_base64_lenient, b2a_base64, uuid4_hex}` 不在 04 Task 4 的 Produces，由 P5 06 Task 0c 補齊（跨計畫審查已把 `shlex_quote`、`b2a_base64` 加入 06 T0c 清單）；Task 0 以編譯期核對，缺漏屬上游缺陷（停止並回報，不由本 Phase 代補）；`RuntimePaths` 沒有 `ttymcu_path`，由本 Phase Task 10 新增 |
| P4 | `sw_pal::ServiceCtl`（`action`／`action_named(mode, name, action, with_sudo)`、`install`、`uninstall`、`wait_stopped`、`set_enabled`）、`ServiceResult{ok, mode, action, rc, stdout, …}`（struct）、`SupervisorDefinition{mode, name, exec, working_dir, …}`、`SystemdCtl`、`WindowsTaskCtl::new(env, fs, clock)`（`service_ctl/windows_task.rs`）、`render_user_unit`／`render_system_unit`、`render_task_xml(&TaskXmlSpec)`、共用 `graceful_stop`（停用拉起→RPC `daemon.stop` 2.0 s→等待 ≤10 s→逾時才硬終止；P4 稱 SC-24-S，已在 Stage 1 實作）、`setup`／`setup --rollback` 七步與 `safety_check`（`FLASHING_BUSY`／`SESSION_BUSY`，SC-23；update／rollback 步驟 3 改用 `graceful_stop_with(StopRpc::Handoff)` 送 `daemon.handoff_stop`，R1-F1）、release manifest、`scripts/rust/mkbundle.sh` |
| P5 | `TxWriterHandle`／`TxJob{permit, payload, reply}`／`TxReport`、`Fence`／`FenceState{…, flash_mode: FlashMode::{Off, Probe(u64), Flashing}, …}`／`FenceReject::{StaleEpoch, Frozen, Quiet, ReconfirmPending, WalUnhealthy, Flash, OwnerMismatch, Cancelled}`（P6 07 D-03 另加 `HumanAdmissionClosed`、`InputStale`；P7 08 T7 另加 `StaleFsmGen`；match 一律含這三個 variant）、`TxPermit{op_id, epoch, owner_gen, class, source, cmd_id, log}`（P6 加 `input_epoch: Option<u64>`、P7 加 `fsm_gen: Option<u64>`；本 Phase 建構點兩者皆填 `None`）、`TxClass::{Flash, FlashProbe, FileTransfer, SystemRecoverKey, SelfTestProbe, …}`、`TxResult`、Two-Phase Drain（`StopOp`、`WorkerStoppedReceipt`、`IoWorker::finish_async`）、`OpRunner::run(grant: OpGrant, spec: OpSpec)`＋`OpCtx{com, sid, profile, rx: Arc<RxShared>, tx: TxWriterHandle, wal, clock, control, rng}`、`OpSpec`／`OpOutcome`、`RxShared::{snapshot_len, text_from, wait_for_regex_from}`、`mailbox::{ControlEvent, DataEvent, Reply}`、`session_core::{SessionRequest, Effect, Effects, SessionCore::on_event(MailboxItem, now)}`、`OpenedBridge{epoch, worker, real_path}`、`StateStore`、`device.release`／`device.attach` 完整路徑（`aborted_cmd`、`closed_consoles`、`DEVICE_STILL_HELD` 預檢、`attach_during_drain_is_session_recovering`、self-test RELEASED 分支、`kill_between_commit_and_reply`；SC-04、SC-05、DF-04、BC-04）、`framing.rs::RpcServer`、`daemon_main`、測試支援 `crates/sw-core/tests/support/mod.rs`（`CoreHarness`、`ActorRig`） |
| P6 | ConsoleHub、Human raw／line、DeferredBuffer（`DeferredEntry{client_lifetime, input_epoch, bytes}`，07 D-07：無 poison 狀態）、`invalidate_human_input(InvalidateReason::Flash, fx)`（D-02，`input_epoch++`）、`OwnershipState.flash_mode: bool`（`reset_for_stop` 保留）、`ConsoleInputBytes{…, input_epoch, …}`、前景操作 `fg_begin`／`fg_finish`、只 suspend human raw 的 `human_suspend_begin`／`human_suspend_end`（07 T6；不存在 `suspend_interactive`／`resume_interactive`）、`ConsoleTransportFactory::create_pty`、`PtyPair`、`console_pty/posix.rs` |
| P7 | Quiet Gate（`retry_after_s`，SC-03）、Ready Reconfirm、`submit_gate_reject(public_session, source)`／`execute_gate_reject(q, com, now)`（`_resolve_session_id` 與 `_agent_gate_reject_locked` 等價）、`RxShared::wait_for_regex_cancellable(re, from, timeout, clock, cancel: &RunCancel) -> WaitOutcome`、Event Engine `RxObserver` |

前置未交付時本 Phase 不開工；不得以替身或 SKIP 代替前置功能。上表名稱以 producer 計畫為準；Task 0 以編譯期核對凍結，之後各 Task 的 Interfaces 只引用核對過的實名（不再寫「若名稱不同…」）。

### 0.2 兩階段 TDD

- **Task 0 先完整執行（步驟 1～6）**：它交付上游介面核對與所有黑箱共用 fixture，階段 A 的黑箱測試必須能對 Python 基線執行，故不適用兩階段切分。
- **階段 A（測試先行，整個 Phase）**：依 Task 1→15 順序，只執行每個 Task 的步驟 1～3。步驟 1 固定包含三個子項：(a) 該 Task 的測試支援檔（Rust `crates/*/tests/support/*.rs`、Python helper），由測試作者交付且列於 Files；(b) `todo!("P8-Tn")` 介面骨架，使測試可編譯；(c) 失敗測試本體。紅燈必須來自行為斷言或明確的 `todo!`，不得來自缺少的 helper 或編譯錯誤。執行者為該 Task 的「Test author model」。階段 A 結束時：
  - `python3 -m pytest -q tests/` 仍全綠（對 Python 基線的黑箱案例必須通過；只對 Rust artifact 才紅的案例由 `SERIALWRAP_DAEMON_EXE` 未指向 Rust 時不受影響）。
  - `cargo test --workspace` 的新增測試全部以 `not yet implemented: P8-Tn` 或斷言失敗收場，不得有編譯錯誤。
  - 對 Rust artifact 執行黑箱：新增案例全部 FAIL（不是 SKIP）。
- **階段 B（實作）**：依 Task 1→15 順序執行步驟 4～6（實作、確認 PASS、commit），執行者為「Implementer model」。實作者不得修改階段 A 的斷言；認為斷言有誤時停下，交回測試作者與審查者裁決（§10.4）。
- 每個 Task 完成後的審查：Claude Sonnet（effort max）＋Codex `gpt-6.1-sol`（medium），審查者不得為該 Task 的實作者（master plan §2）。Task 10（daemon 啟動序、`FlashEndpoint` RAII 清理、start 失敗 exit 1）與 Task 14（更新／回滾守衛、跨 Stage 停止交接）屬 daemon 生命週期，依 master plan §2 與 06 §0.4「daemon 生命週期一律 `codex-gpt-6-luna-max`」由 Codex 實作（R0-F6；原派 haiku 已撤銷）；測試作者維持 `agy-gemini-3.8-flash-high`（≠ 實作者）。Sonnet 審查仍須逐項核對 §11.3 六項不變量與 Review Focus 5 並在審查紀錄列出結論。兩 Task 的 Windows 執行步驟（`scripts/rust/win-test.sh`）因 Codex `-s workspace-write` 沙箱可能擋 `/mnt/c`／`cmd.exe`，一律由 orchestrator 執行並把 `target/win-test-report/<run_id>.json` 路徑交給實作者（同 07 執行順序第 5 點）。

### 0.3 Artifact 與常用命令

```bash
# Rust artifact（Stage 2 bundle 的本體）
cargo build --release --target x86_64-unknown-linux-gnu -p sw-cli -p sw-daemon
export RUST_CLI=$PWD/target/x86_64-unknown-linux-gnu/release/serialwrap
export RUST_DAEMON=$PWD/target/x86_64-unknown-linux-gnu/release/serialwrapd

# 黑箱：Python 基線（預設）
python3 -m pytest -q tests/contract_blackbox/test_s24_<topic>.py
# 黑箱：Rust artifact
SERIALWRAP_CLI_EXE=$RUST_CLI SERIALWRAP_DAEMON_EXE=$RUST_DAEMON SERIALWRAP_AB_LABEL=rust-linux \
  python3 -m pytest -q tests/contract_blackbox/test_s24_<topic>.py
# Windows 原生 L1（WSL interop）
scripts/rust/win-test.sh --package <crate> [--caps <res>]
```

### 0.4 調和表增補（全部由 Task 1 一次寫入；Task 2～15 不再修改 `behavior_changes.yaml`）

依 `spec-reconciliation.md` §0：KEEP 只寫調和表、**不**登記 `behavior_changes.yaml`；SPEC／NEW 以 `SC-xx` 依 P0 schema 補齊全部必填欄位登記。

| ID | 事實 | 裁決 | 寫入位置 | 承接 Task |
|---|---|---|---|---|
| SC-32 | Python `_handle_bridge_down` 與 `_detach_by_id` 在 flash busy 時都直接略過（#83 RACE-2：BSL／reset 期間 by-id 常短暫消失）；bridge 真正 I/O 失敗時 session 會停在 FLASHING | SPEC（SC-04 適用面，**Rust-only**，`applies_to.python: null`）：只有 `UartIoError` 在 Probe／FLASHING 期間進 Phase 1 drain → `RECOVERING`；`DeviceDetached` 在 Probe／FLASHING 期間**延後**到 exit flashing 後再 reconcile（等價 Python）；所有 flash 結束路徑無條件清 `Fence.flash_mode` 與 P6 `OwnershipState.flash_mode` | `behavior_changes.yaml`＋調和表 | Task 8 |
| SC-33 | 規格 §5.2.2／§5.3 要求 Quiet／Reconfirm 期間 FileTransfer「拒絕尚未開始的寫入 → `AUTOBOOT_QUIET`＋`retry_after_s`」；Python 基線明示只在開始前擋、傳輸中不中斷（`session_manager.py` #139 註解，inventory 09 §4.5） | SPEC（**Rust-only**，needs-owner：`owner_confirmation: pending_default`、`owner_item: 8`，預設＝Rust 依規格中途拒絕、Python 維持不中斷並列 A/B 已登記差異）：中途拒絕回 `{"ok":false,"error_code":"AUTOBOOT_QUIET","selector":…,"retry_after_s":…}`，不送 cleanup（cleanup 同樣被 gate） | `behavior_changes.yaml`＋調和表＋`pending_owner_confirmations` 新 item 8（OD-14 已裁決 item 1～7，P1-OC 已把清單清空；編號延續、不重用） | Task 4、5、6 |
| XFER-ABORT-01 | Python 在 release／recover／shutdown 中止進行中的 file transfer 時，依時序可能回 `EXCEPTION`、`TRANSFER_TIMEOUT`、`MOVE_TIMEOUT`、`CHECKSUM_VERIFY_FAILED`、`TARGET_DECODER_FAILED` 等 | KEEP（收斂）：契約為「任何非成功回應（`ok` 不為 true）、不自動重送、已確認 bytes 落 WAL」；Rust 一律回 framing `EXCEPTION`；比較器以「非成功」比對（G2）。needs-owner：檔案傳輸被中止是否納入 BC-03（`COMMAND_ABORTED_IN_FLIGHT`）；預設＝不納入（BC-03 只定義 `command.*`），Rust 於 log 記 `chunks_sent`／`confirmed_bytes` 供診斷 | 調和表（needs-owner yes）；比較器設定與 `cases.yaml` | Task 6 |
| MCU-STATUS-01 | `mcu.status.flashing` 在 probe／偵測期間即為 `true`（`_flash_active` 於首讀後設定） | KEEP | 調和表 | Task 10 |
| EVT-D-F4（既有） | pull 受 131072 字元 RX 視窗限制；F7 `f7-larger-file-not-truncated` 的 1 MB pull 因此 `PULL_PARSE_FAILED` → SKIP | KEEP（既有裁決）＋本 Phase 補鎖定測試；needs-owner：§11.6 L2 F7 出口與此 KEEP 衝突，選 (a) 登記 DF（pull 不依賴視窗，雙邊同改）或 (b) P2 `cases.yaml` 標 `expected: XFAIL`＋`sentinel{issue, signature:"PULL_PARSE_FAILED", expires}`；預設＝(b)，未裁決前該 case 列 BLOCKED-OWNER 並阻擋出口 | 調和表 | Task 4、Task 15 |

已刪除的舊增補（本次修訂）：SC-23-G（P4 `safety_check` 已以 `SESSION_BUSY` 涵蓋，見 Task 14）、SC-24-S（P4 `graceful_stop` 已實作並由 P4 負責登記，見 Task 13）、FLASH-IOERR-01 的 Python 雙邊修改（改為 SC-32 Rust-only）。

---

## Task 總覽

| # | Task | Test author model | Implementer model | 依賴 |
|---|---|---|---|---|
| 0 | 上游介面核對與 P8 黑箱共用 fixture | haiku-xhigh | agy-gemini-3.8-flash-high | P2～P7 |
| 1 | Slice 2.4 案例登記、契約增補登記、斷路器凍結 | haiku-xhigh | agy-gemini-3.8-flash-high | 0、P0、P2 cases.yaml |
| 2 | `mcu_patterns` registry 與 hex 語意 | agy-gemini-3.8-flash-high | haiku-xhigh | P3 pyjson |
| 3 | File transfer 板端命令樣板與 RX 判定 helper（Python golden） | haiku-xhigh | agy-gemini-3.8-flash-high | 0 |
| 4 | File transfer 引擎 `push_file`／`pull_file` | haiku-xhigh | agy-gemini-3.8-flash-high | 3 |
| 5 | `OpTransferPort`：echo-ACK 節流、TX 證據聚合與中止 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max | 4、P5、P7 |
| 6 | `file.push`／`file.pull` 的 session 整合與 RPC 分派 | haiku-xhigh | codex-gpt-6-luna-max | 0、5、P7 |
| 7 | PAL flash 端點能力（PTY symlink、raw、speed、master 寫入分身、line control） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max | P6 PTY |
| 8 | SessionCore FLASHING 仲裁（probe 授權、Matched、HolderGone、flash-critical） | haiku-xhigh | codex-gpt-6-luna-max | P5、P6 |
| 9 | FlashEndpoint 執行緒與認線協調（沉默、sync 回放、pump、holder-probe、baud 鏡射） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max | 0、2、7、8 |
| 10 | `mcu.patterns`／`mcu.status` RPC 與 daemon 組裝 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max | 9 |
| 11 | 外部工具裝置交接的黑箱與整合驗證 | haiku-xhigh | codex-gpt-6-luna-max | 0、6、8、P5 |
| 12 | systemd 監管整合與有序停止 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max | 6、9、11、P4、P5 |
| 13 | `windows-task` 監管對 Rust daemon 的驗證 | haiku-xhigh | codex-gpt-6-luna-max | 12、P4 |
| 14 | 更新／回滾守衛對 Rust daemon 的驗證與補強（SC-23） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max | 6、9、P4 |
| 15 | 全部凍結契約的最終覆蓋判定與切片出口腳本 | haiku-xhigh | agy-gemini-3.8-flash-high | 0～14 |

---

## Task 0：上游介面核對與 P8 黑箱共用 fixture

本 Task 先完整執行（§0.2）。目的：(1) 以編譯期與 grep 核對 §0.1 全部消費符號，任何缺漏即停止並回報上游 Phase（不由本 Phase 代補、不以替身代替）；(2) 交付 Task 6、9、11、12、14 共用的黑箱 fixture，建構於 P3 harness 之上。

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high

**Files:**
- Create: `crates/sw-core/tests/p8_upstream_interfaces.rs`（只做型別／函式引用的編譯期核對，`#[test] fn compiles() {}`）
- Create: `tests/contract_blackbox/p8_fixtures.py`、`tests/contract_blackbox/shelltarget.py`、`tests/contract_blackbox/fakemcu.py`、`tests/contract_blackbox/fakeflasher.py`、`tests/contract_blackbox/extholder.py`、`tests/contract_blackbox/holder_sampler.py`
- Modify: `tests/contract_blackbox/conftest.py`（`pytest_plugins = ["tests.contract_blackbox.p8_fixtures"]` 或等價匯入；不得 import `sw_core`）
- Test: `tests/contract_blackbox/test_p8_fixtures_selftest.py`

**Interfaces（Produces，Python stdlib-only，不 import `sw_core`）：**
```python
# p8_fixtures.py —— 建構於 P3 bb_harness.run_cli / isolated_env、bb_daemon.PyDaemon（或 SERIALWRAP_DAEMON_EXE）、bb_stubd.StubDaemon
ANY: object                                   # == 任何值（用於 version 等欄位）
def drop_version(d: dict) -> dict
@dataclass(frozen=True)
class CliRun: exit: int; json: dict | None; stdout: bytes; stderr: str
class SwEnv:                                  # fixture `sw_env`（function scope，teardown 一律 stop＋清子行程與 fd）
    label: str                                # bb_harness.ab_label()
    run_dir: Path; profile_dir: Path; socket: Path; lock: Path; wal_path: Path; daemon_exe: list[str]
    def isolated_env(self) -> dict[str, str]
    def prepare(self, *, targets: dict[str, object], profile: str = "shell", uart_baud: int = 115200) -> None  # 寫 profiles、by-id symlink，不啟動
    def start(self, *, targets: dict[str, object], profile: str = "shell", uart_baud: int = 115200, timeout_s: float = 15.0) -> None
    def stop(self, timeout_s: float = 5.0) -> None
    def restart(self) -> None
    def cli(self, *args: str, timeout_s: float = 120.0) -> CliRun          # run_cli；stdout 單一 JSON 物件時解析
    def rpc(self, method: str, params: dict, timeout_s: float = 30.0) -> dict   # 最小 NDJSON client（同 bb_daemon），不經產品 CLI
    def rpc_async(self, method: str, params: dict, timeout_s: float = 600.0) -> concurrent.futures.Future
    def rpc_ok(self, method: str) -> bool
    def session(self, com: str) -> dict                                       # session.get_state 的 session 子 dict
    def wait_state(self, com: str, state: str, timeout: float) -> None
    def wait_until(self, pred: Callable[[], bool], timeout: float, interval: float = 0.05) -> None
    def wal_tx(self, com: str) -> list[dict]                                  # 只供關聯（不得當 oracle，Review Focus 6）
    def holders(self, path: str, *, exclude_self: bool) -> list[int]          # 掃 /proc/*/fd（POSIX）
    def unplug(self, name: str) -> None; def replug(self, name: str) -> None  # 移除／還原 by-id symlink
class ShellTarget:                            # fixture `shell_target`
    root: Path; slave_path: str
    def rx_log(self) -> bytes                 # DUT 端（/bin/sh 輸入側）實際收到的全部 bytes，外部 oracle
    def commands(self) -> list[str]           # 依 rx_log 以 "\n" 切出的完整輸入行
# shelltarget.py：PTY pair A（daemon 端開 slave A 當 UART；本程序不持有 slave A）與 pty.fork() 的 /bin/sh -i（PS1='root@sw-sim:/# '、HOME／TMPDIR 指向案例暫存目錄）之間轉送 bytes；
#   選項 line_limit、hide_tools=("base64",…)（PATH 前置 exit 127 腳本）、echo_stall_after（轉送 N 字元後停止 echo）、
#   silent_after（N bytes 後 DUT 完全靜默）、half_prompt（回 prompt 前半段後停住）、slow_reader_bps（限速讀取）；A-master EIO 時 sleep 0.02 s 重試。
class FakeMcu:                                # fixture `fake_mcu`；fakemcu.py：TI SBL 子集（55 55 → 00 cc；之後每封包回 00 cc）
    def rx_log(self) -> bytes; def current_speed(self) -> int
class FakeFlasher:                            # fakeflasher.py
    def __init__(self, link: Path, *, baud: int | None, excl: bool = False): ...   # baud=None → 不 cfsetspeed；excl=True → 開啟後 ioctl(TIOCEXCL)、close 時不清除（模擬自設獨占的 flasher）
    def write(self, b: bytes) -> None; def read_exact(self, n: int, timeout: float) -> bytes
    def set_baud(self, baud: int) -> None; def close(self) -> None
class ExternalHolder:                         # extholder.py：子行程以單一行程持有 tty（flock=True 時 fcntl.flock(LOCK_EX)）
    pid: int
    def __init__(self, path: str, *, flock: bool): ...
    def start(self) -> None; def stop(self) -> None   # start：子行程 open 失敗（含 EBUSY＝daemon 關閉前未 TIOCNXCL，R0-F3）→ raise ExternalHolderOpenError(errno)，不得靜默視為持有中
class HolderSampler:                          # holder_sampler.py：每 10 ms 掃 /proc，記錄 (t, pids) 序列
    def __init__(self, path: str, interval_s: float = 0.01): ...
    def __enter__(self) -> "HolderSampler"; def __exit__(self, *a) -> None
    def max_concurrent(self) -> int; def samples(self) -> list[tuple[float, list[int]]]
def throwaway_daemon(...) -> SwEnv            # fixture factory：獨立 RUN/STATE/WAL/BY_ID/BY_PATH/CONFIG，teardown 自動 stop
```

- [ ] **Step 1：寫失敗測試**
  - `p8_upstream_interfaces.rs`：以 `use` 與 `let _: fn(...) = ...;` 引用 §0.1 表中 P3～P7 每一個 Rust 符號（含 `sw_proto::pycompat::{shlex_quote, strip_ansi, a2b_base64_lenient, b2a_base64, uuid4_hex}`、`OpCtx`、`OpRunner::run`、`RxShared::wait_for_regex_cancellable`、`SessionRequest`、`ControlEvent`、`invalidate_human_input`、`submit_gate_reject`、`execute_gate_reject`、`WindowsTaskCtl::new`）。
  - `test_p8_fixtures_selftest.py`（對 Python 基線）：`sw_env.start`＋`shell_target` 到 READY；`shell_target.rx_log()` 含 ready probe；`HolderSampler` 在 `ExternalHolder` 存活期間 `max_concurrent()==1`、結束後 0；`ExternalHolder` 只有一個 pid 持有；`fake_mcu` 收到 `55 55` 回 `00 cc`；`sw_env.stop()` 後無殘留子行程與 socket。
- [ ] **Step 2：執行並確認 FAIL**：`cargo test -p sw-core --test p8_upstream_interfaces`（缺任一符號 → 編譯錯誤，**此時停止本 Phase，回報缺漏符號與所屬 Phase**）；`python3 -m pytest -q tests/contract_blackbox/test_p8_fixtures_selftest.py` → `ModuleNotFoundError: p8_fixtures`。
- [ ] **Step 3：commit 測試**：`test(blackbox): 新增 P8 上游介面核對與共用 fixture 自測`
- [ ] **Step 4：實作** fixtures（只用 P3 harness 公開介面）。
- [ ] **Step 5：執行並確認 PASS**：上列兩條命令皆 PASS。
- [ ] **Step 6：commit**：`feat(blackbox): 提供 P8 黑箱共用 fixture 與外部 oracle`

**Changelog fragment：** `changelog.d/rust-p8-blackbox-fixtures.md`（`type: change`、`scope: tests`）。

**BLOCKED-OWNER：** 無。

---

## Task 1：Slice 2.4 案例登記、契約增補登記、斷路器凍結

規格 §11.3 與 §12.2「校準與斷路器」要求：切片開始前凍結必要案例集合、分母與觀測窗口起訖；之後不得刪案例、縮小或雙計分母。案例清單與凍結鎖由 P2 擁有（`cases.yaml`＋`cases.lock.json`，`regen_lock.py --add-only` 為唯一更新方式），本 Task **只追加** P8 新增的 L1／L1.5 案例；L2／L3／手動／長跑一律引用 P2 已登記的個別 `case_id`，不另造聚合案例。

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high

**Files:**
- Modify: `tests/acceptance/cases.yaml`（追加下表 `s24-*`）、`tests/acceptance/cases.schema.json`（`suite` 列舉加 `contract-blackbox`、`rust-l1`；`family` 列舉加 `S24`；只增不改）、`tests/acceptance/cases.lock.json`（只經 `regen_lock.py --add-only`）
- Modify: `contracts/behavior_changes.yaml`（新增 SC-32、SC-33；SC-33 `owner_item: 8`，`pending_owner_confirmations` 新增 item 8；**不改** P0 驗證器——P1-OC 已把 `owner_item` 放寬為 null 或 ≥1 的整數、pending item 為 ≥1 且不重複，原「`1..len(pending_owner_confirmations)`」改動作廢）、`docs/rust-refactor/plans/spec-reconciliation.md`（§1 SC 表加 SC-32、SC-33；KEEP 列 XFER-ABORT-01、MCU-STATUS-01；EVT-D-F4 列補 needs-owner；文末 needs-owner 清單加 SC-33、XFER-ABORT-01／BC-03、EVT-D-F4／F7）
- Create: `tests/acceptance/breaker/slice_2.4.json`（凍結快照）、`tests/acceptance/breaker/slice_2.4.snapshots.ndjson`（空檔）
- Modify: `scripts/rust/slice_breaker.py`（P5 06 Task 0b 已建立，介面相同；本 Task 只在缺少下列行為時補上並保留 06 的測試，不重建檔案）、`tests/test_contracts_catalog.py`（`test_bc_real_sc_df_ranges` 的 SC 期望改為 `{f"SC-{i:02d}" for i in range(1, 37)} - {"SC-08"}`（SC-34／SC-35 由 P1-OC 登記；SC-36（OD-15 Windows 安裝選用 com0com 元件，Rust-only）由 P4 Task 19 登記、已在 rust_only 名單；SC-08 依 OD-14 #3 撤回、編號不重用）；`test_bc_real_rust_only_python_null` 名單加 SC-32、SC-33）
- Test: `tests/test_slice24_cases.py`

**本 Phase 追加到 `cases.yaml` 的案例**（共同欄位：`family: S24`、`daemon_slice: "2.4"`、`necessity: required`、`stage0_required: false`、`destructive: false`、`expected: PASS`、`sentinel: null`、`issues: []`、`implemented_by: "09-slice24-transfer-flash.md"`、`cross_slice_reason: null`；`windows_status`：platforms 含 windows 者 `native`，否則 `gap` 並填 `windows_gap_reason`；`cli_stage`：prerequisites 含 `supervision` 或 capabilities 含 `supervisor_runtime` 者 `"1B"`，其餘 `"1A"`；`evidence`：pytest／cargo nodeid 清單；`source_ref`、`title`、`contract` 不得留空）：

| case_id | suite | layer | platforms | capabilities | prerequisites | invariants | behavior_changes |
|---|---|---|---|---|---|---|---|
| `s24-xfer-templates-golden` | rust-l1 | L1 | linux, windows | memory_only | file_transfer | — | — |
| `s24-xfer-push-roundtrip` | contract-blackbox | L1.5 | linux | posix_pty, process_rpc | file_transfer | — | — |
| `s24-xfer-pull-roundtrip` | contract-blackbox | L1.5 | linux | posix_pty, process_rpc | file_transfer | — | — |
| `s24-xfer-error-shapes` | rust-l1 | L1 | linux, windows | memory_only | file_transfer | zero_tx_on_reject | — |
| `s24-xfer-echo-stall` | contract-blackbox | L1.5 | linux | posix_pty, process_rpc | file_transfer | no_auto_resend | — |
| `s24-xfer-session-gates` | contract-blackbox | L1.5 | linux | posix_pty, process_rpc | file_transfer, quiet_reconfirm, lease | zero_tx_on_reject, no_forbidden_interleave, no_stale_context_replay | SC-02, SC-03, SC-14, SC-33, DF-02 |
| `s24-xfer-abort-on-release` | contract-blackbox | L1.5 | linux | posix_pty, process_rpc | file_transfer, drain_release | no_auto_resend, no_stale_context_replay | SC-04 |
| `s24-xfer-no-retry-side-effect` | contract-blackbox | L1.5 | linux, windows | process_rpc | file_transfer, device_handoff_full | no_auto_resend | — |
| `s24-flash-silent-unbridged` | contract-blackbox | L1.5 | linux | posix_pty, process_rpc | mcu_flash | zero_tx_on_reject | — |
| `s24-flash-sync-replay` | contract-blackbox | L1.5 | linux | posix_pty, process_rpc | mcu_flash | no_auto_resend | — |
| `s24-flash-holder-gone` | contract-blackbox | L1.5 | linux | posix_pty, process_rpc | mcu_flash | — | — |
| `s24-flash-baud-mirror` | contract-blackbox | L1.5 | linux | posix_pty, process_rpc | mcu_flash | — | — |
| `s24-flash-arbitration` | rust-l1 | L1 | linux, windows | memory_only | mcu_flash, deferred_buffer | zero_tx_on_reject, no_stale_context_replay | SC-02, SC-14, BC-02 |
| `s24-flash-ioerror-recovering` | rust-l1 | L1 | linux, windows | memory_only | mcu_flash, recovery_min | no_reopen_before_handoff | SC-32 |
| `s24-mcu-rpc-shapes` | contract-blackbox | L1.5 | linux, windows | process_rpc | mcu_flash | — | — |
| `s24-ho-ext-tool` | contract-blackbox | L1.5 | linux | posix_pty, process_rpc | device_handoff_full | no_reopen_before_handoff | SC-05, BC-04 |
| `s24-ho-second-daemon` | contract-blackbox | L1.5 | linux | posix_pty, process_rpc | device_handoff_full | no_reopen_before_handoff | SC-05 |
| `s24-ho-persist-restart` | contract-blackbox | L1.5 | linux | posix_pty, process_rpc | device_handoff_full, state_persistence | — | DF-04 |
| `s24-ho-windows-com` | contract-blackbox | L1.5 | windows | windows_com, process_rpc | device_handoff_full | no_reopen_before_handoff | BC-04 |
| `s24-sup-systemd-runtime` | contract-blackbox | L1.5 | linux | supervisor_runtime, posix_pty | supervision, file_transfer, mcu_flash | no_auto_resend | SC-11 |
| `s24-sup-wintask-runtime` | rust-l1 | L1.5 | windows | supervisor_runtime | supervision | — | SC-24 |
| `s24-upd-guards` | contract-blackbox | L1.5 | linux | posix_pty, process_rpc | supervision, file_transfer, mcu_flash, lease | no_reopen_before_handoff | SC-23 |
| `s24-upd-rollback-cross-stage` | contract-blackbox | L1.5 | linux | posix_pty, process_rpc | supervision, device_handoff_full | no_reopen_before_handoff | SC-23 |
| `s24-contract-coverage` | rust-l1 | L1 | linux, windows | memory_only | — | insufficient_evidence_not_pass | — |

既有、由本 Phase 提供 Rust 證據的 P2 案例（**不重新登記、不聚合**，只在 Task 15 的彙總與 BLOCKED-OWNER 中以原 ID 引用）：L2 `f7-binary-roundtrip-md5`、`f7-larger-file-not-truncated`、`f10-creds-fixed-then-ready`、`f10-unresolved-creds-terminal`（以及 L2 其餘 34 筆的回歸）；L3 `p1-cmd-file`、`p1-ho-cycle`、`p1-ho-persist`、`p1-rst-daemon`、`lr-mixed`；Windows `win-task-logon-trigger`、`win-task-single-instance`、`win-task-restart-on-failure`、`win-task-72h`；手動 `p2-1-mcu-flash-manual`。分母一律取 `cases.lock.json` 的 `sets["slice_2.4"]`（`necessity=="required"` 且 `daemon_slice ≤ 2.4`，已含 2.1～2.3 回歸），本 Task 不另算分母。

**斷路器凍結（`tests/acceptance/breaker/slice_2.4.json`，`DISK_STABLE`）**：
```json
{"schema_version":1,"slice":"2.4","frozen_at":"<開工日 YYYY-MM-DD>","window":{"start":"<frozen_at>","weeks":3},
 "cases_revision":"<cases.yaml revision>","cases_lock_sha256":"<cases.lock.json 位元組 sha256>",
 "denominator_set":"slice_2.4","denominator":<len(sets["slice_2.4"])>,"stage2_started_at":"<Slice 2.1 開工日>"}
```
`scripts/rust/slice_breaker.py`（stdlib＋PyYAML）：
```python
def acceptance_rate(lock: dict, results: list[dict], *, set_name: str) -> float     # 取得有效 PASS 證據（Task 15 判定）的必要案例數 ÷ 凍結分母；SKIP／證據不足不計
def evaluate(freeze: dict, snapshots: list[dict], *, today: str, stage2_estimate_weeks: float) -> dict
#   snapshots：[{"date","rate","results_sha256"}]（每週一筆，check-in 至 tests/acceptance/breaker/slice_2.4.snapshots.ndjson）
#   回 {"tripped": bool, "reason": str|None, "window_delta_pp": float, "od02_resubmit": bool}
#   tripped：觀測窗口內任一連續三週 rate 累計增加 < 10 個百分點；od02_resubmit：stage2_started_at 至今已耗週數＋剩餘預估 > 24
def main(argv=None) -> int      # tripped 或 od02_resubmit → exit 1，stderr 指示「停止擴大部署、退守 Stage 1 artifact、提請擁有者審查／重提 OD-02」
```

- [ ] **Step 1：寫失敗測試**

```python
# tests/test_slice24_cases.py
from __future__ import annotations
import hashlib, json, subprocess, sys
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACC = ROOT / "tests" / "acceptance"
P8_IDS = {
    "s24-xfer-templates-golden", "s24-xfer-push-roundtrip", "s24-xfer-pull-roundtrip",
    "s24-xfer-error-shapes", "s24-xfer-echo-stall", "s24-xfer-session-gates",
    "s24-xfer-abort-on-release", "s24-xfer-no-retry-side-effect", "s24-flash-silent-unbridged",
    "s24-flash-sync-replay", "s24-flash-holder-gone", "s24-flash-baud-mirror", "s24-flash-arbitration",
    "s24-flash-ioerror-recovering", "s24-mcu-rpc-shapes", "s24-ho-ext-tool", "s24-ho-second-daemon",
    "s24-ho-persist-restart", "s24-ho-windows-com", "s24-sup-systemd-runtime", "s24-sup-wintask-runtime",
    "s24-upd-guards", "s24-upd-rollback-cross-stage", "s24-contract-coverage",
}
REUSED_P2 = {"f7-binary-roundtrip-md5", "f7-larger-file-not-truncated", "p1-cmd-file", "p1-ho-cycle",
             "p1-ho-persist", "p1-rst-daemon", "lr-mixed", "win-task-restart-on-failure", "win-task-72h",
             "p2-1-mcu-flash-manual"}

def _cases() -> dict:
    return {c["case_id"]: c for c in yaml.safe_load((ACC / "cases.yaml").read_text(encoding="utf-8"))["cases"]}

def test_p8_cases_follow_p2_schema_and_slice():
    by_id = _cases()
    assert not (P8_IDS - by_id.keys()), sorted(P8_IDS - by_id.keys())
    for cid in P8_IDS:
        c = by_id[cid]
        assert (c["daemon_slice"], c["necessity"], c["family"]) == ("2.4", "required", "S24"), cid
        assert isinstance(c["platforms"], list) and isinstance(c["capabilities"], list), cid
        assert c["contract"] and c["title"] and c["evidence"], cid

def test_no_aggregate_or_duplicate_of_p2_cases():
    by_id = _cases()
    assert REUSED_P2 <= by_id.keys()
    assert not [cid for cid in by_id if cid.startswith("s24-") and by_id[cid]["layer"] in ("L2", "L3", "manual")], \
        "L2／L3／手動一律引用 P2 個別案例，不得新增聚合案例"

def test_lock_contains_p8_and_never_shrinks():
    r = subprocess.run([sys.executable, str(ACC / "regen_lock.py"), "--check"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    lock = json.loads((ACC / "cases.lock.json").read_text(encoding="utf-8"))
    assert P8_IDS | {"f7-binary-roundtrip-md5", "p1-cmd-file"} <= set(lock["sets"]["slice_2.4"])

def test_new_behavior_changes_registered_and_keep_items_not():
    bc = {c["id"]: c for c in yaml.safe_load((ROOT / "contracts/behavior_changes.yaml").read_text(encoding="utf-8"))["changes"]}
    for sid in ("SC-32", "SC-33"):
        assert bc[sid]["kind"] == "SC" and bc[sid]["applies_to"]["python"] is None and "P8" in bc[sid]["phases"], sid
    assert bc["SC-33"]["owner_confirmation"] == "pending_default" and bc["SC-33"]["owner_item"] == 8
    doc = yaml.safe_load((ROOT / "contracts/behavior_changes.yaml").read_text(encoding="utf-8"))
    assert [o["item"] for o in doc["pending_owner_confirmations"]] == [8]   # OD-14 已清空 item 1～7（P1-OC）
    assert "SC-08" not in bc                                                  # OD-14 #3 撤回
    assert not any(k.startswith(("XFER-", "MCU-STATUS", "SC-23-G", "SC-24-S")) for k in bc)

def test_breaker_freeze_matches_lock():
    fr = json.loads((ACC / "breaker" / "slice_2.4.json").read_text(encoding="utf-8"))
    lock_bytes = (ACC / "cases.lock.json").read_bytes()
    lock = json.loads(lock_bytes)
    assert fr["denominator"] == len(lock["sets"]["slice_2.4"]) and fr["window"] == {"start": fr["frozen_at"], "weeks": 3}
    # 凍結後新增案例只會讓 lock 變大；分母不得縮小
    assert fr["denominator"] <= len(lock["sets"]["slice_2.4"])

def test_breaker_trips_on_lt_10pp_over_three_weeks(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("sb", ROOT / "scripts/rust/slice_breaker.py")
    sb = importlib.util.module_from_spec(spec); spec.loader.exec_module(sb)
    fr = {"window": {"start": "2026-11-02", "weeks": 3}, "stage2_started_at": "2026-08-01", "denominator": 100}
    snaps = [{"date": "2026-11-02", "rate": 0.40}, {"date": "2026-11-09", "rate": 0.43},
             {"date": "2026-11-16", "rate": 0.46}, {"date": "2026-11-23", "rate": 0.49}]
    out = sb.evaluate(fr, snaps, today="2026-11-23", stage2_estimate_weeks=4)
    assert out["tripped"] is True and round(out["window_delta_pp"], 1) == 9.0
    snaps[-1]["rate"] = 0.51
    assert sb.evaluate(fr, snaps, today="2026-11-23", stage2_estimate_weeks=4)["tripped"] is False
    assert sb.evaluate(fr, snaps, today="2027-03-01", stage2_estimate_weeks=4)["od02_resubmit"] is True
```

- [ ] **Step 2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_slice24_cases.py` → `cases.yaml` 缺 P8 案例、`slice_breaker.py`／凍結檔不存在。階段 A 以 `@pytest.mark.xfail(strict=True, reason="P8-T1 階段 A")` 維持全套綠燈，階段 B 移除。
- [ ] **Step 3：commit 測試**：`test(acceptance): 新增 Slice 2.4 案例登記、契約增補與斷路器檢查`
- [ ] **Step 4：實作**
  - 依上表與 P2 schema 追加 24 筆；schema 列舉只增不改；執行 `python3 tests/acceptance/regen_lock.py --add-only`（不得手改鎖檔）。
  - `behavior_changes.yaml` 依 P0 schema 寫 SC-32、SC-33 全部必填欄位（`approval.basis: spec_final`、`applies_to: {python: null, rust: {stage: slice-2.4}}`、`spec_refs`、`phases: [P8]`、`case_ids`、`compatibility_impact`、`approval_conditions`、`approved_at_revision`、`ab_participation: absolute_only`；不得寫 `contract_revision` 鍵）；調和表與 needs-owner 清單同步。
  - `--frozen-at` 為本 Phase 開工日，產生並 check-in `breaker/slice_2.4.json`；移除 xfail。
- [ ] **Step 5：執行並確認 PASS**：`python3 -m pytest -q tests/test_slice24_cases.py tests/test_acceptance_cases.py`；`python3 -m policy_check --repo .`。
- [ ] **Step 6：commit**：`feat(acceptance): 登記 Slice 2.4 案例與契約增補並凍結斷路器分母`

**Changelog fragment：** `changelog.d/rust-p8-slice24-cases.md`（`type: change`、`scope: acceptance`）：記錄 Slice 2.4 案例、SC-32／SC-33 登記、斷路器凍結與三週觀測窗口。

**BLOCKED-OWNER：** SC-33、XFER-ABORT-01（BC-03 適用性）、EVT-D-F4／F7 三項 needs-owner 裁決（未裁決前依預設往下做；EVT-D-F4 未裁決則 `f7-larger-file-not-truncated` 阻擋出口）。

---

## Task 2：`mcu_patterns` registry 與 hex 語意

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh

**Files:**
- Modify: `crates/sw-core/src/mcu_patterns.rs`（P5 06 Task 0 已建骨架檔）
- Modify: `crates/sw-core/src/lib.rs`（`pub mod mcu_patterns;`）
- Create: `scripts/rust/capture_mcu_golden.py`
- Create: `contracts/golden/mcu_patterns.ndjson`
- Test: `crates/sw-core/tests/mcu_patterns_golden.rs`、`tests/test_mcu_golden_fresh.py`

**Interfaces:**
- Consumes：`sw_proto::pyjson::{PyValue, PyDict, loads}`、`sw_proto::pycompat::{py_int_parse, py_truthy}`、`sw_proto::pyfmt::py_str`（Task 0 核對的實名）。
- Produces：
  ```rust
  #[derive(Debug, Clone, PartialEq, Eq)]
  pub struct McuPattern { pub family: String, pub probe: Vec<u8>, pub expect: Vec<u8>,
                          pub baud: u32, pub timeout_ms: u64, pub non_destructive: bool }
  #[derive(Debug, Clone, PartialEq, Eq)]
  pub enum McuPatternError {
      NotNonDestructive { family: String },   // ValueError: pattern 'x' 未通過 non_destructive 審核，拒絕載入
      MissingKey(&'static str),               // KeyError
      Hex(String),                            // bytes.fromhex 的 ValueError
      Int(String),                            // int() 的 ValueError
  }
  impl McuPattern {
      pub fn new(family: String, probe: Vec<u8>, expect: Vec<u8>, baud: u32, timeout_ms: u64,
                 non_destructive: bool) -> Result<Self, McuPatternError>;
      pub fn from_dict(d: &PyDict) -> Result<Self, McuPatternError>;
  }
  pub fn hex_to_bytes(s: &str) -> Result<Vec<u8>, McuPatternError>;
  pub fn bytes_hex_spaced(b: &[u8]) -> String;            // bytes.hex(" ")
  #[derive(Debug, Clone)]
  pub struct McuPatternRegistry { /* IndexMap<String, McuPattern>，同名後者覆蓋、位置保留首次 */ }
  impl McuPatternRegistry {
      pub fn default_registry() -> Self;
      pub fn load(rows: Option<&[PyDict]>) -> Result<Self, McuPatternError>;
      pub fn get(&self, family: &str) -> Option<&McuPattern>;
      pub fn all(&self) -> Vec<&McuPattern>;
      pub fn patterns_rpc(&self) -> PyValue;   // [{"family","probe","expect","baud"}]，不含 timeout_ms（UIO-12.10-f）
  }
  ```

**必須重現的 Python 語意（`sw_core/mcu_patterns.py`）：**
- `hex_to_bytes`：先 `replace(" ", "")`，再 `replace("0x", "")`（大寫 `0X` 不移除），再依 `bytes.fromhex` 解析：位元組對之間允許 ASCII 空白 `[ \t\n\r\v\f]`，奇數位或非 hex 字元報錯。
- `from_dict`：`family=str(d["family"])`；`probe`／`expect` 先 `str()` 再 `hex_to_bytes`；`baud=int(d.get("baud",115200))`；`timeout_ms=int(d.get("timeout_ms",500))`；`non_destructive=bool(d.get("non_destructive", False))`，缺省為 False 即拒絕。
- `load(None)` 只含預設一筆；`load(rows)` 為預設＋rows 依序，`by_family` 同名後者覆蓋（dict 插入序：位置保留首次）。

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/mcu_patterns_golden.rs
use sw_core::mcu_patterns::{bytes_hex_spaced, hex_to_bytes, McuPattern, McuPatternRegistry};
use sw_proto::pyjson::{self, PyValue, DumpOpts, Sep};

fn compact(v: &PyValue) -> String {
    pyjson::dumps(v, &DumpOpts { sep: Sep::Compact, ensure_ascii: false, sort_keys: false, allow_nan: true })
}

#[test]
fn default_patterns_rpc_matches_python_bytes() {
    let reg = McuPatternRegistry::default_registry();
    assert_eq!(compact(&reg.patterns_rpc()),
        r#"[{"family":"ti-cc26xx","probe":"55 55","expect":"00 cc","baud":115200}]"#);
    let p = reg.get("ti-cc26xx").unwrap();
    assert_eq!((p.timeout_ms, p.non_destructive), (500, true));
}

#[test]
fn golden_rows_match_python() {
    let text = include_str!("../../../contracts/golden/mcu_patterns.ndjson");
    let mut n = 0;
    for line in text.lines().filter(|l| !l.is_empty()) {
        let PyValue::Dict(rec) = pyjson::loads(line).unwrap() else { panic!("bad golden line") };
        let f = match rec.get("fn") { Some(PyValue::Str(s)) => s.clone(), _ => panic!() };
        let input = rec.get("in").unwrap();
        let want = rec.get("out").unwrap();
        let got = match f.as_str() {
            "hex_to_bytes" => match hex_to_bytes(input.as_str().unwrap()) {
                Ok(b) => PyValue::Str(bytes_hex_spaced(&b)),
                Err(_) => PyValue::Str("<error>".into()),
            },
            "from_dict" => match McuPattern::from_dict(input.as_dict().unwrap()) {
                Ok(p) => PyValue::Str(format!("{}|{}|{}|{}|{}", p.family, bytes_hex_spaced(&p.probe),
                                              bytes_hex_spaced(&p.expect), p.baud, p.timeout_ms)),
                Err(_) => PyValue::Str("<error>".into()),
            },
            "load" => {
                let rows: Vec<_> = input.as_list().unwrap().iter().map(|r| r.as_dict().unwrap().clone()).collect();
                match McuPatternRegistry::load(Some(&rows)) {
                    Ok(reg) => PyValue::Str(reg.all().iter().map(|p| format!("{}:{}", p.family, p.baud))
                                              .collect::<Vec<_>>().join(",")),
                    Err(_) => PyValue::Str("<error>".into()),
                }
            }
            other => panic!("unknown fn {other}"),
        };
        assert_eq!(&got, want, "fn={f} in={}", compact(input));
        n += 1;
    }
    assert_eq!(n, GOLDEN_ROWS, "golden 筆數與輸入表不符");   // GOLDEN_ROWS = 12 + 9 + 2 = 23（表內列數；增列時同步改）
}
const GOLDEN_ROWS: usize = 23;
```

`capture_mcu_golden.py` 產生的輸入集合（每筆 `{"fn","in","out"}`，`out` 為上面相同格式字串或 `"<error>"`）：

| fn | 輸入 | 期望（由 Python 決定） |
|---|---|---|
| `hex_to_bytes` | `"5555"`、`"55 55"`、`"0x55 0x55"`、`"0X55"`、`"55\t55"`、`"5"`、`"zz"`、`""`、`"00cc"`、`"0x0x55"`、`"55\n55"`、`"5 5"` | Python `_hex_to_bytes` 的結果 |
| `from_dict` | 缺 `non_destructive`、`non_destructive:"false"`（truthy）、`baud:"9600"`、`baud:"x"`、`timeout_ms:0`、缺 `family`、`family:123`、`probe:5555`（int → str）、`non_destructive:0` | `McuPattern.from_dict` 結果或 `<error>` |
| `load` | (1) 自訂一筆同名 `ti-cc26xx`（baud 9600）；(2) 一筆新 family；每筆 `in` 為 rows list | `out` 為 `"family:baud,…"`（依 `all()` 順序）或 `<error>` |

- [ ] **Step 2：執行並確認 FAIL**：`cargo test -p sw-core --test mcu_patterns_golden` → `not yet implemented: P8-T2`。
- [ ] **Step 3：commit 測試**：`test(mcu): 新增 MCU pattern registry golden 失敗測試`
- [ ] **Step 4：實作**：依上列語意；`patterns_rpc` 以 `pydict!{"family"=>…, "probe"=>bytes_hex_spaced(..), "expect"=>…, "baud"=>…}` 建構；`tests/test_mcu_golden_fresh.py` 以 subprocess 重跑 `capture_mcu_golden.py --stdout` 並比對 check-in 檔，確保 golden 與 Python 同步。
- [ ] **Step 5：執行並確認 PASS**：`cargo test -p sw-core --test mcu_patterns_golden`、`python3 -m pytest -q tests/test_mcu_golden_fresh.py`。
- [ ] **Step 6：commit**：`feat(mcu): 承接 MCU pattern registry 與 hex 解析語意`

**Changelog fragment：** `changelog.d/rust-p8-mcu-patterns.md`（`type: feat`、`scope: rust-core`）。

**BLOCKED-OWNER：** 無。

---

## Task 3：File transfer 板端命令樣板與 RX 判定 helper（Python golden）

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high

**Files:**
- Create: `crates/sw-core/src/file_transfer.rs`（模組根；本 Task 只宣告 `pub mod templates; pub mod rx_match;`）
- Create: `crates/sw-core/src/file_transfer/templates.rs`
- Create: `crates/sw-core/src/file_transfer/rx_match.rs`
- Create: `scripts/rust/capture_transfer_golden.py`
- Create: `contracts/golden/file_transfer.ndjson`
- Modify: `crates/sw-core/Cargo.toml`（`md-5 = "=0.10.6"`）、`docs/rust-refactor/plans/rust-architecture.md` §1.2 依賴表加一列 `md-5 | =0.10.6 | sw-core | Python hashlib.md5`
- Test: `crates/sw-core/tests/file_transfer_golden.rs`、`tests/test_transfer_golden_fresh.py`

**Interfaces:**
- Consumes：`sw_proto::pycompat::{shlex_quote, strip_ansi, b2a_base64}`（P5 06 Task 0c）、`splitlines`（P3 04 Task 4）（Task 0 核對；缺漏屬上游缺陷）、`sw_proto::pyre::{compile, PyRegex}`。
- Produces：
  ```rust
  // templates.rs
  pub const SENTINEL_BEGIN: &str = "===SW_XFER_BEGIN===";
  pub const SENTINEL_END: &str = "===SW_XFER_END===";
  pub const TOOL_PROBE_PAYLOAD: &str = "c3ctcHJvYmU=";
  pub const DECODER_B64_SENTINEL: &str = "SW_XFER_DECODER_B64_42";
  pub const DECODER_OPENSSL_SENTINEL: &str = "SW_XFER_DECODER_OPENSSL_42";
  pub const ENCODER_B64_SENTINEL: &str = "SW_XFER_ENCODER_B64_42";
  pub const ENCODER_OPENSSL_SENTINEL: &str = "SW_XFER_ENCODER_OPENSSL_42";
  pub const CHUNK_SENTINEL: &str = "SW_XFER_CHUNK_42";
  pub const MD5_SENTINEL: &str = "SW_XFER_MD5_42";
  pub const MV_SENTINEL: &str = "SW_XFER_MV_42";
  pub const CLEANUP_SENTINEL: &str = "SW_XFER_CLEANUP_42";
  pub const ENCODER_FAILURE_SENTINEL: &str = "SW_XFER_ENCODER_FAILED_42";
  pub const DECODERS: [&str; 2] = ["base64", "openssl"];
  pub const ENCODERS: [&str; 2] = ["base64", "openssl"];
  pub const ACK_MODES: [&str; 3] = ["auto", "echo", "none"];
  pub const DEFAULT_CHUNK_SIZE: i128 = 512;
  pub const DEFAULT_ECHO_SLICE_SIZE: usize = 64;
  pub const DEFAULT_ECHO_TIMEOUT_S: f64 = 5.0;
  #[derive(Debug, PartialEq, Eq)] pub enum TemplateError { UnsupportedMarker(String), UnknownTool(String) }
  pub fn runtime_sentinel(prefix: &str) -> String;                 // printf '%s\n' '<prefix>'$((6*7))
  pub fn marker_expression(marker: &str) -> Result<String, TemplateError>;
  pub fn decoder_probe_command() -> String;
  pub fn encoder_probe_command() -> String;
  pub fn decoder_command_name(decoder: &str) -> Result<&'static str, TemplateError>;
  pub fn encoder_command_name(encoder: &str) -> Result<&'static str, TemplateError>;
  pub fn chunk_command(encoded: &str, decoder: &str, op: &str, path: &str) -> Result<String, TemplateError>;  // 未知 decoder → UnknownTool（Python raise ValueError）
  pub fn md5_command(path: &str) -> String;
  pub fn mv_command(source_path: &str, remote_path: &str) -> String;
  pub fn cleanup_command(path: &str) -> String;
  pub fn pull_command(remote_path: &str, encoder: &str) -> Result<String, TemplateError>;                    // 未知 encoder → UnknownTool
  pub fn split_chunks(data: &[u8], chunk_size: usize) -> Vec<&[u8]>;   // 空檔 → vec![&[]]
  pub fn line_budget_exceeded(commands: &[&str], budget: Option<i128>) -> bool; // UTF-8 bytes > budget
  pub fn b64encode(data: &[u8]) -> String;                             // base64.b64encode().decode()（無換行）
  pub fn md5_hex(data: &[u8]) -> String;                               // hashlib.md5().hexdigest()
  // rx_match.rs
  pub fn strip_command_echo<'a>(text: &'a str, command: Option<&str>) -> &'a str;
  pub fn runtime_sentinel_seen(text: &str, sentinel: &str, command: Option<&str>) -> bool;
  pub fn failure_sentinel_seen(text: &str, sentinel: &str, command: Option<&str>) -> bool;
  pub fn probe_shell_error(text: &str, command: &str) -> bool;
  pub fn extract_between_sentinels(text: &str, command: Option<&str>) -> Option<String>;
  pub fn remote_md5_from_output(raw: &str, command: &str) -> Option<String>;   // _remote_md5 的解析段
  ```
  `os.path.basename` 一律使用 P3 已有 golden 的 `sw_proto::pypath::basename(flavor, path)`，本 Task 不重寫；`PathFlavor` 取自 `sw_proto::pypath`。

**必須重現的語意（`inventory/09` §4.2～§4.4，`sw_core/file_transfer.py:350-603`）：**
- `strip_command_echo`：`command` 非空且 `text.startswith(command)` → 去掉，再去一個 `\r\n` 或 `\n`。
- `runtime_sentinel_seen`：去 echo 後找 sentinel 出現位置 `p`，條件「`p==0` 或前一字元為 `\n`（含 `\r\n`）」且「其後為字串結尾、`\n` 或 `\r\n`」；任一出現符合即 true（等價 `(?:^|\r?\n)ESC(?:\r?\n|$)`，非 MULTILINE，`$` 亦匹配結尾換行前）。
- `failure_sentinel_seen`：只要求後綴條件（可黏在前文之後）。
- `probe_shell_error`：用 `pyre::compile(r"(?i)(?:syntax error|bad substitution|unterminated|permission denied)")` 對去 echo 後文字 `search`（保留 Python Unicode 大小寫摺疊，例如 `ſ`、`K`）。
- `extract_between_sentinels`：去 echo；`rfind` BEGIN 與 END；任一缺或 `end <= begin` → `None`；中段 `strip_ansi` 後刪除所有 Python `re` 的 `\s` 字元（`\t\n\x0b\x0c\r\x1c-\x1f \x85` 與 Unicode White_Space）。
- `remote_md5_from_output`：未見 `MD5_SENTINEL`（以 `runtime_sentinel_seen`）→ None；取去 echo 後文字中 `MD5_SENTINEL` 首次出現之前的部分，以 Python `splitlines` 分行，第一個符合 `\s*(?:\\)?([0-9a-fA-F]{32})(?:\s|$)`（`re.match`，自行首）的行取 group 1 並轉小寫。
- `chunk_command`／`md5_command`／`mv_command`／`cleanup_command`／`pull_command` 逐字照 `inventory/09` §4.3，路徑與 payload 以 `shlex_quote`。
- `marker_expression`：marker 必須以 `===SW_XFER_` 開頭、`===` 結尾，否則 `UnsupportedMarker`；輸出 `'===SW_XFER_'<middle>'==='`。

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/file_transfer_golden.rs
use sw_core::file_transfer::{rx_match as m, templates as t};
use sw_proto::pyjson::{self, PyValue};

fn st(v: &PyValue) -> &str { v.as_str().expect("str") }
fn opt(v: &PyValue) -> Option<&str> { match v { PyValue::Null => None, x => Some(st(x)) } }

#[test]
fn every_golden_row_matches_python() {
    let mut n = 0usize;
    for line in include_str!("../../../contracts/golden/file_transfer.ndjson").lines().filter(|l| !l.is_empty()) {
        let PyValue::Dict(rec) = pyjson::loads(line).unwrap() else { panic!() };
        let f = st(rec.get("fn").unwrap()).to_string();
        let a = rec.get("args").unwrap().as_list().unwrap();
        let want = rec.get("out").unwrap();
        let got = match f.as_str() {
            "runtime_sentinel" => PyValue::Str(t::runtime_sentinel(st(&a[0]))),
            "marker_expression" => t::marker_expression(st(&a[0])).map(PyValue::Str)
                                     .unwrap_or(PyValue::Str("<ValueError>".into())),
            "decoder_probe_command" => PyValue::Str(t::decoder_probe_command()),
            "encoder_probe_command" => PyValue::Str(t::encoder_probe_command()),
            "chunk_command" => t::chunk_command(st(&a[0]), st(&a[1]), st(&a[2]), st(&a[3])).map(PyValue::Str)
                                 .unwrap_or(PyValue::Str("<ValueError>".into())),
            "md5_command" => PyValue::Str(t::md5_command(st(&a[0]))),
            "mv_command" => PyValue::Str(t::mv_command(st(&a[0]), st(&a[1]))),
            "cleanup_command" => PyValue::Str(t::cleanup_command(st(&a[0]))),
            "pull_command" => t::pull_command(st(&a[0]), st(&a[1])).map(PyValue::Str)
                                .unwrap_or(PyValue::Str("<ValueError>".into())),
            "b64encode" => PyValue::Str(t::b64encode(&hex::decode_str(st(&a[0])))),
            "strip_command_echo" => PyValue::Str(m::strip_command_echo(st(&a[0]), opt(&a[1])).to_string()),
            "runtime_sentinel_seen" => PyValue::Bool(m::runtime_sentinel_seen(st(&a[0]), st(&a[1]), opt(&a[2]))),
            "failure_sentinel_seen" => PyValue::Bool(m::failure_sentinel_seen(st(&a[0]), st(&a[1]), opt(&a[2]))),
            "probe_shell_error" => PyValue::Bool(m::probe_shell_error(st(&a[0]), st(&a[1]))),
            "extract_between_sentinels" => m::extract_between_sentinels(st(&a[0]), opt(&a[1]))
                                             .map(PyValue::Str).unwrap_or(PyValue::Null),
            "remote_md5_from_output" => m::remote_md5_from_output(st(&a[0]), st(&a[1]))
                                          .map(PyValue::Str).unwrap_or(PyValue::Null),
            other => panic!("golden 出現未處理的函式 {other}"),
        };
        assert_eq!(&got, want, "fn={f} line={line}");
        n += 1;
    }
    assert!(n >= 600, "golden 只有 {n} 筆");
}

#[test]
fn chunk_template_lengths_match_inventory() {
    let tmp = "/tmp/.sw_upload_0123456789ab";   // 28 字元
    assert_eq!(t::chunk_command("", "base64", ">", tmp).unwrap().len(), 99);
    assert_eq!(t::chunk_command("", "openssl", ">>", tmp).unwrap().len(), 116);
    assert_eq!(t::chunk_command("", "bogus", ">", tmp), Err(t::TemplateError::UnknownTool("bogus".into())));
    assert_eq!(t::decoder_probe_command().len(), 282);
    assert_eq!(t::encoder_probe_command().len(), 273);
}

#[test]
fn split_chunks_empty_file_yields_one_empty_chunk() {
    assert_eq!(t::split_chunks(b"", 512), vec![&b""[..]]);
    assert_eq!(t::split_chunks(&[0u8; 1025], 512).iter().map(|c| c.len()).collect::<Vec<_>>(), vec![512, 512, 1]);
}
```
（`hex::decode_str` 為測試檔內的 10 行 helper，把 golden 中以 hex 表示的 bytes 轉回 `Vec<u8>`。）

`capture_transfer_golden.py` 的輸入集合（`random.Random(20261015)` 決定性產生，至少 600 筆）：

| fn | 輸入族 |
|---|---|
| 樣板類 | 路徑：`/tmp/a`、`/tmp/with space`、`/tmp/it's`、`""`、`/tmp/ü中`、`/tmp/$(rm -rf)`、`/tmp/a\nb`、`-rf`、`/tmp/.sw_upload_0123456789ab`；decoder／encoder：兩者＋`"bogus"`（期望 `<ValueError>`：`chunk_command`／`pull_command` 對未知工具由 Python raise，Rust 回 `Err(UnknownTool)`，測試以 `unwrap_or("<ValueError>")` 正規化）；op：`>`、`>>`；encoded：空、`b64encode` of 0/1/2/3/512/513 bytes |
| `b64encode` | 0～6 bytes 全組合抽樣、512 bytes 隨機 |
| `strip_command_echo` | echo 完全相同／前綴不同／後接 `\r\n`、`\n`、`\r`、無換行；`command=None` |
| `*_sentinel_seen` | sentinel 位於開頭、`\n` 後、`\r\n` 後、行中間、黏前文、字串結尾、結尾 `\n`、後接 `\r` 非 `\n`、只出現在 echo 中（`'..._'$((6*7))` 形式） |
| `probe_shell_error` | 四種關鍵字大小寫變化、`ſyntax error`（U+017F）、echo 內含關鍵字 |
| `extract_between_sentinels` | 正常、多組（取最後）、END 在 BEGIN 前、缺一、含 ANSI、含 `\x1c`、`\x85`、U+2028、全形空白 U+3000 |
| `remote_md5_from_output` | GNU 格式、`\` 前綴、路徑含 32 hex、大寫 hex、多行、無 sentinel、sentinel 只在 echo |

- [ ] **Step 2：執行並確認 FAIL**：`cargo test -p sw-core --test file_transfer_golden` → `not yet implemented: P8-T3`；`python3 -m pytest -q tests/test_transfer_golden_fresh.py` 在 golden 檔 check-in 後 PASS（它驗 Python 與 golden 同步，不驗 Rust）。
- [ ] **Step 3：commit 測試**：`test(transfer): 新增檔案傳輸樣板與 RX 判定的 Python golden 測試`
- [ ] **Step 4：實作**：依上列語意逐函式實作；`md5_hex` 用 `md5::Md5`（md-5 crate）。`runtime_sentinel_seen` 以手寫掃描實作（不經 regex），由 golden 保證等價。
- [ ] **Step 5：執行並確認 PASS**：`cargo test -p sw-core --test file_transfer_golden`；`scripts/rust/win-test.sh --package sw-core` 同樣通過（純邏輯，`core_portable`）。
- [ ] **Step 6：commit**：`feat(transfer): 承接檔案傳輸板端命令樣板與 sentinel 判定`

**Changelog fragment：** `changelog.d/rust-p8-transfer-templates.md`（`type: feat`、`scope: rust-core`）。

**BLOCKED-OWNER：** 無。

---

## Task 4：File transfer 引擎 `push_file`／`pull_file`

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high

**Files:**
- Modify: `crates/sw-core/src/file_transfer.rs`（引擎本體）
- Create: `crates/sw-core/tests/support/fake_shell.rs`
- Test: `crates/sw-core/tests/file_transfer_engine.rs`

**Interfaces:**
- Consumes：Task 3 全部；`sw_proto::pycompat::a2b_base64_lenient`；`sw_proto::pypath::{basename, PathFlavor}`；`sw_proto::pyre::PyRegex`；P5 `tx_writer::FenceReject`。
- Produces：
  ```rust
  #[derive(Debug, Clone, PartialEq, Eq)]
  pub struct EchoPaced { pub ok: bool, pub acked_chars: usize, pub sent_chars: usize }
  #[derive(Debug, Clone, PartialEq, Eq)]
  pub enum TransferAbort {
      SerialNotReady,        // epoch 失效／frozen／cancelled／flash／drain／bridge 不在 → framing EXCEPTION（XFER-ABORT-01）
      Refused(FenceReject),  // 只用於 Quiet／ReconfirmPending：尚未開始的寫入被 gate 拒絕 → session 層轉 AUTOBOOT_QUIET（SC-33）
      LocalIo(String),       // 讀寫 daemon 端本地檔失敗 → framing EXCEPTION（message 為 Python str(OSError) 格式）
      BadPromptRegex(String) // profile prompt_regex 無法編譯 → framing EXCEPTION
  }
  pub trait TransferPort {
      fn send_command(&mut self, cmd: &str, source: &str, cmd_id: Option<&str>) -> Result<(), TransferAbort>;
      fn supports_echo_paced(&self) -> bool;                     // 產品實作恆 true；fake 可 false
      fn send_command_echo_paced(&mut self, cmd: &str, source: &str, cmd_id: Option<&str>,
                                 slice_size: usize, echo_timeout: std::time::Duration)
          -> Result<EchoPaced, TransferAbort>;
      fn cancel_input_line(&mut self, source: &str) -> Result<(), TransferAbort>;
      fn rx_snapshot_len(&self) -> u64;
      fn rx_text_from(&self, from_offset: u64) -> String;
      fn wait_for_regex_from(&mut self, re: &PyRegex, from_offset: u64, timeout: std::time::Duration)
          -> Result<bool, TransferAbort>;
  }
  pub trait LocalFiles: Send + Sync {
      fn is_file(&self, path: &str) -> bool;                                // os.path.isfile
      fn read_all(&self, path: &str) -> Result<Vec<u8>, String>;            // Err = str(OSError)
      fn write_truncate(&self, path: &str, data: &[u8]) -> Result<(), String>; // open(p,"wb").write，非原子、不 mkdir
  }
  pub struct StdLocalFiles;                                                  // std::fs 實作，相對路徑以 daemon cwd 為準
  pub trait TransferIds { fn hex(&mut self, n: usize) -> String; }          // uuid4().hex[:n]
  pub struct PushParams { pub local_path: String, pub remote_path: String, pub chunk_size: i128,
      pub timeout: std::time::Duration, pub prompt_regex: String, pub source: String, pub ack_mode: String,
      pub echo_slice_size: usize, pub echo_timeout: std::time::Duration, pub max_console_line_chars: Option<i128> }
  pub struct PullParams { pub remote_path: String, pub local_path: Option<String>, pub timeout: std::time::Duration,
      pub prompt_regex: String, pub source: String, pub max_console_line_chars: Option<i128>, pub path_flavor: PathFlavor }
  pub fn push_file(port: &mut dyn TransferPort, files: &dyn LocalFiles, ids: &mut dyn TransferIds,
                   p: &PushParams) -> Result<PyDict, TransferAbort>;
  pub fn pull_file(port: &mut dyn TransferPort, files: &dyn LocalFiles, ids: &mut dyn TransferIds,
                   p: &PullParams) -> Result<PyDict, TransferAbort>;
  ```

**必須重現的流程（`inventory/09` §4.5、§4.6；`file_transfer.py:63-347`）：**
- push：ack_mode 驗證 → line budget 驗證 → `chunk_size<=0` → `LOCAL_FILE_NOT_FOUND` → ack 模式解析（`none` 不 paced；`echo` 且不支援 → `ECHO_ACK_UNSUPPORTED`；`auto` 依支援與否）→ 讀檔與 md5 → `tmp=/tmp/.sw_upload_{ids.hex(12)}`、`prefix=ft-{ids.hex(8)}`（呼叫順序固定：先 tmp 後 prefix）→ 固定命令預算 → decoder probe 預算 → 空 chunk 四種樣板預算與 `raw_capacity` → **第一個 TX**（decoder probe）→ effective chunk 與實際 decoder 預算驗證 → 逐 chunk（op 第 0 段 `>`）→ md5 驗證 → mv。
- 錯誤 dict 的 key 與插入序逐字照盤點（例：`{"ok":false,"error_code":"TRANSFER_ECHO_STALL","chunks_sent":idx,"chunks_total":total,"acked_chars":…,"sent_chars":…}`）。
- 每個 chunk 先 `pre = rx_snapshot_len()` 再送；cleanup 結果忽略；`MOVE_TIMEOUT`／`MOVE_FAILED` 不 cleanup。
- pull：`local_path=None` → `pypath::basename(flavor, remote_path)` → budget → md5＋encoder probe 預算 → 兩種 pull 命令預算 → encoder probe → pull 命令（cmd_id `{prefix}-b64`）單一 timeout → 擷取（失敗時看 ENCODER_FAILED sentinel 決定 `TARGET_ENCODER_FAILED` 或 `PULL_PARSE_FAILED`）→ 解碼：內容含非 ASCII → `BASE64_DECODE_FAILED`；否則 `a2b_base64_lenient`，錯誤 → `BASE64_DECODE_FAILED` → md5 比對（`expected`=remote、`actual`=local）→ `write_truncate`（錯誤 → `Err(LocalIo)`）→ `{"ok":true,"bytes","md5","local_path"}`。
- prompt regex 每次呼叫 `pyre::compile(prompt_regex, PyFlags::empty())`；失敗 → `Err(BadPromptRegex)`。
- 任何 `TransferPort` 方法回 `Err(_)`（含 `Refused`）即立即上拋，之後不再送任何命令（含 cleanup；cleanup 本身也會被同一 gate 拒絕）。

- [ ] **Step 1：寫失敗測試**

`fake_shell.rs`：`FakeShell` 實作 `TransferPort`，以虛擬檔案系統解譯 Task 3 樣板產生的命令（只認這些樣板，其他命令輸出 `sh: <cmd>: not found`）。每次 `send_*` 先把命令文字當 echo 加到 RX，再依 `ShellCaps` 加入輸出與 prompt `# `。可設定：`base64`／`openssl`／`md5sum` 是否存在、`line_limit: Option<usize>`（超過時截斷並不執行）、`echo_stall_after_chars: Option<usize>`、`corrupt_md5: bool`、`mv_fails: bool`、`drop_prompt_on: Option<&'static str>`（某 cmd_id 後綴不回 prompt）、`inject_noise: Option<String>`、`abort_on_send_n: Option<usize>`（第 n 次 send 回 `Err(SerialNotReady)`）、`refuse_on_send_n: Option<(usize, FenceReject)>`（第 n 次 send 回 `Err(Refused(_))`）、`probe_shell_error: bool`、`rx_max_chars: Option<usize>`（照 `RxWindow` 修剪頭段，`rx_text_from` 以絕對偏移計）。並記錄 `cmd_ids()`、`commands()`、`file(path)`、`send_attempts()`（含被拒的嘗試）。`fake_shell.rs` 由測試作者在 Step 1 交付。

```rust
// crates/sw-core/tests/file_transfer_engine.rs
mod support;
use std::time::Duration;
use support::fake_shell::{FakeShell, FixedIds, MemFiles, ShellCaps};
use sw_core::file_transfer::{pull_file, push_file, templates::md5_hex, PullParams, PushParams, TransferAbort};
use sw_proto::pyjson::{dumps, DumpOpts, PyValue, Sep};
use sw_proto::pypath::PathFlavor;

fn j(d: sw_proto::pyjson::PyDict) -> String {
    dumps(&PyValue::Dict(d), &DumpOpts { sep: Sep::Compact, ensure_ascii: false, sort_keys: false, allow_nan: true })
}
fn push_params(local: &str, remote: &str) -> PushParams {
    PushParams { local_path: local.into(), remote_path: remote.into(), chunk_size: 512,
        timeout: Duration::from_secs(10), prompt_regex: r"(?m)^# $".into(), source: "agent".into(),
        ack_mode: "auto".into(), echo_slice_size: 64, echo_timeout: Duration::from_secs(5),
        max_console_line_chars: None }
}

#[test]
fn push_roundtrip_base64_three_chunks() {
    let data: Vec<u8> = (0u8..=255).cycle().take(1300).collect();
    let files = MemFiles::with("/host/a.bin", &data);
    let mut sh = FakeShell::new(ShellCaps::busybox_base64());
    let mut ids = FixedIds::new(&["0123456789ab", "cdef0123"]);
    let r = push_file(&mut sh, &files, &mut ids, &push_params("/host/a.bin", "/tmp/dst.bin")).unwrap();
    assert_eq!(j(r), format!(r#"{{"ok":true,"bytes":1300,"chunks":3,"md5":"{}","remote_path":"/tmp/dst.bin"}}"#, md5_hex(&data)));
    assert_eq!(sh.file("/tmp/dst.bin").unwrap(), data);
    assert!(sh.file("/tmp/.sw_upload_0123456789ab").is_none());
    assert_eq!(sh.cmd_ids(), ["ft-cdef0123-probe", "ft-cdef0123-0", "ft-cdef0123-1", "ft-cdef0123-2",
                              "ft-cdef0123-md5", "ft-cdef0123-mv"]);
    assert_eq!(sh.paced_cmd_ids(), ["ft-cdef0123-0", "ft-cdef0123-1", "ft-cdef0123-2"],
               "auto 模式下只有 chunk 走 echo-paced");
}

#[test]
fn push_echo_stall_sends_ctrl_u_once_then_cleanup() {
    let files = MemFiles::with("/h/x", &[7u8; 600]);
    let mut caps = ShellCaps::busybox_base64(); caps.echo_stall_after_chars = Some(100);
    let mut sh = FakeShell::new(caps);
    let mut ids = FixedIds::new(&["aaaaaaaaaaaa", "bbbbbbbb"]);
    let r = push_file(&mut sh, &files, &mut ids, &push_params("/h/x", "/t/x")).unwrap();
    assert_eq!(j(r), r#"{"ok":false,"error_code":"TRANSFER_ECHO_STALL","chunks_sent":0,"chunks_total":2,"acked_chars":64,"sent_chars":128}"#);
    assert_eq!(sh.raw_tx_count(b"\x15\n"), 1);
    assert_eq!(sh.cmd_ids().last().unwrap(), "ft-bbbbbbbb-cleanup");
}

#[test]
fn push_abort_mid_transfer_is_exception_without_retry() {
    let files = MemFiles::with("/h/x", &[1u8; 2000]);
    let mut caps = ShellCaps::busybox_base64(); caps.abort_on_send_n = Some(3);
    let mut sh = FakeShell::new(caps);
    let mut ids = FixedIds::new(&["cccccccccccc", "dddddddd"]);
    let err = push_file(&mut sh, &files, &mut ids, &push_params("/h/x", "/t/x")).unwrap_err();
    assert_eq!(err, TransferAbort::SerialNotReady);
    assert_eq!(sh.send_attempts(), 3, "中止後不得再送任何命令（含 cleanup）");
}

#[test]
fn pull_roundtrip_openssl_keeps_newlines_and_writes_basename_in_cwd() {
    let data = b"hello\n\x00\xff world".repeat(40);
    let mut sh = FakeShell::new(ShellCaps::openssl_only());
    sh.put_file("/etc/blob.bin", &data);
    let files = MemFiles::empty();
    let mut ids = FixedIds::new(&["eeeeeeee"]);
    let p = PullParams { remote_path: "/etc/blob.bin".into(), local_path: None, timeout: Duration::from_secs(30),
        prompt_regex: r"(?m)^# $".into(), source: "agent".into(), max_console_line_chars: None,
        path_flavor: PathFlavor::Posix };
    let r = pull_file(&mut sh, &files, &mut ids, &p).unwrap();
    assert_eq!(j(r), format!(r#"{{"ok":true,"bytes":{},"md5":"{}","local_path":"blob.bin"}}"#, data.len(), md5_hex(&data)));
    assert_eq!(files.get("blob.bin").unwrap(), data);
    assert_eq!(sh.cmd_ids(), ["ft-eeeeeeee-probe", "ft-eeeeeeee-b64", "ft-eeeeeeee-md5"]);
}
```

錯誤形狀族（`s24-xfer-error-shapes`；每列一個 `#[test]`，斷言 `j(r)` 完全相等，且「TX 數」以 `sh.send_attempts()` 斷言）：

| 測試名 | 設定 | 期望 `j(r)` | TX 數 |
|---|---|---|---|
| `push_invalid_ack_mode` | `ack_mode:"ehco"` | `{"ok":false,"error_code":"INVALID_ARGS","ack_mode":"ehco"}` | 0 |
| `push_budget_zero` | `max_console_line_chars:Some(0)` | `{"ok":false,"error_code":"CONSOLE_LINE_LIMIT_TOO_SMALL","max_console_line_chars":0}` | 0 |
| `push_chunk_size_zero_direct_api` | `chunk_size:0` | `{"ok":false,"error_code":"INVALID_ARGS","chunk_size":0}` | 0 |
| `push_local_missing` | 不存在 `/nope` | `{"ok":false,"error_code":"LOCAL_FILE_NOT_FOUND","local_path":"/nope"}` | 0 |
| `push_echo_unsupported` | `ack_mode:"echo"`、fake `supports_echo_paced=false` | `{"ok":false,"error_code":"ECHO_ACK_UNSUPPORTED"}` | 0 |
| `push_budget_fixed_cmds` | budget 60 | `CONSOLE_LINE_LIMIT_TOO_SMALL`＋`max_console_line_chars:60` | 0 |
| `push_budget_raw_capacity_zero` | budget = 最長空樣板長度＋3、資料非空 | 同上 | 0 |
| `push_probe_timeout` | `drop_prompt_on:"-probe"` | `{"ok":false,"error_code":"TRANSFER_TIMEOUT"}` | 1 |
| `push_probe_shell_error` | probe 輸出 `sh: syntax error` | `{"ok":false,"error_code":"TRANSFER_PROBE_FAILED"}` | 1 |
| `push_decoder_missing` | 無 base64／openssl | `{"ok":false,"error_code":"TARGET_DECODER_MISSING"}` | 1 |
| `push_chunk_timeout` | `drop_prompt_on:"-1"` | `{"ok":false,"error_code":"TRANSFER_TIMEOUT","chunks_sent":1,"chunks_total":3}` | probe＋2 chunk＋cleanup |
| `push_decoder_failed` | 第 0 段 decoder 回非零 | `{"ok":false,"error_code":"TARGET_DECODER_FAILED","chunks_sent":0,"chunks_total":3}` | probe＋1＋cleanup |
| `push_md5_unavailable` | 無 md5sum | `{"ok":false,"error_code":"CHECKSUM_VERIFY_FAILED"}` | …＋md5＋cleanup |
| `push_md5_mismatch` | `corrupt_md5` | `{"ok":false,"error_code":"CHECKSUM_MISMATCH","expected":<local>,"actual":<remote>}` | …＋cleanup |
| `push_move_timeout_no_cleanup` | `drop_prompt_on:"-mv"` | `{"ok":false,"error_code":"MOVE_TIMEOUT"}` | 最後一筆為 `-mv` |
| `push_move_failed_no_cleanup` | `mv_fails` | `{"ok":false,"error_code":"MOVE_FAILED"}` | 最後一筆為 `-mv` |
| `push_empty_file_one_chunk` | 0 bytes | `{"ok":true,"bytes":0,"chunks":1,"md5":"d41d8cd98f00b204e9800998ecf8427e","remote_path":…}` | probe＋1＋md5＋mv |
| `push_ack_none_uses_plain_send` | `ack_mode:"none"` | ok:true | `paced_cmd_ids()` 為空 |
| `push_quiet_refused_mid_transfer_no_cleanup` | `refuse_on_send_n:Some((3, FenceReject::Quiet))` | `Err(TransferAbort::Refused(FenceReject::Quiet))`（SC-33） | 3 次嘗試；之後零送出（無 cleanup） |
| `pull_probe_timeout` | `drop_prompt_on:"-probe"` | `{"ok":false,"error_code":"TRANSFER_TIMEOUT"}` | 1 |
| `pull_probe_shell_error` | `probe_shell_error` | `{"ok":false,"error_code":"TRANSFER_PROBE_FAILED"}` | 1 |
| `pull_encoder_missing` | 無工具 | `{"ok":false,"error_code":"TARGET_ENCODER_MISSING"}` | 1 |
| `pull_timeout` | `drop_prompt_on:"-b64"` | `{"ok":false,"error_code":"TRANSFER_TIMEOUT"}` | 2 |
| `pull_encoder_failed` | 檔不存在 → `||` 分支 | `{"ok":false,"error_code":"TARGET_ENCODER_FAILED"}` | 2 |
| `pull_parse_failed` | END marker 被吃掉 | `{"ok":false,"error_code":"PULL_PARSE_FAILED"}` | 2 |
| `pull_b64_non_ascii` | 內容夾 `é` | `{"ok":false,"error_code":"BASE64_DECODE_FAILED"}` | 2 |
| `pull_b64_bad_padding` | 內容 `abc` | `{"ok":false,"error_code":"BASE64_DECODE_FAILED"}` | 2 |
| `pull_b64_noise_tolerated` | 內容夾 `*`（非字母表）→ 被丟棄、md5 仍符 | ok:true | 3 |
| `pull_md5_mismatch` | `corrupt_md5` | `{"ok":false,"error_code":"CHECKSUM_MISMATCH","expected":<remote>,"actual":<local>}` | 3 |
| `pull_md5_unavailable` | 無 md5sum | `{"ok":false,"error_code":"CHECKSUM_VERIFY_FAILED"}`、不寫本地檔 | 3 |
| `pull_larger_than_rx_window_parse_failed` | `rx_max_chars:Some(131072)`、遠端檔 120 KiB（base64 輸出 > 131072 字元，BEGIN 被修剪） | `{"ok":false,"error_code":"PULL_PARSE_FAILED"}`（EVT-D-F4 KEEP 鎖定；擁有者改判 DF 時此列隨之改寫） | 2 |
| `pull_local_write_error` | `MemFiles` 對該路徑回錯 | `Err(TransferAbort::LocalIo(_))` | 3 |
| `pull_budget_pull_cmd` | budget 介於 probe 與 pull 命令長度之間 | `CONSOLE_LINE_LIMIT_TOO_SMALL` | 0 |
| `pull_windows_basename` | `PathFlavor::Windows`、remote `/a/b\\c.bin` | `local_path:"c.bin"` | 3 |
| `push_bad_prompt_regex` | `prompt_regex:"("` | `Err(TransferAbort::BadPromptRegex(_))` | 1 |

- [ ] **Step 2：執行並確認 FAIL**：`cargo test -p sw-core --test file_transfer_engine` → 全部 `not yet implemented: P8-T4`。
- [ ] **Step 3：commit 測試**：`test(transfer): 新增 push/pull 引擎流程與錯誤形狀失敗測試`
- [ ] **Step 4：實作**：照上列流程；虛擬碼（push 主迴圈）：
  ```text
  for (idx, chunk) in chunks.enumerate():
      cmd = chunk_command(b64encode(chunk), decoder, if idx==0 {">"} else {">>"}, tmp)
      if line_budget_exceeded([cmd], budget): cleanup(); return line_limit_error(budget)
      pre = port.rx_snapshot_len()
      if paced { r = port.send_command_echo_paced(cmd, source, Some(f"{prefix}-{idx}"), slice, echo_timeout)?
                 if !r.ok { port.cancel_input_line(source)?; cleanup(); return ECHO_STALL{idx,total,r.acked,r.sent} } }
      else     { port.send_command(cmd, source, Some(f"{prefix}-{idx}"))? }
      if !port.wait_for_regex_from(re, pre, timeout)? { cleanup(); return TIMEOUT{idx,total} }
      if !runtime_sentinel_seen(&port.rx_text_from(pre), CHUNK_SENTINEL, Some(&cmd)) { cleanup(); return DECODER_FAILED{idx,total} }
  ```
  `cleanup()` 內部的 `?` 同樣上拋 `TransferAbort`（中止後不再送）。
- [ ] **Step 5：執行並確認 PASS**：`cargo test -p sw-core --test file_transfer_engine`；`scripts/rust/win-test.sh --package sw-core`。
- [ ] **Step 6：commit**：`feat(transfer): 承接 push/pull 引擎、checksum 與錯誤形狀`

**Changelog fragment：** `changelog.d/rust-p8-transfer-engine.md`（`type: feat`、`scope: rust-core`）。

**BLOCKED-OWNER：** 無（真機 encoder/decoder 組合見 Task 15 的 L2 F7）。

---

## Task 5：`OpTransferPort`：echo-ACK 節流、TX 證據聚合與中止

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max

**Files:**
- Create: `crates/sw-core/src/file_transfer/op_port.rs`（`OpTransferPort`，`impl TransferPort`）
- Modify: `crates/sw-core/src/tx_writer.rs`（修訂 P5 T9：`TxJob` 加 `aggregate: Option<AggregateOp>` 欄位，既有呼叫點填 `None`；per-op 聚合緩衝；交回 writer 前 flush）
- Modify: `crates/sw-core/src/drain.rs`（`WorkerStoppedReceipt` 組裝前呼叫聚合 flush；寫入失敗列入 `unrecorded`）
- Create: `crates/sw-core/tests/support/transfer_port.rs`（測試作者交付：以 P5 `RxShared`、`spawn_tx_writer`、`Fence`、`FakeSerialBackend`／`FakeDevice`、`FakeWalFs`、`FakeClock` 組成 `PortRig`）
- Modify: `crates/sw-core/tests/support/mod.rs`（`pub mod transfer_port;`）、`crates/sw-core/Cargo.toml`（`[[test]] name="echo_paced" required-features=["test-fakes"]`）
- Test: `crates/sw-core/tests/echo_paced.rs`

**Interfaces:**
- Consumes：P5 `OpCtx{rx: Arc<RxShared>, tx: TxWriterHandle, wal, clock, …}`、`TxJob{permit, payload, reply}`／`TxReport`／`TxResult`、`TxPermit`、`Fence`／`FenceReject`、`RxShared::{snapshot_len, text_from}`；P7 `RxShared::wait_for_regex_cancellable(re, from, timeout, clock, cancel: &RunCancel) -> WaitOutcome{Matched, TimedOut, Cancelled}`、`RunCancel`；`sw_proto::pycompat::strip_ansi`。
- Produces：
  ```rust
  pub struct OpTransferPort<'a> { /* &'a OpCtx、op_id、epoch、owner_gen、&'a RunCancel、AggregateKey */ }
  impl<'a> OpTransferPort<'a> { pub fn new(ctx: &'a OpCtx, grant: &OpGrant, cancel: &'a RunCancel) -> Self; }
  impl TransferPort for OpTransferPort<'_> { /* 見下 */ }
  // tx_writer.rs 追加
  #[derive(Clone, Debug, PartialEq, Eq)] pub struct AggregateKey { pub op_id: OpId, pub source: String, pub cmd_id: Option<String> }
  pub enum AggregateOp { Append(AggregateKey), Finish(AggregateKey) }
  pub struct TxJob { pub permit: TxPermit, pub payload: Vec<u8>, pub reply: Sender<TxReport>, pub aggregate: Option<AggregateOp> }
  // Append：permit.log=false，OS 確認的 bytes 累加到該 key 的緩衝（不單獨落 WAL）
  // Finish：payload 必須為空；把緩衝以一筆 TX（source／cmd_id 取自 key）寫入 WAL 後清除，TxReport.wal_seq 回該筆 seq；緩衝為空時不寫
  impl TxWriter { fn flush_open_aggregates(&mut self) -> Vec<UnrecordedGap>; }   // Phase 2：WorkerStoppedReceipt 送出前呼叫
  ```

**語意（`uart_io.py:1049-1143`＋SC-07＋XFER-ABORT-01＋SC-33）：**
- `FenceReject` 對應：`Quiet`／`ReconfirmPending` → `Err(TransferAbort::Refused(r))`；其餘（`StaleEpoch`、`Frozen`、`Cancelled`、`Flash`、`WalUnhealthy`、`OwnerMismatch`）→ `Err(SerialNotReady)`。皆不落 WAL、不重送。
- `send_command`：UTF-8 編碼；不以 `\n` 結尾補 `\n`；`TxClass::FileTransfer`、`log=true`；`Completed(n==len)` → Ok；`Completed(n<len)`＋error 或 `Unknown` → TxWriter 已依 SC-07 只記錄確認 bytes → `Err(SerialNotReady)`。
- `send_command_echo_paced`：`body = cmd.trim_end_matches(['\r','\n'])`；`slice_size=max(1,slice)`；以**字元**切；`pre=snapshot_len()`；每段以 `aggregate=Some(key)`、`log=false` 交 TxWriter；TxWriter 在 OS 確認後把 bytes 累加到該 key 的緩衝；等 `await_echo_progress(slices[..=idx], pre, timeout)`；停滯 → `Finish(key)` → `Ok(EchoPaced{ok:false,…})`；全部確認後以 `Append(key)` 送 `b"\n"` → `Finish(key)` → `Ok(EchoPaced{ok:true,…})`。任何一段被拒／部分寫入／Unknown → 先 `Finish(key)`（緩衝非空時）再回 `Err`；不得重送該段。
- **證據順序**：聚合緩衝由 TxWriter 持有；Phase 1 中止使 OpRunner 來不及送 `Finish` 時，TxWriter 在交回 writer 之前呼叫 `flush_open_aggregates()`，把每個未結緩衝寫成一筆 TX（寫入失敗列入 receipt 的 `unrecorded`），因此 `WorkerStoppedReceipt` 必晚於這些 TX 紀錄（§5.2.4、SC-07）；shutdown 時同理先於 `wal.sync_close`。
- `await_echo_progress`：`strip_ansi(text_from(pre))` 去 `\r`、`\n` 後，以移動起點依序 `find` 每段；完成即回累計字元數；逾時回部分。等待用 `wait_for_regex_cancellable`／同一 `RunCancel` 與 `Clock`（不直接使用 `Condvar::wait_timeout`）；`Cancelled` → `Err(SerialNotReady)`。
- `cancel_input_line`：送 `b"\x15\n"`、`cmd_id=None`、`log=true`。
- `wait_for_regex_from`：委派 `wait_for_regex_cancellable(re, Some(from), timeout, clock, cancel)`：`Matched` → `Ok(true)`；`TimedOut` → `Ok(false)`；`Cancelled`（drain Phase 1 觸發）→ 立即 `Err(SerialNotReady)`（Python 會等到 timeout；Rust 收斂為 XFER-ABORT-01）。`timeout <= 0` 時與 Python `while monotonic() < deadline` 相同：**不做任何檢查**直接 `Ok(false)`（即使 RX 已含 prompt）。
- `rx_text_from` 與 `rx_snapshot_len` 委派 `RxShared::{text_from, snapshot_len}`（`inventory/09` §4.7）。

- [ ] **Step 1：寫失敗測試**（含 `support/transfer_port.rs`；`PortRig::new(FakeDut)` 回 `(rig, port)`，`rig.dut.writes()` 為外部 peer 實際收到的寫入序列，`rig.wal.tx_records(com)` 為 WAL 讀回）

```rust
// crates/sw-core/tests/echo_paced.rs
mod support;
use std::time::Duration;
use support::transfer_port::{FakeDut, PortRig};
use sw_core::file_transfer::{EchoPaced, TransferAbort, TransferPort};
use sw_core::tx_writer::FenceReject;

#[test]
fn paced_success_writes_one_aggregated_wal_record() {
    let mut rig = PortRig::new("COM0", FakeDut::echo_all());
    let cmd = "x".repeat(150);
    let r = rig.port().send_command_echo_paced(&cmd, "agent", Some("ft-1-0"), 64, Duration::from_secs(5)).unwrap();
    assert_eq!(r, EchoPaced { ok: true, acked_chars: 150, sent_chars: 150 });
    assert_eq!(rig.dut.writes(), vec![b"x".repeat(64), b"x".repeat(64), b"x".repeat(22), b"\n".to_vec()]);
    let tx = rig.wal.tx_records("COM0");
    assert_eq!(tx.len(), 1);
    assert_eq!((tx[0].source.as_str(), tx[0].cmd_id.as_deref()), ("agent", Some("ft-1-0")));
    assert_eq!(tx[0].payload, format!("{}\n", cmd).into_bytes());
}

#[test]
fn paced_stall_records_only_sent_slices_and_never_sends_newline() {
    let mut rig = PortRig::new("COM0", FakeDut::echo_first_n_chars(64));
    let r = rig.port().send_command_echo_paced(&"y".repeat(150), "agent", Some("ft-2-0"), 64, Duration::from_millis(50)).unwrap();
    assert_eq!(r, EchoPaced { ok: false, acked_chars: 64, sent_chars: 128 });
    assert!(!rig.dut.writes().iter().any(|w| w == b"\n"));
    assert_eq!(rig.wal.tx_records("COM0")[0].payload, b"y".repeat(128));
}

#[test]
fn paced_abort_after_partial_write_records_confirmed_bytes_then_errors() {
    let mut rig = PortRig::new("COM0", FakeDut::echo_all().partial_write_on(2, 10));
    let e = rig.port().send_command_echo_paced(&"z".repeat(150), "agent", Some("ft-3-0"), 64, Duration::from_secs(5)).unwrap_err();
    assert_eq!(e, TransferAbort::SerialNotReady);
    assert_eq!(rig.wal.tx_records("COM0")[0].payload.len(), 64 + 10, "只記 OS 確認的 bytes（SC-07）");
    assert_eq!(rig.dut.writes().len(), 2, "不得重送失敗的 slice");
}

#[test]
fn drain_during_echo_paced_records_confirmed_slices_before_receipt() {
    let mut rig = PortRig::new("COM0", FakeDut::echo_first_n_chars(128));
    let h = rig.spawn_paced(&"w".repeat(300), "agent", Some("ft-4-0"), 64, Duration::from_secs(30));
    rig.wait_dut_bytes(128);
    let receipt = rig.begin_drain_and_wait_receipt();        // Phase 1 → cancel → TxWriter 交回 writer
    assert_eq!(h.join().unwrap(), Err(TransferAbort::SerialNotReady));
    let tx = rig.wal.tx_records("COM0");
    assert_eq!(tx.len(), 1);
    assert_eq!(tx[0].payload, b"w".repeat(128), "已確認 slice 必須落 WAL");
    assert!(tx[0].seq < receipt.wal_seq_at_receipt, "TX 紀錄必須早於回條（§5.2.4）");
    assert!(receipt.unrecorded.is_empty());
}
```

其餘案例（每列一個 `#[test]`；「外部」欄以 `rig.dut.writes()` 斷言，不以 WAL 推論）：

| 測試名 | 設定 | 斷言 |
|---|---|---|
| `paced_strips_trailing_crlf_from_body` | cmd `"abc\r\n"` | 外部 writes `["abc","\n"]`；WAL payload `b"abc\n"` |
| `paced_tolerates_noise_between_slices` | DUT 在 echo 之間插入 `"[  1.0] kmsg\r\n"` | `ok:true` |
| `paced_ignores_ansi_in_echo` | echo 夾 `\x1b[0m` | `ok:true` |
| `paced_counts_chars_not_bytes` | cmd 為 70 個 `中` | 第一段 64 字元＝192 bytes；`sent_chars` 以字元計 |
| `paced_slice_size_zero_treated_as_one` | slice 0 | 每字元一段 |
| `send_command_appends_lf_once` | `"ls"`、`"ls\n"` | 外部 writes `b"ls\n"` 兩次，各一筆 WAL |
| `send_command_not_submitted_after_fence_freeze` | 送前 `fence.freeze()` | `Err(SerialNotReady)`；外部零寫入；WAL 無紀錄 |
| `quiet_armed_between_chunks_refused` | 第一個 `send_command` 成功後 `fence.with(|f| f.quiet_active = true)` | 第二個 → `Err(Refused(FenceReject::Quiet))`；外部 writes 只有第一筆 |
| `reconfirm_pending_blocks_cleanup` | `reconfirm_pending=true` 後 `cancel_input_line` | `Err(Refused(FenceReject::ReconfirmPending))`；外部零新增 |
| `cancel_input_line_bytes_and_wal` | — | 外部 write `b"\x15\n"`；WAL `cmd_id:null` |
| `wait_regex_full_window_when_trimmed` | RX 超過 131072 字元後以舊 offset 等 | 對全窗 search |
| `wait_regex_aborts_on_drain` | 等待中觸發 Phase 1（`RunCancel`） | 立即 `Err(SerialNotReady)`，不等 timeout |
| `wait_regex_timeout_uses_clock` | FakeClock 推進 10 s | `Ok(false)` |
| `wait_regex_timeout_zero_returns_false_even_if_matched` | RX 已含 prompt，timeout 0 與 -1 s | 皆 `Ok(false)`（Python 語意） |
| `flash_mode_blocks_file_transfer_class` | `fence.flash_mode=Flashing` | `send_command` → `Err(SerialNotReady)`；外部零寫入；WAL 無紀錄 |
| `shutdown_during_paced_push_wal_has_confirmed_slices` | paced 中送 shutdown（Phase 1＋2） | `wal.sync_close` 前 WAL 已有確認 bytes 的 TX；`unrecorded` 為空 |
| `aggregate_flush_wal_failure_listed_unrecorded` | flush 時 `fail_next_writes(wal, 1)` | receipt `unrecorded` 列出該 op 的確認 bytes 範圍 |
| `selftest_probe_between_paced_slices_never_splits_slice` | paced 進行中送 `TxClass::SelfTestProbe`（SES-16.3b KEEP：self-test 不取前景 token） | 外部 byte 序列中 probe 只出現在 slice 邊界、從不切開單一 slice；WAL 順序與外部序列一致 |
| `system_recover_key_refused_while_foreground_held` | ForegroundSlot 由 FileTransfer 持有時嘗試 `SystemRecoverKey` | 依 P5 ForegroundSlot 規則不出列；外部零寫入（no_forbidden_interleave） |

- [ ] **Step 2：執行並確認 FAIL**：`cargo test -p sw-core --features test-fakes --test echo_paced` → `not yet implemented: P8-T5`。
- [ ] **Step 3：commit 測試**：`test(uart): 新增 echo-ACK 節流與傳輸 TX 證據失敗測試`
- [ ] **Step 4：實作**：`OpTransferPort` 以 `OpCtx.tx` 送 `TxJob`、以 `RxShared` 讀 RX；TxWriter 的聚合緩衝以 `HashMap<AggregateKey, Vec<u8>>` 實作，上限為單一命令長度（受 `CMD_REJECT_BYTES`／line budget 約束，屬有界），op 結束未 finish 者由 `flush_open_aggregates` 收尾。`ForegroundSlot` 保證期間沒有其他 `Command`／`FileTransfer` TX；Human 已 suspend。
- [ ] **Step 5：執行並確認 PASS**：`cargo test -p sw-core --features test-fakes --test echo_paced`；`scripts/rust/win-test.sh --package sw-core`。
- [ ] **Step 6：commit**：`feat(uart): 承接 echo-ACK 節流、取消半行與傳輸 TX 證據`

**Changelog fragment：** `changelog.d/rust-p8-echo-paced.md`（`type: feat`、`scope: rust-core`）。

**擁有者 host 可執行 gate（OD-15，原 BLOCKED-OWNER）：** Windows 原生 COM 上的 echo-paced 部分寫入／取消（`windows_com`，OD-08）：`SW_TEST_COM_PAIR=COM20:COM21`，orchestrator 經 WSL interop 代跑。

---

## Task 6：`file.push`／`file.pull` 的 session 整合與 RPC 分派

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max

**Files:**
- Modify: `crates/sw-core/src/session_manager.rs`（`file_transfer` 路由）
- Modify: `crates/sw-core/src/session_core.rs`（`SessionRequest::FileTransfer` 處理：manager 層 gate、lease、ForegroundSlot、`Effect::StartOp{spec: OpSpec::FileTransfer}`、`OpOutcome::FileTransfer` 收斂）、`crates/sw-core/src/op_runner.rs`（`OpSpec::FileTransfer`、`OpOutcome::FileTransfer` 變體與執行體）
- Modify: `crates/sw-daemon/src/service.rs`（`"file.push"`／`"file.pull"` 分支）
- Create: `crates/sw-core/tests/support/file_transfer_session.rs`（測試作者交付：以 P5 `CoreHarness`／`ActorRig`＋Task 4 `FakeShell` 組裝）
- Modify: `crates/sw-core/tests/support/mod.rs`（`pub mod file_transfer_session;`）
- Test: `crates/sw-core/tests/file_transfer_session.rs`、`tests/contract_blackbox/test_s24_file_transfer.py`、`tests/contract_blackbox/test_s24_no_retry.py`

**Interfaces:**
- Consumes：Task 4、5；Task 0 fixtures（`sw_env`、`shell_target`、`ANY`、`drop_version`）；P3 `bb_stubd.StubDaemon`；P5 `ForegroundSlot`、`SessionManager::route(selector)`、`OpRunner::run(grant, spec)`、`SessionRequest`／`ControlEvent::OpFinished`；P7 `submit_gate_reject(public_session, source)`（RPC 層，`_resolve_session_id` 的 gate 段）與 `execute_gate_reject(q, com, now)`（manager 層，`_agent_gate_reject_locked`＋SC-03）。
- Produces：
  ```rust
  // sw-core
  pub struct FileTransferRequest { pub kind: FileTransferKind, pub selector: String, pub source: String,
      pub local_path: Option<String>, pub remote_path: String, pub chunk_size: i128,
      pub chunk_timeout_s: Option<f64>, pub ack_mode: String }
  pub enum FileTransferKind { Push, Pull }
  // session_core.rs：SessionRequest 追加 FileTransfer(FileTransferRequest)
  // op_runner.rs：OpSpec 追加 FileTransfer(FileTransferPlan)；OpOutcome 追加 FileTransfer(Result<PyDict, TransferAbort>)
  impl SessionManager {
      pub fn file_transfer(&self, req: FileTransferRequest) -> Result<PyDict, PyErr>;
  }
  // sw-daemon service.rs 分支（平面 match）
  "file.push" => self.file_push(p),   // fn file_push(&self, p: &PyDict) -> Result<PyDict, PyErr>
  "file.pull" => self.file_pull(p),
  ```
  `TransferAbort::{SerialNotReady, LocalIo, BadPromptRegex}` 在 service 層轉為 `PyErr`（framing `EXCEPTION`，無 `version`）；`TransferAbort::Refused(Quiet|ReconfirmPending)` 在 actor 收斂時轉為 `{"ok":false,"error_code":"AUTOBOOT_QUIET","selector":<com>,"retry_after_s":<quiet 剩餘秒數 round 1 位；只剩 pending 時 5.0>}`（與 SC-03 execute-time 形狀相同，SC-33）。

**語意：**
- RPC 層（`service.py:1069-1131`）：`selector`／`local_path`／`remote_path` 用 `str(x or "")`；`chunk_size = max(1, int(params.get("chunk_size") or 512))`；`chunk_timeout_s = float(raw) if raw is not None else None`；轉型失敗 → `{"ok":false,"error_code":"INVALID_ARGS"}`；`ack_mode=str(x or "auto")` 不在白名單 → `INVALID_ARGS`；`source=str(x or "agent")`；必填缺 → `INVALID_ARGS`；再走 `_resolve_session_id` 等價順序（P1 凍結優先序，P1-OC 補 SC-35）：查無 → `SESSION_NOT_FOUND`；`platform=passthrough`（任何 state，含 SC-35 安靜轉入的 READY）→ `{"ok":false,"error_code":"PROFILE_NOT_COMMAND_CAPABLE","hint":…,"session":…}`（passthrough 沒有 shell 傳輸契約，零 TX；位置照 P1-OC Python 實作凍結）；`RECOVERING` → `{"ok":false,"error_code":"SESSION_RECOVERING","session":…}`；`FLASHING` → `{"ok":false,"error_code":"FLASHING_BUSY","session":…}`（SC-02，三個方法共用）；其餘非 READY → `PROFILE_NOT_COMMAND_CAPABLE`／`SESSION_NOT_READY`；READY → `submit_gate_reject`（DF-02：`human:` 開頭一律以 agent 對待）。`file.pull` 的 `local_path` 只有 truthy 時 `str()`，否則 None；`ack_mode` 只驗不傳。
- Manager 層（`session_manager.py:4843-5011`）在 actor 內原子執行：session 不在／無 bridge／非 READY → `{"ok":false,"error_code":"SESSION_NOT_READY"}`（無其他欄位）；`execute_gate_reject` → `READY_UNCONFIRMED`（`selector`、`hint`、`recommended_action:"self_test"`）或 `AUTOBOOT_QUIET`（`selector`、`retry_after_s`，SC-03）；`recovering` → `SESSION_RECOVERING`；lease：agent 對 human lease → suspend human、結束 resume；其他 lease → `SESSION_INTERACTIVE_BUSY`；前景槽已占 → `{"ok":false,"error_code":"SESSION_BUSY","selector":<com>}`（不排隊，EVT-D-F6）。
- 傳輸中途（SC-33）：Quiet／Reconfirm 在傳輸中被 arm → 下一個尚未開始的寫入被 fence 拒絕 → 回 `AUTOBOOT_QUIET` 形狀；不送 cleanup；已送 chunk 的遠端暫存檔保留（不重送、不補救）。Python 基線在此情境繼續傳完（A/B 已登記差異，比較器對 SC-33 案例只跑 Rust 側）。
- timeout：push chunk `chunk_timeout_s` 或 `max(profile.timeout_s, 5.0)`；push echo `max(profile.timeout_s, 5.0)`；pull `chunk_timeout_s` 或 `max(profile.timeout_s, 30.0)`；`chunk_timeout_s=0`（或負值）照收，每段不檢查即逾時（EVT-D-F1、Task 5 `wait_regex_timeout_zero_returns_false_even_if_matched`）。`prompt_regex=profile.prompt_regex`、`max_console_line_chars=profile.max_console_line_chars`。
- finally：resume human（若曾 suspend）→ 釋放 ForegroundSlot（重放 deferred human bytes；`input_epoch` 已因 release／recovery／quiet 遞增者依 BC-02 丟棄，P6 D-02）。
- 中止（XFER-ABORT-01）：OpRunner 收到 `TransferAbort::SerialNotReady` → RPC 回 framing `EXCEPTION`；session 不另行重送。
- reply 等待上限 600 s（arch §6.3）；shutdown 時不得以此上限阻塞停止序（Task 12）。

- [ ] **Step 1：寫失敗測試**

```python
# tests/contract_blackbox/test_s24_file_transfer.py
from __future__ import annotations
import hashlib, os, time
import pytest
from .p8_fixtures import ANY, drop_version   # fixtures sw_env／shell_target 由 conftest 載入（Task 0）

pytestmark = [pytest.mark.posix_pty, pytest.mark.process_rpc]

def test_push_then_pull_roundtrip_md5(sw_env, shell_target, tmp_path):
    sw_env.start(targets={"COM0": shell_target})
    sw_env.wait_state("COM0", "READY", timeout=20)
    src = tmp_path / "blob.bin"
    data = bytes(range(256)) * 20
    src.write_bytes(data)
    remote = str(shell_target.root / "dst.bin")
    r = sw_env.cli("--timeout", "120", "file", "push", "--selector", "COM0",
                   "--local", str(src), "--remote", remote)
    assert r.exit == 0, r.stderr
    assert r.json == {"ok": True, "bytes": len(data), "chunks": 10,
                      "md5": hashlib.md5(data).hexdigest(), "remote_path": remote, "version": ANY}
    assert (shell_target.root / "dst.bin").read_bytes() == data          # 外部 oracle：DUT 端檔案
    out = tmp_path / "back.bin"
    r = sw_env.cli("--timeout", "120", "file", "pull", "--selector", "COM0",
                   "--remote", remote, "--local", str(out))
    assert r.exit == 0 and r.json["md5"] == hashlib.md5(data).hexdigest()
    assert out.read_bytes() == data
    chunk_lines = [c for c in shell_target.commands() if "SW_XFER_CHUNK" in c]
    assert len(chunk_lines) == 10, "DUT 端收到的 chunk 命令恰為 10 行（不重送）"

def test_release_during_push_aborts_without_resend(sw_env, shell_target, tmp_path):
    sw_env.start(targets={"COM0": shell_target})
    sw_env.wait_state("COM0", "READY", timeout=20)
    src = tmp_path / "big.bin"; src.write_bytes(os.urandom(64 * 1024))
    push = sw_env.rpc_async("file.push", {"selector": "COM0", "local_path": str(src),
                                          "remote_path": str(shell_target.root / "x"), "chunk_size": 512})
    sw_env.wait_until(lambda: sw_env.session("COM0")["foreground_busy"] is True, timeout=10)
    rel = sw_env.rpc("device.release", {"selector": "COM0", "source": "agent:p8", "reason": "p8"})
    assert rel["ok"] is True and rel["aborted_cmd"] is True and rel["session"]["state"] == "RELEASED"
    res = push.result(timeout=30)
    assert res.get("ok") is not True                                      # XFER-ABORT-01：任何非成功
    if sw_env.label.startswith("rust"):
        assert res["error_code"] == "EXCEPTION"
    chunk_lines = [l for l in shell_target.commands() if "SW_XFER_CHUNK" in l]
    assert len(chunk_lines) == len(set(chunk_lines)), "外部 oracle：中止前後同一 chunk 命令不得出現兩次"
    n_after = len(shell_target.rx_log())
    time.sleep(1.0)                                   # 觀察窗（證明無新增），不作同步點
    assert len(shell_target.rx_log()) == n_after, "RELEASED 後 DUT 端零新增 bytes"
```

Session gate 族（`s24-xfer-session-gates`，`file_transfer_session.rs` 以 `support::file_transfer_session` 執行；黑箱再以 `test_s24_file_transfer.py::test_gate_*` 對 artifact 跑一次）。**每一列都另斷言外部零 TX**：Rust 側 `FakeShell.send_attempts()==0`（除註明者），黑箱側呼叫前後 `shell_target.rx_log()` 長度不變（§11.3 `zero_tx_on_reject`）：

| 測試名 | 前置 | 呼叫 | 期望（去掉 `version`） |
|---|---|---|---|
| `rpc_missing_fields` | — | `file.push {}` | `{"ok":false,"error_code":"INVALID_ARGS"}` |
| `rpc_chunk_size_non_numeric` | — | `chunk_size:"abc"` | `INVALID_ARGS` |
| `rpc_chunk_size_coercions` | READY | `chunk_size` 為 `0`／`-5`／`true`／`"7"` | 引擎收到 512／1／1／7（此列有 TX） |
| `rpc_chunk_timeout_bad` | — | `chunk_timeout_s:"x"` | `INVALID_ARGS` |
| `rpc_ack_mode_bad_pull` | — | `file.pull ack_mode:"ehco"` | `{"ok":false,"error_code":"INVALID_ARGS"}`（無 `ack_mode` 欄） |
| `resolve_not_found` | — | selector `COM9` | 與 `session.get_state` 相同的 `SESSION_NOT_FOUND` 形狀 |
| `resolve_passthrough_attached` | passthrough ATTACHED | push／pull | `PROFILE_NOT_COMMAND_CAPABLE`＋`hint`＋`session`（SC-35：passthrough 任何 state 皆同；零 TX） |
| `resolve_passthrough_ready`（新增，SC-35） | passthrough READY（others-template 與 uboot-template 各一，經 30 s 安靜轉入） | push／pull | `PROFILE_NOT_COMMAND_CAPABLE`＋`hint`＋`session`；不進 manager 層、不占前景槽、DUT 端零 byte、WAL 無 TX 行；同一 session 的 `command.submit` 仍可進 Arbiter（對照） |
| `resolve_detached` | DETACHED | push | `SESSION_NOT_READY`＋`session` |
| `resolve_recovering` | RECOVERING | push／pull | `{"ok":false,"error_code":"SESSION_RECOVERING","session":…}`（SC-02） |
| `resolve_flashing` | FLASHING | push／pull | `{"ok":false,"error_code":"FLASHING_BUSY","session":…}`（SC-02，與 `command.submit` 共用 `_resolve_session_id`） |
| `resolve_quiet` | READY＋quiet 12.3 s | push | `AUTOBOOT_QUIET`＋`retry_after_s:12.3`＋`hint`＋`session` |
| `resolve_reconfirm_pending` | quiet 結束、pending | push | `AUTOBOOT_QUIET`＋`retry_after_s:5.0` |
| `resolve_reconfirm_failed` | failed | push | `READY_UNCONFIRMED`＋`hint`＋`recommended_action:"self_test"`＋`session` |
| `human_source_treated_as_agent` | quiet | `source:"human:x"` | 同 `resolve_quiet`（DF-02） |
| `manager_gate_race_quiet` | resolve 後、actor 前 arm quiet | push | `{"ok":false,"error_code":"AUTOBOOT_QUIET","selector":"COM0","retry_after_s":…}`（SC-03） |
| `manager_recovering` | READY＋`recovering` | push | `{"ok":false,"error_code":"SESSION_RECOVERING"}` |
| `manager_agent_lease_busy` | agent lease | push | `{"ok":false,"error_code":"SESSION_INTERACTIVE_BUSY"}` |
| `manager_human_lease_suspended` | human raw lease、push 期間按鍵 | push | ok:true；按鍵在 push 完成後才送出（#53 回放）；外部 byte 序列中按鍵不夾在任何 chunk 行內（no_forbidden_interleave） |
| `manager_foreground_busy` | 前景命令執行中 | push | `{"ok":false,"error_code":"SESSION_BUSY","selector":"COM0"}` |
| `quiet_armed_mid_push_refuses_next_chunk`（SC-33，`rust_only`） | push 送出 2 個 chunk 後注入 boot banner arm quiet | push | `{"ok":false,"error_code":"AUTOBOOT_QUIET","selector":"COM0","retry_after_s":…}`；之後 DUT 端零新增（無 cleanup）；黑箱以 shelltarget 注入 banner |
| `reconfirm_pending_mid_pull_refused`（SC-33，`rust_only`） | pull 的 encoder probe 後 pending | pull | `AUTOBOOT_QUIET`＋`retry_after_s:5.0` |
| `release_during_push_discards_deferred_human_bytes` | human raw lease、push 期間按鍵進 deferred、之後 `device.release` | push＋release | 按鍵永不出現在 DUT 端（release 後也不回放，BC-02 `input_epoch`） |
| `timeout_derivation_push` | `profile.timeout_s=2.0` | push 無 `chunk_timeout_s` | 引擎 `timeout=5.0`、`echo_timeout=5.0`（有 TX） |
| `timeout_derivation_pull` | `profile.timeout_s=40.0` | pull | `timeout=40.0`（有 TX） |
| `timeout_zero_accepted` | RX 已含 prompt | `chunk_timeout_s:0`、`-1` | 引擎 `timeout=0.0`／負值；probe 即回 `TRANSFER_TIMEOUT`（有 1 筆 TX） |
| `pull_local_default_basename` | daemon cwd＝tmp | pull 無 `local_path` | 檔案寫在 daemon cwd 的 basename（有 TX） |
| `pull_write_error_exception` | `local_path` 指向不存在目錄 | pull | framing `{"id":…,"ok":false,"error_code":"EXCEPTION","message":…}`（無 version；有 TX） |
| `line_limit_from_profile` | `max_console_line_chars:200` | push | `CONSOLE_LINE_LIMIT_TOO_SMALL`＋`max_console_line_chars:200` |

黑箱另含：`test_echo_stall_blackbox`（shelltarget `echo_stall_after=100`）→ `TRANSFER_ECHO_STALL`、DUT 端 `rx_log().count(b"\x15\n")==1`；`test_decoder_missing_blackbox`（`hide_tools=("base64","openssl")`）→ `TARGET_DECODER_MISSING`；`test_line_limit_blackbox`（`line_limit=505`，模擬 prpl）→ 以 profile `max_console_line_chars:505` 成功傳完；§15.7.1 傳輸情境：`test_half_prompt_then_silence`（`half_prompt`）→ `TRANSFER_TIMEOUT`、無重送；`test_dut_silent_mid_push`（`silent_after`）→ `TRANSFER_TIMEOUT`、chunk 行不重複；`test_slow_reader_push`（`slow_reader_bps=2400`）→ 成功或 `TRANSFER_TIMEOUT`，兩者皆不重送、DUT 端 bytes 與 WAL 已確認 bytes 一致；舊事件延遲不適用於 file 傳輸（無非同步完成事件），於 `cases.yaml` 該案例 `contract` 註明。

`test_s24_no_retry.py`（`s24-xfer-no-retry-side-effect`，`process_rpc`；兩平台）：以 P3 `StubDaemon`，腳本 `{"methods": {m: [{"close": true}]}}` 對 `file.push`、`file.pull`、`device.release`、`device.attach` 各執行 `serialwrap --retries 3 <對應子命令>` → stub `requests()` 恰 1 筆、CLI exit 非 0（§4.2：副作用方法絕不重試）；對照組 `mcu status` 同腳本 → 4 筆。

- [ ] **Step 2：執行並確認 FAIL**：
  - `cargo test -p sw-core --features test-fakes --test file_transfer_session` → FAIL。
  - `python3 -m pytest -q tests/contract_blackbox/test_s24_file_transfer.py tests/contract_blackbox/test_s24_no_retry.py` → Python 基線 PASS（`rust_only` 兩列 SKIP 並已於 `cases.yaml` 登記；驗證測試本身正確）。
  - 同檔對 Rust artifact（§0.3）→ FAIL。
- [ ] **Step 3：commit 測試**：`test(transfer): 新增 file push/pull session gate、中止與不重試黑箱測試`
- [ ] **Step 4：實作**：service 分支逐行照 Python；manager 路由經 `SessionManager::route` 送 `ControlEvent::Request{req: SessionRequest::FileTransfer(req), reply}` 給 actor；actor 在同一次事件處理中完成 manager 層檢查並取得 `ForegroundSlot`，以 `Effect::StartOp{grant, spec: OpSpec::FileTransfer(plan)}` 交 OpRunner；執行體為 `OpTransferPort::new(&ctx, &grant, &cancel)`＋`StdLocalFiles`＋`SysEnvIds`；完成後 `ControlEvent::OpFinished{outcome: OpOutcome::FileTransfer(..)}` 回 actor 做 resume、回放與 reply。`SysEnvIds` 以 `OpCtx.rng` 產生 uuid4 hex。
- [ ] **Step 5：執行並確認 PASS**：上述三組命令全部 PASS（Rust artifact 側亦 PASS）。
- [ ] **Step 6：commit**：`feat(transfer): 承接 file push/pull 的 session gate、前景槽與 RPC 分派`

**Changelog fragment：** `changelog.d/rust-p8-file-transfer-rpc.md`（`type: feat`、`scope: rust-daemon`）：含 XFER-ABORT-01 與 SC-33 的說明。

**BLOCKED-OWNER：** L2 F7（`f7-binary-roundtrip-md5`、`f7-larger-file-not-truncated` 1 MB；後者 pull 端受 EVT-D-F4 影響必為 SKIP，待擁有者就 §0.4 EVT-D-F4 裁決）與 realhw `p1-cmd-file` 對 Rust bundle 實機執行（交 bench 前先以 `scripts/docker/run-l15.sh` 跑 `L1.5-docker`，不計 L2／L3）。Windows 原生 COM 上的 push/pull（`windows_com`）OD-15 起為擁有者 host 可執行 gate（`COM20:COM21`）。

---

## Task 7：PAL flash 端點能力

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max

**Files:**
- Modify: `crates/sw-pal/src/traits.rs`
- Modify: `crates/sw-pal/src/console_pty.rs`、`crates/sw-pal/src/console_pty/posix.rs`（P6 已建立；擴充 flash 能力並修正 `create_pty` 清除 speed 位元的問題）、`crates/sw-pal/src/console_pty/windows.rs`（Unsupported）
- Modify: `docs/rust-refactor/plans/rust-architecture.md`（§4.2：`SerialHandle::line_control()` 與 `SerialLineControl`，取代 split 後無法呼叫的 `SerialHandle::{set_baud, mirror_termios_from}`；§4.7：`ConsoleIo::clone_writer`／`ConsoleWriteHalf`、`unlink`、`TermSpeed`、`create_pty` 保留 speed 的規定）
- Modify: `crates/sw-pal/src/serial.rs`、`crates/sw-pal/src/serial/posix.rs`、`crates/sw-pal/src/serial/windows.rs`
- Modify: `crates/sw-pal/src/fakes/console.rs`、`crates/sw-pal/src/fakes/serial.rs`
- Test: `crates/sw-pal/tests/flash_pty.rs`

**Interfaces:**
- Consumes：P6 `ConsoleTransportFactory::create_pty() -> PalResult<PtyPair>`、`PtyPair{master, slave_keepalive, slave_path, slave_ref}`、`UartExclusive::probe_holders(real_path) -> HolderReport`；arch §4.2 `SerialHandle::{set_baud, mirror_termios_from}`（本 Task 把兩者移到 `SerialLineControl`，`SerialHandle` 上保留為委派以免破壞 P5 呼叫點）。
- Produces（`traits.rs` 追加，對應 arch §4.7 末段）：
  ```rust
  pub struct TermSpeed { pub ispeed: u32, pub ospeed: u32 }   // termios speed_t 原值
  pub trait ConsoleTransportFactory: Send + Sync {
      // …P6 既有…
      /// makedirs(dirname(link)) → 移除既有 link（NotFound 忽略）→ symlink(slave_path, link)
      fn link(&self, slave_path: &str, link_path: &Path) -> PalResult<()>;
      /// os.remove(link)，錯誤忽略
      fn unlink(&self, link_path: &Path);
      /// CPython 3.12 tty.setraw（tty.cfmakeraw＋TCSAFLUSH）等價；不改 speed
      fn tty_setraw(&self, slave: &PtySlaveRef) -> PalResult<()>;
      fn tcgetattr_speed(&self, slave: &PtySlaveRef) -> PalResult<TermSpeed>;
  }
  pub trait ConsoleWriteHalf: Send { fn try_write(&mut self, data: &[u8]) -> PalResult<usize>; } // EAGAIN → Ok(0)
  pub trait ConsoleIo: Send {
      // …P6 既有…
      fn clone_writer(&self) -> PalResult<Box<dyn ConsoleWriteHalf>>;   // POSIX dup(master)；TCP try_clone
      /// 只等可讀、不讀取（等價 select）；Err 只代表等待本身失敗（Task 9 用以區分「select 失敗→結束」與「read 失敗→first 為空」）
      fn wait_readable(&mut self, timeout: Option<Duration>) -> PalResult<Readiness>;   // Readiness::{Ready, TimedOut, Woken}
  }
  pub trait SerialLineControl: Send + Sync {
      fn set_baud(&self, baud: u32) -> PalResult<()>;       // 未列於 POSIX_BAUD_MAP → Ok(()) no-op
      /// POSIX：tcgetattr(slave) 的 ispeed/ospeed 寫入 serial fd（TCSANOW）；讀到 B0（任一方向）視為「未設」，
      ///        改走 fallback；tcgetattr 失敗且 fallback 有值 → set_baud(fallback)，再失敗忽略。絕不把 B0 寫到實體 UART（B0 會拉低 DTR、可能重置 MCU）。
      /// Windows：依 arch §4.2 回 Err(Unsupported)，呼叫端只 set_baud（Windows `flash_endpoint=false`，實際不會被呼叫）
      fn mirror_termios_from(&self, pty: &PtySlaveRef, fallback_baud: Option<u32>) -> PalResult<()>;
  }
  pub trait SerialHandle: Send {
      // …P5 既有…
      fn line_control(&self) -> Arc<dyn SerialLineControl>;   // split 前取得，IoWorker 持有
  }
  ```
  Windows 的 `link`／`tty_setraw`／`tcgetattr_speed`／`mirror_termios_from` 回 `PalErrorKind::Unsupported`；`Capabilities.flash_endpoint` POSIX＝true、Windows＝false（Windows flash 走 #54 device release，無端點，故不實作也不測 Windows 鏡射）。
  `create_pty` 修正（P6 posix.rs）：Python flash 端點為 `pty.openpty()`＋`tty.setraw`，speed 維持 kernel 預設；P6 現行以 `c_cflag=CREAD|CLOCAL|CS8` 直接賦值，在 Linux 會清掉 `CBAUD`／`CIBAUD` 使 speed 變 B0（Python 以 list 形式的 `tcsetattr` 會另外寫回 ispeed／ospeed，不受影響）。改為 `c_cflag = (orig & (CBAUD|CIBAUD)) | CREAD|CLOCAL|CS8`（或設定後以原 speed 呼叫 `cfsetispeed`／`cfsetospeed`），console 與 flash 兩條路徑同受益；P6 `console_conformance` 測試須重跑。

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-pal/tests/flash_pty.rs
use std::time::Duration;
use sw_pal::{native, traits::*};

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__link_setraw_is_byte_transparent() {
    let p = native();
    assert!(p.caps.flash_endpoint);
    let dir = tempfile::tempdir().unwrap();
    let link = dir.path().join("dev").join("ttyMCU");
    let mut pair = p.console.create_pty().unwrap();
    p.console.tty_setraw(&pair.slave_ref).unwrap();
    p.console.link(&pair.slave_path, &link).unwrap();
    assert_eq!(std::fs::read_link(&link).unwrap().to_str().unwrap(), pair.slave_path);
    // 外部 flasher 開 link 寫入 0x00..=0xFF，master 收到相同 bytes（無 CR/LF 轉換）
    let flasher = std::fs::OpenOptions::new().read(true).write(true).open(&link).unwrap();
    let all: Vec<u8> = (0u8..=255).collect();
    std::io::Write::write_all(&mut &flasher, &all).unwrap();
    let mut got = Vec::new(); let mut buf = [0u8; 4096];
    while got.len() < 256 {
        match pair.master.read(&mut buf, Some(Duration::from_secs(2))).unwrap() {
            ReadOutcome::Data(n) => got.extend_from_slice(&buf[..n]),
            other => panic!("unexpected {other:?}"),
        }
    }
    assert_eq!(got, all);
    // master 寫分身：另一執行緒寫入不需借用 master
    let mut w = pair.master.clone_writer().unwrap();
    assert_eq!(w.try_write(b"\x00\xcc").unwrap(), 2);
    p.console.unlink(&link);
    assert!(!link.exists());
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__holder_probe_sees_flasher_then_none() {
    let p = native();
    let pair = p.console.create_pty().unwrap();
    assert!(p.uart_excl.probe_holders(&pair.slave_path).pids.is_empty(), "自身 pid 必須排除");
    let tty = std::fs::OpenOptions::new().read(true).write(true).open(&pair.slave_path).unwrap();
    let mut child = std::process::Command::new("sleep").arg("30").stdin(tty).spawn().unwrap();   // 單一行程持有
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(p.uart_excl.probe_holders(&pair.slave_path).pids, vec![child.id()]);
    child.kill().unwrap(); child.wait().unwrap();
    assert!(p.uart_excl.probe_holders(&pair.slave_path).pids.is_empty());
}
```

其餘案例：

| 測試名 | 資源 | 斷言 |
|---|---|---|
| `cap_posix_pty__new_slave_speed_not_b0` | posix_pty | `create_pty`＋`tty_setraw` 後 `tcgetattr_speed` 的 ispeed／ospeed 皆不為 `B0`，且等於同機 `os.openpty()` 的 kernel 預設（測試在執行期讀取比對，不硬編碼） |
| `cap_posix_pty__tcgetattr_speed_follows_flasher_cfsetspeed` | posix_pty | 外部 `stty -F <slave> 115200` 後 `tcgetattr_speed` 為 `B115200` 原值；未設時為 kernel 預設（同上，不為 B0） |
| `cap_posix_pty__mirror_ignores_b0_uses_fallback` | posix_pty | 外部把 slave 設為 B0 → `mirror_termios_from(_, Some(115200))` 後「serial」speed 為 B115200；fallback None → speed 不變（不寫 B0） |
| `cap_posix_pty__setraw_matches_cpython_cfmakeraw` | posix_pty | `tty_setraw` 後 iflag／oflag／cflag／lflag／cc 與同一 slave 以 Python 3.12 `tty.setraw` 處理的結果逐欄相等（以 golden 檔記錄 Python 端旗標） |
| `cap_posix_pty__mirror_termios_copies_speed_to_serial` | posix_pty | 以第二組 PTY 當「serial」，`mirror_termios_from` 後其 speed 等於 slave speed |
| `cap_posix_pty__mirror_fallback_on_error` | posix_pty | slave_ref 已關閉 → 呼叫 `set_baud(115200)`；fallback None → 不呼叫 |
| `cap_posix_pty__link_replaces_existing` | posix_pty | 既有一般檔／舊 symlink 被替換 |
| `set_baud_unlisted_is_noop`（fakes） | memory_only | `set_baud(12345)` → Ok，speed 不變 |
| `windows_flash_caps_unsupported` | memory_only（以 `--target x86_64-pc-windows-gnu` 經 win-test 執行） | `caps.flash_endpoint==false`；`link`／`tty_setraw`／`tcgetattr_speed`／`mirror_termios_from` 回 `Unsupported` |

- [ ] **Step 2：執行並確認 FAIL**：`cargo test -p sw-pal --test flash_pty -- --ignored cap_posix_pty__` → FAIL；`scripts/rust/win-test.sh --package sw-pal` → `windows_flash_caps_unsupported` FAIL。
- [ ] **Step 3：commit 測試**：`test(pal): 新增 flash 端點 PTY、holder 與 baud 鏡射失敗測試`
- [ ] **Step 4：實作**：`posix.rs` 以 `libc::symlink`、`tcgetattr` 後依 CPython 3.12 `tty.cfmakeraw` 逐位設定（iflag 清 `IGNBRK|BRKINT|IGNPAR|PARMRK|INPCK|ISTRIP|INLCR|IGNCR|ICRNL|IXON|IXANY|IXOFF`；oflag 清 `OPOST`；cflag 清 `PARENB|CSIZE` 後設 `CS8`；lflag 清 `ECHO|ECHOE|ECHOK|ECHONL|ICANON|IEXTEN|ISIG|NOFLSH|TOSTOP`；`VMIN=1`、`VTIME=0`；`TCSAFLUSH`；不動 speed）、`cfgetispeed`／`cfgetospeed`、`dup` 實作；`create_pty` 保留 speed 位元（見上）；`SerialLineControl` 由 POSIX serial handle 持有 `Arc<OwnedFd>` 共享 fd，`split()` 前以 `line_control()` 取得。Windows 端 `SerialLineControl::set_baud` 以 `GetCommState`／`SetCommState` 實作，`mirror_termios_from` 回 `Unsupported`。
- [ ] **Step 5：執行並確認 PASS**：`cargo test -p sw-pal --test flash_pty -- --include-ignored`（Linux）；P6 既有 `cargo test -p sw-pal --test console_conformance -- --include-ignored` 不退步（其 `cflag` 斷言只檢查 `CREAD|CLOCAL|CS8` 位元，保留 speed 不影響）；`scripts/rust/win-test.sh --package sw-pal`。
- [ ] **Step 6：commit**：`feat(pal): 提供 /dev/ttyMCU 所需 PTY symlink、raw、speed 與 line control 能力`

**Changelog fragment：** `changelog.d/rust-p8-pal-flash.md`（`type: feat`、`scope: rust-pal`）。

**BLOCKED-OWNER：** 無（Windows 無 flash 端點，不在本 Task 範圍）。

---

## Task 8：SessionCore FLASHING 仲裁

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max

**Files:**
- Modify: `crates/sw-core/src/session_core.rs`、`crates/sw-core/src/session_manager.rs`
- Modify: `crates/sw-core/src/tx_writer.rs`（**必做**，修訂 P5 T9：`TxPermit` 加 `probe_id: Option<u64>`；`Fence::admit` 加 FlashProbe／Probe 規則，見下表；既有呼叫點填 `None`）
- Modify: `docs/rust-refactor/plans/rust-architecture.md` §5.4（fence 規則表加 `Probe(_)` 列與 `TxPermit.probe_id`）
- Create: `crates/sw-core/tests/support/flash_core.rs`（測試作者交付：以 P5 `CoreHarness`＋`ActorRig` 組裝 `kit`）
- Modify: `crates/sw-core/tests/support/mod.rs`（`pub mod flash_core;`）、`crates/sw-core/Cargo.toml`（`[[test]] name="flash_session_core" required-features=["test-fakes"]`）
- Test: `crates/sw-core/tests/flash_session_core.rs`、`crates/sw-core/tests/tx_writer.rs`（追加 fence 列）

（契約登記 SC-32 已由 Task 1 完成；本 Task 不修改 Python `sw_core`。）

**Interfaces:**
- Consumes：P5 `SessionCore::on_event(MailboxItem, now) -> Effects`、`ControlEvent`、`SessionRequest`、`FenceState.flash_mode`、`StopOp`、`ForegroundSlot`、`Effect::{OpenBridge, BeginDrain}`、`Generations{conn_epoch, owner_gen, …}`；P6 `invalidate_human_input(InvalidateReason::Flash, fx)`、`Generations.input_epoch`、`OwnershipState.flash_mode: bool`、`ConsoleInputBytes{…, input_epoch, …}`。
- Produces：
  ```rust
  pub type ProbeId = u64;
  #[derive(Debug, Clone, PartialEq, Eq)]
  pub enum FlashReject { CommandCapable, BadState(SessionState), Released, Recovering, DrainInProgress,
                         ForegroundBusy, FlashAlreadyActive, ProbeMismatch }
  #[derive(Debug, Clone, PartialEq, Eq)] pub struct ProbeGrant { pub epoch: u64, pub probe_id: ProbeId }
  pub type FlashReply<T> = crossbeam_channel::Sender<T>;          // 本 Task 自訂（P5 `Reply` 固定為 Result<PyDict, PyErr>）
  // mailbox.rs：ControlEvent 追加（P5 enum 擴充，非新 enum）
  //   FlashSyncProbeStart { probe_id: ProbeId, reply: FlashReply<Result<ProbeGrant, FlashReject>> },
  //   FlashProbeReleased  { probe_id: ProbeId },
  //   FlashSyncMatched    { probe_id: ProbeId, reply: FlashReply<Result<(), FlashReject>> },
  //   FlashHolderGone     { probe_id: ProbeId, reply: FlashReply<()> },
  impl SessionManager {
      /// 原子收集候選並標 flash-critical（session_manager.py:1892-1918）；候選＝非 command_capable 且 state ∈ {READY, ATTACHED}
      pub fn collect_flash_candidates_and_mark(&self) -> Vec<FlashCandidate>;
      pub fn unmark_flash_critical(&self, com: &str);
      pub fn flash_critical(&self, com: &str) -> bool;
  }
  ```
  `FlashCandidate` 定義於 Task 9 的 `flash_endpoint::detect`；本 Task 先建立型別（`pub struct FlashCandidate { pub com: String, pub by_id: Option<String>, pub real_path: Option<String>, pub command_capable: bool }`）。SessionCore 新增欄位 `flash_probe_id: Option<ProbeId>`。

**Fence 規則（`Fence::admit` 追加，必做）：**

| `flash_mode` | class | 條件 | 結果 |
|---|---|---|---|
| `Probe(id)` | `FlashProbe` | `permit.probe_id == Some(id)` | `Ok` |
| `Probe(id)` | `FlashProbe` | `probe_id` 為 None 或不等於 `id`（上一個 probe 的 stale permit） | `Err(Flash)` |
| `Probe(_)` | 非 `FlashProbe`（含 `Flash`） | — | `Err(Flash)` |
| `Flashing` | `Flash` | — | `Ok`（P5 既有） |
| `Flashing` | 非 `Flash`（含 `FlashProbe`） | — | `Err(Flash)` |
| `Off` | `Flash`／`FlashProbe` | — | `Err(Flash)` |

**狀態轉移（規格 §5.3 Flash 列＋SC-02、SC-14、BC-02、UIO-12.9-d、SC-32）：**

```text
FlashSyncProbeStart{id}:
  reject CommandCapable   if profile.command_capable
  reject BadState(s)      if state ∉ {READY, ATTACHED}
  reject Released         if by_id ∈ manager.released_by_ids（RELEASED 轉移中的競態）
  reject Recovering       if recovering 旗標為真
  reject DrainInProgress  if pending_stop.is_some()
  reject ForegroundBusy   if foreground_slot.is_some()                         # SC-14
  reject FlashAlreadyActive if fence.flash_mode != Off
  in fence lock: fence.flash_mode = Probe(id); flash_probe_id = Some(id); ownership.flash_mode = true   # 其他 class TX 自此 NotSubmitted
  start rx tap (buffer 4096, keep last 4096); reply Ok(ProbeGrant{epoch: active_epoch, probe_id: id})
FlashProbeReleased{id}:  if flash_probe_id == Some(id) && fence.flash_mode == Probe(id) { fence.flash_mode = Off; flash_probe_id = None; ownership.flash_mode = false; stop rx tap }   # 只解同一 probe
FlashSyncMatched{id}:
  reject ProbeMismatch if fence.flash_mode != Probe(id)
  flash_prev_state = state; state = FLASHING; fence.flash_mode = Flashing
  invalidate_human_input(InvalidateReason::Flash, fx)                           # BC-02（P6 D-02：input_epoch++；owner_gen 不變）
  reply Ok(())                                                                  # 同 handle 模式切換：不得產生 OpenBridge／BeginDrain，conn_epoch 不變
FlashHolderGone{id}:   # exit_flashing（session_manager.py:1935）
  if flash_probe_id != Some(id): reply(); return                                # 舊 probe 的遲到事件：忽略（§5.1.2「只解除同一 probe」）
  fence.flash_mode = Off; ownership.flash_mode = false; flash_probe_id = None    # 無條件清兩處 flash_mode
  if state != FLASHING: flash_prev_state = None; reply(); then 補做延後的 detach reconcile; return   # not_flashing
  state = flash_prev_state.unwrap_or(if bridge {READY} else {DETACHED}); flash_prev_state = None; reply()
  若 Probe／FLASHING 期間曾收到 DeviceDetached → 此時補做 reconcile（等價 Python 下一個 device_watcher tick）
UartIoError while Probe or FLASHING:                                            # SC-32（Rust-only）
  fence.flash_mode = Off; ownership.flash_mode = false; Phase 1 drain（state → RECOVERING）；flash_prev_state = None
  （flash_probe_id 保留到 FlashHolderGone 收尾，使 coordinator 的 finally 能正常完成；之後的 HolderGone 對狀態為 no-op）
DeviceDetached while Probe or FLASHING:                                         # SC-32：延後，等價 Python `_detach_by_id` 的 flash busy 略過（#83 RACE-2）
  記錄 pending_detach = Some(reason)，不 drain、不改狀態
FLASHING + CmdSubmit / InteractiveOpen → FLASHING_BUSY（SC-02）
FLASHING + InteractiveSend → FLASHING_BUSY；FLASHING + session.recover / clear / bind / device.release → FLASHING_BUSY
flash-critical（probe 窗口）+ recover / clear / bind / device.release → FLASHING_BUSY（manager 先檢查，actor 以 fence.flash_mode != Off 再檢查）
Probe/FLASHING 期間 ConsoleInputBytes → 丟棄（P6 `DropFlash`；不進 DeferredBuffer、不落 WAL，UIO-12.3-d）
```

- [ ] **Step 1：寫失敗測試**（含 `support/flash_core.rs`）

```rust
// crates/sw-core/tests/flash_session_core.rs
mod support;
use support::flash_core::{kit, Ev};
use sw_core::session_core::{FlashReject, ProbeGrant, SessionState};
use sw_core::mailbox::ControlEvent;
use sw_core::tx_writer::FlashMode;

#[test]
fn probe_then_matched_enters_flashing_and_invalidates_human_input() {
    let (mut core, mut now) = kit::attached_passthrough("COM0");
    kit::human_raw_with_deferred(&mut core, &mut now, "c1", b"stale-keys");
    let conn0 = kit::conn_epoch(&core);
    let ie0 = kit::input_epoch(&core);
    let og0 = kit::owner_gen(&core);
    let mut fx = Vec::new();
    let g = kit::call_fx(&mut core, &mut now, &mut fx, |r| ControlEvent::FlashSyncProbeStart { probe_id: 7, reply: r });
    assert_eq!(g, Ok(ProbeGrant { epoch: kit::epoch(&core), probe_id: 7 }));
    assert_eq!(kit::fence(&core).flash_mode, FlashMode::Probe(7));
    kit::fake_mcu_ack(&mut core, &mut now, b"\x00\xcc");                     // ACK 進 rx tap
    assert_eq!(kit::call_fx(&mut core, &mut now, &mut fx, |r| ControlEvent::FlashSyncMatched { probe_id: 7, reply: r }), Ok(()));
    assert_eq!(kit::state(&core), SessionState::Flashing);
    assert_eq!(kit::fence(&core).flash_mode, FlashMode::Flashing);
    assert_eq!(kit::deferred_len(&core, "c1"), 0, "BC-02：FlashSyncMatched 使舊 input_epoch 的 deferred 全部失效");
    assert_eq!(kit::input_epoch(&core), ie0 + 1);
    assert_eq!(kit::owner_gen(&core), og0, "owner_gen 不用於 deferred 失效（P6 D-02）");
    assert_eq!(kit::rx_tap(&core, "COM0"), b"\x00\xcc", "ACK 回放資料不丟");
    kit::call_fx(&mut core, &mut now, &mut fx, |r| ControlEvent::FlashHolderGone { probe_id: 7, reply: r });
    assert_eq!(kit::state(&core), SessionState::Attached, "還原 flash_prev_state（UIO-12.9-d）");
    assert_eq!(kit::fence(&core).flash_mode, FlashMode::Off);
    assert!(!kit::ownership_flash_mode(&core));
    // §5.2.6：同 handle 模式切換
    assert!(!fx.iter().any(|e| kit::is_open_bridge(e) || kit::is_begin_drain(e)), "Probe→Matched→Gone 不得換 handle");
    assert_eq!(kit::conn_epoch(&core), conn0);
    assert_eq!(kit::serial_close_count(&core), 0);
}

#[test]
fn probe_rejected_when_foreground_busy_sc14() {
    let (mut core, mut now) = kit::attached_passthrough("COM0");
    kit::occupy_foreground(&mut core, &mut now);
    let g = kit::call(&mut core, &mut now, |r| ControlEvent::FlashSyncProbeStart { probe_id: 1, reply: r });
    assert_eq!(g, Err(FlashReject::ForegroundBusy));
    assert_eq!(kit::fence(&core).flash_mode, FlashMode::Off);
    assert_eq!(kit::dut_writes(&core).len(), kit::dut_writes_before_probe(&core), "外部零 TX");
}

#[test]
fn unmatched_release_only_clears_same_probe() {
    let (mut core, mut now) = kit::attached_passthrough("COM0");
    kit::call(&mut core, &mut now, |r| ControlEvent::FlashSyncProbeStart { probe_id: 3, reply: r }).unwrap();
    kit::send(&mut core, &mut now, ControlEvent::FlashProbeReleased { probe_id: 99 });
    assert_eq!(kit::fence(&core).flash_mode, FlashMode::Probe(3));
    kit::send(&mut core, &mut now, ControlEvent::FlashProbeReleased { probe_id: 3 });
    assert_eq!(kit::fence(&core).flash_mode, FlashMode::Off);
}

#[test]
fn io_error_during_flashing_drains_to_recovering() {
    let (mut core, mut now) = kit::flashing("COM0", 5);
    kit::send(&mut core, &mut now, Ev::uart_io_error(&core));
    assert_eq!(kit::state(&core), SessionState::Recovering);
    assert_eq!(kit::fence(&core).flash_mode, FlashMode::Off);
    assert!(kit::fence(&core).frozen);
    kit::call(&mut core, &mut now, |r| ControlEvent::FlashHolderGone { probe_id: 5, reply: r });
    assert_eq!(kit::state(&core), SessionState::Recovering, "事後 exit flashing 對狀態為 no-op");
}

#[test]
fn device_detached_during_flashing_is_deferred() {
    let (mut core, mut now) = kit::flashing("COM0", 6);
    kit::send(&mut core, &mut now, Ev::device_detached(&core));
    assert_eq!(kit::state(&core), SessionState::Flashing, "BSL／reset 的短暫 by-id 消失不得中斷燒錄（#83 RACE-2）");
    assert_eq!(kit::fence(&core).flash_mode, FlashMode::Flashing);
    kit::call(&mut core, &mut now, |r| ControlEvent::FlashHolderGone { probe_id: 6, reply: r });
    assert_ne!(kit::state(&core), SessionState::Flashing);
    assert!(kit::reconcile_ran_after_exit(&core), "延後的 detach 在 exit flashing 後補做");
}
```

`support/flash_core.rs` 的 `kit` 以 P5 `CoreHarness`（`new`、`feed`、`rpc`、`force_state`）組裝，提供 `attached_passthrough`、`flashing`、`call`、`call_fx`、`send`、`state`、`fence`、`owner_gen`、`input_epoch`、`conn_epoch`、`deferred_len`、`epoch`、`rx_tap`、`fake_mcu_ack`、`occupy_foreground`、`human_raw_with_deferred`、`ownership_flash_mode`、`is_open_bridge`、`is_begin_drain`、`serial_close_count`、`dut_writes`、`dut_writes_before_probe`、`reconcile_ran_after_exit`、`queue_console_input_with_epoch`。

其餘族（「零 TX」皆以 `kit::dut_writes` 外部序列斷言）：

| 測試名 | 前置 | 事件 | 期望 |
|---|---|---|---|
| `probe_rejects_command_capable` | READY shell profile | ProbeStart | `Err(CommandCapable)`、零 TX |
| `probe_rejects_released` | RELEASED | ProbeStart | `Err(BadState(Released))` |
| `probe_rejects_recovering` | RECOVERING | ProbeStart | `Err(BadState(Recovering))` |
| `probe_rejects_drain_pending` | drain 進行中 | ProbeStart | `Err(DrainInProgress)` |
| `probe_rejects_second_probe` | Probe(1) | ProbeStart(2) | `Err(FlashAlreadyActive)` |
| `matched_requires_same_probe` | Probe(1) | Matched(2) | `Err(ProbeMismatch)`，狀態不變 |
| `stale_flashprobe_permit_refused` | Probe(1) 釋放後 Probe(2) | 以 `probe_id:Some(1)` 的 `FlashProbe` permit 送出 | `NotSubmitted(Flash)`、零 TX、WAL 無紀錄 |
| `stale_holder_gone_ignored_during_new_flashing` | Probe(1) 釋放、Probe(2)→Matched→FLASHING | `FlashHolderGone{probe_id:1}` | 仍 FLASHING、`flash_mode==Flashing` |
| `stale_console_input_queued_before_probe_dropped_after_matched` | human raw；Probe 前以舊 `input_epoch` 排入 `ConsoleInputBytes` 但尚未處理 | ProbeStart→Matched→HolderGone 後才處理該事件 | 該 bytes 永不出現在外部序列（BC-02） |
| `console_input_resumes_after_flash_ioerror_recovery` | FLASHING → `UartIoError` → RECOVERING → 重新 ATTACHED | human 按鍵 | 按鍵正常送出（`ownership.flash_mode` 已清，不再 `DropFlash`） |
| `flashing_cmd_submit_busy` | FLASHING | Submit | `FLASHING_BUSY`（SC-02），零 TX |
| `flashing_interactive_open_busy` | FLASHING | InteractiveOpen | `FLASHING_BUSY`，零 TX |
| `flashing_interactive_send_busy` | FLASHING＋舊 lease | InteractiveSend | `FLASHING_BUSY`，零 TX |
| `flashing_release_busy` | FLASHING | Release | `{"ok":false,"error_code":"FLASHING_BUSY","selector":"COM0","session":…}` |
| `flashing_recover_clear_bind_busy` | FLASHING | recover／clear／bind | 各回 `FLASHING_BUSY`＋`selector`＋`session` |
| `probe_window_release_busy` | Probe(1)（flash-critical） | Release | `FLASHING_BUSY` |
| `probe_window_console_bytes_dropped` | Probe(1)、human raw | ConsoleInputBytes | 零 TX、DeferredBuffer 不增加 |
| `probe_window_interactive_send_silent` | Probe(1)、救援 lease | InteractiveSend `"ab"` | `{"ok":true,"interactive_id":…,"bytes":2}`、外部零 TX（KEEP UIO-12.3-d） |
| `flashing_non_flash_tx_not_submitted` | FLASHING | `TxClass::LoginProbe` permit | `NotSubmitted` |
| `flashing_flash_tx_allowed` | FLASHING | `TxClass::Flash` | `Completed`，WAL `source:"flash"` |
| `probe_class_only_in_probe` | Flashing | `TxClass::FlashProbe` | `NotSubmitted` |
| `self_test_during_flashing_unresponsive` | FLASHING | self_test | `classification:"TARGET_UNRESPONSIVE"`，不 `confirm_ready`（RPC-H-07）、外部零 TX |
| `holder_gone_from_ready_prev` | prev=READY | HolderGone | `READY` |
| `holder_gone_without_bridge` | prev=None、bridge 已關 | HolderGone | `DETACHED` |
| `candidates_atomic_mark` | 兩個 passthrough、一個 shell | collect | 只回兩個 passthrough，二者 `flash_critical==true` |
| `candidates_include_passthrough_quiet_ready`（新增，SC-35） | 一個 others-template passthrough 已經 30 s 安靜轉 READY 且前景閒置、一個 passthrough ATTACHED、一個 passthrough READY 但有前景命令執行中 | collect | 回前兩者（`command_capable:false`，state 為 `READY`／`ATTACHED`），二者 `flash_critical==true`；有前景操作者不列入（SC-14）；SC-35 不改 `command_capable` 定義，候選資格不因 passthrough 轉 READY 而失去 |
| `flash_critical_blocks_passthrough_quiet_tick`（新增，SC-35） | passthrough ATTACHED 已安靜 29.5 s、被 collect 標 flash-critical 後再過 1 s 的 `Tick` | state 維持 `ATTACHED`（flash 臨界期間不轉 READY）；unmark 後下一個 `Tick` 才轉 READY |
| `flashing_not_persisted` | FLASHING | StateStore 快照 | `state.json` 無 FLASHING 相關欄位 |

`tx_writer.rs` 追加：上方 Fence 規則表的 6 列各一個 `#[test]`。

- [ ] **Step 2：執行並確認 FAIL**：`cargo test -p sw-core --features test-fakes --test flash_session_core --test tx_writer` → FAIL。
- [ ] **Step 3：commit 測試**：`test(flash): 新增 FLASHING 仲裁與 probe 授權失敗測試`
- [ ] **Step 4：實作**：依上列 fence 表與轉移表；manager 端 `flash_critical: HashSet<String>` 與候選快照在 manager actor 內同一次處理完成；destructive RPC 路由前先查 `flash_critical`。
- [ ] **Step 5：執行並確認 PASS**：`cargo test -p sw-core --features test-fakes --test flash_session_core --test tx_writer`；P5／P6 既有 `cargo test -p sw-core --features test-fakes` 全綠；`scripts/rust/win-test.sh --package sw-core`。
- [ ] **Step 6：commit**：`feat(flash): 承接 FLASHING 仲裁、probe 授權與 flash-critical 保護`

**Changelog fragment：** `changelog.d/rust-p8-flash-arbitration.md`（`type: feat`、`scope: rust-core`；含 SC-32 Rust-only 行為說明）。

**BLOCKED-OWNER：** 無（純狀態機；實機見 Task 9）。#155 評估：USB 重列舉期間 by-id 消失的真機情境由 `p2-1-mcu-flash-manual` 程序涵蓋（BSL 進入／reset 步驟），本 Task 以 `device_detached_during_flashing_is_deferred` 覆蓋邏輯。

---

## Task 9：FlashEndpoint 執行緒與認線協調

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max

**Files:**
- Create: `crates/sw-core/src/flash_endpoint.rs`（模組根：`FlashEndpoint`）
- Create: `crates/sw-core/src/flash_endpoint/detect.rs`、`coordinator.rs`、`pump.rs`、`holder_watch.rs`
- Modify: `crates/sw-core/src/uart_bridge.rs`（Reader thread：WAL append 之後、console fan-out 之前呼叫 `FlashCoordinator::rx_observer`；`IoWorker`／`OpenedBridge` 加 `line_control: Arc<dyn SerialLineControl>`，於 `split()` 前自 Task 7 `SerialHandle::line_control()` 取得）
- Modify: `crates/sw-core/src/session_manager.rs`（`pub fn tx_for(&self, com: &str) -> Option<TxWriterHandle>`、`pub fn line_control_for(&self, com: &str) -> Option<Arc<dyn SerialLineControl>>`、`pub fn send_control(&self, com: &str, ev: ControlEvent) -> bool`；皆取 actor 目前 `conn_epoch` 的值，epoch 失效回 None）
- Create: `crates/sw-core/tests/support/flash_rig.rs`（測試作者交付：`FlashRig`／`Mcu`，以 fakes 組裝；含 R2-F5 所需 `holder_sequence`、`run_until_state`、`coord()`、`endpoint()`、`endpoint_shutdown()`、`events_of`、`flash_mode`、`flash_critical`、`ttymcu_link_exists`、`stop_endpoint`、`stop_endpoint_on_next_run_registered`（於 `current_run` 登記後、第一個 probe 前在另一 thread 呼叫 `stop()`，回傳可 `join_within` 取 `EndpointStopReceipt` 的 handle）；原計畫放 `src/flash_endpoint/testkit.rs` 改為測試支援檔）；Modify: `crates/sw-core/tests/support/mod.rs`（P5 06 Task 11 已建；只追加 `pub mod flash_rig;`）
- Test: `crates/sw-core/tests/flash_coordinator.rs`、`tests/contract_blackbox/test_s24_flash_endpoint.py`（fixtures `fake_mcu`、`FakeFlasher` 由 Task 0 提供）

**Interfaces:**
- Consumes：Task 2 registry、Task 7 PAL（`SerialLineControl`、`ConsoleWriteHalf`、`tcgetattr_speed`）、Task 8 事件；P5 `TxWriterHandle`／`TxJob`／`TxPermit`（`TxClass::FlashProbe` 帶 `probe_id`、`TxClass::Flash`，`source:"flash"`）、`SessionManager`（本 Task 新增的 `tx_for`／`line_control_for`／`send_control`）；`Clock`；`UartExclusive`。
- Produces：
  ```rust
  // detect.rs
  #[derive(Debug, Clone, PartialEq, Eq)] pub enum DetectStatus { Matched, Ambiguous, None }
  #[derive(Debug, Clone, PartialEq, Eq)]
  pub struct DetectResult { pub status: DetectStatus, pub by_id: Option<String>, pub family: Option<String>, pub hits: Vec<String> }
  pub trait ProbeTransport { fn probe(&mut self, by_id: &str, probe_bytes: &[u8], expect: &[u8], timeout_ms: u64) -> bool; }
  // detect_mcu_line 對 by_id 為 None 的候選直接略過（等價 Python probe(None, …) 恆 False），不呼叫 ProbeTransport::probe
  pub fn detect_mcu_line(cands: &[FlashCandidate], reg: &McuPatternRegistry, t: &mut dyn ProbeTransport) -> DetectResult;
  // coordinator.rs
  #[derive(Debug, Clone, PartialEq)]
  pub struct LastDetect { pub status: DetectStatus, pub com: Option<String>, pub family: Option<String>, pub hits: Vec<(String, Option<String>)> }
  // 兩層停止旗標（R2-F5；對照 Python：flash_endpoint.py 的 self._stop 為端點停止旗標，service.py:434 每次 flash 另建局部 threading.Event）
  /// 端點停止旗標：只由 FlashEndpoint::stop／Drop（daemon 停止序）設定；端點 thread 迴圈只看它。整個 daemon 生命週期一個。
  #[derive(Clone)] pub struct EndpointShutdown { /* Arc<AtomicBool> + Arc<dyn IoWaker>（喚醒端點 thread 的 wait_readable） */ }
  impl EndpointShutdown { pub fn new(waker: Arc<dyn IoWaker>) -> Self; pub fn set(&self); pub fn is_set(&self) -> bool; }
  /// 單次 flash 的取消旗標：on_flash_open 進入時新建（每次 open 一個新值，絕不重用），由 detection probe、pump、holder watcher 共用；
  /// is_cancelled() == 本身已取消 || endpoint_shutdown.is_set()。holder watcher 結束（armed→空、或 max 逾時）只呼叫 cancel()，**不得**設 EndpointShutdown。
  #[derive(Clone)] pub struct FlashRunCancel { /* own: Arc<AtomicBool>, shutdown: EndpointShutdown, waker: Arc<dyn IoWaker> */ }
  impl FlashRunCancel { pub fn new(shutdown: &EndpointShutdown, waker: Arc<dyn IoWaker>) -> Self; pub fn cancel(&self); pub fn is_cancelled(&self) -> bool; }
  pub struct FlashCoordinator { /* mgr, registry, clock, uart_excl, endpoint_shutdown, Mutex<FlashShared>（含 current_run: Option<FlashRunCancel>） */ }
  impl FlashCoordinator {
      pub fn new(mgr: Arc<SessionManager>, registry: Arc<McuPatternRegistry>, clock: Arc<dyn Clock>,
                 uart_excl: Arc<dyn UartExclusive>, console: Arc<dyn ConsoleTransportFactory>, endpoint_shutdown: EndpointShutdown) -> Arc<Self>;
      /// 進入時於 shared 鎖內建立新 FlashRunCancel 並登記為 current_run；返回前於同鎖內清回 None（之後 FlashRunCancel 不再被任何 thread 參照）
      pub fn on_flash_open(&self, io: &mut FlashPtyIo, first: &[u8]);       // service.py:381-462 等價
      /// FlashEndpoint::stop 呼叫：同一 shared 鎖內設 endpoint_shutdown 並 wake current_run（若有）——與 on_flash_open 的登記互斥，無遺失喚醒
      pub fn request_shutdown(&self);
      #[cfg(feature = "test-fakes")] pub fn current_run_for_test(&self) -> Option<FlashRunCancel>;
      pub fn rx_observer(&self, com: &str, data: &[u8]);                   // service.py:363-379 等價，非阻塞
      pub fn active_com(&self) -> Option<String>;
      pub fn last_detect(&self) -> Option<LastDetect>;
  }
  pub struct FlashPtyIo { pub master: Box<dyn ConsoleIo>, pub slave_path: String, pub slave_ref: PtySlaveRef }
  // flash_endpoint.rs
  pub struct FlashEndpoint { /* thread "serialwrap-ttyMCU", endpoint_shutdown: EndpointShutdown, waker, Arc<AtomicBool> active */ }
  impl FlashEndpoint {
      /// caps.flash_endpoint=false → Ok(None)；否則 create_pty → tty_setraw → link → 起 thread
      pub fn start(pal: &Platform, link_path: &Path, coord: Arc<FlashCoordinator>, grace: Duration)
          -> PalResult<Option<FlashEndpoint>>;
      pub fn is_flashing(&self) -> bool;
      #[cfg(feature = "test-fakes")] pub fn thread_alive_for_test(&self) -> bool;   // 端點 thread 尚未結束
      pub fn stop(self) -> EndpointStopReceipt;   // coord.request_shutdown()（設 EndpointShutdown＋wake 本次 FlashRunCancel）→ 喚醒端點 thread → join 端點與 holder thread（各 2.0 s）→ 關 master/slave → unlink
      // 只有 stop()／Drop 會設 EndpointShutdown；單次 flash 結束（holder gone、pump NotSubmitted／Eof、max 逾時）只取消該次 FlashRunCancel，端點 thread 繼續服務下一次 open
  }
  pub struct EndpointStopReceipt { pub joined: bool, pub stalled_threads: Vec<&'static str>, pub unlinked: bool }
  // joined=false 時不得宣稱清理完成：WARNING log 列出 stalled_threads，daemon 停止序照常往下（Task 12）並把它列入 incomplete（最終 exit 1），但不關閉仍被 stalled thread 使用的 master
  impl Drop for FlashEndpoint { fn drop(&mut self); }   // 未呼叫 stop 的任何退出路徑（含 panic unwind）仍 unlink ttyMCU
  // holder_watch.rs
  // 迴圈條件 !run.is_cancelled()；結束（armed 後歸零、max 逾時、run 已取消）一律 run.cancel()；簽章不接受 EndpointShutdown
  pub fn watch_flasher_disconnect(excl: &dyn UartExclusive, slave_path: &str, run: &FlashRunCancel,
                                  clock: &dyn Clock, poll: Duration, max: Duration);
  // pump.rs
  pub fn pump_endpoint_to_sink(master: &mut dyn ConsoleIo, sink: &mut dyn FnMut(&[u8]) -> bool,
                               run: &FlashRunCancel, chunk: usize, select: Duration);  // sink 回 false（NotSubmitted）、Eof 或 run.is_cancelled() 即結束
  ```

**行為（逐字對照 `inventory/06` §9、`flash_endpoint.py`、`service.py:146-499`）：**
1. 端點 thread（對照 `flash_endpoint.py:170-195`）：先等可讀：`master.wait_readable(grace)`（等價 `select`）——`TimedOut`／`Woken` → 檢查 `endpoint_shutdown`（設定才結束 thread）後繼續，**從不寫 master**；迴圈條件只看 `EndpointShutdown`，**不**看任何 `FlashRunCancel`——`on_flash_open` 返回（不論 matched→holder gone、none、ambiguous 或 cancel）後回到等待，繼續服務下一次 flasher open（與 Python `flash_endpoint.py:171` 只看 `self._stop` 相同，R2-F5）；**等待本身失敗**（等價 `select` 的 `OSError`）→ thread 結束（WARNING log）。可讀後 `master.read(buf[4096])`：`Data(n)` → `first = buf[..n]`；**讀取錯誤或 `Eof`** → `first = b""`（等價 `os.read` 的 `OSError`／`BlockingIOError`），照常往下。若 `active` 未設：設 active → 同步呼叫 `on_flash_open(io, first)`（panic 以 `catch_unwind` 吸收並 WARNING）→ 清 active。未匹配時 `first` 已被讀掉、不回放。
2. `on_flash_open`：先於 shared 鎖內新建本次 `run = FlashRunCancel::new(&endpoint_shutdown, waker)` 並登記 `current_run=Some(run)`（之後的 detection、holder watcher、pump 只拿這個 `run`）→ `collect_flash_candidates_and_mark()` → 建 `by_id→com`（略過無 by_id 者）→ `detect_mcu_line`，`ProbeTransport::probe` 實作：
   - `run.is_cancelled()` → false（不送 `FlashSyncProbeStart`、零 TX；涵蓋 daemon 停止與本次 open 同時發生）；com 不在 map → false；送 `FlashSyncProbeStart{probe_id}`（`probe_id` 由 coordinator 單調遞增）；`Err(_)` → false。
   - 重置 `rx_buffers[com]=[]`；以 `TxClass::FlashProbe`（`permit.probe_id=Some(probe_id)`）經 `mgr.tx_for(com)` 送 `first`（`first` 為空時改送 pattern `probe_bytes`）；失敗 → `FlashProbeReleased` → false。
   - 在 `timeout_ms` 內每 20 ms（經 `Clock`，並檢查本次 `run.is_cancelled()`，含 `endpoint_shutdown`）檢查 `expect` 是否為 buffer 的子字串；命中 → `acks[by_id]=buffer 全段`、`held.insert(com, probe_id)`、true（**不**送 Released）；逾時或 cancelled → `FlashProbeReleased` → false。
   - detection 結束後若 `run.is_cancelled()`（即使已有命中）→ 視同非 matched 處理（第 3 點），不進 FLASHING、不回放 ACK。
3. 記錄 `last_detect = {status, com, family, hits:[(by_id, com)]}`；非 matched → 對所有 held 送 `FlashProbeReleased`、unmark 全部候選、return（端點保持沉默）。
4. matched：`fallback_baud = registry.get(family).baud`；`ack = acks[by_id]`；送 `FlashSyncMatched{held[com]}`；其他 held 全部 Released；unmark 全部候選；`active_com=com`、`master_writer = io.master.clone_writer()`。
5. 起 holder watch thread（`slave_path`、本次 `run`、poll 0.5 s、max 1800 s）：`pids` 非空 → armed；armed 後為空 → 結束；結束一律 `run.cancel()`（等價 Python `service.py:483` 對 `service.py:434` 局部 Event 的 `stop.set()`）——**只**取消本次 flash 的 pump，**絕不**設 `EndpointShutdown`（R2-F5：否則第一次正常燒錄結束即停掉常駐端點，第二次燒錄無法認線、回放 ACK）。
6. `mgr.line_control_for(com).mirror_termios_from(slave_ref, Some(fallback_baud))`（Task 7：B0 視為未設）；`ack` 非空 → `master_writer.try_write(ack)` 一次（錯誤與部分寫入忽略）；`pump_endpoint_to_sink`（不重送 `first`）：每筆 `TxClass::Flash` 一筆 WAL TX `source:"flash"`；`NotSubmitted` → 結束。
7. finally：`run.cancel()` → join 本次 holder watch thread（被 `run` 的 waker 喚醒，上限 2.0 s；逾時 WARNING 並由 `stop()` 列入 `stalled_threads`）→ `active_com=None`、`master_writer=None`、`rx_buffers.remove(com)`、送 `FlashHolderGone{probe_id}`；外層 finally：所有仍 held 者 Released、unmark 全部、shared 鎖內 `current_run=None`。`EndpointShutdown` 在整個 `on_flash_open` 內不被寫入。
   獨占不變式（R0-F3 延伸檢查）：實體 MCU UART 在整個 flash 期間由既有 bridge handle 持有，**不**因 flash 開始／結束而 close／reopen（不經 `CloseToken`）；`/dev/ttyMCU` 端點 slave 由 `create_pty` 建立、daemon 同持 master＋slave keepalive，daemon **絕不**對端點 slave 設 `TIOCEXCL`、也不以 `open_exclusive` 開它，故 flasher 每次關閉後可再開。外部 flasher 自行設 TIOCEXCL 後未清除而離開的情形（keepalive 使 tty 物件不釋放、下一次 flasher open 會 EBUSY）屬 Python 基線同樣存在的行為，本 Phase 不改產品行為，只以 `endpoint_reopen_after_flasher_set_excl_recorded` 記錄並於 PR 登記為 DF 候選。
8. `rx_observer(com, data)`（reader thread）：持 shared 鎖：若 `rx_buffers` 有 com → extend 並保留最後 4096；`active = active_com == com`；鎖外 `active` 時 `master_writer.try_write(data)`（錯誤忽略、不重試、不阻塞）。

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/flash_coordinator.rs
use std::time::Duration;
use sw_core::flash_endpoint::detect::{detect_mcu_line, DetectResult, DetectStatus, ProbeTransport};
mod support;
use support::flash_rig::{FlashRig, Mcu};
use sw_core::mcu_patterns::McuPatternRegistry;

struct Scripted(Vec<(&'static str, bool)>, Vec<String>);
impl ProbeTransport for Scripted {
    fn probe(&mut self, by_id: &str, _p: &[u8], _e: &[u8], _t: u64) -> bool {
        self.1.push(by_id.to_string());
        self.0.iter().find(|(b, _)| *b == by_id).map(|x| x.1).unwrap_or(false)
    }
}

#[test]
fn detect_excludes_command_capable_and_flags_ambiguity() {
    let reg = McuPatternRegistry::default_registry();
    let cands = vec![
        FlashRig::cand("COM0", Some("a"), false), FlashRig::cand("COM1", Some("b"), true),
        FlashRig::cand("COM2", Some("c"), false)];
    let mut t = Scripted(vec![("a", true), ("b", true), ("c", true)], vec![]);
    let r = detect_mcu_line(&cands, &reg, &mut t);
    assert_eq!(r, DetectResult { status: DetectStatus::Ambiguous, by_id: None, family: None, hits: vec!["a".into(), "c".into()] });
    assert_eq!(t.1, vec!["a", "c"], "command_capable 不得被 probe");
}

#[test]
fn sync_bytes_are_flashers_own_and_ack_replayed_once() {
    let mut rig = FlashRig::posix_fake();                       // FakeSerial＋fake PTY＋FakeClock
    rig.add_passthrough("COM0", "by-id/mcu", Mcu::ti_sbl());   // 收到 55 55 回 00 CC
    rig.flasher_write(b"\x55\x55");
    rig.run_until_flashing("COM0");
    assert_eq!(rig.serial_tx("COM0"), vec![b"\x55\x55".to_vec()], "只送 flasher 自己的 sync，不另注入");
    assert_eq!(rig.master_writes(), vec![b"\x00\xcc".to_vec()], "MCU 回應整段回放給 flasher");
    rig.flasher_write(b"\x03\x10\x20");
    rig.pump_once();
    assert_eq!(rig.serial_tx("COM0").last().unwrap(), b"\x03\x10\x20");
    assert!(rig.wal_tx("COM0").iter().all(|r| r.source == "flash"));
}

#[test]
fn endpoint_never_writes_when_unbridged() {
    let mut rig = FlashRig::posix_fake();
    rig.add_passthrough("COM0", "by-id/mcu", Mcu::silent());
    rig.advance_secs(30.0);
    assert!(rig.master_writes().is_empty());
    rig.flasher_write(b"\x55\x55");                       // 無 ACK → none
    rig.run_detection();
    assert!(rig.master_writes().is_empty(), "未匹配：端點保持沉默");
    assert_eq!(rig.last_detect_json(), r#"{"status":"none","com":null,"family":null,"hits":[]}"#);
    assert_eq!(rig.state("COM0"), "ATTACHED");
}

// R2-F5：單次 flash 結束（holder gone）只取消該次 FlashRunCancel，常駐端點必須繼續服務下一次燒錄
#[test]
fn two_consecutive_flashes_same_endpoint() {
    let mut rig = FlashRig::posix_fake();
    rig.add_passthrough("COM0", "by-id/mcu", Mcu::ti_sbl());
    rig.holder_sequence(&[vec![4242], vec![4242], vec![]]);           // 第一次 flash：armed 後歸零
    rig.flasher_write(b"\x55\x55");
    rig.run_until_flashing("COM0");
    let run1 = rig.coord().current_run_for_test().expect("第一次 flash 有 run");
    rig.advance_secs(1.5);                                             // holder watcher 第 3 次 poll → 結束
    rig.run_until_state("COM0", "ATTACHED");
    assert!(run1.is_cancelled(), "holder gone 取消本次 run");
    assert!(!rig.endpoint_shutdown().is_set(), "holder gone 不得設 EndpointShutdown");
    assert!(rig.endpoint().thread_alive_for_test(), "端點 thread 必須仍在服務");
    assert!(rig.coord().current_run_for_test().is_none());
    assert!(rig.ttymcu_link_exists());
    assert_eq!(rig.events_of("COM0", "FlashHolderGone").len(), 1);

    rig.holder_sequence(&[vec![4343], vec![4343], vec![]]);           // 第二次 flash（新 flasher）
    rig.flasher_write(b"\x55\x55");
    rig.run_until_flashing("COM0");
    let run2 = rig.coord().current_run_for_test().expect("第二次 flash 有新 run");
    assert!(!run2.is_cancelled(), "第二次 run 為新建值，不沿用第一次已取消的旗標");
    assert_eq!(rig.serial_tx("COM0"), vec![b"\x55\x55".to_vec(), b"\x55\x55".to_vec()], "第二次 sync 仍轉送 MCU");
    assert_eq!(rig.master_writes(), vec![b"\x00\xcc".to_vec(), b"\x00\xcc".to_vec()], "第二次 ACK 仍回放");
    let starts = rig.events_of("COM0", "FlashSyncProbeStart");
    assert_eq!(starts.len(), 2);
    assert_ne!(starts[0].probe_id, starts[1].probe_id);
    rig.flasher_write(b"\x03\x10\x20");
    rig.pump_once();
    assert_eq!(rig.serial_tx("COM0").last().unwrap(), b"\x03\x10\x20");
    rig.advance_secs(1.5);
    rig.run_until_state("COM0", "ATTACHED");
    assert_eq!(rig.events_of("COM0", "FlashHolderGone").len(), 2);
    assert!(rig.endpoint().thread_alive_for_test());
    let r = rig.stop_endpoint();
    assert_eq!((r.joined, r.unlinked), (true, true));
    assert!(r.stalled_threads.is_empty());
}

// R2-F5：daemon 停止恰在第二次 flash 開始時到達——本次 run 必須立即視為取消，零 TX、不進 FLASHING、stop 正常收斂
#[test]
fn shutdown_at_second_flash_start() {
    let mut rig = FlashRig::posix_fake();
    rig.add_passthrough("COM0", "by-id/mcu", Mcu::ti_sbl());
    rig.holder_sequence(&[vec![4242], vec![4242], vec![]]);
    rig.flasher_write(b"\x55\x55");
    rig.run_until_flashing("COM0");
    rig.advance_secs(1.5);
    rig.run_until_state("COM0", "ATTACHED");                          // 第一次 flash 正常結束
    // hook：第二次 on_flash_open 登記 current_run 之後、第一個 ProbeTransport::probe 之前呼叫 stop()（另一 thread）
    let stopper = rig.stop_endpoint_on_next_run_registered();
    rig.flasher_write(b"\x55\x55");
    let r = stopper.join_within(Duration::from_secs(2));
    assert_eq!((r.joined, r.unlinked), (true, true), "stop 在 2.0 s 內返回並收斂");
    assert!(r.stalled_threads.is_empty());
    assert!(rig.endpoint_shutdown().is_set());
    assert_eq!(rig.serial_tx("COM0"), vec![b"\x55\x55".to_vec()], "第二次 sync 零 TX");
    assert_eq!(rig.master_writes(), vec![b"\x00\xcc".to_vec()], "第二次不回放 ACK");
    assert_eq!(rig.events_of("COM0", "FlashSyncProbeStart").len(), 1, "第二次未開始 probe");
    assert_eq!(rig.events_of("COM0", "FlashSyncMatched").len(), 1);
    assert_eq!(rig.events_of("COM0", "FlashHolderGone").len(), 1);
    assert_eq!(rig.state("COM0"), "ATTACHED");
    assert_eq!(rig.flash_mode("COM0"), "Off");
    assert!(!rig.flash_critical("COM0"), "候選已 unmark");
    assert!(!rig.ttymcu_link_exists());
}
```

其餘族：

| 測試名 | 設定 | 斷言 |
|---|---|---|
| `empty_first_read_falls_back_to_pattern_probe` | 可讀後首讀錯誤（`first=b""`） | serial TX 為 pattern `55 55`；thread 未結束 |
| `wait_readable_error_ends_thread` | `wait_readable` 回錯誤 | 端點 thread 結束、WARNING、未呼叫 `on_flash_open` |
| `candidate_without_by_id_is_skipped` | 候選 `by_id=None` | 不呼叫 `ProbeTransport::probe`、不計入 hits |
| `stop_during_multi_candidate_probe` | 三候選、probe 第二個時 `stop()` | `stop()` 在 2.0 s 內返回 `joined:true`；held 全部 Released、unmark 全部候選 |
| `stop_while_holder_present` | FLASHING、holder 恆存在 | `stop()` 經 `request_shutdown` 設 `EndpointShutdown` 並 wake 本次 `FlashRunCancel`（`is_cancelled()==true`）→ 喚醒 holder watcher 與 pump，`joined:true`、`FlashHolderGone` 已送、link 已移除 |
| `stop_reports_stalled_thread` | 注入不回應 stop 的 pump sink | `joined:false`、`stalled_threads==["pump"]`、不關 master、`unlinked:true` |
| `drop_without_stop_unlinks` | `FlashEndpoint` 直接 drop | `ttyMCU` link 已移除 |
| `ack_substring_not_prefix` | MCU 回 `ff 00 cc 01` | matched；回放 `ff 00 cc 01` 整段 |
| `rx_buffer_keeps_last_4096` | probe 期間 RX 5000 bytes 後才出現 ACK | matched；回放長度 4096 |
| `probe_timeout_uses_pattern_timeout_ms` | ACK 在 520 ms 後 | none；`FlashProbeReleased` 已送 |
| `held_gate_kept_during_ambiguity_scan` | 兩候選、第一個命中 | 掃第二個期間第一個 `flash_mode==Probe(_)` |
| `matched_releases_other_held` | 三候選、只一個命中 | 另兩個 `flash_mode==Off` |
| `flash_critical_unmarked_after_matched` | matched | 全部候選 `flash_critical==false` |
| `holder_watch_armed_then_gone` | holder 序列 `[pid],[pid],[]` | 第 3 次 poll 後本次 `run.is_cancelled()==true`；`endpoint_shutdown.is_set()==false`、`thread_alive_for_test()==true`；`FlashHolderGone`；state ATTACHED（R2-F5） |
| `holder_watch_never_armed_hits_max` | holder 恆空 | FakeClock 推進 1800 s 才 `run.cancel()`（1799.5 s 時 `is_cancelled()==false`）；`endpoint_shutdown.is_set()==false`、端點 thread 仍在 |
| `holder_watch_permission_status_never_arms` | `HolderScanStatus::Permission` | 同上（`/proc` 不可讀與無 holder 不可區分） |
| `pump_stops_on_not_submitted` | pump 中 fence freeze | pump 結束、本次 run 取消並 join holder watcher、`FlashHolderGone` 已送；`endpoint_shutdown.is_set()==false`、端點 thread 仍在 |
| `pump_stops_on_master_eof` | master `Eof` | 同上 |
| `holder_watch_signature_has_no_endpoint_shutdown`（compile-fail doctest，`holder_watch.rs`；R2-F5） | 以 `&EndpointShutdown` 呼叫 `watch_flasher_disconnect`／`pump_endpoint_to_sink` | 編譯失敗（型別不符）——holder watcher 與 pump 結構上不可能停止端點 |
| `baud_mirror_uses_slave_speed` | slave speed B57600 | serial `mirror_termios_from` 被呼叫一次、fallback 115200 |
| `baud_mirror_once_only` | flash 中途 slave 改 B9600 | 不再鏡射（UIO-12.9-f） |
| `rx_observer_non_blocking` | master writer `try_write` 回 `Ok(0)` | reader thread 不阻塞、不重試 |
| `rx_observer_only_active_com` | COM1 RX | 不寫 master |
| `second_open_while_active_ignored` | flash 中 flasher 再寫 | 不重入 `on_flash_open` |
| `cap_posix_pty__endpoint_slave_never_exclusive`（`crates/sw-pal/tests/flash_pty.rs`，`posix_pty`；R0-F3 延伸） | `FlashEndpoint::start` 後、完成一次 matched→pump→`FlashHolderGone` 循環後，各以一般 open 開 `ttyMCU` 並 `ioctl(TIOCGEXCL)` | 兩次讀回皆為 `0`（daemon 從未對端點設 TIOCEXCL）；第二次 flasher open 成功、送 `55 55` 仍得 `00 cc` 回放 |
| `endpoint_reopen_after_flasher_set_excl_recorded`（黑箱，只記錄、不判 PASS／FAIL） | `FakeFlasher(excl=True)` 開端點後設 `TIOCEXCL`、完成 flash、不清除即關閉 → 第二個 `FakeFlasher` 開 link | 記錄第二次 open 的 errno（預期 Python 與 Rust 皆為 `EBUSY`）與 `mcu.status`；結果寫入 gate 報告「待裁決」欄（DF 候選：端點於 `FlashHolderGone` 後以 keepalive fd `TIOCNXCL`） |
| `endpoint_thread_survives_on_flash_open_panic` | coordinator panic | thread 繼續服務下一次 open（C1） |
| `windows_caps_no_endpoint` | `caps.flash_endpoint=false` | `FlashEndpoint::start` 回 `Ok(None)`，無 thread |

黑箱（`test_s24_flash_endpoint.py`，`posix_pty`＋`process_rpc`）：`fakemcu.py` 以 PTY 當 MCU 線（TI SBL 子集：`55 55` → `00 cc`；之後每個封包回 `00 cc` ACK；記錄全部 RX bytes 與每次 `tcgetattr` speed），profile 為 `platform: passthrough`（非 command_capable）；`fakeflasher.py` 開 `<RUN_DIR>/dev/ttyMCU`、`tty.setraw`、`cfsetspeed`、送 sync、讀 ACK、送封包、關閉。

```python
# tests/contract_blackbox/test_s24_flash_endpoint.py（節錄）
@pytest.mark.contract("case:s24-flash-silent-unbridged")
def test_ttymcu_silent_when_unbridged(sw_env, fake_mcu):
    sw_env.start(targets={"COM0": fake_mcu}, profile="passthrough")
    sw_env.wait_state("COM0", "ATTACHED", timeout=10)
    link = sw_env.run_dir / "dev" / "ttyMCU"
    sw_env.wait_until(link.is_symlink, timeout=10)
    fd = os.open(str(link), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        tty.setraw(fd)
        r, _, _ = select.select([fd], [], [], 2.5)
        assert r == [], "未 bridge：端點不得主動寫任何 bytes"
    finally:
        os.close(fd)
    assert fake_mcu.rx_log() == b""

@pytest.mark.contract("case:s24-flash-sync-replay", "case:s24-flash-holder-gone", "rpc_method:mcu.status")
def test_sync_replay_pump_and_holder_gone(sw_env, fake_mcu):
    sw_env.start(targets={"COM0": fake_mcu}, profile="passthrough")
    sw_env.wait_state("COM0", "ATTACHED", timeout=10)
    fl = FakeFlasher(sw_env.run_dir / "dev" / "ttyMCU", baud=115200)
    fl.write(b"\x55\x55")
    assert fl.read_exact(2, timeout=3.0) == b"\x00\xcc"
    assert fake_mcu.rx_log().count(b"\x55\x55") == 1, "double-sync 會吃掉 ACK"
    assert sw_env.session("COM0")["state"] == "FLASHING"
    st = sw_env.rpc("mcu.status", {})
    assert st["flashing"] is True and st["flashing_com"] == "COM0"
    fl.write(b"\x03\x10\x20")
    sw_env.wait_until(lambda: fake_mcu.rx_log().endswith(b"\x03\x10\x20"), timeout=3)
    time.sleep(1.2)                    # 讓 holder-probe 至少 armed 一次（poll 0.5 s）
    fl.close()
    sw_env.wait_until(lambda: sw_env.session("COM0")["state"] == "ATTACHED", timeout=5)
    st = sw_env.rpc("mcu.status", {})
    assert (st["flashing"], st["flashing_com"]) == (False, None)
    assert st["last_detect"] == {"status": "matched", "com": "COM0", "family": "ti-cc26xx", "hits": []}
    assert all(r["source"] == "flash" for r in sw_env.wal_tx("COM0"))

@pytest.mark.contract("case:s24-flash-baud-mirror")
def test_baud_mirrored_once_from_flasher(sw_env, fake_mcu):
    sw_env.start(targets={"COM0": fake_mcu}, profile="passthrough", uart_baud=115200)
    sw_env.wait_state("COM0", "ATTACHED", timeout=10)
    fl = FakeFlasher(sw_env.run_dir / "dev" / "ttyMCU", baud=57600)
    fl.write(b"\x55\x55"); fl.read_exact(2, timeout=3.0)
    sw_env.wait_until(lambda: fake_mcu.current_speed() == termios.B57600, timeout=3)
    fl.set_baud(9600); fl.write(b"\x00")
    time.sleep(0.5)
    assert fake_mcu.current_speed() == termios.B57600, "只在 flash 開始時鏡射一次"
    fl.close()

@pytest.mark.contract("case:s24-flash-holder-gone")
def test_two_flashes_same_daemon(sw_env, fake_mcu):
    """R2-F5：第一次 flash 正常結束（holder gone）後，同一 daemon 的常駐端點仍能認線並回放 ACK。"""
    sw_env.start(targets={"COM0": fake_mcu}, profile="passthrough")
    sw_env.wait_state("COM0", "ATTACHED", timeout=10)
    link = sw_env.run_dir / "dev" / "ttyMCU"
    for round_no in (1, 2):
        fl = FakeFlasher(link, baud=115200)
        fl.write(b"\x55\x55")
        assert fl.read_exact(2, timeout=3.0) == b"\x00\xcc", f"第 {round_no} 次 ACK 回放"
        assert sw_env.session("COM0")["state"] == "FLASHING"
        fl.write(b"\x03\x10\x20")
        sw_env.wait_until(lambda: fake_mcu.rx_log().endswith(b"\x03\x10\x20"), timeout=3)
        time.sleep(1.2)                # holder-probe armed
        fl.close()
        sw_env.wait_until(lambda: sw_env.session("COM0")["state"] == "ATTACHED", timeout=5)
        assert link.is_symlink(), f"第 {round_no} 次結束後端點 link 仍在"
        assert sw_env.rpc("mcu.status", {})["flashing"] is False
    assert fake_mcu.rx_log().count(b"\x55\x55") == 2, "兩次 sync 各轉送一次"
    assert fake_mcu.rx_log().count(b"\x03\x10\x20") == 2
```

- [ ] **Step 2：執行並確認 FAIL**：`cargo test -p sw-core --features test-fakes --test flash_coordinator` → FAIL；黑箱對 Python 基線 PASS、對 Rust artifact FAIL。
- [ ] **Step 3：commit 測試**：`test(flash): 新增 /dev/ttyMCU 認線、ACK 回放、pump 與 holder-probe 失敗測試`
- [ ] **Step 4：實作**：依上列 8 點；holder watch 與 pump 各為獨立 thread（`serialwrap-ttyMCU-holder`、pump 跑在端點 thread 內，與 Python 相同）；停止旗標分兩層（R2-F5）：`EndpointShutdown` 只由 `FlashEndpoint::stop`／Drop 設定、只控制端點 thread 迴圈；每次 `on_flash_open` 新建一個 `FlashRunCancel`，由該次 detection、pump、holder watcher 共用（含 waker），holder gone／pump 結束只 `cancel()` 本次 run，`is_cancelled()` 同時反映 `EndpointShutdown` 使 daemon 停止能中止進行中的 flash；`request_shutdown` 與 `current_run` 登記同鎖，避免遺失喚醒；等待一律經 `Clock`。`two_consecutive_flashes_same_endpoint`、`shutdown_at_second_flash_start`、黑箱 `test_two_flashes_same_daemon` 必須 PASS。
- [ ] **Step 5：執行並確認 PASS**：Rust 測試、黑箱兩側全 PASS；`scripts/rust/win-test.sh --package sw-core`（`windows_caps_no_endpoint`）。
- [ ] **Step 6：commit**：`feat(flash): 承接 /dev/ttyMCU 端點、sync 回放、pump 與 holder-probe`

**Changelog fragment：** `changelog.d/rust-p8-flash-endpoint.md`（`type: feat`、`scope: rust-core`）。

**BLOCKED-OWNER：** P2-1 MCU flash 手動程序（真機，CLAUDE.md「MCU flash 真機驗證手法」）：擁有者以 Rust bundle 啟動 throwaway daemon（`SERIALWRAP_RUN_DIR`／`_STATE_DIR`／`_BY_ID_DIR` 只含 FTDI MCU 線 symlink，prod daemon 不停），於 DUT console 逐行短指令進 BSL，執行 `ocp-mcu-upgrade -d <RUN_DIR>/dev/ttyMCU -b 115200 -t 8 -e -s -i <fw.bin>`，證據：`Return error code : 0x0`、燒後 session 回 `ATTACHED`、daemon 不死、其他 COM 不受影響、WAL `source:"flash"` 紀錄與 `mcu status` 輸出。程序 revision、執行入口、層級、責任人依規格 §11.6 由 Stage 0 清單提供。

---

## Task 10：`mcu.patterns`／`mcu.status` RPC 與 daemon 組裝

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max（daemon 啟動序與 `FlashEndpoint` RAII 清理屬 daemon 生命週期，R0-F6；Sonnet 審查須逐項核對不變量，§0.2）

**Files:**
- Modify: `crates/sw-daemon/src/service.rs`（`"mcu.patterns"`、`"mcu.status"` 分支；位置照 `service.py:711-724`，在 `health.status` 之後）
- Modify: `crates/sw-daemon/src/daemon_main.rs`（啟動序第 7 步 bootstrap attach 之後 `FlashEndpoint::start`；`TTYMCU_PATH` 取 `RuntimePaths.ttymcu_path`；start 失敗 → exit 1；`FlashEndpoint` 以 RAII 持有，任何退出路徑（RPC 綁定失敗、`wait_stop` 後、panic unwind）都 unlink）
- Modify: `crates/sw-proto/src/runtime_paths.rs`（`RuntimePaths` 加 `ttymcu_path: String`＝`$SERIALWRAP_TTYMCU_PATH` 或 `join(run_dir, "dev", "ttyMCU")`，對應 `sw_core` 常數 `TTYMCU_PATH`）、P3 `runtime_paths` golden 擷取腳本與 golden 檔（加 `ttymcu_path` 欄；Windows flavor 亦產生值但不使用）
- Create: `crates/sw-daemon/tests/support/service_rig.rs`（測試作者交付）；Modify: `crates/sw-daemon/Cargo.toml`（P3 已建；追加 `[[test]] name="mcu_rpc" required-features=["test-fakes"]`）
- Test: `crates/sw-daemon/tests/mcu_rpc.rs`、`tests/contract_blackbox/test_s24_mcu_rpc.py`、P3 `runtime_paths` golden 測試（加欄後重跑）

**Interfaces:**
- Consumes：Task 2、9；`SessionManager::list_sessions()`、`list_devices()`。
- Produces：
  ```rust
  impl Service {
      fn mcu_patterns(&self) -> Result<PyDict, PyErr>;   // {"ok":true,"patterns":[…]}
      fn mcu_status(&self) -> Result<PyDict, PyErr>;     // {"ok":true,"candidates":[…],"flashing":b,"flashing_com":s|null,"last_detect":…}
  }
  ```

**語意：** `candidates` 依 `list_sessions()` 順序取「非 command_capable 且 state ∈ {READY, ATTACHED}」，每筆 `{"com","by_id","real_path","command_capable":false}`，`real_path` 取 `list_devices()` 對應 by_id，查無為 null（非原子組成，KEEP）；`flashing` = `FlashEndpoint::is_flashing()`（無端點時 false，MCU-STATUS-01）；`last_detect` 為 null 或 `{"status","com","family","hits":[{"by_id","com"}]}`。兩個方法都在 `--retries` 白名單內。

- [ ] **Step 1：寫失敗測試**

| 測試名（`mcu_rpc.rs` 以測試作者交付的 `crates/sw-daemon/tests/support/service_rig.rs`（P5 Service＋fake 平台）執行；黑箱同名再跑一次） | 前置 | 期望（RPC_WIRE 位元組，去 `version` 前） |
|---|---|---|
| `patterns_exact_bytes` | — | `{"ok":true,"patterns":[{"family":"ti-cc26xx","probe":"55 55","expect":"00 cc","baud":115200}],"version":…}` |
| `status_idle_no_sessions` | 無 session | `{"ok":true,"candidates":[],"flashing":false,"flashing_com":null,"last_detect":null,…}` |
| `status_candidates_order_and_real_path` | COM0 shell READY、COM1 passthrough ATTACHED、COM2 passthrough DETACHED | `candidates==[{"com":"COM1","by_id":…,"real_path":…,"command_capable":false}]` |
| `status_real_path_null_when_device_gone` | COM1 by_id 不在 devices | `real_path:null` |
| `status_flashing_true_during_probe` | probe 進行中 | `flashing:true`、`flashing_com:null` |
| `status_last_detect_ambiguous` | 兩線都回 ACK | `last_detect=={"status":"ambiguous","com":null,"family":null,"hits":[{"by_id":"a","com":"COM1"},{"by_id":"b","com":"COM2"}]}` |
| `status_windows_no_endpoint` | win-test、`caps.flash_endpoint=false` | `flashing:false`、`last_detect:null`，仍回 candidates |
| `cli_mcu_status_retries_allowed` | stubd 第一次斷線 | `serialwrap --retries 1 mcu status` exit 0，stubd 收到兩次請求 |
| `daemon_starts_ttymcu_after_bootstrap` | 啟動 | `<RUN_DIR>/dev/ttyMCU` 在第一個 attach 開始後才出現；`SERIALWRAP_TTYMCU_PATH` 覆寫生效 |
| `daemon_ttymcu_start_failure_exit1` | link 目錄不可寫 | `serialwrapd` exit 1 |
| `daemon_bind_failure_removes_ttymcu` | ttyMCU 建立後 RPC 綁定失敗（socket 路徑不可寫） | exit 1 且 `ttyMCU` link 已移除 |
| `daemon_panic_after_start_removes_ttymcu`（fake 平台） | ttyMCU 建立後注入 panic | exit 1 且 link 已移除 |
| `runtime_paths_ttymcu_golden` | P3 golden 加欄 | 與 Python `sw_core` `TTYMCU_PATH` 解析一致（含 env 覆寫） |

- [ ] **Step 2：執行並確認 FAIL**：`cargo test -p sw-daemon --features test-fakes --test mcu_rpc` → FAIL；黑箱 Python PASS、Rust FAIL。
- [ ] **Step 3：commit 測試**：`test(mcu): 新增 mcu patterns/status RPC 形狀失敗測試`
- [ ] **Step 4：實作**：`pydict!` 依 Python 插入序；`daemon_main` 停止序見 Task 12。
- [ ] **Step 5：執行並確認 PASS**：Rust 與兩側黑箱；`scripts/rust/win-test.sh --package sw-daemon`。
- [ ] **Step 6：commit**：`feat(mcu): 承接 mcu patterns/status RPC 與 ttyMCU 啟動順序`

**Changelog fragment：** `changelog.d/rust-p8-mcu-rpc.md`（`type: feat`、`scope: rust-daemon`）。

**BLOCKED-OWNER：** 無。

---

## Task 11：外部工具裝置交接的黑箱與整合驗證

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max

**Files:**
P5 已交付 release／attach 的完整安全路徑（`aborted_cmd`、`closed_consoles`、`DEVICE_STILL_HELD` 預檢、`attach_during_drain_is_session_recovering`、`kill_between_commit_and_reply`、self-test RELEASED 分支，06 L2140-2200、L2493）；本 Task **不重做**，只以黑箱與跨 P8 功能（file transfer、flash）的整合測試驗證，並修正 P8 新增路徑上的缺口。屬 P5 既有路徑的缺陷回報 P5 owner，不在此代修（同一工作只由一個 owner 推進）。

- Modify（僅限 P8 新增交互，視測試結果）：`crates/sw-core/src/session_core.rs`（FileTransfer／Flash 前景槽與 release 的交互）
- Create: `crates/sw-core/tests/support/handoff.rs`（測試作者交付，基於 P5 `ActorRig`）；Modify: `crates/sw-core/tests/support/mod.rs`（只追加 `pub mod handoff;`）
- Test: `crates/sw-core/tests/handoff.rs`、`tests/contract_blackbox/test_s24_device_handoff.py`、`tests/contract_blackbox/test_s24_device_handoff_windows.py`（`ExternalHolder`、`HolderSampler`、`throwaway_daemon` 由 Task 0 提供）

**Interfaces:**
- Consumes：P5 `device.release`／`device.attach`（SC-04、SC-05、DF-04、BC-04）、`SerialBackend::open_exclusive -> Result<_, OpenError::{Held, AccessDenied, NotFound, Other}>`、`UartExclusive::probe_holders`；Task 6（傳輸中止）、Task 8（flash busy）。
- Produces：無新公開型別；契約形狀如下（CLI 輸出 `sort_keys`，下表以 dict 表示、略 `version`）：
  - release 成功：`{"ok":true,"session":SessionPublic(state="RELEASED"),"closed_consoles":int,"aborted_cmd":bool}`；已 RELEASED：`{"ok":true,"already_released":true,"session":…}`。
  - attach 被外部持有（非 force，預檢）：`{"ok":false,"error_code":"DEVICE_STILL_HELD","pids":[int],"holder_unknown":false,"selector":…}`。
  - attach force 但 `open_exclusive` 回 `Held`：同上（`pids` 取 holder 掃描）。`force` 只略過開檔前預檢，**不得**略過已識別的外部 holder（規格 §6.2「拒絕已識別的外部衝突」）：外部程式未取 flock（minicom 類）先開埠時，`open`→`flock`→`TIOCEXCL` 雖都成功，`open_exclusive` 的鎖後 holder 重掃（arch §4.2、06 Task 2）仍回 `Held{pids:[tool], holder_unknown:false}`，session 維持 `RELEASED`、UART 零 TX。Windows `AccessDenied`：`{"ok":false,"error_code":"DEVICE_STILL_HELD","pids":[],"holder_unknown":true,"selector":…}`。
  - 裝置不在：`{"ok":false,"error_code":"DEVICE_NOT_PRESENT","selector":…,"device_by_id":str|null}`。
  - self-test RELEASED（P5 06 L2493 規則）：`classification:"RELEASED"`、`recommended_action`（`pids` 為空**且** `holder_unknown:false` → `device_attach`、`reclaimable:true`；`pids` 非空，或 `pids` 為空但 `holder_unknown:true`（Windows／`/proc` 不可讀）→ `wait_external_flash`、`reclaimable:false`；BC-04：不明不得當安全證明）、`released_by`、`released_at`、`reason`、`external_holder`（pids 或 `"none"`）、`reclaimable`。

- [ ] **Step 1：寫失敗測試**

`ExternalHolder`（Task 0）：單一子行程開啟指定 tty 路徑並持有；`flock=True` 時 `fcntl.flock(LOCK_EX)`，模擬遵守 advisory lock 的工具；False 模擬 minicom。

```python
# tests/contract_blackbox/test_s24_device_handoff.py（節錄）
pytestmark = [pytest.mark.posix_pty, pytest.mark.process_rpc]

@pytest.mark.contract("case:s24-ho-ext-tool", "error_code:DEVICE_STILL_HELD", "cli_cmd:device attach")
def test_external_tool_blocks_attach_until_it_exits(sw_env, shell_target):
    sw_env.start(targets={"COM0": shell_target})
    sw_env.wait_state("COM0", "READY", timeout=20)
    r = sw_env.cli("device", "release", "--selector", "COM0", "--source", "agent:p8", "--reason", "p8 handoff")
    assert r.exit == 0 and r.json["session"]["state"] == "RELEASED"
    assert sw_env.holders(shell_target.slave_path, exclude_self=True) == [], "release 回成功前 daemon handle 已關"
    holder = ExternalHolder(shell_target.slave_path, flock=False); holder.start()
    try:
        r = sw_env.cli("device", "attach", "--selector", "COM0")
        assert r.exit == 2
        assert drop_version(r.json) == {"ok": False, "error_code": "DEVICE_STILL_HELD",
                                        "pids": [holder.pid], "holder_unknown": False, "selector": "COM0"}
        assert sw_env.session("COM0")["state"] == "RELEASED", "失敗不得清除 released 保護"
        st = sw_env.rpc("session.self_test", {"selector": "COM0"})
        assert (st["classification"], st["recommended_action"], st["external_holder"]) == \
               ("RELEASED", "wait_external_flash", [holder.pid])
    finally:
        holder.stop()
    r = sw_env.cli("device", "attach", "--selector", "COM0")
    assert r.exit == 0 and r.json["ok"] is True
    sw_env.wait_state("COM0", "READY", timeout=20)

@pytest.mark.contract("case:s24-ho-second-daemon")
def test_second_daemon_takes_device_then_returns(sw_env, shell_target, throwaway_daemon):
    sw_env.start(targets={"COM0": shell_target}); sw_env.wait_state("COM0", "READY", timeout=20)
    sw_env.cli("device", "release", "--selector", "COM0", "--source", "agent:p8", "--reason", "f10-like")
    b = throwaway_daemon(by_id={"dut": shell_target.slave_path})   # 獨立 RUN/STATE/WAL/BY_ID/BY_PATH/CONFIG
    b.wait_state("COM0", "READY", timeout=20)
    r = sw_env.cli("device", "attach", "--selector", "COM0", "--force")
    assert r.exit == 2 and r.json["error_code"] == "DEVICE_STILL_HELD" and r.json["pids"] == [b.pid]
    b.stop()
    assert sw_env.cli("device", "attach", "--selector", "COM0").exit == 0
    sw_env.wait_state("COM0", "READY", timeout=20)

@pytest.mark.contract("case:s24-ho-persist-restart", "bc:DF-04")
def test_released_survives_two_restarts_and_replug(sw_env, shell_target):
    sw_env.start(targets={"COM0": shell_target}); sw_env.wait_state("COM0", "READY", timeout=20)
    sw_env.cli("device", "release", "--selector", "COM0", "--source", "agent:p8", "--reason", "persist")
    for _ in range(2):
        sw_env.restart()
        time.sleep(3.0)
        assert sw_env.session("COM0")["state"] == "RELEASED"
    sw_env.unplug("dut"); time.sleep(2.0); sw_env.replug("dut"); time.sleep(3.0)
    assert sw_env.session("COM0")["state"] == "RELEASED", "離線 tick 不得把 RELEASED 降為 DETACHED"
    assert sw_env.cli("device", "attach", "--selector", "COM0").exit == 0
```

其餘族：

| 測試名 | 情境 | 期望 |
|---|---|---|
| `force_attach_plain_open_tool_still_held`（取代原 `force_attach_minicom_like_tool_opens`；規格 §6.2，先寫、須先 FAIL） | RELEASED 後 `ExternalHolder(slave_path, flock=False)` 持有（minicom 類，不取 flock、不設 TIOCEXCL）→ 記 `n0 = len(shell_target.rx_log())` → `attach --force` | `exit==2`；`drop_version(r.json) == {"ok": False, "error_code": "DEVICE_STILL_HELD", "pids": [holder.pid], "holder_unknown": False, "selector": "COM0"}`；session 仍 `RELEASED`（`released_by`／`reason` 不變）；回覆後再等 1.0 s，`shell_target.rx_log()[n0:] == b""` 且 WAL 無新 TX 記錄（UART 零 TX）；回覆後 `sw_env.holders(slave_path, exclude_self=True) == [holder.pid]`（daemon 已關閉其試開的 fd，未與外部工具共持）；`holder.stop()` 後同一 `attach --force` 回 ok、`ATTACHING` |
| `auto_attach_plain_open_tool_never_starts_io` | 外部 `flock=False` 先持有 slave，之後才 `sw_env.start` | 5 s 內 session `DETACHED`、`last_error=="ATTACH_FAILED:DeviceHeldError"`、從未 `ATTACHING`／`READY`；`shell_target.rx_log() == b""`（無 probe／登入 TX）；`holder.stop()` 後 `device attach` ok 並 READY |
| `force_attach_flock_tool_still_held` | 外部 `flock=True` | `DEVICE_STILL_HELD`、`pids==[tool]`、`holder_unknown:false`、session 仍 RELEASED |
| `attach_device_absent` | by-id symlink 移除 | `DEVICE_NOT_PRESENT`＋`device_by_id` |
| `release_dedupe_concurrent` | 兩個 `device.release` 同時送 | 兩者皆 ok；只產生一次 StopOp；第二次若晚到則 `already_released:true` |
| `release_during_push_aborted_cmd_true` | 同 Task 6 abort 案例 | `aborted_cmd:true`、`closed_consoles` 為 console 數 |
| `release_closes_consoles_count` | 兩個 console attached | `closed_consoles:2`；PTY 端點收 EOF |
| `release_flashing_busy` | FLASHING | `FLASHING_BUSY`＋`selector`＋`session`（Task 8） |
| `attach_while_drain_in_progress` | release drain 尚未回條 | attach 不得呼叫 `open_exclusive`；回 `SESSION_RECOVERING`＋`drain_stalled` 欄位（SC-04 形狀；OD-14 #4 不改 attach 的碼） |
| `release_drain_stalled_then_completes`（新增，OD-14 #4；Rust L1，FakeSerial writer `BlockForever`＋FakeClock，外部無法決定性製造故不做黑箱） | 前景命令已提交 TX 後 writer 卡住；`device.release` | 3,000 ms 後回 `{"ok":false,"error_code":"DRAIN_STALLED","drain_stalled":true,"drain_timeout_epoch":<bridge_generation>,"session":…}`、state `RECOVERING`；再次 `device.release` → 同形 `DRAIN_STALLED`、不產生第二個 StopOp；期間 `device.attach` → `SESSION_RECOVERING`、`open_exclusive` 呼叫 0 次；放開 writer 送回條 → state `RELEASED`、`drain_stalled:false`、state.json 有 released 列；下一次 `device.release` → `already_released:true` |
| `selftest_released_none_holder` | RELEASED、無 holder（POSIX，`holder_unknown:false`） | `external_holder:"none"`、`reclaimable:true`、`recommended_action:"device_attach"` |
| `release_flash_interaction_probe_window` | Probe(1) 窗口中 `device.release` | `FLASHING_BUSY`；probe 結束後 release 成功；外部零 TX |
| `handoff_never_double_held` | release→外部工具開→關→attach，全程 `HolderSampler(slave_path)` | `max_concurrent()<=1`（含 daemon pid 與外部工具） |
| `release_external_open_not_ebusy_repeated`（POSIX 黑箱，非 root；R0-F3） | `shell_target` 全程持有 PTY master；release → 外部一般 `os.open(slave)` → 關 → attach → READY，重複 3 輪 | 每輪外部 open 成功（`EBUSY` 判 FAIL）；每輪 attach `ok:true`、READY；全程無 `DEVICE_STILL_HELD` 回應 |
| `released_blocks_auto_attach_on_hotplug` | RELEASED、裝置重新出現 | 不自動 attach、零 TX |
| `released_cmd_submit_not_ready` | RELEASED | `command.submit` → `SESSION_NOT_READY` |
| `handoff_wal_continuity` | release→外部工具→attach | WAL `seq` 嚴格遞增；外部工具期間無 TX 紀錄 |
| `windows_access_denied_holder_unknown`（`test_s24_device_handoff_windows.py`，`windows_com`） | `SW_TEST_COM_PAIR=COMa:COMb`，release 後外部程式以 `dwShareMode=0` 開 COMa | `DEVICE_STILL_HELD`、`pids:[]`、`holder_unknown:true`；外部關閉後 attach ok |
| `windows_selftest_released_holder_unknown`（`windows_com`） | RELEASED、外部未持有 | `holder_unknown:true` → `external_holder:"none"`、`reclaimable:false`、`recommended_action:"wait_external_flash"` |
| `windows_external_flasher_cycle`（`windows_com`） | release → 外部工具寫入 COMa → 結束 → attach | session 回 READY／ATTACHED；外部期間 WAL 無 daemon TX |

- [ ] **Step 2：執行並確認 FAIL**：`cargo test -p sw-core --features test-fakes --test handoff` → FAIL；POSIX 黑箱 Python PASS（P1 已實作 SC-05、DF-04、BC-04）、Rust FAIL；Windows 黑箱在無 `SW_TEST_COM_PAIR` 時為 SKIP（標「證據不足」，不計 PASS）；OD-15 起擁有者 host 以 `SW_TEST_COM_PAIR=COM20:COM21` 由 orchestrator 實跑，紅燈須為 Rust FAIL（非 SKIP）。
- [ ] **Step 3：commit 測試**：`test(handoff): 新增外部工具裝置交接黑箱測試`
- [ ] **Step 4：實作**：只修 P8 新增交互的缺口（FileTransfer 占前景槽時 `aborted_cmd:true`、Probe 窗口 flash-critical 擋 release）；P5 既有語意（attach 前置序 `drain_in_progress` → 非 force `probe_holders` → 同步 `open_exclusive`（含鎖後 holder 重掃，force 不略過）、self-test RELEASED 規則）黑箱若失敗（例如 `force_attach_plain_open_tool_still_held` 回 ok），記錄並回報 P5 owner，本 Task 不修改。
- [ ] **Step 5：執行並確認 PASS**：Rust 測試與 POSIX 黑箱兩側全 PASS。
- [ ] **Step 6：commit**：`test(handoff): 以黑箱驗證外部工具交接、強制收回與 released 診斷`（若 Step 4 有程式修正，另以 `fix(handoff): …` commit）

**Changelog fragment：** `changelog.d/rust-p8-device-handoff.md`（`type: change`、`scope: rust-core`；Step 4 有修正時改 `type: fix`）。

**擁有者 host 可執行 gate（OD-15）：** `windows_access_denied_holder_unknown`、`windows_external_flasher_cycle`（OD-08 原生 COM）以 `SW_TEST_COM_PAIR=COM20:COM21`（或 SC-36 元件安裝的 pair）由 orchestrator 經 WSL interop 執行；Spike 1 已證實 COM20 第二次開啟得 `ERROR_ACCESS_DENIED`，本 Task 以 Rust daemon 重驗。
**BLOCKED-OWNER：** realhw `p1-ho-cycle`（外部 tmux `minicom -D <attached_real_path> -b 115200`；`device attach` 與 `device attach --force` 都須回 `DEVICE_STILL_HELD`，kill minicom 後才收回 READY）、`p1-ho-persist`（RELEASED 跨 `sudo systemctl restart`）、L2 `f10-creds-fixed-then-ready`（ThrowawayDaemon 交接）於 live bench 以 Rust bundle 執行。

---

## Task 12：systemd 監管整合與有序停止

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max

**Files:**
- Modify: `crates/sw-daemon/src/daemon_main.rs`（停止序；沿用 P5 Task 19 的 `incomplete` 累積與 exit 1 判定，新增 flash 與 handler join 兩個失敗來源，以及 R1-F2 的 event handler 未回收來源；`DaemonHooks` 加 test-fakes 欄位 `block_flash_endpoint_stop: bool`）
- Modify: `crates/sw-daemon/src/framing.rs`（修訂 P5 `RpcServer::stop`：拆為 `stop_accept(&self)`（停止 accept、不等 handler）與 `join_handlers(self, deadline) -> Vec<String>`（以剩餘預算 join，逾時回仍在執行的 method 清單、不無界等待）；P5 06 L3215「等進行中的 handler 回應」語意同步改寫）
- Modify: `docs/rust-refactor/plans/rust-architecture.md` §6.1 停止順序（加 stopping gate、watcher.stop、engine intake／stop、flash 步驟與 handler join 位置）
- Modify: `crates/sw-pal/src/signal.rs`、`crates/sw-pal/src/signal/posix.rs`（如 P5 未覆蓋 SIGTERM 期間的二次訊號）
- Test: `crates/sw-daemon/tests/shutdown_order.rs`、`tests/contract_blackbox/test_s24_supervision_systemd.py`

**Interfaces:**
- Consumes：P4 `render_user_unit(exec_start) -> String`（golden 不變）；P5 `SignalWait::wait_stop`、`SessionManager::begin_shutdown`（stopping gate）、`WatcherHandle::stop`、drain、`WalWriter::sync_close`、`InstanceLockGuard::release`；P7 `EventEngine::{close_intake, stop}`（`DispatcherStopReport`）；Task 9 `FlashEndpoint::stop`。
- Produces（`daemon_main.rs` 停止序，arch §6.1 加上 flash 步驟）：
  ```text
  1. server.stop_accept()               # 只停止 accept，不等進行中的 handler（避免 600 s file.push 卡死停止序）
  1a. manager.begin_shutdown()          # P5 Task 16 stopping gate（R1-F2）：持 manager 鎖設 stopping；之後熱插 Delta／Tick reattach、RequestAttach、bootstrap、
                                        #   bind／clear／recover／device.attach 重開一律不 open、不建 session／worker（RPC 路徑回 EXCEPTION `daemon stopping`）；
                                        #   已越過 gate 的 inflight attach 納入步驟 3 的 drain 集合；也擋住步驟 2 flash 結束時 SC-32 延後的 DeviceDetached reconcile 觸發重開
  1b. watcher.stop()                    # P5 ②：停 DeviceWatcher thread 並 join；channel 殘留 Delta 由 1a gate 丟棄
  1c. engine.close_intake()             # P7 Task 15：matcher 停、dispatcher 不再 spawn handler；已在跑的 handler 不中斷，與步驟 3～5 並行
  2. flash_endpoint.stop()              # 設 EndpointShutdown（唯一設定點）＋wake 本次 FlashRunCancel + join 2.0 s；pump 結束 → FlashHolderGone；unlink ttyMCU（Task 9 receipt）
                                        #   receipt.joined==false 或 unlinked==false → incomplete.push("flash endpoint stop incomplete: <stalled_threads 以 , 串接>")
  3. 所有 session Phase 1（fence frozen、RunCancel、TransferAbort）——進行中 file transfer 的 OpRunner 因 Cancelled 立即返回（XFER-ABORT-01）
  4. server.join_handlers(剩餘預算)     # handler 收到 EXCEPTION 回覆後結束；逾時者 WARNING 列出 method，不再等待
                                        #   回傳非空 → incomplete.push("rpc handlers did not converge: <methods 以 , 串接>")
  5. Phase 2 回條（TxWriter 先 flush 聚合 TX，Task 5）＋ arbiter 全部 FLUSHED_BY_SHUTDOWN；步驟 3～5 共用總上限 5 s（SC-11，以 Clock 計）
                                        #   未回條的 session → incomplete.push("drain stalled: <session_id 以 , 串接>")
  5a. engine.stop(步驟 3～5 的共用期限)  # P7 Task 15：等仍在跑的 handler 至同一 5 s 期限；逾時 HandlerAbort 終止整個 process group／Job，再最多 100 ms 回收；
                                        #   report.unreaped 非空 → incomplete.push("event handlers not reaped: <rule_id 以 , 串接>")；必在步驟 6 WAL close 前返回
  6. WAL flush + sync + close           #   Err(e) → incomplete.push("WAL sync failed: <e.py_message>")
  7. lock.release()；Unix socket 檔移除；
     incomplete 為空 → exit 0（SIGTERM／SIGINT／daemon.stop）；
     否則 stderr 寫一行 `serialwrapd: shutdown incomplete: <incomplete 以 "; " 串接>\n` 並 exit 1，不宣告 graceful success（P5 Task 19 L3991 規則原樣沿用）
  第二次 SIGTERM／SIGINT 只記 log，不改變上述步驟與結果。
  ```
  子步驟 1a～1c、5a 屬於 1～7 範圍內，本 Task 文中「1～7」「3～7」皆包含其間的子步驟。1a～1c 必在步驟 3 之前：gate 先於 watcher.stop，使 watcher 最後一輪送出的 Delta 也被擋下；事件派工先關，Phase 1 之後的 RX 不再產生新 handler（R1-F2；P1 Python 順序為 engine.stop → watcher.stop → shutdown_drain，Rust 把 engine 拆成「1c 關 intake」與「5a 有界收斂」，handler 與 drain 並行而不先耗盡 5 s 預算）。
  上限來源：步驟 2 的 2.0 s 為 Python `FlashEndpoint.stop` join 值；步驟 3～5 與 5a 共用 SC-11／arch §6.1 的 5 s，5a 另含 P7 的 100 ms 回收寬限；不另訂其他總上限。**有界等待與 exit code 分開判定**：任何一步逾時或失敗都不阻擋後續步驟（停止序必定走完 1～7），但失敗會累積到 `incomplete`，最終 exit 1；只有 1～7 全部收斂才 exit 0。未攔截 panic → exit 1。exit 1 讓 `Restart=on-failure`／Task Scheduler RestartOnFailure 能辨識異常停機（`daemon.stop` 觸發的未收斂停機會被監管器重啟；`systemctl stop` 觸發者 unit 標為 failed；`daemon.handoff_stop` 觸發者——update／rollback／回退／復原／`supervision-mode set`——同樣可能 exit 1，故 P4 CLI 在送交接前一律 `hold_autostart`（systemd 持久 drop-in `Restart=no`＋`RefuseManualStart=yes`＋`disable`，05 Task 3／7／12，R5-F1），交接觸發的 exit 1 不會在備份／切換期間被拉起；daemon 端不為此改變 exit code）。本規則屬 SC-11 細化，延續 P5 Task 19 已登記的 DF 候選（P5 PR 描述），不另立 ID；Python 基線此情境的 exit code 不改（02 Task T08），不屬 A/B 比較範圍，A/B 案例不製造停機未收斂。

- [ ] **Step 1：寫失敗測試**

```python
# tests/contract_blackbox/test_s24_supervision_systemd.py（節錄）
pytestmark = [pytest.mark.supervisor_runtime, pytest.mark.posix_pty]

def _systemd_user_ok() -> bool:
    r = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True)
    return r.stdout.strip() in ("running", "degraded")

@pytest.fixture
def transient_unit(sw_env, tmp_path):
    if not _systemd_user_ok():
        pytest.skip("cap:supervisor_runtime 不可用（systemd --user 未執行）")
    name = f"sw-p8-test-{os.getpid()}-{int(time.time())}"
    status = tmp_path / "exit-status"
    def start():
        cmd = ["systemd-run", "--user", f"--unit={name}", "-p", "Type=simple",
               "-p", "Restart=on-failure", "-p", "RestartSec=2",
               "-p", f"ExecStopPost=/bin/sh -c 'echo $$EXIT_CODE:$$EXIT_STATUS >> {status}'",
               *[f"--setenv={k}={v}" for k, v in sw_env.isolated_env().items()],
               sw_env.daemon_exe, "--profile-dir", str(sw_env.profile_dir),
               "--socket", str(sw_env.socket), "--lock", str(sw_env.lock)]
        subprocess.run(cmd, check=True)
    yield types.SimpleNamespace(name=name, start=start, status=status)
    subprocess.run(["systemctl", "--user", "stop", name], check=False)
    subprocess.run(["systemctl", "--user", "reset-failed", name], check=False)

def _main_pid(name: str) -> int:
    return int(subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", name],
                              capture_output=True, text=True).stdout.strip() or 0)

@pytest.mark.contract("case:s24-sup-systemd-runtime", "bc:SC-11")
def test_sigterm_ordered_shutdown(sw_env, shell_target, transient_unit):
    sw_env.prepare(targets={"COM0": shell_target})
    transient_unit.start()
    sw_env.wait_state("COM0", "READY", timeout=20)
    subprocess.run(["systemctl", "--user", "stop", transient_unit.name], check=True)
    assert transient_unit.status.read_text().strip().splitlines()[-1] == "exited:0"
    assert sw_env.holders(shell_target.slave_path, exclude_self=True) == []
    assert not sw_env.socket.exists()
    last = json.loads(sw_env.wal_path.read_text(encoding="utf-8").splitlines()[-1])
    assert last["crc32"] == f"{zlib.crc32(base64.b64decode(last['payload_b64'])):08x}"

@pytest.mark.contract("case:s24-sup-systemd-runtime")
def test_restart_on_failure_keeps_released_and_wal_seq(sw_env, shell_target, transient_unit):
    sw_env.prepare(targets={"COM0": shell_target}); transient_unit.start()
    sw_env.wait_state("COM0", "READY", timeout=20)
    sw_env.cli("device", "release", "--selector", "COM0", "--source", "agent:p8", "--reason", "sup")
    seq0 = sw_env.cli("wal", "current-seq").json["seq"]
    pid0 = _main_pid(transient_unit.name)
    os.kill(pid0, signal.SIGKILL)
    sw_env.wait_until(lambda: _main_pid(transient_unit.name) not in (0, pid0), timeout=15)
    sw_env.wait_until(lambda: sw_env.rpc_ok("health.ping"), timeout=15)
    assert sw_env.session("COM0")["state"] == "RELEASED"
    assert sw_env.cli("wal", "current-seq").json["seq"] >= seq0
```

其餘族：

| 測試名 | 情境 | 期望 |
|---|---|---|
| `stop_during_flashing_removes_ttymcu`（黑箱，fakemcu＋fakeflasher） | FLASHING 中 `systemctl --user stop` | `exited:0`；`ttyMCU` link 已移除；重啟後 session 為 ATTACHED（FLASHING 不持久化） |
| `stop_during_push_flushes_wal`（黑箱） | 64 KiB push 進行中 `systemctl --user stop` | 自 stop 到 `exited:0` ≤ 7.0 s（2.0 s flash join＋5 s drain 上限）；WAL 最後一筆可解析且 CRC 正確；shelltarget `rx_log` 中已收到的 chunk 行都有對應 WAL TX；重啟後 DUT 端零新增（無重送） |
| `shutdown_with_inflight_file_push_within_cap`（`shutdown_order.rs`，fake 平台＋FakeClock） | `file.push` handler 卡在等 prompt 時送 stop | handler 在步驟 3 後收到 `EXCEPTION` 並結束；`join_handlers` 未逾時；總停止時間（FakeClock）≤ 2.0 s＋5 s；exit 0 |
| `handler_join_timeout_does_not_block`（`shutdown_order.rs`） | 注入不回應 cancel 的 handler | `join_handlers` 於剩餘預算到期返回 `["file.push"]`、WARNING；後續步驟照常（trace 仍含 `drain.phase2`、`flush_shutdown`、`wal.sync_close`、`lock.release` 各一次）；exit code 見 `shutdown_handler_timeout_exits_1`（為 1，不得為 0） |
| `shutdown_handler_timeout_exits_1`（`shutdown_order.rs`，fake 平台＋FakeClock；R1-F4，修正前必 FAIL） | 同上：一個不回應 cancel 的 `file.push` handler，其餘 session 正常回條、WAL 正常 | `run_with_hooks` 回 **1**；stderr 恰含一行 `serialwrapd: shutdown incomplete: rpc handlers did not converge: file.push\n`；trace 停止序完整走完至 `lock.release`；WAL 已 `sync_close`（`fs.sync_calls(wal) >= 1`）、lock 可被新實例取得；總停止時間（FakeClock）≤ 2.0 s＋5 s |
| `double_sigterm_still_clean`（`shutdown_order.rs`，fake 平台） | 連送兩次 stop 訊號 | 停止序 1～7（含 1a～1c、5a）各執行一次；exit 0 |
| `shutdown_order_steps`（`shutdown_order.rs`；R1-F2 修訂，修正前必 FAIL） | 記錄各步驟呼叫序（含一條 event rule 已啟用） | 恰為 `["server.stop_accept","manager.begin_shutdown","watcher.stop","engine.close_intake","flash.stop","drain.phase1","server.join_handlers","drain.phase2","flush_shutdown","engine.stop","wal.sync_close","lock.release"]`；缺 `manager.begin_shutdown`／`watcher.stop`／`engine.close_intake`／`engine.stop` 任一或順序不同即 FAIL |
| `shutdown_hotplug_after_phase1_never_opens_uart`（`shutdown_order.rs`，fake 平台＋FakeClock；R1-F2，修正前必 FAIL） | COM0 READY；以 test-fakes hook 在 trace 記下 `drain.phase1` 後，(a) 於 `FakeDeviceSource` 新增 `/s/by-id/usb-NEW` 並直接經 `manager.watcher_sender()` 注入 `Delta{added:["/s/by-id/usb-NEW"]}`（模擬 watcher 最後一輪殘留），(b) 對 COM0 注入 `DeviceDetached(Rebound)`＋新 real_path | `FakeSerial.open_calls("/s/by-id/usb-NEW") == 0`、COM0 新 real_path 的 open 次數為 0；自 `manager.begin_shutdown` 起 `manager.spawned_worker_count()`（test-fakes 專用計數器，本 Task 新增）不增；drain 回條集合恰為 `["t:COM0"]`；WAL 無任何 `usb-NEW` 對應 COM 的 TX／RX 紀錄；log 含 `shutdown 中，略過 attach /s/by-id/usb-NEW`；trace 走完至 `lock.release`、`run_with_hooks` 回 0 |
| `shutdown_inflight_attach_drained_before_wal_close`（`shutdown_order.rs`，fake 平台；R1-F2） | 熱插 `/s/by-id/usb-NEW` 使 attach worker 卡在 `FakeSerial::block_open`（已越過 gate）時送 stop，於 `drain.phase1` 後放行 open | 該 session 出現在 drain 回條集合且 `drained:true`；其 handle `close_count()==1` 且 close 發生在 `wal.sync_close` 之前；FakeTarget 收到 0 bytes；`wal.sync_close` 後 `fs.append_calls(wal)` 不再增加；回 0 |
| `shutdown_reaps_event_handler_tree`（`shutdown_order.rs`，POSIX 真 `HandlerSpawner`＋fake serial；R1-F2，修正前必 FAIL） | 一條 rule 的 handler 為 `sh -c 'sleep 60 & echo $! > <tmp>/child.pid; sleep 60'`（自身與孫行程同一 process group），確認 `child.pid` 已寫出後送 stop；`engine.close_intake` 後再餵一行命中 RX | 本案用真 `Clock`（handler 期限為 `std::time::Instant`）、無 flash endpoint；自 stop 起 `run_with_hooks` 於實時 ≤ 5.5 s 返回 0（5 s 共用期限＋100 ms 回收寬限＋排程餘裕）；返回時 `kill(-pgid, 0)` 與 `kill(child_pid, 0)` 皆為 `ESRCH`（整個行程樹已終止且已回收，無 zombie：`/proc/<pid>` 不存在）；stderr 含 `event dispatcher shutdown 逾時，終止 1 個 handler`；`engine.close_intake` 之後 spawn 次數為 0；trace 中 `engine.stop` 在 `wal.sync_close` 之前；`wal.sync_close` 後 events log 與 WAL 皆無新 append |
| `shutdown_unreaped_handler_exits_1`（`shutdown_order.rs`，fake 平台；R1-F2） | `FakeHandlerSpawner::ignore_abort()` 的 handler（rule `r1`）執行中送 stop，其餘全部收斂 | 停止序照常走完至 `lock.release`；`run_with_hooks` 回 **1**；stderr 恰含一行 `serialwrapd: shutdown incomplete: event handlers not reaped: r1\n` |
| `drain_total_cap_5s`（`shutdown_order.rs`） | 一個 worker 卡住不回條 | 5 s（FakeClock）後仍繼續後續步驟；log WARNING 列出 stalled session；exit code 見 `shutdown_stalled_worker_exits_1`（為 1，不得為 0） |
| `shutdown_stalled_worker_exits_1`（`shutdown_order.rs`，fake 平台＋FakeClock；R1-F4，修正前必 FAIL） | COM0 為 READY 且前景命令已提交 TX 後 writer `BlockForever`（不回 `WorkerStoppedReceipt`）；COM1 正常；送 stop | `run_with_hooks` 回 **1**；stderr 恰含一行 `serialwrapd: shutdown incomplete: drain stalled: t:COM0\n`（不含 COM1）；COM1 的排隊命令仍為 `FLUSHED_BY_SHUTDOWN`；WAL 仍 `sync_close`、lock 仍釋放；自 stop 起到返回（FakeClock）≤ 2.0 s＋5 s |
| `shutdown_flash_stop_incomplete_exits_1`（`shutdown_order.rs`，fake 平台；R1-F4） | 以 `run_with_hooks` 的 test-fakes hook `block_flash_endpoint_stop`（本 Task 新增於 P5 `DaemonHooks`，產品 build 不存在）使 `serialwrap-ttyMCU` thread 忽略 `EndpointShutdown` → join 2.0 s 逾時，`FlashEndpoint::stop` 回 `EndpointStopReceipt{joined:false, stalled_threads:["serialwrap-ttyMCU"], …}`；其餘全部收斂 | 停止序 3～7 照常執行；`run_with_hooks` 回 **1**；stderr 恰含一行 `serialwrapd: shutdown incomplete: flash endpoint stop incomplete: serialwrap-ttyMCU\n` |
| `shutdown_multiple_failures_accumulate_exits_1`（`shutdown_order.rs`，fake 平台；R1-F4） | `block_flash_endpoint_stop`（flash receipt `joined:false`）＋handler 逾時（`file.push`）＋COM0 drain stalled＋WAL `sync` 失敗同時注入 | `run_with_hooks` 回 **1**；stderr 恰為單行 `serialwrapd: shutdown incomplete: flash endpoint stop incomplete: serialwrap-ttyMCU; rpc handlers did not converge: file.push; drain stalled: t:COM0; WAL sync failed: <py_message>\n`（順序依停止序）；`lock.release` 仍執行 |
| `shutdown_all_converged_exits_0`（`shutdown_order.rs`，fake 平台） | 全部步驟收斂 | `run_with_hooks` 回 0；stderr 不含 `shutdown incomplete` |
| `panic_exits_1`（`shutdown_order.rs`） | 注入 panic | exit code 1 |
| `user_unit_text_unchanged`（`tests/test_systemd_units.py` 既有 golden） | Rust `render_user_unit("%h/.local/bin/serialwrapd")` | 與 Python 位元組相同、無尾端換行 |

- [ ] **Step 2：執行並確認 FAIL**：`cargo test -p sw-daemon --features test-fakes --test shutdown_order` → FAIL；systemd 黑箱 Python PASS、Rust FAIL（本機 `systemctl --user is-system-running` 為 `running`，可實跑）。
- [ ] **Step 3：commit 測試**：`test(supervision): 新增 systemd 監管有序停止與失敗重啟測試`
- [ ] **Step 4：實作**：依停止序；stopping gate、watcher.stop、engine.close_intake 在 flash.stop 與 Phase 1 之前（R1-F2：Phase 1 之後的熱插或 SC-32 延後 reconcile 不得 open UART、不得新增未納入 drain 的 worker；事件派工不得在 drain 後新增 handler），engine.stop 在 flush_shutdown 之後、wal.sync_close 之前（handler 行程樹不得活過 WAL close 與 lock.release）；flash endpoint 停止在 drain 之前，避免 pump 與 drain 同時持有 fence；handler join 放在 Phase 1 之後，使長 RPC 先被取消再等待；drain 總上限以 `Clock` 計；第二次訊號只記 log；flash receipt、`join_handlers` 回傳、drain stalled、WAL `sync_close` 錯誤依序累積進 `incomplete`，非空時 exit 1（不得改成 WARNING 後 exit 0）。P5 `daemon_main` 與 `framing` 既有測試只同步更新步驟名稱；P5 `shutdown_with_stalled_drain_exits_1_with_diagnostic`、`shutdown_wal_sync_failure_exits_1` 的 exit code 與 stderr 斷言**不得修改**，須維持 PASS。
- [ ] **Step 5：執行並確認 PASS**：上述兩組在 Rust artifact 皆 PASS。
- [ ] **Step 6：commit**：`feat(supervision): 承接 systemd 下的有序停止、flash 收尾與失敗重啟`

**Changelog fragment：** `changelog.d/rust-p8-systemd-supervision.md`（`type: feat`、`scope: rust-daemon`）。

**docker 先行（OD-15）：** 本 Task 的 journald 收到 daemon stderr、`shutdown incomplete` 診斷、SC-23 autostart hold drop-in（`systemctl show -p Restart,RefuseManualStart` 為 `Restart=no`、`RefuseManualStart=yes`）與 `setup --user`／`--system` 實裝，先以 P2 Task 17 `scripts/docker/run-systemd.sh`（systemd 為 PID 1 的 container，Rust bundle 唯讀掛入）跑過並附 `L1.5-docker` 報告；docker 結果不取代下列實機項。
**BLOCKED-OWNER：** 正式 `serialwrap.service`（`setup --user`／`setup --system --with-sudo`，以 Rust bundle 為 ExecStart 目標）在擁有者 bench 實裝與 realhw `p1-rst-daemon`（`sudo systemctl restart` 後 `bridge_generation`、`profile`、`device_by_id` 不變）；本 Task 的 transient unit 只驗 daemon 行為，不改動擁有者既有 `serialwrap` unit。

---

## Task 13：`windows-task` 監管對 Rust daemon 的驗證

P4 已在 Stage 1 實作共用 `graceful_stop`（P4 Global Constraints：先停用拉起能力 → RPC `daemon.stop` 2.0 s → 等待結束 ≤10 s → 逾時才硬終止；P4 §0.5 註明 SC-24-S 由 P4 實作、P8 只補 Rust daemon 側驗證）。本 Task **不**新增 `TaskSchedulerCtl`、不重寫 stop 序；只 (a) 修正 P4 契約內 `service stop`／`restart` 仍直接 `/End` 的不一致，使其經 `graceful_stop`；(b) 以 Rust daemon 驗證整條序列。

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max

**Files:**
- Modify: `crates/sw-cli/src/commands/service_cmd.rs`（`windows-task` 的 `service stop`／`restart`／`daemon stop`（routed）改呼叫 P4 `graceful_stop`）
- Modify: P4 測試與契約文字：`crates/sw-cli/tests/*` 中 `stop_routes_supervised_unless_explicit_target`（期望序改為 `["schtasks","/Change","/TN","serialwrap","/DISABLE"]`→RPC `daemon.stop`→`wait_stopped`→（僅逾時或 RPC 不可達且工作仍 Running 時）`["schtasks","/End",…]`→`/ENABLE`）、`windows_task_restart_*`；`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` 對應行（L457、L1065）
- Modify: `scripts/rust/win-runner.sh`（`WSLENV` 加入 `SW_TEST_ALLOW_SCHTASKS`）
- Create: `crates/sw-pal/tests/support/wintask.rs`（測試作者交付：`staging(name) -> String`、`query_xml(task) -> XmlTree`（`schtasks /Query /XML` 輸出以測試檔內自寫的最小解析器轉為元素樹，忽略縮排與空白；不新增依賴）、`xml_text(&XmlTree, path) -> String`、`staging_dir() -> String`、`wait_daemon_pid(timeout) -> u32`、`wait_new_daemon_pid(old, timeout) -> u32`、`taskkill_force(pid)`、`wal_last_line_crc_ok(dir) -> bool`；XML 以元素路徑比對，不比對原始字串）
- Test: `crates/sw-cli/tests/service_wintask.rs`、`crates/sw-pal/tests/wintask_runtime.rs`

**Interfaces:**
- Consumes：P4 `graceful_stop`、`WindowsTaskCtl::new(env, fs, clock)`、`ServiceCtl::{action_named, install, uninstall, wait_stopped, set_enabled}`、`SupervisorDefinition{mode, name, exec, working_dir, …}`（全部欄位依 P4 L205）、`ServiceResult{ok, mode, action, rc, stdout, stderr, …}`（struct 欄位存取）、`render_task_xml(&TaskXmlSpec)`；P3 client `rpc_call(method, params, timeout)`。
- Produces：無新公開型別。`/End` 規則（寫入 P4 契約）：只有 `wait_stopped(10 s)` 逾時，或 RPC 不可達且 `/Query` 顯示工作仍 Running 時才執行；daemon 已正常結束時不執行 `/End`（避免「無執行中實例」的非 0 rc 讓健康的 stop 報 `ok:false`）。輸出形狀維持 P4 契約（`ServiceResult`），不新增欄位。

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-pal/tests/wintask_runtime.rs（以 win-test.sh 在 Windows host 執行）
mod support;
use std::time::{Duration, Instant};
use support::wintask::*;
use sw_pal::service_ctl::windows_task::WindowsTaskCtl;
use sw_pal::traits::{ServiceAction, ServiceCtl, SupervisionMode, SupervisorDefinition};

fn allowed() -> bool { std::env::var("SW_TEST_ALLOW_SCHTASKS").as_deref() == Ok("1") }

#[test]
#[ignore = "cap:supervisor_runtime"]
fn cap_supervisor_runtime__task_pt0s_restart_and_graceful_stop() {
    if !allowed() { panic!("需擁有者設定 SW_TEST_ALLOW_SCHTASKS=1 才可建立測試排程工作"); }
    let name = format!("sw-p8-test-{}", std::process::id());
    let p = sw_pal::native();
    let ctl = WindowsTaskCtl::new(p.env.clone(), p.fs.clone(), p.clock.clone());
    let def = SupervisorDefinition { mode: SupervisionMode::WindowsTask, name: name.clone(),
        exec: vec![staging("run-daemon.cmd")], working_dir: Some(staging_dir()), ..SupervisorDefinition::default_for_test() };
    let ran = ctl.install(&def, false);
    assert!(ran.iter().all(|c| c.rc == Some(0)), "{ran:?}");
    let xml = query_xml(&name);
    assert_eq!(xml_text(&xml, "Settings/ExecutionTimeLimit"), "PT0S");
    assert_eq!(xml_text(&xml, "Settings/MultipleInstancesPolicy"), "IgnoreNew");
    assert_eq!(xml_text(&xml, "Settings/RestartOnFailure/Interval"), "PT1M");
    assert_eq!(xml_text(&xml, "Settings/RestartOnFailure/Count"), "999");
    assert!(ctl.action_named(&SupervisionMode::WindowsTask, &name, ServiceAction::Start, false).ok);
    let pid0 = wait_daemon_pid(Duration::from_secs(20));
    taskkill_force(pid0);
    let t0 = Instant::now();
    let pid1 = wait_new_daemon_pid(pid0, Duration::from_secs(150));
    assert!(t0.elapsed() <= Duration::from_secs(150) && pid1 != pid0);
    let r = graceful_stop_for_test(&ctl, &name);               // 呼叫 P4 graceful_stop
    assert!(r.ok, "{r:?}");
    assert!(!r.ran_end, "daemon 正常結束時不得執行 /End");
    assert!(ctl.wait_stopped(&SupervisionMode::WindowsTask, Duration::from_secs(10)));
    assert!(wal_last_line_crc_ok(&staging_dir()), "有序停止後 WAL 最後一筆完整");
    for c in ctl.uninstall(&SupervisionMode::WindowsTask, false) { assert_eq!(c.rc, Some(0)); }
}
```
（`staging("run-daemon.cmd")` 為測試在 staging 目錄產生的批次檔：設定 `SERIALWRAP_STATE_DIR`／`_RUN_DIR`／`_CONFIG_DIR`／`_WAL_DIR`／`_BY_ID_DIR` 指向 staging 後執行 `serialwrapd.exe --socket tcp://127.0.0.1:<空閒埠> --lock <staging>\\lock`，最後 `exit /b %ERRORLEVEL%`；排程工作 XML 不能帶 env，故以批次檔隔離，不影響使用者正式 daemon 的 48700 與 state。`SupervisorDefinition` 其餘欄位以 P4 實際定義逐一填寫；若 P4 無 `default_for_test`，support 檔內提供建構函式。）

`service_wintask.rs`（memory_only，以 P4 `FakeSysEnv`＋stubd）：

| 測試名 | 情境 | 期望 |
|---|---|---|
| `stop_sends_rpc_before_end` | daemon 收到 `daemon.stop` 後 1 s 結束 | 呼叫序 `["schtasks:/Change /DISABLE","rpc:daemon.stop","wait_stopped","schtasks:/Change /ENABLE"]`（**無** `/End`）；`ok:true`；輸出為 P4 形狀 |
| `stop_daemon_unreachable_task_running_ends` | RPC 連不上、`/Query` 為 Running | `/DISABLE`→`rpc`（失敗）→`wait_stopped`→`/End`→`/ENABLE`；`ok` 依 `/End` rc |
| `stop_daemon_unreachable_task_not_running_no_end` | RPC 連不上、`/Query` 為 Ready | 不執行 `/End`；`ok:true` |
| `stop_wait_timeout_then_end` | daemon 不退出 | 10 s 後 `/End`；`ok` 依 rc |
| `restart_is_graceful_stop_then_run` | restart | `[…graceful_stop…,"schtasks:/Run"]`；`/Run` 前已 `/ENABLE` |
| `daemon_stop_cli_routes_to_service_in_wintask` | `serialwrap daemon stop`（windows-task） | `_routed_to:"service stop"`，走同一序 |
| `systemd_modes_unchanged` | systemd-user | 呼叫序只有 `systemctl --user stop serialwrap`（Python 形狀不變） |

- [ ] **Step 2：執行並確認 FAIL**：`cargo test -p sw-cli --test service_wintask` → FAIL（P4 現行直接 `/End`）；`SW_TEST_ALLOW_SCHTASKS=1 scripts/rust/win-test.sh --package sw-pal --caps supervisor_runtime` → FAIL（未授權時該測試為 ignored，報告標「證據不足」）。
- [ ] **Step 3：commit 測試**：`test(supervision): 新增 windows-task 有序停止與失敗重啟測試`
- [ ] **Step 4：實作**：`service_cmd.rs` 的 windows-task stop／restart 改呼叫 `graceful_stop`；`graceful_stop` 依上方 `/End` 規則（若 P4 實作與此不同，以本 Task 同 PR 修正並更新 P4 測試與計畫文字）。
- [ ] **Step 5：執行並確認 PASS**：`service_wintask.rs` 與 P4 既有 `cargo test -p sw-cli` 全 PASS；擁有者授權時 runtime 測試 PASS。
- [ ] **Step 6：commit**：`fix(supervision): windows-task 停止一律經 graceful_stop，僅逾時才 /End`

**Changelog fragment：** `changelog.d/rust-p8-windows-task-supervision.md`（`type: fix`、`scope: rust-cli`）。

**擁有者 host 可執行（OD-15）：** `win-task-restart-on-failure`、`win-task-single-instance` 與 `win-task-72h` 的 >72 小時 `PT0S` 實證改以 P2 Task 18 `scripts/stage0/win-task-throwaway.ps1 -RunId <id> -Hours 72 -Bundle <Rust bundle>` 在擁有者 host 執行（工作名 `serialwrap-throwaway-<run_id>`、`SERIALWRAP_STATE_DIR`／`_RUN_DIR`／`_CONFIG_DIR` 在隔離根下、allowlist `COM20`，不動正式 `serialwrap` 工作；結束 `-Cleanup`），報告 `evidence_layer: "host-throwaway"`；同時段 Linux 監管 >72h 在 docker（`run-supervision.sh`）並行。
**BLOCKED-OWNER：** (1) 建立測試排程工作的授權（`SW_TEST_ALLOW_SCHTASKS=1`，擁有者本機 Windows 11 Pro 24H2，OD-05；throwaway 工作亦需此授權）——**已由 OD-28（2026-10-27）授權**：範圍僅限 `scripts/stage0/win-task-throwaway.ps1` 的 `serialwrap-throwaway-<run_id>`（隔離根、allowlist 只有 `COM20`、不動正式 `serialwrap` 工作、結束必 `-Cleanup`），於本 Task（P8 Rust daemon bundle 完成後）執行；`win-task-single-instance`、`win-task-restart-on-failure`、`win-task-72h` 由 agent 執行，`win-task-logon-trigger` 需擁有者登出／登入一次、屆時由 agent 提醒；Stage 1 不執行任何排程工作；(2) P2 已登記的 `win-task-logon-trigger`、`win-task-single-instance`、`win-task-restart-on-failure`、`win-task-72h`（Spike 2：>72 小時 `PT0S` 實證、登入觸發需登出／登入 `interactive_desktop`、測試團隊工作站以 `setup` 安裝正式 `serialwrap` 工作）以 Rust bundle 執行；(3) Task Scheduler 對 `TerminateProcess` 是否觸發 `RestartOnFailure` 的實測結論（若 150 s 內未重啟即為 FAIL，回報擁有者調整監管策略，不得改寬斷言）；(4) `schtasks /End` 在無執行中實例時的 rc 需於 Windows 實機確認（本計畫依規則避免該情境，不依賴其 rc）。

---

## Task 14：更新／回滾守衛對 Rust daemon 的驗證與補強（SC-23）

P4 已交付 §12.1 七步與 `safety_check`（05 L1617-1654：`mcu.status`(0.5 s).`flashing` → `FLASHING_BUSY`；`session.list`(1.0 s) 任一 `state=="FLASHING"` 或 `foreground_busy` → `SESSION_BUSY`（P4 不新增 `UPDATE_BUSY`，05 `test_stage1b_adds_no_error_codes`），detail `busy:[{com,state,foreground_busy}]`；**`--force` 不影響此步**——§12.1 第 2 步不符合交接條件時一律拒絕更新，`--force` 只在 detail 記 `force_ignored:true`，見 05 Task 12 第 2 步）。本 Task **不**另建 CLI 守衛、不改錯誤碼；只 (a) 對 Rust daemon 驗證 P4 契約；(b) 補 §12.1 第 2 步「活躍寫入與其他不可安全中止操作」中 P4 未涵蓋的 interactive lease；(c) 修正 P4 內部矛盾：Global Constraint「安全檢查無法確認時一律拒絕（fail-closed）」與第 2 步「不健康視為通過」衝突，依 fail-closed 收斂；(d) **Rust daemon 承接 SC-11 交接停止 `daemon.handoff_stop`**（R1-F1）：P4 步驟 3 不再送一般 `daemon.stop`，而由 daemon 在共同 admission 邊界內原子完成 busy 判定＋凍結新操作＋發 token 後才停止（Python 版由 P1 T08 交付；arch §6.2 第 7 步）。本 Task 之前的 Rust artifact 對此方法回 `METHOD_NOT_FOUND`，P4 會拒絕從其更新／回滾（fail-closed），故 (d) 是 Slice 2.4「監管整合」出口的必要項。交接排空窗口（OD-16 (3)，SC-11 列）：Rust daemon 須承接 Python 基線的相同語意——`daemon.handoff_stop` 成功回覆後保留與 Python `HANDOFF_DRAIN_GRACE_S` 同值（3 秒）的 RPC 排空窗口，窗口內有副作用請求回 `FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`、窗口內 DeviceWatcher 重探與 console 行零 TX，窗口結束才停止。本段僅為交叉參照；實作與證據以 `spec-reconciliation.md` SC-11 列、`contracts/behavior_changes.yaml` SC-11，以及 `owner-decisions-addenda.md` 之 `PENDING-EVID-S1X-HANDOFF-WINDOW-L15`（Rust 版 L1.5 證據缺口）為準。

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max（daemon 生命週期／跨 Stage 停止交接，R0-F6；Sonnet 審查須逐項核對不變量，§0.2）

**Files:**
- Modify: P4 `safety_check` 所在模組（`crates/sw-cli/src/` 的 update 流程；以 P4 實際檔名為準）：busy 條件加 `interactive_session_id != null`；daemon 健康判定改為三態
- Modify: `docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` 第 2 步文字（同步上述兩點）、P4 `mgmt_update.rs` 既有測試（不改既有斷言，只追加）
- Create: `crates/sw-core/src/admission.rs`（`AdmissionGate`，arch §6.2 第 7 步）；Modify: `crates/sw-daemon/src/framing.rs`（`daemon.handoff_stop` 與 `daemon.stop` 同層）、`crates/sw-daemon/src/service.rs`（有副作用方法分派前 `admit`）、CommandRegistry／SessionActor（命令終態 `done`；`recovering`／`flashing`／`lease` 旗標發布）、file transfer 與 flash endpoint（Task 5～9 模組：傳輸返回 `done`；sync-probe 第一個 byte 前 `admit("flash")`）
- Test: `crates/sw-cli/tests/update_guard.rs`、`crates/sw-core/tests/admission.rs`、`crates/sw-daemon/tests/handoff_stop.rs`、`tests/contract_blackbox/test_s24_update_rollback.py`（`HolderSampler` 由 Task 0 提供）

**Interfaces：**
- Consumes：P4 `safety_check(ctx, endpoint) -> Result<(), (&'static str, PyDict)>`、`run_update`、`run_rollback`、版本目錄與 Current／Previous、`mkbundle.sh --dev`、`ProcScan`；Task 6、9 的 daemon 行為。
- 行為變更（寫入 P4 契約）：
  - busy 條件：`state=="FLASHING"` 或 `foreground_busy==true` 或 `interactive_session_id` 非 null → `SESSION_BUSY`（detail 每筆加 `interactive_session_id`；message 不變）。背景 capture 為被動 RX、可安全中止，不列入（理由寫入 P4 文字）。
  - daemon 健康三態：**確認不存在**（連線被拒或 socket／TCP 端點不存在，且 `ProcScan` 找不到 `serialwrapd`）→ 視為通過；**健康**→ 照常檢查；**未知**（RPC timeout、壞回應、連線成功但回應不可解析、行程存在但 RPC 不通）→ 拒絕，碼依 05 Task 12 錯誤表：探測 RPC 逾時／壞回應／連線失敗回該 RPC 的碼（`TIMEOUT`／`INVALID_RESPONSE`／`SOCKET_ERROR`，detail `probe:<method>`），行程存在但 RPC 不通回 `DEVICE_STILL_HELD`（detail `pids`），兩者 detail 皆加 `{"health":"unknown","reason":…}`；**`--force` 不可略過**（健康不明時可能正處 FLASHING／FileTransfer，停 daemon 無法撤銷 DUT 已發生的部分燒寫）。
  - 步驟 3 交接（R1-F1）：停用後送 `daemon.handoff_stop`，**不送**一般 `daemon.stop`；daemon 回忙碌或交接無法確認 → 還原 enabled、不硬停、不切換；CLI 不得以第二次 `session.list` 取代 daemon 原子判定。
  - 步驟 7 回退與 journal 復原（R2-F6，05 Task 12 `stop_for_revert`）：停止步驟 6 已啟動、已對外服務的 Rust daemon 同樣只送 `daemon.handoff_stop{"reason":"revert"}`；Rust daemon 對 `reason:"revert"` 的 busy 判定與凍結語意與 `"update"` 相同；忙碌／不明 → 不硬停、不切換回 Previous、journal 保留。
  - 交接前抑制監管拉起與重新交接（R5-F1，05 Task 3／7／12）：本 Task 以 Rust daemon 驗證 P4 契約——送 `daemon.handoff_stop` 前 CLI 已對當下 mode `hold_autostart`（systemd 持久 drop-in＋`disable`；windows-task／windows-service disable，hold 期間重新註冊以停用狀態送出；on-demand 持 `autostart.lock`＋journal 擋 SC-18），Rust daemon 交接後以 exit 1（P8 Task 12 停機未收斂）結束時不得被監管器拉起；若 endpoint 仍出現另一個 Rust daemon 實例（hold 前已拉起、hold 失效、手動啟動），它的 `daemon.handoff_stop` 由它自己的 `AdmissionGate` 原子判定（舊 token 對它無效、daemon 不接受外來 token），忙碌即拒絕、CLI 不硬停不切換。daemon 端不新增方法、不改 exit code。
  - `--force` 對 update／rollback 的 busy、健康三態、身分確認與步驟 3 交接**全部無效**；帶 `--force` 的拒絕結果與未帶時逐欄相同，只多 detail `force_ignored:true`，且不得 disable／stop 監管、不得送 `daemon.stop`。一般 `setup`（非 update 路徑）的 Python 對等 `--force` 覆寫不變。
  - `supervision-mode set`（SC-22，R1-F3，05 Task 12 `run_supervision_switch`）：不屬 Python 對等例外；停止 Rust daemon 只送 `daemon.handoff_stop{"reason":"supervision_set"}`，Rust daemon 對該 reason 的 busy 判定與凍結語意與 `"update"` 相同；忙碌／不明 → CLI 還原 enabled、不硬停、不寫 config，`--force` 無效。

- [ ] **Step 1：寫失敗測試**

| 測試名 | 情境 | 期望 |
|---|---|---|
| `guard_flashing_blocks_rollback`（`update_guard.rs`，stubd） | `mcu.status.flashing:true` | P4 `FLASHING_BUSY` JSON（message `flash 進行中，拒絕更新／回滾（--force 不適用，請待 flash 完成後重試）`）、exit 2、未執行任何 stop／切換命令 |
| `guard_foreground_blocks_rollback` | COM1 `foreground_busy:true` | `SESSION_BUSY`、detail `busy` 含 `{"com":"COM1",…}`、exit 2 |
| `guard_interactive_lease_blocks_rollback` | COM0 `interactive_session_id:"i-1"`、`foreground_busy:false` | `SESSION_BUSY`、detail 含 `interactive_session_id:"i-1"` |
| `guard_rpc_timeout_refuses` | stubd `session.list` 延遲 > 1.0 s | `TIMEOUT`、`probe:"session.list"`、`health:"unknown"`、未停止監管 |
| `guard_malformed_response_refuses` | stubd 回 `reply_raw:"{not json"` | `INVALID_RESPONSE`，其餘同上 |
| `guard_daemon_absent_passes` | socket 不存在且無 `serialwrapd` 行程 | 放行進第 3 步 |
| `guard_process_alive_rpc_down_refuses` | socket 不通但 `ProcScan` 找到 `serialwrapd` | `DEVICE_STILL_HELD`、`pids` 非空、`health:"unknown"` |
| `guard_force_does_not_override`（參數化，取代原 `guard_force_overrides`） | 上述每一拒絕列（flashing、foreground、interactive lease、RPC timeout、壞回應、行程在但 RPC 不通）各＋`--force`，update 與 `setup --rollback --force` 各跑一次 | 錯誤碼／`phase`／`busy` 與未帶 `--force` 時逐欄相同；`safety_check.detail.force_ignored==true`、無 `skipped`；steps 止於 `safety_check`；監管呼叫序為空（無 `/Change /DISABLE`、`/End`、`systemctl stop`）；stubd 未收到 `daemon.stop`；`current-release.json` 未變 |
| `guard_force_recheck_after_disable_still_refuses` | `session.list` 閒置、停用後 `daemon.handoff_stop` 回 `FLASHING_BUSY`，帶 `--force` | `FLASHING_BUSY`、`set_enabled(true)` 已呼叫、stubd 未收到 `daemon.stop`、無 `/End` |
| `gate_admit_vs_freeze_linearizable`（`crates/sw-core/tests/admission.rs`；R1-F1，修正前必 FAIL） | 8 執行緒反覆 `admit("file.push","COM0")`／`done`，1 執行緒 `freeze_if_idle("update")`，10,000 輪（`loom` 不適用時以 Barrier＋隨機 yield） | 每輪恰為二者之一：freeze 回 token ⇒ 其後所有 `admit` 回 `None` 且 freeze 當下 ticket 數為 0；freeze 回 busy ⇒ `frozen==false` 且 busy 的 `ops` 含當下持有的 ticket kind；不得出現「token 發出時仍有 live ticket」 |
| `handoff_file_push_after_last_probe`（`crates/sw-daemon/tests/handoff_stop.rs`，daemon 側；CLI 側同情境為 05 `update_file_push_after_last_probe`，Python A/B 對應 P1 `test_handoff_file_push_after_last_probe`；真 daemon＋FakeSerial；R1-F1，修正前必 FAIL） | `session.list` 閒置後開始 `file.push`（FakeSerial 寫入阻塞），再送 `daemon.handoff_stop{"reason":"update"}` | `{"ok":false,"error_code":"SESSION_BUSY","busy":[{"com":"COM0",…,"ops":["file.push"]}]}`；trace 無 `BeginDrain`／`TransferAbort`；放行後 push `ok:true` 且 FakeSerial 收到完整 payload；再送交接 → `ok:true`、`stopping:true`、token 32 hex、回應無 `version`，之後 trace 才出現 `ServerStopped`，`SessionsDrained{flushed:[],stalled:[]}`、exit 0 |
| `handoff_tx_fence_timeout_thaws_without_stop`（`crates/sw-daemon/tests/handoff_stop.rs`；Python 基線 `tests/test_stage0_handoff_stop.py::test_handoff_refused_when_background_write_holds_tx_fence`；R1-F1） | 已通過 admission 的背景 TX 留在 TX fence 內超過交接收斂期限，再送 `daemon.handoff_stop` | `SESSION_BUSY`，`busy` 的 `ops` 含 `tx.in_flight`；交接未成立、不停止 daemon；只撤銷本次建立的凍結，先前 shutdown freeze 若不存在則 admission／session／bridge 回到未凍結，放行原 TX 後可再送交接 |
| `handoff_rollback_preserves_shutdown_freeze`（`crates/sw-daemon/tests/handoff_stop.rs`；AdmissionGate／bridge freeze） | shutdown 已建立 admission、session 與 bridge freeze；handoff rollback 路徑遇 TX fence 未收斂 | rollback 只能保留 shutdown freeze；admission 與 session 仍 frozen、bridge 仍拒絕新 TX，原在途 TX 可完成；不得因 handoff rollback 呼叫 thaw，也不得送出第二次 stop |
| `handoff_flash_races_prepare`（同檔；CLI 側為 05 `update_flash_races_prepare`；R1-F1，修正前必 FAIL） | fakemcu：sync-probe 與 `daemon.handoff_stop{"reason":"rollback"}` 以 Barrier 同時觸發，200 輪；另兩個固定順序案例 | 每輪：交接 ok ⇒ probe 被拒、fakemcu 收到 0 bytes、session 從未 `FLASHING`；probe 先 admit ⇒ 交接回 `FLASHING_BUSY`、daemon 未停止、flash 完成後 `Return error code : 0x0` |
| `handoff_frozen_rejects_mutating_methods`（同檔） | 交接 ok 後（停止尚未完成前）對 `rpc_schema.json` 中 `side_effects` 非空且 `retryable_readonly==false` 的每個方法送請求 | 皆 `{"ok":false,"error_code":"FLUSHED_BY_SHUTDOWN","reason":"handoff_frozen"}`；FakeSerial 自凍結起零寫入、WAL 無新 TX、`state.json` 未改寫；`health.ping`／`session.list`／`mcu.status` 仍 ok |
| `handoff_reason_supervision_set_same_semantics`（同檔；R1-F3） | 參數化 reason `update`／`rollback`／`revert`／`supervision_set`：`file.push` ticket 已 admit；或 flash sync-probe ticket；或全閒置 | 前兩者各回 `SESSION_BUSY`（`ops` 含 `file.push`）／`FLASHING_BUSY`、未凍結、push 完成且 sha256 相符；閒置回 32 hex token 且凍結；四個 reason 除 token 外回應逐欄相同 |
| `supervision_set_force_refuses_rust_daemon_pushing`（`tests/contract_blackbox/test_s24_update_rollback.py`；R1-F3） | Rust daemon（P8 Task 14 後）`file push` 進行中，`serialwrap supervision-mode set systemd-user --force`（stub `systemctl`） | exit 2、`SESSION_BUSY`、`phase:"supervision_set_safety_check"`、`force_ignored:true`；push 完成、daemon pid 不變、stubd／stub `systemctl` 未收到停止、`config.yaml` 位元組不變 |
| `handoff_busy_lease_and_recovering`（同檔） | lease 開啟；或 IoError drain 進行中 | 各回 `SESSION_BUSY`（`ops:["session.interactive_open"]`／`recovering:true`），未凍結、之後 `command.submit` 照常受理 |
| `rollback_refuses_push_started_after_probe`（黑箱，Rust daemon，`posix_pty`，`process_rpc`；R1-F1） | dev bundle 以 `SW_TEST_UPDATE_PAUSE_AFTER=safety_check` 在預檢後暫停，期間開始 `file push`，再放行 `setup --rollback`（與 `--force` 各一次） | `SESSION_BUSY`、`handoff:"refused"`、exit 2；`daemon status.pid` 不變、WAL 無 shutdown 記錄；push 正常完成且 DUT 端 md5 正確；`current-release.json` 未變 |
| `guard_daemon_absent_force_same_as_without` | daemon 確認不存在＋`--force` | 放行進第 3 步，steps 與未帶 `--force` 相同（`--force` 不改變通過路徑） |
| `setup_guard_message_unchanged` | 一般 `setup` 且 flashing | Python 原文 `flash 進行中，拒絕 setup（可用 --force 覆寫）`、exit 2 |
| `rollback_refuses_while_push`（黑箱，Rust daemon） | `file.push` 進行中 rollback | `SESSION_BUSY`；push 正常完成且 DUT 端檔案 md5 正確 |
| `rollback_refuses_while_flashing`（黑箱，fakemcu） | FLASHING 中 rollback | `FLASHING_BUSY`；flash 正常完成 |
| `rollback_force_refuses_while_flashing`（黑箱，fakemcu，`posix_pty`；R0-F4） | FLASHING 中 `setup --rollback --force`；同樣以 `setup --force`（bundle 更新）再跑一次 | 兩者皆 `FLASHING_BUSY`、`phase:"update_safety_check"`、exit 2；拒絕前後 `daemon status.pid` 不變且 `health.ping` 全程 ok；監管仍 enabled（stub 監管呼叫序為空）；fakemcu 收到的燒錄位元組與單獨燒錄時完全相同、flasher `Return error code : 0x0`、燒後 session 回 `ATTACHED`；`current-release.json` 未變 |
| `rollback_force_refuses_while_push`（黑箱，Rust daemon；R0-F4） | `file.push` 進行中 `setup --rollback --force`；同樣以 `setup --force` 再跑一次 | 兩者皆 `SESSION_BUSY`、exit 2；`daemon status.pid` 不變、未收到 `daemon.stop`（WAL 無 shutdown 記錄）；push 正常完成且 DUT 端檔案 md5 正確 |
| `rollback_refuses_while_interactive_send`（黑箱） | interactive lease 開啟、`interactive_send` 進行中 | `SESSION_BUSY`；DUT 端收到完整 payload |
| `rollback_cross_stage_state_compat_released`（黑箱，`posix_pty`，`rust_only`） | Current＝Stage 2（Rust daemon）、Previous＝Stage 1B（Python daemon）；Current 下 release COM0 後 `setup --rollback` | Previous daemon 讀同一 `state.json`：COM0 仍 RELEASED；`device attach` 成功並 READY |
| `rollback_cross_stage_ready_never_double_held`（黑箱，`posix_pty`，`rust_only`） | 同上兩版；COM0 為 READY（Rust daemon 持有 tty）時 `setup --rollback`，全程 `HolderSampler(slave_path, 0.01)` | `max_concurrent()<=1`；Rust daemon pid 消失後 Python daemon pid 才出現；最終 READY |
| `rollback_verifies_pairing` | Previous manifest 與實際 sha 不符 | 回到 Current，保存失敗證據（P4 行為，以 Rust daemon 再跑）；停止 Previous daemon 只經 `daemon.handoff_stop{"reason":"revert"}`、stubd 未收到 `daemon.stop` |
| `revert_refuses_while_new_rust_daemon_pushing`（黑箱，Rust daemon，`posix_pty`，`process_rpc`；R2-F6，CLI 側為 05 `revert_refuses_while_new_daemon_pushing`，修正前必 FAIL） | 更新到 Rust daemon 的 dev bundle，以 `SW_TEST_UPDATE_PAUSE_AFTER=start_and_verify:started`＋`SW_TEST_UPDATE_FORCE_VERIFY_FAIL=smoke_failed` 在新版啟動後暫停，期間對新版開始 1 MiB `file push`，再放行 | `setup` exit 2、`revert_pending:true`、`revert_error{error_code:"SESSION_BUSY",handoff:"refused"}`；新版 trace 無 `BeginDrain`／`TransferAbort`、`daemon status.pid` 不變；push 正常完成且 DUT 端 md5 正確；`current-release.json` 仍為新版、journal 保留；push 完成後再跑 `setup` → 交接 ok、`SessionsDrained{flushed:[],stalled:[]}`、回到 Previous 且 `version_pairing.ok` |
| `journal_recovery_refuses_while_new_rust_daemon_flashing`（黑箱，fakemcu，`posix_pty`；R2-F6，CLI 側為 05 `journal_recovery_refuses_while_new_daemon_flashing`） | dev bundle 在新版啟動後以 test hook 模擬 updater 被殺（`SW_TEST_UPDATE_CRASH_AT=start_and_verify:after_effect`），fakemcu 對新版開始燒錄後執行 `setup` | `FLASHING_BUSY`／`install_recover`、`recovered_journal.ok:false`、journal 逐位元組不變；新版未停止、flash 完成 `Return error code : 0x0`、燒後 session 回 `ATTACHED`；之後再跑 `setup` 才回到 Previous |
| `update_rust_daemon_exit1_after_handoff_not_relaunched`（黑箱，Rust daemon，`posix_pty`，`process_rpc`，`supervisor_runtime`；R5-F1，CLI 側為 05 `update_systemd_exit1_after_handoff_not_relaunched`／`test_update_systemd_exit1_not_relaunched_l15`；預設 ignored、擁有者授權） | 真 `systemd --user` 隔離 unit（`Restart=on-failure`、`RestartSec=2`，dev bundle `SERIALWRAP_SYSTEMD_UNIT_NAME=sw-r5f1-<pid>`）跑 Rust daemon（Current）；以 `DaemonHooks.block_flash_endpoint_stop` 的 env 版 `SW_TEST_DAEMON_SHUTDOWN_INCOMPLETE=flash_endpoint`（只在 `test-hooks` feature）讓交接後停機以 exit 1 結束；執行 `setup`（更新到另一 Rust dev bundle） | 舊 daemon stderr 含 `serialwrapd: shutdown incomplete:`、`ExecStopPost` 記錄 `exited:1`；自交接到新版啟動之間 `systemctl --user show -p NRestarts --value` 維持 0、`MainPID` 只出現舊與新兩個 pid（無第三個）、`HolderSampler(slave_path, 0.01)` `max_concurrent()<=1`；更新 `ok:true`、drop-in 已移除、`Restart=on-failure`、`UnitFileState==enabled`；舊版不得在 `entries_switching` 之後被拉起（新版 `health.status.version` 恰為目標版） |
| `update_switch_window_relaunched_rust_daemon_flash_not_truncated`（黑箱，fakemcu，`posix_pty`，`process_rpc`；R5-F1，CLI 側為 05 `update_relaunched_instance_during_backup_rehandoff_refuses_flash`） | dev bundle 以 `SW_TEST_UPDATE_PAUSE_AFTER=backup_started` 在舊版交接結束、備份進行中暫停；期間手動以舊穩定入口啟動同 endpoint 的 Rust daemon（模擬 hold 失效／手動啟動），fakemcu 對它開始燒錄，再放行 | `setup` exit 2、`FLASHING_BUSY`／`update_confirm_released`、`handoff:"refused"`、`relaunched.pid` 等於手動啟動的 pid；該 daemon trace 無 `BeginDrain`／`TransferAbort`、未收到 `daemon.stop`、flash 完成 `Return error code : 0x0`；`current-release.json` 與穩定入口未變、無 `entries_switching` journal 紀錄、journal 已刪 |

- [ ] **Step 2：執行並確認 FAIL**：`cargo test -p sw-cli --test update_guard` → 新增三態與 lease 列 FAIL（P4 既有列 PASS）；`cargo test -p sw-core --test admission`、`cargo test -p sw-daemon --test handoff_stop` → FAIL（`METHOD_NOT_FOUND`／`AdmissionGate` 未實作）；黑箱對 Rust bundle FAIL（`rust_only` 兩列在 Python 側 SKIP 並已於 `cases.yaml` 登記）。
- [ ] **Step 3：commit 測試**：`test(setup): 新增更新／回滾守衛對 Rust daemon 的驗證與 fail-closed 測試`
- [ ] **Step 4：實作**：CLI 側只改 P4 `safety_check` 的 busy 條件與健康三態判定，並確認 `--force` 未傳入 `safety_check`／步驟 3 交接（若 P4 實作仍有 force 略過分支或一般 `daemon.stop` 分支，於本 Task 同 PR 移除並更新 P4 測試）；錯誤碼不變、message 依 P4 現行文字（`--force 不適用`）；P4 計畫文字同步。daemon 側依 arch §6.2 第 7 步實作 `AdmissionGate`＋`daemon.handoff_stop`（與 P1 T08 Python 版同形狀：成功 `{"ok":true,"stopping":true,"handoff_token"}`、忙碌 `FLASHING_BUSY` 優先於 `SESSION_BUSY`、凍結後 `FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`），所有 admission 點在任何副作用前 `admit`；以 P1 T08 的 `tests/test_stage0_handoff_stop.py` 對 Rust artifact 跑 A/B（雙邊案例）。
- [ ] **Step 5：執行並確認 PASS**：Rust 測試（含 `cargo test -p sw-core --test admission`、`cargo test -p sw-daemon --test handoff_stop`）、P4 既有 `cargo test -p sw-cli`、黑箱與 `tests/test_stage0_handoff_stop.py` 對 Rust artifact 的 A/B PASS。
- [ ] **Step 6：commit**：`fix(setup): 更新／回滾守衛納入 interactive lease、健康不明時拒絕，Rust daemon 承接原子交接停止`

**Changelog fragment：** `changelog.d/rust-p8-update-guard.md`（`type: fix`、`scope: rust-cli`）。

**擁有者 host 可執行（OD-15，原 BLOCKED-OWNER）：** Windows 版本目錄的實機回滾（真實 COM 釋放與重新取得，規格 §12.1 末段「回滾驗收須包括真實串口釋放與重新取得」）於擁有者 Windows 主機以原生 COM `COM20`（peer `COM21` 驗證外部可開／daemon 收回）由 orchestrator 執行；daemon 一律為 OD-11 隔離實例。實體 USB-UART 上的同一流程仍屬 Spike 6（P4 B-05）。

---

## Task 15：全部凍結契約的最終覆蓋判定與切片出口腳本

P3～P7 的黑箱與 Rust 測試沒有 `contract` marker，故 ID↔測試的對應採**宣告式**：來源依序為 `cases.yaml` 各案例的 `evidence`（nodeid）、`error_codes.yaml`／`session_last_errors.yaml` 的 `contract_tests`、本 Task 新增的 `contracts/coverage_map.yaml`（其餘 ID → nodeid 清單）；不依賴程式內 marker。

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high

**Files:**
- Create: `tests/contract_blackbox/coverage_plugin.py`（pytest plugin：只記錄 nodeid 與 outcome，含 setup／teardown 失敗）
- Modify: `tests/contract_blackbox/conftest.py`（啟用 plugin）
- Create: `contracts/coverage_map.yaml`（`{id: [nodeid, …]}`；P8 自身的 ID 由本 Task 填寫；其他 Phase 的 ID 只列出、nodeid 由各 Phase owner 補）
- Modify: `contracts/error_codes.yaml`、`contracts/session_last_errors.yaml`（P8 可達碼的 `contract_tests` 與 `status` 欄更新；`ECHO_ACK_UNSUPPORTED` 標註僅 L1 可達：RPC 路徑產品實作恆支援 paced）
- Create: `scripts/rust/contract_coverage.py`、`scripts/rust/import_cargo_results.py`（以 `cargo test --no-run --message-format json` 取得各 test binary 路徑，逐一以 `<bin> --list` 列出測試、`<bin> --exact <name>`（cap 測試加 `--include-ignored`）執行並記錄 exit code，轉為與 pytest 相同的 results 格式，`nodeid` 為 `cargo:<crate>::<test_file>::<name>`；只用 stable 功能，不新增工具依賴）、`tests/fixtures/cargo_results_sample.json`
- Create: `scripts/rust/slice24_exit.sh`
- Test: `tests/test_rust_contract_coverage.py`

**Interfaces:**
- Consumes：`contracts/error_codes.yaml`、`session_last_errors.yaml`、`behavior_changes.yaml`、`rpc_schema.json`（`x-methods`）、`cli_schema.json`、`wal_record.schema.json`、`state_file.schema.json`、`contracts/coverage_map.yaml`、`tests/acceptance/cases.yaml`、`cases.lock.json`、各輪結果檔。
- Produces：
  ```python
  # 每輪執行使用獨立 run directory：target/contract-coverage/<run_id>/（run_id = UTC 時間戳＋label）
  # coverage_plugin.py／import_cargo_results.py 寫 <run_dir>/results-<label>.json：
  #   {"label","platform","cli_exe","cli_sha256","daemon_exe","daemon_sha256","contract_revision",
  #    "cases_yaml_sha256","cases_lock_sha256","tool":{"name","revision"},"started_at","finished_at",
  #    "collected":[nodeid…],"exit_code":int,"finished":bool,           # R5-F3：runner 實際收集清單、exit code（pytest exitstatus／cargo 各 exe 最大值）與是否跑完
  #    "results":[{"nodeid","outcome":"passed|failed|skipped|xfailed|error","phase":"setup|call|teardown","skip_reason",
  #                "evidence":[{"path","sha256"}]}]}
  #   （pytest 端 coverage_plugin 與 P2 SW_RESOURCE_REPORT 同源欄位；cargo 端 import_cargo_results.py 以 P3 cargo_l1_results.py 的 collected／exit_code／finished 填寫）
  # contract_coverage.py
  EXTRACTORS: dict[str, Callable[[Path], list[str]]]
  def universe(contracts_dir: Path, cases: Path, lock: Path, cmap: Path) -> dict[str, dict]: ...
  #   id → {kind, platforms, rust_only: bool, l1_only: bool, owner_phase, nodeids: [...], blocked_caps: [...]}
  def load_results(run_dir: Path) -> list[dict]: ...   # 只讀單一 run_dir；拒絕身分不一致（見判定）
  def evaluate(universe: dict, results: list[dict]) -> dict: ...
  #   {"covered","missing":{owner_phase:[id…]},"blocked_owner":{id: reason},"ab_mismatch","identity_errors","incomplete_runs":{label: [reason…]}}
  def main(argv=None) -> int: ...   # missing、ab_mismatch、identity_errors、incomplete_runs 任一非空 → exit 1
  ```
  ID 規則：`error_code:<CODE>`（`reachable:true`）、`last_error:<VALUE>`（`reachable:true`）、`rpc_method:<name>`（`rpc_schema.json` 的 `x-methods` 鍵，55 個＝54 基線＋P1 T08 併入的 SC-11 `daemon.handoff_stop`，R1-F1）、`cli_cmd:<group> <sub>`（`cli_schema.json` 的 `/commands[*].path`）、`bc:<ID>`（`behavior_changes.yaml` 的 `changes[*].id`，含 SC-32、SC-33）、`schema:wal_record`、`schema:state_file`、`case:<case_id>`（`cases.lock.json` 的 `sets["slice_2.4"]`）。`EXTRACTORS` 的路徑與 P0 實際檔案不同時，於本 Task 同 PR 調整並由 `test_extractors_match_real_contracts` 把關。

  判定規則：
  1. **身分**：同一 run_dir 內所有結果檔的 `contract_revision`、`cases_yaml_sha256`、`cases_lock_sha256` 必須一致；`rust-*` 結果檔的 `cli_sha256`／`daemon_sha256` 必須彼此一致且等於當輪 artifact 的實際 sha256；`evidence[*].sha256` 必須與檔案相符。不一致 → `identity_errors`，不得跨候選版本或跨輪合併。
  2. **平台矩陣**：依 ID 的 `platforms` 展開必要證據——含 `linux` 者需 `rust-linux` 的 `passed`；含 `windows` 者需 `rust-windows` 的 `passed`，否則必須列 `blocked_owner` 並附能力原因（`windows_com`／`supervisor_runtime`／`interactive_desktop`）；Windows-only ID 不要求 Linux 證據。
  3. **A/B**：非 `rust_only` 的 ID，對應 nodeid 在同平台 `python-*` 亦須 `passed`（同一 nodeid 結果不一致 → `ab_mismatch`）；`l1_only`（只有 Rust L1 nodeid，如 `ECHO_ACK_UNSUPPORTED`）不做 A/B 比對，以 `cargo:` 結果滿足。
  4. **案例**：`case:` 屬 L2／L3／manual 或 capabilities 含 `physical_uart`／`windows_com`／`interactive_desktop`／`supervisor_runtime`／`longrun_bench` 者，只能列 `blocked_owner`，**不得**計入 covered，直到匯入有效實機證據。
  5. `skipped`、`xfailed`、任何 `phase in {setup, teardown}` 的 `error` 一律不算 PASS。
  5b. **執行完整性（R5-F3）**：一個 ID 在某平台成立，必須其對應的**每個** nodeid（`nodeids` 全部，不是任一）在該 label 結果中恰有一列 call `passed`、且無同 nodeid 的 setup／teardown `error`；結果檔 `finished is not True`、`exit_code != 0`、`collected` 內有重複 nodeid、`results` 內同一 nodeid 同 phase 多列、或 `results` 的 nodeid 不在 `collected` 內 → 該 label 列入 `incomplete_runs`（附原因），其整檔 `passed` 不得滿足任何 ID（不得挑最好一列、不得以中途中止的輪次湊證據）；對應 nodeid 根本未被收集（`coverage_map.yaml` 寫錯或測試被刪改名）→ 該 ID 列 `missing`（原因 `nodeid not collected: <nodeid>`）。
  6. `missing` 依 `owner_phase` 分組輸出；本 Task 只補 P8 自身 ID 的對應與案例，其他 Phase 的缺口交回該 Phase owner（不由本 Task 代寫他 Phase 的黑箱），出口前必須清零。

- [ ] **Step 1：寫失敗測試**

```python
# tests/test_rust_contract_coverage.py
from __future__ import annotations
import hashlib, json, subprocess, sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "rust" / "contract_coverage.py"
REV = {"contract_revision": "r", "cases_yaml_sha256": "c" * 64, "cases_lock_sha256": "d" * 64,
       "tool": {"name": "contract_coverage", "revision": "t"}, "started_at": "x", "finished_at": "y"}

def _run(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)

def _results(run: Path, label: str, platform: str, rows: list[dict], **over) -> Path:
    run.mkdir(parents=True, exist_ok=True)
    doc = {"label": label, "platform": platform, "cli_exe": "x", "cli_sha256": "0" * 64, "daemon_exe": "y",
           "daemon_sha256": "1" * 64, **REV, "collected": sorted({r["nodeid"] for r in rows}), "exit_code": 0, "finished": True,
           "results": rows}
    doc.update(over)
    p = run / f"results-{label}.json"; p.write_text(json.dumps(doc)); return p

def _row(nodeid, outcome="passed", phase="call"):
    return {"nodeid": nodeid, "outcome": outcome, "phase": phase, "skip_reason": None, "evidence": []}

def test_extractors_match_real_contracts():
    import importlib.util
    spec = importlib.util.spec_from_file_location("cc", TOOL); cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    u = cc.universe(ROOT / "contracts", ROOT / "tests/acceptance/cases.yaml",
                    ROOT / "tests/acceptance/cases.lock.json", ROOT / "contracts/coverage_map.yaml")
    kinds = {v["kind"] for v in u.values()}
    assert {"error_code", "rpc_method", "cli_cmd", "bc", "schema", "case"} <= kinds
    assert sum(1 for k in u if k.startswith("rpc_method:")) == 55 and "rpc_method:daemon.handoff_stop" in u
    assert "rpc_method:mcu.status" in u and "error_code:FLASHING_BUSY" in u
    assert "bc:SC-32" in u and "bc:SC-33" in u and "case:s24-flash-sync-replay" in u
    assert u["error_code:ECHO_ACK_UNSUPPORTED"]["l1_only"] is True

def test_skipped_and_teardown_error_never_count(tmp_path):
    run = tmp_path / "run1"
    _results(run, "rust-linux", "linux", [_row("t::a", "skipped"), _row("t::b", "error", "teardown")])
    _results(run, "python-linux", "linux", [_row("t::a"), _row("t::b")])
    r = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "rpc_method:mcu.status",
             "--map", "rpc_method:mcu.status=t::a,t::b")
    assert r.returncode == 1
    assert "rpc_method:mcu.status" in sum(json.loads((tmp_path / "o.json").read_text())["missing"].values(), [])

def test_blocked_owner_cases_are_reported_not_covered(tmp_path):
    run = tmp_path / "run2"
    _results(run, "rust-linux", "linux", [_row("t::b")])
    _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "case:f7-binary-roundtrip-md5",
         "--map", "case:f7-binary-roundtrip-md5=t::b")
    out = json.loads((tmp_path / "o.json").read_text())
    assert "case:f7-binary-roundtrip-md5" in out["blocked_owner"] and "case:f7-binary-roundtrip-md5" not in out["covered"]

def test_ab_mismatch_detected(tmp_path):
    run = tmp_path / "run3"
    _results(run, "rust-linux", "linux", [_row("t::c")])
    _results(run, "python-linux", "linux", [_row("t::c", "failed")])
    r = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "error_code:SESSION_BUSY",
             "--map", "error_code:SESSION_BUSY=t::c")
    assert r.returncode == 1
    assert "error_code:SESSION_BUSY" in json.loads((tmp_path / "o.json").read_text())["ab_mismatch"]

def test_identity_mismatch_rejected(tmp_path):
    run = tmp_path / "run4"
    _results(run, "rust-linux", "linux", [_row("t::d")])
    _results(run, "python-linux", "linux", [_row("t::d")], cases_yaml_sha256="e" * 64)
    r = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"))
    assert r.returncode == 1 and json.loads((tmp_path / "o.json").read_text())["identity_errors"]

def test_stale_artifact_hash_rejected(tmp_path):
    run = tmp_path / "run5"
    _results(run, "rust-linux", "linux", [_row("t::e")], daemon_sha256="2" * 64)
    r = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--expect-daemon-sha256", "1" * 64)
    assert r.returncode == 1 and json.loads((tmp_path / "o.json").read_text())["identity_errors"]

def test_evidence_hash_mismatch_rejected(tmp_path):
    run = tmp_path / "run6"; ev = run / "ev.json"; run.mkdir(); ev.write_text("{}")
    row = _row("t::f"); row["evidence"] = [{"path": str(ev), "sha256": "0" * 64}]
    _results(run, "rust-linux", "linux", [row])
    r = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"))
    assert r.returncode == 1 and json.loads((tmp_path / "o.json").read_text())["identity_errors"]

def test_windows_only_id_does_not_require_linux(tmp_path):
    run = tmp_path / "run7"
    _results(run, "rust-windows", "windows", [_row("t::w")])
    _results(run, "python-windows", "windows", [_row("t::w")])
    r = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "error_code:X_WIN_ONLY",
             "--map", "error_code:X_WIN_ONLY=t::w", "--platforms", "error_code:X_WIN_ONLY=windows")
    out = json.loads((tmp_path / "o.json").read_text())
    assert r.returncode == 0 and "error_code:X_WIN_ONLY" in out["covered"], "Windows-only ID 不需 rust-linux 證據"

def test_all_mapped_nodeids_required_not_any(tmp_path):        # R5-F3，failing-first
    run = tmp_path / "run8"
    _results(run, "rust-linux", "linux", [_row("t::g1"), _row("t::g2", "skipped")])
    _results(run, "python-linux", "linux", [_row("t::g1"), _row("t::g2")])
    r = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "rpc_method:mcu.status",
             "--map", "rpc_method:mcu.status=t::g1,t::g2")
    assert r.returncode == 1, "一個 nodeid passed、另一個 skipped 不得滿足 ID"
    assert "rpc_method:mcu.status" in sum(json.loads((tmp_path / "o.json").read_text())["missing"].values(), [])

def test_incomplete_run_cannot_satisfy(tmp_path):              # R5-F3，failing-first
    for i, over in enumerate([{"exit_code": 2}, {"finished": False}, {"collected": ["t::h", "t::h"]}]):
        run = tmp_path / f"run9{i}"
        _results(run, "rust-linux", "linux", [_row("t::h")], **over)
        _results(run, "python-linux", "linux", [_row("t::h")])
        r = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "error_code:SESSION_BUSY",
                 "--map", "error_code:SESSION_BUSY=t::h")
        out = json.loads((tmp_path / "o.json").read_text())
        assert r.returncode == 1 and "rust-linux" in out["incomplete_runs"], over
        assert "error_code:SESSION_BUSY" not in out["covered"]

def test_duplicate_result_rows_not_best_of(tmp_path):          # R5-F3
    run = tmp_path / "run10"
    _results(run, "rust-linux", "linux", [_row("t::k", "failed"), _row("t::k")])
    _results(run, "python-linux", "linux", [_row("t::k")])
    r = _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "error_code:SESSION_BUSY",
             "--map", "error_code:SESSION_BUSY=t::k")
    out = json.loads((tmp_path / "o.json").read_text())
    assert r.returncode == 1 and out["incomplete_runs"]["rust-linux"]

def test_mapped_nodeid_not_collected_is_missing(tmp_path):     # R5-F3
    run = tmp_path / "run11"
    _results(run, "rust-linux", "linux", [_row("t::m")])
    _results(run, "python-linux", "linux", [_row("t::m")])
    _run("--run-dir", str(run), "--json", str(tmp_path / "o.json"), "--only", "error_code:SESSION_BUSY",
         "--map", "error_code:SESSION_BUSY=t::m,t::renamed")
    out = json.loads((tmp_path / "o.json").read_text())
    assert "error_code:SESSION_BUSY" in sum(out["missing"].values(), [])

def test_cargo_results_import_roundtrip(tmp_path):
    imp = ROOT / "scripts" / "rust" / "import_cargo_results.py"
    r = subprocess.run([sys.executable, str(imp), "--fixture", str(ROOT / "tests/fixtures/cargo_results_sample.json"),
                        "--label", "rust-linux", "--out-dir", str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    doc = json.loads((tmp_path / "results-rust-linux.json").read_text())
    assert all(row["nodeid"].startswith("cargo:") for row in doc["results"])
    assert doc["finished"] is True and isinstance(doc["exit_code"], int) and set(doc["collected"]) >= {r["nodeid"] for r in doc["results"]}   # R5-F3
```
（`tests/fixtures/cargo_results_sample.json` 為測試作者交付的小型樣本。）

`slice24_exit.sh` 依序執行並彙總（建立單一 `RUN_DIR=target/contract-coverage/<run_id>`；任一步失敗即 exit 1，最後印 BLOCKED-OWNER 清單）：
1. `python3 scripts/rust/check_cfg_boundary.py`
2. `cargo test --workspace --features test-fakes`；`cargo test --workspace -- --ignored cap_posix_pty__ cap_process_rpc__`；`python3 scripts/rust/import_cargo_results.py --label rust-linux --out-dir $RUN_DIR`
3. `scripts/rust/win-test.sh --package sw-proto --package sw-pal --package sw-core --package sw-daemon --package sw-cli`（WSL 交叉連結＋interop，開發迴圈證據）＋匯入為 `rust-windows-dev`（只作參考，**不**滿足 Windows 正式 gate）
4. `python3 -m pytest -q tests/`（Python 基線，`SERIALWRAP_COVERAGE_RUN_DIR=$RUN_DIR`、label `python-linux`）
5. `SERIALWRAP_CLI_EXE=$RUST_CLI SERIALWRAP_DAEMON_EXE=$RUST_DAEMON SERIALWRAP_AB_LABEL=rust-linux SERIALWRAP_COVERAGE_RUN_DIR=$RUN_DIR python3 -m pytest -q tests/contract_blackbox tests/test_multiagent_e2e.py tests/test_human_agent_coexist.py`
6. `func-test` 外部模式（P2）以 Rust artifact 執行 13 個既有 case＋§15.7.1 的 11 個情境，結果匯入 `$RUN_DIR`
7. Windows 正式 gate（arch §1.1、規格 §11.5）：讀取擁有者 Windows 自架 runner 以**原生 build/link** 產生的 artifact 與其 `results-rust-windows.json`（路徑由 `--windows-run <dir>` 傳入，sha256 須與 runner 產出的 `SHA256SUMS` 相符）；未提供 → 該平台 ID 全列 BLOCKED-OWNER
8. 長跑證據匯入（規格 §11.6、§15.11）：`--longrun <dir>`，內容須含 pinned bundle sha256、對應 Python 48h baseline、停用自動重啟的紀錄、bench lock 紀錄、P2 `realhw/acceptance/gate.py` 的 `release-gate.json`（Windows 另須 `excl/probes.ndjson`＋`excl/handoff.ndjson` 原生 COM 獨占證據，P2 Task 15「Windows 獨占證據契約」，R1-F7；`holder_unknown` 只作診斷，不是雙重持有判定依據）；未提供 → `lr-mixed` 列 BLOCKED-OWNER
9. `python3 scripts/rust/contract_coverage.py --run-dir $RUN_DIR --expect-cli-sha256 $(sha256sum $RUST_CLI|cut -d' ' -f1) --expect-daemon-sha256 $(sha256sum $RUST_DAEMON|cut -d' ' -f1) --json $RUN_DIR/slice24.json`
10. `python3 scripts/rust/slice_breaker.py --freeze tests/acceptance/breaker/slice_2.4.json --snapshots tests/acceptance/breaker/slice_2.4.snapshots.ndjson --today $(date -u +%F)`（tripped 或 `od02_resubmit` → exit 1 並印退守指示）
11. `python3 -m policy_check --repo .`

- [ ] **Step 2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_rust_contract_coverage.py` → FAIL（工具不存在）；階段 A 以 `xfail(strict=True)` 標記維持全套綠燈。
- [ ] **Step 3：commit 測試**：`test(contracts): 新增凍結契約覆蓋判定測試`
- [ ] **Step 4：實作**：plugin 以 `pytest_runtest_logreport` 記錄各 phase outcome、`pytest_sessionfinish` 寫檔（`DISK_STABLE` 等價：`sort_keys=True, separators=(",", ":")`）；`contract_coverage.py`／`import_cargo_results.py` stdlib＋PyYAML；填寫 `coverage_map.yaml` 中 P8 自身 ID；移除 xfail。之後執行 `scripts/rust/slice24_exit.sh`；`missing` 中屬 P8 者補案例，屬他 Phase 者依 `owner_phase` 交回該 owner（不得改判定規則、不得代寫）。
- [ ] **Step 5：執行並確認 PASS**：`python3 -m pytest -q tests/test_rust_contract_coverage.py` → 13 passed；`scripts/rust/slice24_exit.sh` exit 0，`slice24.json` 的 `missing`、`ab_mismatch`、`identity_errors`、`incomplete_runs` 為空，`blocked_owner` 只含下節彙總項目。
- [ ] **Step 6：commit**：`feat(contracts): 加入凍結契約覆蓋判定與 Slice 2.4 出口腳本`

**Changelog fragment：** `changelog.d/rust-p8-contract-coverage.md`（`type: change`、`scope: contracts`）。

**BLOCKED-OWNER：** 見下節彙總（本 Task 只負責把它們列出並阻擋出口，不得計為 PASS）。

---

## BLOCKED-OWNER 彙總（本環境無法產出，交付程序與判定器，證據待擁有者執行）

| # | 證據 | 對應 case（P2 原 ID）／Task | 需要的資源與授權 | 入口命令與證據位置 |
|---|---|---|---|---|
| 1 | L2 TestPilot `serialwrap_regression` 全部適用 38 case（含 `f7-binary-roundtrip-md5`、`f7-larger-file-not-truncated`、`f10-creds-fixed-then-ready`、`f10-unresolved-creds-terminal`）以 Rust bundle 為 pinned `serialwrap_exe` | 各 L2 case_id；Task 6、11 | live bench、bench 互斥鎖、破壞集授權（testbed `allow_destructive: true`） | `testpilot run serialwrap_regression`（單案：`--case <id>`）；`report.json`／`report.md`。`f7-larger-file-not-truncated` 的 pull 端在 EVT-D-F4 KEEP 下必為 SKIP，需擁有者就 §0.4 裁決（預設 XFAIL＋sentinel）才可判定 |
| 2 | realhw L3 P0＋P1＋remote（含 `p1-ho-cycle`、`p1-ho-persist`、`p1-cmd-file`、`p1-rst-daemon`） | 各 L3 case_id；Task 6、11、12 | 兩板 bench、systemd system 模式、sudo | `python3 -m realhw --tier p0,p1` 與 `python3 -m realhw --tier remote`；`--report-dir` 下 `report.json`／`report.md` |
| 3 | P2-1 MCU flash 真機手動程序（`ocp-mcu-upgrade … /dev/ttyMCU` → `Return error code : 0x0`） | `p2-1-mcu-flash-manual`；Task 9 | 實體 MCU 線（FTDI）、DUT console、韌體檔 | `realhw/procedures/p2-1-mcu-flash.md`（P2）；其列出的證據檔 |
| 4 | Windows 原生 COM：交接 `holder_unknown:true`、self-test RELEASED、外部 flasher 循環、push/pull、echo-paced 部分寫入（**OD-15 起為擁有者 host 可執行 gate**，非等待擁有者） | `s24-ho-windows-com`；Task 5、6、11 | OD-08：擁有者 host 的 com0com DUT-side pair `SW_TEST_COM_PAIR=COM20:COM21`（Spike 1 已通過；或 SC-36 元件安裝的 pair；實體 USB-UART 直連 Windows 為備援，不經 usbipd）、OD-11 隔離實例＋allowlist `COM20`，orchestrator 經 WSL interop 代跑 | Windows 自架 runner 執行 `pytest -m windows_com tests/contract_blackbox`；`results-rust-windows.json` |
| 5 | Windows 正式 gate：自架 runner 原生 build/link 的 artifact 與其 L1／L1.5 結果（WSL cross build 不取代） | 全部 platforms 含 windows 的 ID | 擁有者 Windows 自架 runner（arch §1.1、OD-08） | `slice24_exit.sh --windows-run <dir>`（含 `SHA256SUMS`） |
| 6 | `windows-task` runtime：測試排程工作建立授權、失敗重啟實測、>72 小時 `PT0S`（OD-15：以 P2 Task 18 throwaway 排程工作在擁有者 host 執行，與 Linux docker >72h 監管並行）、登入觸發、測試團隊工作站正式安裝 | `s24-sup-wintask-runtime`、`win-task-logon-trigger`、`win-task-single-instance`、`win-task-restart-on-failure`、`win-task-72h`；Task 13 | `SW_TEST_ALLOW_SCHTASKS=1`、Spike 2、互動桌面 | `SW_TEST_ALLOW_SCHTASKS=1 scripts/rust/win-test.sh --package sw-pal --caps supervisor_runtime`；Spike 2 程序 |
| 7 | 正式 `serialwrap.service`（user／system）以 Rust bundle 實裝與重啟 | `p1-rst-daemon`、`p1-ho-persist`；Task 12 | 擁有者機器或 bench、sudo | `setup --user`／`setup --system --with-sudo` 後執行 #2 |
| 8 | 雙平台 Rust daemon 各 48 小時 `lr-mixed`（Linux、Windows 依序，同機同板經 usbipd），滿足 OD-09 絕對與相對閘門。OD-15：開 bench 前先以 P2 Task 17 `scripts/docker/run-longrun.sh --hours 48` 在 docker 跑 Rust 候選（`L1.5-docker`，不計為本項證據） | `lr-mixed` | OD-10 bench 排程；pinned bundle、對應 Python 48h baseline、停用自動重啟、bench lock；OD-09 延後凍結項（RSS／資源預算、最低負載、比較容許誤差）已於正式 Stage 0 出口以 P2 Task 16 凍結（R4-F2） | 開跑前 `python3 -m realhw.acceptance.od09_freeze register-candidate --item S24-L3-LRMIXED-48H-<LINUX\|WINDOWS> --platform <p> --run-id <id> --started-at <now>`（非 0 不得開跑）→ `python3 -m realhw --tier longrun --duration 48h`＋P2 `realhw/acceptance/gate.py` → `release-gate.json`（Windows 依 P2 Task 15 README 前後加 `winexcl handoff --phase pre／post`、observer `--excl-dir`、gate `--excl-evidence`）；以 `slice24_exit.sh --longrun <dir>` 匯入 |
| 9 | Windows 實機回滾（真實串口釋放與重新取得） | Task 14 | 擁有者 Windows 主機＋原生 COM | P4 `setup --rollback` 於 Windows；`failure.json`／步驟紀錄 |
| 10 | needs-owner 裁決：SC-33（Python 側）、XFER-ABORT-01 是否納入 BC-03、EVT-D-F4／F7 | Task 1 | 擁有者 | `spec-reconciliation.md` needs-owner 清單；未裁決依預設往下做，EVT-D-F4 未裁決則阻擋出口 |

`windows-service` 本切片不適用（OD-12：未完成多使用者認證／授權前不得部署），不列入出口。

OD-15 補充：#1、#2、#7 交 bench 前須附 docker 兩容器拓樸（`run-l15.sh`）與 systemd container（`run-systemd.sh`）的 `L1.5-docker` 報告；`slice24_exit.sh` 匯入證據時，`evidence_layer` 為 `L1.5-docker` 的報告只能填 L1.5 列，掛到 #1、#2、#7、#8 一律拒收（P2 `assert_not_real_board`）。#4、#9 與 #6 的 throwaway 部分可在擁有者 host 執行，仍須回填實跑報告才算取得證據。

## 切片出口判定（規格 §11.6 Slice 2.4 列）

- **可在本環境宣告完成的部分**：Task 0～15 全部 PASS、`scripts/rust/slice24_exit.sh` exit 0（`missing`、`ab_mismatch`、`identity_errors`、`incomplete_runs` 為空，斷路器未觸發）、雙審通過、`python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .` 通過。狀態記為「實作與 L1／L1.5 完成」。
- **不得宣告 Slice 2.4 出口通過**，直到上表 1～10 全部取得有效證據或裁決，且每筆證據可追溯至相同 artifact（CLI／daemon sha256、契約 revision、案例清單與鎖檔 hash、工具 revision，§11.4）。任何 BLOCKED-OWNER 項以 SKIP、替身或 Linux 結果代替時，一律判為「證據不足」並阻擋出口。
- **斷路器**：`slice_breaker.py` 判定觸發（連續三週承接率累計增加 < 10 個百分點）或 Stage 2 總日曆預估超過 24 週時，停止候選版本擴大部署、退守已驗收的 Stage 1 artifact 組合並提請擁有者審查／重提 OD-02（規格 §12.2「校準與斷路器」）；安全／平台里程碑只作審查證據，不自動豁免。
