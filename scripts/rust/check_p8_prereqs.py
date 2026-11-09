#!/usr/bin/env python3
"""P8（Slice 2.4）前置符號檢查（09-slice24-transfer-flash.md Task 0；§0.1 前置交付表逐項核對）。

對 §0.1 表中 P0～P7 上游 producer 的每個符號，以 `(檔案, 正規式)` 檢查是否存在；函式另檢查本體是否仍為
`s21_todo!`／`todo!` 骨架。每列標「最早消費的 P8 Task」。另以 `PLANNED` 表列出**由 P8 自己新增**的符號
（各 Task 的 Produces）：在對應 Task 合入前缺漏是預期，合入後成為後續 Task 的前置。

  python3 scripts/rust/check_p8_prereqs.py               # 全表：上游缺任何一項 exit 1；PLANNED 缺漏只列出
  python3 scripts/rust/check_p8_prereqs.py --max-task 6  # 開工 Task 6 前：上游 Task ≤6 的缺漏、PLANNED:Tn（n<6）的缺漏阻擋
  python3 scripts/rust/check_p8_prereqs.py --strict      # 切片出口：上游與全部 PLANNED 皆須存在

輸出狀態：
  MISSING    上游符號不存在（上游缺陷：停止並回報，不在 P8 代補、不以替身代替）；或 --max-task／--strict 下應已合入的 P8 產物缺漏
  SKELETON   符號存在但本體仍為 `s21_todo!`／`todo!`（上游行為尚未實作；依賴其行為的 P8 Task 不得開工）
  DIFFERS    符號存在但形狀與計畫 §0.1／各 Task Interfaces 宣告不同（列出實際形狀與處置；計畫原文以此覆蓋）
  PLANNED:Tn P8 Task n 新增的符號目前缺漏（Task n 合入前為預期）
  LANDED:Tn  P8 Task n 新增的符號已存在

只有 MISSING 影響 exit code；SKELETON／DIFFERS／PLANNED／LANDED 為資訊列。`--max-task` 是局部檢查，末行固定印
「仍有 N 項後續缺漏」、不以 OK 結尾（同 check_p6_prereqs #180 的旁路防護）；切片出口只能以 `--strict` 呼叫。
stdlib only。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
CORE = "crates/sw-core/src"
CORE_ALL = "crates/sw-core/src/**/*.rs"
PROTO = "crates/sw-proto/src"
PAL = "crates/sw-pal/src"
PAL_ALL = "crates/sw-pal/src/**/*.rs"
DAEMON = "crates/sw-daemon/src"
DAEMON_ALL = "crates/sw-daemon/src/**/*.rs"
CLI = "crates/sw-cli/src"
BB = "tests/contract_blackbox"

# 上游（P0～P7）：(檔案或 glob, 正規式, 上游來源, 最早消費的 P8 Task, 若為函式則填函式名以檢查骨架本體)
CHECKS: list[tuple[str, str, str, int, str | None]] = [
    # P3／P5 06 T0c：sw_proto::pycompat
    *[
        (f"{PROTO}/pycompat.rs", rf"^pub fn {name}\b", src, task, None)
        for name, src, task in [
            ("shlex_quote", "06 T0c", 3), ("shlex_split", "06 T0c", 3), ("b2a_base64", "06 T0c", 3),
            ("a2b_base64_lenient", "06 T0c", 4), ("strip_ansi", "06 T0c", 5), ("uuid4_hex", "06 T0c", 4),
            ("splitlines", "P3 04 T4", 3), ("py_len_chars", "P3 04 T4", 3), ("py_int_parse", "P3 04 T4", 2),
            ("py_truthy", "P3 04 T4", 2), ("decode_utf8_replace", "P3 04 T4", 4),
        ]
    ],
    (f"{PROTO}/pyre/mod.rs", r"pub struct PyRegex\b", "P3", 4, None),
    (f"{PROTO}/pypath.rs", r"pub fn basename\b", "P3", 4, None),
    (f"{PROTO}/runtime_paths.rs", r"pub struct RuntimePaths\b", "P3", 10, None),
    # P5 06 T9：TxWriter／Fence
    (f"{CORE}/tx_writer.rs", r"pub enum TxClass\b", "06 T9", 5, None),
    (f"{CORE}/tx_writer.rs", r"^\s*FileTransfer,", "06 T9", 5, None),
    (f"{CORE}/tx_writer.rs", r"^\s*FlashProbe,", "06 T9", 8, None),
    (f"{CORE}/tx_writer.rs", r"pub enum FlashMode \{\s*Off,\s*Probe\(u64\),\s*Flashing,\s*\}", "06 T9", 8, None),
    (f"{CORE}/tx_writer.rs", r"pub enum FenceReject\b", "06 T9", 5, None),
    (f"{CORE}/tx_writer.rs", r"pub struct TxPermit\b", "06 T9", 5, None),
    (f"{CORE}/tx_writer.rs", r"pub struct TxJob\b", "06 T9", 5, None),
    (f"{CORE}/tx_writer.rs", r"pub struct TxReport\b", "06 T9", 5, None),
    (f"{CORE}/tx_writer.rs", r"pub struct TxWriterHandle\b", "06 T9", 5, None),
    (f"{CORE}/tx_writer.rs", r"pub fn admit\b", "06 T9", 5, "admit"),
    (f"{CORE}/tx_writer.rs", r"pub fn revoke_ops\b", "06 T9", 5, "revoke_ops"),
    (f"{CORE}/tx_writer.rs", r"pub fn with<R>", "06 T9", 5, None),
    (f"{CORE}/drain.rs", r"pub struct StopOp\b", "06 T12", 5, None),
    (f"{CORE}/drain.rs", r"pub struct WorkerStoppedReceipt\b", "06 T12", 5, None),
    (f"{CORE}/uart_bridge.rs", r"pub fn finish_async\b", "06 T12", 5, "finish_async"),
    # P5 06 T10＋P7 08 T3／T16：RX
    (f"{CORE}/uart_bridge.rs", r"pub fn snapshot_len\b", "06 T10", 5, "snapshot_len"),
    (f"{CORE}/uart_bridge.rs", r"pub fn text_from\b", "06 T10", 5, "text_from"),
    (f"{CORE}/uart_bridge.rs", r"pub fn wait_for_regex_from\b", "06 T10", 5, "wait_for_regex_from"),
    (f"{CORE}/uart_bridge.rs", r"pub fn wait_for_regex_cancellable\b", "08 T3", 5, "wait_for_regex_cancellable"),
    (f"{CORE}/uart_bridge.rs", r"pub struct RunCancel\b", "08 T3", 5, None),
    (f"{CORE}/uart_bridge.rs", r"pub enum WaitOutcome \{\s*Matched,\s*TimedOut,\s*Cancelled,\s*\}", "08 T3", 5, None),
    (f"{CORE}/uart_bridge.rs", r"pub trait RxObserver\b", "08 T16", 9, None),
    (f"{CORE}/uart_bridge.rs", r"pub fn start_observed\b", "08 T16", 9, "start_observed"),
    # P5 06 T14：OpRunner
    (f"{CORE}/op_runner.rs", r"pub struct OpCtx\b", "06 T14", 5, None),
    (f"{CORE}/op_runner.rs", r"pub registry: Arc<crate::arbiter::CommandRegistry>", "06 T14", 5, None),
    (f"{CORE}/op_runner.rs", r"pub enum OpSpec\b", "06 T14", 6, None),
    (f"{CORE}/op_runner.rs", r"pub enum OpOutcome\b", "06 T14", 6, None),
    (f"{CORE}/op_runner.rs", r"pub fn spawn\b", "06 T14", 6, "spawn"),
    (f"{CORE}/op_runner.rs", r"pub fn run\(&self, grant: OpGrant, spec: OpSpec\)", "06 T14", 6, "run"),
    # P5 06 T11：SessionCore／mailbox
    (f"{CORE}/session_core.rs", r"pub enum SessionRequest\b", "06 T11", 6, None),
    (f"{CORE}/session_core.rs", r"pub enum Effect\b", "06 T11", 6, None),
    (f"{CORE}/session_core.rs", r"pub struct Effects\(pub Vec<Effect>\)", "06 T11", 6, None),
    (f"{CORE}/session_core.rs", r"pub struct OpGrant \{\s*pub op_id: OpId,\s*pub epoch: u64,\s*pub owner_gen: u64,\s*pub fsm_gen: u64,\s*pub cmd_id: Option<String>,\s*\}", "06 T11", 6, None),
    (f"{CORE}/session_core.rs", r"^\s*OpenBridge \{", "06 T11", 8, None),
    (f"{CORE}/session_core.rs", r"^\s*BeginDrain \{", "06 T11", 8, None),
    (f"{CORE}/session_core.rs", r"pub fn on_event\b", "06 T11", 8, "on_event"),
    (f"{CORE}/generations.rs", r"pub conn_epoch: Option<u64>", "06 T11", 8, None),
    (f"{CORE}/generations.rs", r"pub input_epoch: u64", "07 T0 D-02", 8, None),
    (f"{CORE}/mailbox.rs", r"pub enum ControlEvent\b", "06 T11", 8, None),
    (f"{CORE}/mailbox.rs", r"pub enum DataEvent\b", "06 T11", 8, None),
    (f"{CORE}/mailbox.rs", r"ConsoleInputBytes \{ client_id: String, client_lifetime: u64, input_epoch: u64, bytes: Vec<u8> \}", "07 T5", 8, None),
    (f"{CORE}/mailbox.rs", r"pub enum MailboxItem\b", "06 T11", 8, None),
    (f"{CORE}/mailbox.rs", r"pub type Reply = Sender<Result<PyDict, PyErr>>;", "06 T11", 6, None),
    (f"{CORE}/mailbox.rs", r"pub struct OpenedBridge\b", "06 T11", 11, None),
    # 整合線 wp510 於四個 variant 之後再加 `CloseUnconfirmed`／`CloseBlocked`（見 DIFFERS #22）：只釘前四個 variant 與順序。
    (f"{CORE}/mailbox.rs", r"pub enum OpenFailure \{\s*Held\(HolderReport\),\s*AccessDenied\(HolderReport\),\s*NotFound,\s*Other\(PalError\),", "06 T11", 11, None),
    # P6 07 T6：human console（整合線上為實作；骨架即擋 T6／T8）
    (f"{CORE}/session_core/console.rs", r"pub fn fg_begin\b", "07 T6", 6, "fg_begin"),
    (f"{CORE}/session_core/console.rs", r"pub fn fg_finish\b", "07 T6", 6, "fg_finish"),
    (f"{CORE}/session_core/console.rs", r"pub fn human_suspend_begin\b", "07 T6", 6, "human_suspend_begin"),
    (f"{CORE}/session_core/console.rs", r"pub fn human_suspend_end\b", "07 T6", 6, "human_suspend_end"),
    (f"{CORE}/session_core/console.rs", r"pub fn invalidate_human_input\b", "07 T6 D-02", 8, "invalidate_human_input"),
    (f"{CORE}/deferred.rs", r"pub enum InvalidateReason\b", "07 T0 D-02", 8, None),
    (f"{CORE}/deferred.rs", r"^\s*Flash,", "07 T0 D-02", 8, None),
    (f"{CORE}/human_console/ownership.rs", r"pub flash_mode: bool", "07 T4", 8, None),
    (f"{CORE}/human_console/ownership.rs", r"^\s*DropFlash,", "07 T4", 8, None),
    # P7 08：Quiet gate、EventEngine 停止
    (f"{CORE}/quiet_gate.rs", r"pub fn submit_gate_reject\(public_session: &PyDict, _?source: &str\) -> Option<PyDict>", "08 T4", 6, "submit_gate_reject"),
    (f"{CORE}/quiet_gate.rs", r"pub fn execute_gate_reject\(q: &mut QuietState, com: &str, now: Mono\) -> Option<PyDict>", "08 T4", 6, "execute_gate_reject"),
    (f"{CORE}/event_engine/mod.rs", r"pub fn close_intake\b", "08 T15", 12, "close_intake"),
    (f"{CORE}/event_engine/mod.rs", r"pub fn stop\(&self, deadline: std::time::Instant\) -> DispatcherStopReport", "08 T15", 12, "stop"),
    # P5 06 T16：SessionManager
    (f"{CORE}/session_manager.rs", r"pub fn route\b", "06 T16", 6, "route"),
    (f"{CORE}/session_manager.rs", r"pub fn list_sessions\b", "06 T16", 10, "list_sessions"),
    (f"{CORE}/session_manager.rs", r"pub fn list_devices\b", "06 T16", 10, "list_devices"),
    (f"{CORE}/session_manager.rs", r"pub fn flash_candidates\b", "06 T16", 8, "flash_candidates"),
    (f"{CORE}/session_manager.rs", r"pub fn begin_shutdown\b", "06 T16", 12, "begin_shutdown"),
    (f"{CORE}/session_manager.rs", r"pub fn shutdown\b", "06 T16", 12, "shutdown"),
    # P4／P5／P6：sw-pal
    (f"{PAL}/console_pty.rs", r"pub struct PtyPair \{\s*pub master: Box<dyn ConsoleIo>,\s*pub slave_keepalive: KeepAlive,\s*pub slave_path: String,\s*pub slave_ref: PtySlaveRef,\s*\}", "07 T2", 9, None),
    (f"{PAL}/console_pty.rs", r"pub struct PtySlaveRef \{\s*pub path: String,\s*\}", "07 T2", 9, None),
    (f"{PAL}/console_pty.rs", r"fn create_pty\(&self\) -> PalResult<PtyPair>", "07 T2", 9, None),
    *[
        (f"{PAL}/console_pty.rs", rf"^\s*fn {name}\(", "07 T2", 9, None)
        for name in ("read", "try_write", "waker", "has_external_peer", "close", "mux_handle")
    ],
    (f"{PAL}/traits.rs", r"pub trait SerialHandle: Send\b", "06 T2", 9, None),
    (f"{PAL}/traits.rs", r"fn set_baud\(&self, baud: u32\) -> PalResult<\(\)>;", "06 T2", 9, None),
    (f"{PAL}/traits.rs", r"fn mirror_termios_from\(", "06 T2", 9, None),
    (f"{PAL}/traits.rs", r"fn is_alive\(&self\) -> bool;", "06 T2", 9, None),
    (f"{PAL}/traits.rs", r"pub trait UartExclusive: Send \+ Sync \{\s*fn probe_holders\(&self, real_path: &str\) -> HolderReport;", "06 T2", 9, None),
    (f"{PAL}/traits.rs", r"pub trait IoWaker: Send \+ Sync\b", "06 T2", 9, None),
    (f"{PAL}/traits.rs", r"pub enum OpenError\b", "06 T2", 11, None),
    (f"{PAL}/lib.rs", r"pub console: Arc<dyn ConsoleTransportFactory>", "06 T19", 9, None),
    (f"{PAL}/lib.rs", r"pub uart_excl: Arc<dyn UartExclusive>", "06 T19", 9, None),
    (f"{PAL}/lib.rs", r"pub serial: Arc<dyn SerialBackend>", "06 T19", 9, None),
    (f"{PAL}/lib.rs", r"pub service: Arc<dyn ServiceCtl>", "P4 T1", 12, None),
    (f"{PAL}/service_ctl.rs", r"fn action_named\(", "P4 T1", 12, None),
    (f"{PAL}/service_ctl.rs", r"pub fn render_task_xml\(spec: &TaskXmlSpec\) -> String", "P4 T1", 13, "render_task_xml"),
    (f"{PAL}/service_ctl/windows_task.rs", r"pub fn new\(env: Arc<dyn SysEnv>, fs: Arc<dyn FsOps>, clock: Arc<dyn Clock>\) -> Self", "P4 T3", 13, None),
    # P4：sw-cli（sw-core 測試無法引用，故只以正規式核對）
    (f"{CLI}/graceful_stop.rs", r"pub fn graceful_stop\(", "P4 T7", 12, "graceful_stop"),
    (f"{CLI}/graceful_stop.rs", r"pub fn graceful_stop_with\(", "P4 T12", 14, "graceful_stop_with"),
    (f"{CLI}/graceful_stop.rs", r"pub enum StopRpc \{\s*Plain,\s*Handoff \{ reason: &'static str \},\s*\}", "P4 T12", 14, None),
    (f"{CLI}/rollback.rs", r"pub fn safety_check\(", "P4 T12", 14, "safety_check"),
    (f"{CLI}/supervisor_defs.rs", r"pub fn render_user_unit\(exec_start: &str\) -> String", "P4 T3", 12, None),
    # 測試支援（P5 06 T11／T13／T18）
    ("crates/sw-core/tests/support/mod.rs", r"pub struct CoreHarness\b", "06 T11", 5, None),
    ("crates/sw-core/tests/support/mod.rs", r"pub struct ActorRig\b", "06 T13", 5, None),
    ("crates/sw-daemon/tests/support/service_rig.rs", r"pub struct ServiceRig\b", "06 T18", 10, None),
    # P3 黑箱 harness（Task 0 fixture 建構其上，不重寫 daemon 啟停）
    *[
        (f"{BB}/bb_harness.py", rf"^def {name}\b", "P3 T2", 0, None)
        for name in ("run_cli", "isolated_env", "ab_label", "cli_prefix", "daemon_prefix", "is_rust")
    ],
    (f"{BB}/bb_harness.py", r"^class CliResult\b", "P3 T2", 0, None),
    (f"{BB}/bb_daemon.py", r"^class PyDaemon\b", "P3 T2", 0, None),
    (f"{BB}/bb_daemon.py", r"^def _collect_pids\b", "P3 T2", 0, None),
    (f"{BB}/bb_faketarget.py", r"^class FakeTarget\b", "P3 T2", 0, None),
    (f"{BB}/bb_p7.py", r"^class P7Rig\b", "08 T18A", 0, None),
    (f"{BB}/bb_stubd.py", r"^class StubDaemon\b", "P3 T2", 6, None),
    (f"{BB}/slice21/harness.py", r"^def start_daemon\b", "06 T15", 0, None),
    (f"{BB}/slice21/harness.py", r"^class Slice21Daemon\b", "06 T15", 0, None),
    # 契約與凍結工具（不得新增錯誤碼：本切片輸出的碼全部須已存在）
    *[
        ("contracts/error_codes.yaml", rf"^  - code: {code}$", "P0／P1", 4, None)
        for code in (
            "TRANSFER_TIMEOUT", "TRANSFER_ECHO_STALL", "TARGET_DECODER_MISSING", "TARGET_DECODER_FAILED",
            "CHECKSUM_VERIFY_FAILED", "MOVE_TIMEOUT", "MOVE_FAILED", "PULL_PARSE_FAILED", "BASE64_DECODE_FAILED",
            "ECHO_ACK_UNSUPPORTED", "CONSOLE_LINE_LIMIT_TOO_SMALL", "LOCAL_FILE_NOT_FOUND", "FLASHING_BUSY",
            "DEVICE_STILL_HELD", "DEVICE_NOT_PRESENT", "SESSION_BUSY", "AUTOBOOT_QUIET", "DRAIN_STALLED",
            "PROFILE_NOT_COMMAND_CAPABLE", "SESSION_RECOVERING",
        )
    ],
    ("tests/acceptance/regen_lock.py", r"--l1-add-only", "P2 T12", 1, None),
    ("scripts/rust/freeze_slice_cases.py", r"revision_advance", "OD-76", 1, None),
    ("scripts/rust/freeze_slice_cases.py", r"attr_update", "OD-76", 1, None),
    ("scripts/rust/check_cfg_boundary.py", r"", "P5 06 T0", 1, None),
]

# P8 自身各 Task 的 Produces：(Task, 檔案或 glob, 正規式, 函式名或 None, 說明)。
# Task n 合入前缺漏為預期（PLANNED:Tn）；`--max-task M` 時 n < M 的缺漏即阻擋（Task M 的前置）；`--strict` 時全部須存在。
PLANNED: list[tuple[int, str, str, str | None, str]] = [
    # T0：本 Task（上游介面核對與黑箱共用 fixture）
    (0, "crates/sw-core/tests/p8_upstream_interfaces.rs", r"fn compiles\(\)", None, "上游介面編譯期核對"),
    *[
        (0, f"{BB}/{mod}.py", rf"^(class|def) {name}\b", None, "黑箱共用 fixture")
        for mod, name in [
            ("p8_fixtures", "SwEnv"), ("p8_fixtures", "CliRun"), ("p8_fixtures", "drop_version"),
            ("p8_fixtures", "throwaway_daemon"), ("p8_fixtures", "sw_env"), ("p8_fixtures", "shell_target"),
            ("p8_fixtures", "fake_mcu"), ("shelltarget", "ShellTarget"), ("fakemcu", "FakeMcu"),
            ("fakeflasher", "FakeFlasher"), ("extholder", "ExternalHolder"), ("extholder", "ExternalHolderOpenError"),
            ("holder_sampler", "HolderSampler"), ("holder_sampler", "scan_holders"),
        ]
    ],
    (0, f"{BB}/p8_fixtures.py", r"^ANY\b", None, "黑箱共用 fixture"),
    (0, f"{BB}/conftest.py", r"from p8_fixtures import", None, "fixture 由 conftest 載入"),
    # T1：契約增補登記（SC-32；SC-33 依 OD-79 撤回，不登記）
    (1, "contracts/behavior_changes.yaml", r'^  - id: "SC-32"', None, "SC-32（flash 期間 UartIoError／DeviceDetached）"),
    # T2：mcu_patterns registry（已合入整合線）
    (2, f"{CORE}/mcu_patterns.rs", r"pub struct McuPattern \{\s*pub family: String,\s*pub probe: Vec<u8>,", None, "McuPattern 擁有 bytes"),
    (2, f"{CORE}/mcu_patterns.rs", r"pub fn default_registry\b", "default_registry", "McuPatternRegistry"),
    (2, f"{CORE}/mcu_patterns.rs", r"pub fn load\b", "load", "McuPatternRegistry"),
    (2, f"{CORE}/mcu_patterns.rs", r"pub fn get\b", "get", "McuPatternRegistry"),
    (2, f"{CORE}/mcu_patterns.rs", r"pub fn all\b", "all", "McuPatternRegistry"),
    (2, f"{CORE}/mcu_patterns.rs", r"pub fn patterns_rpc\b", "patterns_rpc", "McuPatternRegistry"),
    (2, f"{CORE}/mcu_patterns.rs", r"pub fn hex_to_bytes\b", "hex_to_bytes", "hex 語意"),
    # T3：板端命令樣板與 RX 判定（已合入）
    (3, f"{CORE}/file_transfer/templates.rs", r"pub fn chunk_command\b", "chunk_command", "樣板"),
    (3, f"{CORE}/file_transfer/templates.rs", r"pub fn pull_command\b", "pull_command", "樣板"),
    (3, f"{CORE}/file_transfer/rx_match.rs", r"pub fn extract_between_sentinels\b", "extract_between_sentinels", "RX 判定"),
    # T4：push／pull 引擎（已合入）
    (4, f"{CORE}/file_transfer.rs", r"pub trait TransferPort\b", None, "引擎 port"),
    (4, f"{CORE}/file_transfer.rs", r"pub fn push_file\b", "push_file", "引擎"),
    (4, f"{CORE}/file_transfer.rs", r"pub fn pull_file\b", "pull_file", "引擎"),
    (4, f"{CORE}/file_transfer.rs", r"pub struct EchoPaced\b", None, "引擎"),
    # T5：OpTransferPort 與 TX 聚合
    (5, f"{CORE}/file_transfer/op_port.rs", r"pub struct OpTransferPort\b", None, "OpTransferPort"),
    (5, f"{CORE}/tx_writer.rs", r"pub struct AggregateKey\b", None, "TX 聚合"),
    (5, f"{CORE}/tx_writer.rs", r"pub enum AggregateOp\b", None, "TX 聚合"),
    (5, f"{CORE}/tx_writer.rs", r"pub aggregate: Option<AggregateOp>", None, "TxJob.aggregate"),
    (5, f"{CORE}/tx_writer.rs", r"fn flush_open_aggregates\b", "flush_open_aggregates", "drain 前 flush"),
    # T6：file.push／file.pull session 整合
    (6, CORE_ALL, r"pub struct FileTransferRequest\b", None, "session 請求"),
    (6, CORE_ALL, r"pub enum FileTransferKind\b", None, "session 請求"),
    (6, f"{CORE}/session_core.rs", r"^\s*FileTransfer\(FileTransferRequest\)", None, "SessionRequest::FileTransfer"),
    (6, f"{CORE}/op_runner.rs", r"^\s*FileTransfer\(FileTransferPlan\)", None, "OpSpec::FileTransfer"),
    (6, f"{CORE}/op_runner.rs", r"FileTransfer\(Result<PyDict,\s*TransferAbort>\)", None, "OpOutcome::FileTransfer"),
    (6, f"{CORE}/session_manager.rs", r"pub fn file_transfer\b", "file_transfer", "SessionManager"),
    (6, f"{DAEMON}/service.rs", r'"file\.push" =>', None, "RPC 分派"),
    (6, f"{DAEMON}/service.rs", r'"file\.pull" =>', None, "RPC 分派"),
    # T7：PAL flash 端點能力（已合入）
    (7, f"{PAL}/lib.rs", r"pub flash_endpoint: bool", None, "Capabilities.flash_endpoint"),
    (7, f"{PAL}/console_pty.rs", r"fn clone_writer\(&self\) -> PalResult<Box<dyn ConsoleWriteHalf>>", None, "ConsoleIo"),
    (7, f"{PAL}/console_pty.rs", r"fn wait_readable\(&mut self, _?timeout: Option<Duration>\) -> PalResult<Readiness>", None, "ConsoleIo"),
    (7, f"{PAL}/console_pty.rs", r"fn link\(&self, _?slave_path: &str, _?link(_path)?: &Path\) -> PalResult<\(\)>", None, "ConsoleTransportFactory"),
    (7, f"{PAL}/console_pty.rs", r"fn unlink\(&self, _?link(_path)?: &Path\)", None, "ConsoleTransportFactory"),
    (7, f"{PAL}/console_pty.rs", r"fn tty_setraw\(&self, _?slave: &PtySlaveRef\) -> PalResult<\(\)>", None, "ConsoleTransportFactory"),
    (7, f"{PAL}/console_pty.rs", r"fn tcgetattr_speed\(&self, _?slave: &PtySlaveRef\) -> PalResult<TermSpeed>", None, "ConsoleTransportFactory"),
    (7, f"{PAL}/console_pty.rs", r"pub struct TermSpeed \{\s*pub ispeed: u32,\s*pub ospeed: u32,\s*\}", None, "TermSpeed"),
    (7, f"{PAL}/console_pty.rs", r"pub enum Readiness\b", None, "Readiness"),
    (7, f"{PAL}/traits.rs", r"pub trait SerialLineControl: Send \+ Sync\b", None, "SerialLineControl"),
    (7, f"{PAL}/traits.rs", r"fn line_control\(&self\) -> std::sync::Arc<dyn SerialLineControl>", None, "SerialHandle::line_control"),
    # T8：SessionCore FLASHING 仲裁
    (8, f"{CORE}/tx_writer.rs", r"pub probe_id: Option<u64>", None, "TxPermit.probe_id"),
    (8, CORE_ALL, r"pub type ProbeId = u64;", None, "ProbeId"),
    (8, CORE_ALL, r"pub enum FlashReject\b", None, "FlashReject"),
    (8, CORE_ALL, r"pub struct ProbeGrant\b", None, "ProbeGrant"),
    (8, CORE_ALL, r"pub struct FlashCandidate\b", None, "FlashCandidate"),
    *[
        (8, f"{CORE}/mailbox.rs", rf"^\s*{ev} \{{", None, "ControlEvent 擴充")
        for ev in ("FlashSyncProbeStart", "FlashProbeReleased", "FlashSyncMatched", "FlashHolderGone")
    ],
    (8, f"{CORE}/session_manager.rs", r"pub fn collect_flash_candidates_and_mark\b", "collect_flash_candidates_and_mark", "SessionManager"),
    (8, f"{CORE}/session_manager.rs", r"pub fn unmark_flash_critical\b", "unmark_flash_critical", "SessionManager"),
    (8, f"{CORE}/session_manager.rs", r"pub fn flash_critical\b", "flash_critical", "SessionManager"),
    (8, f"{CORE}/session_core.rs", r"flash_probe_id: Option<ProbeId>", None, "SessionCore 欄位"),
    # T9：FlashEndpoint 與認線協調
    *[
        (9, CORE_ALL, rf"pub (struct|trait|fn) {name}\b", None, "flash_endpoint")
        for name in (
            "ProbeTransport", "detect_mcu_line", "EndpointShutdown", "FlashRunCancel", "FlashCoordinator",
            "FlashPtyIo", "FlashEndpoint", "EndpointStopReceipt", "watch_flasher_disconnect", "pump_endpoint_to_sink",
        )
    ],
    (9, f"{CORE}/session_manager.rs", r"pub fn tx_for\b", "tx_for", "SessionManager"),
    (9, f"{CORE}/session_manager.rs", r"pub fn line_control_for\b", "line_control_for", "SessionManager"),
    (9, f"{CORE}/session_manager.rs", r"pub fn send_control\b", "send_control", "SessionManager"),
    # T10：mcu RPC 與 daemon 組裝
    (10, f"{PROTO}/runtime_paths.rs", r"pub ttymcu_path: String", None, "RuntimePaths.ttymcu_path"),
    (10, f"{DAEMON}/service.rs", r"fn mcu_patterns\(&self\)", None, "Service::mcu_patterns"),
    (10, f"{DAEMON}/service.rs", r"fn mcu_status\(&self\)", None, "Service::mcu_status"),
    (10, f"{DAEMON}/daemon_main.rs", r"FlashEndpoint::start\(", None, "啟動序"),
    # T12：有序停止
    (12, f"{DAEMON}/framing.rs", r"pub fn stop_accept\(&self\)", None, "RpcServer"),
    (12, f"{DAEMON}/framing.rs", r"pub fn join_handlers\(", None, "RpcServer"),
    (12, f"{DAEMON}/daemon_main.rs", r"pub block_flash_endpoint_stop: bool", None, "DaemonHooks"),
    # T14：更新／回滾守衛與交接停止
    (14, f"{CORE}/admission.rs", r"pub struct AdmissionGate\b", None, "AdmissionGate"),
    (14, DAEMON_ALL, r'"daemon\.handoff_stop"|Method::DaemonHandoffStop', None, "daemon.handoff_stop 承接"),
]

# 計畫宣告形狀與實際 producer 已知不同之處：(檔案或 glob, 實際形狀的正規式, 說明與處置)。正規式命中＝差異仍存在。
DIFFERS: list[tuple[str, str, str]] = [
    (f"{CORE}/tx_writer.rs", r"pub fsm_gen: u64,\s*pub class: TxClass,",
     "#1 計畫 `TxPermit{op_id,epoch,owner_gen,class,source,cmd_id,log}`、建構點 `input_epoch`／`fsm_gen` 填 None；"
     "實際另有 `fsm_gen: u64`（非 Option）、`input_epoch: Option<u64>`、`replay_hold: Option<u64>`，無 `probe_id` → "
     "T8 新增 `probe_id: Option<u64>`，所有建構點補 `probe_id: None` 與正確 `fsm_gen`"),
    (f"{CORE}/tx_writer.rs", r"^\s*FsmMismatch,",
     "#2 計畫 `FenceReject::StaleFsmGen`；實名 `FsmMismatch`，另有 `HumanAdmissionClosed`、`InputStale`、`ReplayRevoked`（共 12 個）"
     " → 所有 match 涵蓋 12 個 variant、用實名"),
    (f"{CORE}/tx_writer.rs", r"pub wal: Option<WalAppend>,\s*pub reject: Option<FenceReject>,",
     "#3 計畫 `TxReport{…wal_seq…}`；實際 `wal: Option<WalAppend>`、`reject: Option<FenceReject>` → T5 用實名"),
    (f"{CORE}/tx_writer.rs", r"\(TxClass::FlashProbe, FlashMode::Probe\(_\)\) => return Err\(FenceReject::Flash\)",
     "#4 計畫視 `Fence::admit` 已有 `Probe(id)` 放行；實際 FlashProbe 在 Probe 模式一律 `Err(Flash)`（註解：TxPermit 尚無 probe_id）→ T8 改"),
    (f"{CORE}/session_core.rs", r"pub struct FgAdmission\b",
     "#9 P7 分支上 `fg_begin`／`fg_finish`／`human_suspend_*`／`invalidate_human_input` 為 `todo!` 骨架；整合線上已在 "
     "`session_core/console.rs` 實作（骨架檢查見 SKELETON 列；無列＝非骨架）"),
    (f"{CORE}/mailbox.rs", r"^\s*ConsoleInputBytes \{",
     "#10 計畫 `ConsoleInputBytes` 為 struct；實際為 `DataEvent::ConsoleInputBytes{client_id,client_lifetime,input_epoch,bytes}` "
     "variant；`InputRoute::DropFlash` 在 `human_console/ownership.rs` → T8 用實名"),
    (f"{PAL}/traits.rs", r"pub enum OpenError \{\s*(///[^\n]*\n\s*)*Held\(HolderReport\),",
     "#11 計畫 `OpenError::{Held,AccessDenied,NotFound,Other}`；PAL `OpenError` 只有 `Held`／`AccessDenied{..}`，"
     "`NotFound`／`Other` 在 `mailbox::OpenFailure` → T11 用實名"),
    (f"{CORE}/mailbox.rs", r"pub struct OpenedBridge \{\s*pub epoch: u64,\s*pub worker: IoWorker,\s*pub real_path: String,\s*(///[^\n]*\n\s*)*pub sentinel:",
     "#12 計畫 `OpenedBridge{epoch,worker,real_path}`（brief 另列 vtty）；實際為 `{epoch,worker,real_path,sentinel,console_endpoint,rx}`、"
     "無 `vtty` 欄 → T11 以 `..` 解構、用實名"),
    (f"{CORE}/op_runner.rs", r"ForegroundSlot，pool size 1",
     "#13 §0.1 消費 `ForegroundSlot`；實際無此型別：前景槽＝`OpRunner` 每 epoch 單一 lane thread（`op_runner.rs`）＋"
     "`FenceState.foreground_busy` → T5／T6／T8 以 OpRunner／`foreground_busy` 實作，不另建型別"),
    (f"{CORE}/file_transfer.rs", r"TxRejected \{ rej: TxRejection, chunks: Option<\(usize, usize\)>, transfer_cmd_id: String \}",
     "#14 計畫 T4／T5 的 `TransferAbort::{SerialNotReady, Refused(FenceReject), LocalIo, BadPromptRegex}`；實際（T4 合入，OD-62）為 "
     "`TransferAbort::{TxRejected{rej: TxRejection{reason: RejectReason::{Quiet,Cancelled,StaleGeneration,Draining,Other},…},chunks,"
     "transfer_cmd_id}, LocalIo, BadPromptRegex}` → T5 `OpTransferPort` 依 RejectReason 映射，T6 依此收斂（中途 Quiet 拒絕列 SC-03）"),
    (f"{PAL}/console_pty.rs", r"fn unlink\(&self, _?link(_path)?: &Path\) -> PalResult<\(\)>",
     "#15 計畫 T7 `ConsoleTransportFactory::unlink(&self, link) -> ()`（錯誤忽略）；實際回 `PalResult<()>`，且 "
     "`link`／`unlink`／`tty_setraw`／`tcgetattr_speed`／`clone_writer`／`wait_readable` 皆為預設 `Unsupported` 的 trait 方法 → T9 呼叫端自行忽略錯誤"),
    (f"{PAL}/traits.rs", r"fn mirror_termios_from\([^)]*\)[^{]*\{\s*self\.line_control\(\)\.mirror_termios_from",
     "#16 brief 記 `SerialHandle::mirror_termios_from` 預設 Unsupported；T7 合入後預設改為委派 `line_control()`（`UnsupportedLineControl` 才回 Unsupported）"),
    (f"{CORE}/session_manager.rs", r"pub fn flash_candidates\(&self\) -> Vec<PyDict>",
     "#17 現有 `SessionManager::flash_candidates() -> Vec<PyDict>`（Slice 2.1 唯讀，`mcu.status` 用）；T8 另增 "
     "`collect_flash_candidates_and_mark() -> Vec<FlashCandidate>`（原子標 flash-critical），不取代既有函式"),
    (f"{DAEMON}/service.rs", r'"mcu\.status" => \{\s*// Slice 2\.1 不承接 flash',
     "#18 `mcu.patterns`／`mcu.status` 已在 `Service::dispatch` 內聯（status 恆 `flashing:false`、`last_detect:null`）；"
     "T10 抽成 `fn mcu_patterns`／`fn mcu_status` 並接 FlashEndpoint"),
    (f"{DAEMON}/framing.rs", r"pub fn stop\(mut self, budget: Duration\) -> bool",
     "#19 P5 `RpcServer::stop(self, budget) -> bool`（等 handler）；T12 拆為 `stop_accept`／`join_handlers`"),
    ("crates/sw-daemon/tests/dispatch.rs", r'r\.call\("daemon\.handoff_stop", "\{\}"\), "error_code"\),\s*"METHOD_NOT_FOUND"',
     "#20 brief 記 `daemon.handoff_stop` 在 framing.rs 攔截；實際 framing 只攔 `daemon.stop`，handoff 經分派回 `METHOD_NOT_FOUND`"
     "（`unsupported_catalog_methods` 2.4）→ T14 承接"),
    ("contracts/behavior_changes.yaml", r'^  - id: "SC-03"',
     "#21 計畫的 SC-33（Quiet／Reconfirm 傳輸中途拒絕）依擁有者 OD-79 撤回、不登記；原列 SC-33 的案例改列 SC-03。"
     "DF-25 已用（OD-77 B1）、DF-26 保留給 OD-80（f7-larger-file-not-truncated）、SC-38 不得佔用"),
    (f"{CORE}/mailbox.rs", r"^\s*CloseUnconfirmed\(Box<OpenFailure>\),\s*(///[^\n]*\n\s*)*CloseBlocked,",
     "#22 整合線 wp510 在 `OpenFailure::{Held,AccessDenied,NotFound,Other}` 之後新增 `CloseUnconfirmed(Box<OpenFailure>)`"
     "（正式 bridge 接手前關閉未確認）與 `CloseBlocked`（開埠 gate 拒絕）；合併 P8 線時 `p8_upstream_interfaces.rs` 的窮舉 match 補上兩者"),
]

def _shape(pat: str) -> str:
    """欄位形狀（`X \\{ a: T, b: U \\}`）容忍 rustfmt 換行與尾逗號：空白→`\\s*`、`\\}` 前允許 `,`。"""
    if r"\{ " not in pat:
        return pat
    return pat.replace(r" \}", r",?\s*\}").replace(", ", r",\s*").replace(r"\{ ", r"\{\s*")


_FN_TODO = re.compile(r"\b(s21_todo!|todo!|unimplemented!)\s*\(")


def _fn_is_skeleton(text: str, name: str) -> bool | None:
    """找 `fn name` 第一個定義，取其本體（到第一個同縮排 `}`）；本體含 todo 類巨集即骨架。"""
    m = re.search(rf"^(\s*)(?:pub(?:\([^)]*\))?\s+)?fn {re.escape(name)}\b", text, re.M)
    if m is None:
        return None
    indent = m.group(1)
    rest = text[m.end():]
    end = re.search(rf"^{indent}\}}", rest, re.M)
    body = rest[: end.start()] if end else rest[:400]
    return _FN_TODO.search(body) is not None


def _texts(root: pathlib.Path, rel: str) -> list[str]:
    """`rel` 為檔案或 glob（含 `*`）；回傳存在的檔案內容（glob 依路徑排序）。"""
    if "*" in rel:
        return [p.read_text(encoding="utf-8") for p in sorted(root.glob(rel)) if p.is_file()]
    path = root / rel
    return [path.read_text(encoding="utf-8")] if path.is_file() else []


def _locate(root: pathlib.Path, rel: str, pat: str) -> str | None:
    """回傳第一個命中 `pat` 的檔案內容；檔案不存在或未命中回 None（空正規式＝只要求檔案存在）。"""
    for text in _texts(root, rel):
        if not pat or re.search(_shape(pat), text, re.M):
            return text
    return None


def check(root: pathlib.Path):
    """回傳 (missing, skeleton, differs, planned)。

    missing：上游缺漏 (rel, pat, src, task)；skeleton：(rel, fn, src, task)；differs：(rel, note)；
    planned：(task, rel, pat, note, present: bool)。PLANNED 已存在者另做骨架檢查（列入 skeleton，src＝`P8 Tn`）。
    """
    missing, skeleton = [], []
    for rel, pat, src, task, fn in CHECKS:
        text = _locate(root, rel, pat)
        if text is None:
            missing.append((rel, pat or "<檔案存在>", src, task))
            continue
        if fn and _fn_is_skeleton(text, fn):
            skeleton.append((rel, fn, src, task))
    planned = []
    for task, rel, pat, fn, note in PLANNED:
        text = _locate(root, rel, pat)
        planned.append((task, rel, pat, note, text is not None))
        if text is not None and fn and _fn_is_skeleton(text, fn):
            skeleton.append((rel, fn, f"P8 T{task}", task + 1))
    differs = []
    for rel, pat, note in DIFFERS:
        if any(re.search(pat, text, re.M) for text in _texts(root, rel)):
            differs.append((rel, note))
    return missing, skeleton, differs, planned


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=pathlib.Path, default=ROOT)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--max-task", type=int, default=None,
                      help="開工 Task N 前的局部檢查：上游最早消費 Task ≤ N 與 PLANNED:Tn（n < N）的缺漏才阻擋（非全表結果）")
    mode.add_argument("--strict", action="store_true",
                      help="切片出口：上游與全部 PLANNED 皆須存在，缺任何一項 exit 1")
    a = ap.parse_args(argv)
    missing, skeleton, differs, planned = check(a.root)
    limit = a.max_task

    blocking = [m for m in missing if limit is None or m[3] <= limit]
    later = [m for m in missing if m not in blocking]
    p_missing = [p for p in planned if not p[4]]
    if a.strict:
        p_blocking = p_missing
    elif limit is not None:
        p_blocking = [p for p in p_missing if p[0] < limit]
    else:
        p_blocking = []
    p_pending = [p for p in p_missing if p not in p_blocking]

    print(f"[check_p8_prereqs] 上游 {len(CHECKS)} 項：缺漏 {len(missing)}、骨架 {len(skeleton)}、形狀不同 {len(differs)}；"
          f"P8 產物 {len(PLANNED)} 項：已合入 {len(planned) - len(p_missing)}、待合入 {len(p_missing)}")
    for rel, pat, src, task in blocking:
        print(f"MISSING (阻擋) {rel} :: {pat}（上游 {src}；P8 Task {task} 需要）")
    for task, rel, pat, note, _ok in p_blocking:
        need = "切片出口" if a.strict else f"P8 Task {limit} 的前置"
        print(f"MISSING (阻擋) {rel} :: {pat}（P8 T{task} 產物「{note}」尚未合入；{need}）")
    for rel, pat, src, task in later:
        print(f"MISSING (後續) {rel} :: {pat}（上游 {src}；P8 Task {task} 需要，不擋 --max-task {limit}）")
    for rel, fn, src, task in sorted(skeleton, key=lambda r: (r[3], r[0], r[1])):
        print(f"SKELETON {rel} :: fn {fn}（{src} 尚未實作；P8 Task {task} 起需要其行為）")
    for rel, note in differs:
        print(f"DIFFERS {rel} :: {note}")
    for task, rel, pat, note, ok in sorted(planned, key=lambda r: r[0]):
        if ok:
            print(f"LANDED:T{task} {rel} :: {pat}（{note}）")
        elif (task, rel, pat, note, ok) in p_pending:
            print(f"PLANNED:T{task} {rel} :: {pat}（{note}；P8 Task {task} 新增，目前缺為預期）")

    rc = 0
    if blocking or p_blocking:
        print("[check_p8_prereqs] FAIL：上游缺陷或前置 P8 Task 未合入，停止並回報（不在 P8 代補、不以替身代替）")
        rc = 1
    if limit is not None:
        # 末行固定格式：局部檢查不得以 OK 結尾（#180 旁路防護）。
        print(f"[check_p8_prereqs] 仍有 {len(later) + len(p_pending)} 項後續缺漏"
              f"（--max-task {limit} 非全表結果，不得作為切片出口依據）")
    elif not rc:
        print("[check_p8_prereqs] OK（" + ("全表＋全部 PLANNED" if a.strict else "上游全表；PLANNED 待合入者只列出") + "）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
