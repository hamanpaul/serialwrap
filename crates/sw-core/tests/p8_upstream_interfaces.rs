//! P8（Slice 2.4）上游介面編譯期核對（計畫 09 Task 0 Step 1；§0.1 前置交付表）。
//!
//! 只引用 P3～P7 已交付、P8 各 Task 要消費的 Rust 符號，以 `use`、fn pointer 型別斷言與解構樣式核對
//! 名稱與形狀；任一符號缺漏或簽章改變即編譯失敗，表示上游前置未到位（停止並回報，不在 P8 代補、不以替身代替）。
//!
//! 規則：
//! - **不引用 P8 自己要新增的符號**（`TxPermit.probe_id`、`OpSpec::FileTransfer`、`ControlEvent::Flash*`、
//!   `AggregateKey`、`FlashEndpoint`、`AdmissionGate`…）；這些列在 `scripts/rust/check_p8_prereqs.py` 的 `PLANNED:Tn`。
//!   已合入整合線的 P8 Task（T2／T3／T4／T7）的 Produces 同樣屬 P8 自身產物，不在本檔核對（由 `check_p8_prereqs.py` 以一般列追蹤）。
//! - P8 之後的 Task 會擴充的 struct／enum（`TxJob`、`TxPermit`、`OpSpec`、`SessionRequest`、`ControlEvent`）一律以 `..`
//!   或 `_ =>` 容許追加；P8 不擴充的 enum（`TxClass`、`FlashMode`、`FenceReject`、`OpenFailure`、`WaitOutcome`）以
//!   無萬用分支的 match 鎖住完整 variant 集合（計畫「計畫與實際不符」#2：FenceReject 共 12 個 variant、用實名）。
//! - sw-cli（`graceful_stop`、`rollback::safety_check`）與 sw-daemon（`ServiceRig`）不是 sw-core 的依賴，
//!   改由 `check_p8_prereqs.py` 以檔案正規式核對（依賴方向不得反轉，`check_dep_direction.py`）。
//! - 測試支援 `support::{CoreHarness, ActorRig}` 依賴 `test-fakes` feature 才編得過，而本檔須在
//!   `cargo test -p sw-core --test p8_upstream_interfaces`（不帶 feature）下編譯，故同樣改由 `check_p8_prereqs.py` 核對。
#![allow(unused_imports, dead_code, unused_variables, clippy::type_complexity)]


use std::sync::Arc;
use std::time::{Duration, Instant};

use crossbeam_channel::Sender;
use sw_core::arbiter::CommandRegistry;
use sw_core::deferred::InvalidateReason;
use sw_core::event_engine::dispatcher::DispatcherStopReport;
use sw_core::event_engine::EventEngine;
use sw_core::human_console::ownership::InputRoute;
use sw_core::mailbox::{ControlEvent, DataEvent, MailboxItem, OpenFailure, OpenedBridge, Reply};
use sw_core::op_runner::{OpCtx, OpOutcome, OpRunner, OpSpec};
use sw_core::quiet_gate::{execute_gate_reject, submit_gate_reject, QuietState};
use sw_core::session_core::{Effect, Effects, FgAdmission, OpGrant, SessionCore, SessionRequest};
use sw_core::session_manager::SessionManager;
use sw_core::tx_writer::{
    Fence, FenceReject, FenceState, FlashMode, OpId, TxClass, TxJob, TxPermit, TxReport, TxWriterHandle,
};
use sw_core::uart_bridge::{IoWorker, IoWorkerArgs, RunCancel, RxMatch, RxObserver, RxShared, WaitOutcome};
use sw_core::wal::{WalAppend, WalWriter};
use sw_core::Mono;
use sw_pal::console_pty::{ConsoleIo, ConsoleTransportFactory, KeepAlive, PtyPair, PtySlaveRef};
use sw_pal::fs_ops::FsOps;
use sw_pal::service_ctl::{
    render_task_xml, ServiceAction, ServiceCtl, ServiceResult, SupervisionMode, TaskXmlSpec, WindowsTaskCtl,
};
use sw_pal::traits::{
    Clock, HolderReport, IoWaker, OpenError, PalError, PalResult, SerialBackend, SerialHandle, SysEnv, TxResult,
    UartExclusive,
};
use sw_pal::Platform;
use sw_proto::config_types::SessionProfile;
use sw_proto::pycompat::{
    a2b_base64_lenient, b2a_base64, decode_utf8_replace, py_int_parse, py_len_chars, py_truthy, shlex_quote,
    shlex_split, splitlines, strip_ansi, uuid4_hex, B64Error, PyErr, ShlexError,
};
use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::pyre::PyRegex;

/// P3／P5 06 T0c：`sw_proto::pycompat`（file transfer 樣板與 RX 判定的 Python 等價函式）。
fn pycompat_signatures() {
    let _: fn(&str) -> String = shlex_quote;
    let _: fn(&str) -> Result<Vec<String>, ShlexError> = shlex_split;
    let _: fn(&[u8]) -> String = b2a_base64;
    let _: fn(&[u8]) -> Result<Vec<u8>, B64Error> = a2b_base64_lenient;
    let _: fn(&str) -> String = strip_ansi;
    let _: fn(&mut dyn FnMut(&mut [u8])) -> String = uuid4_hex;
    let _: fn(&str) -> Vec<&str> = splitlines;
    let _: fn(&str) -> usize = py_len_chars;
    let _: fn(&str) -> Option<PyInt> = py_int_parse;
    let _: fn(&PyValue) -> bool = py_truthy;
    let _: fn(&[u8]) -> String = decode_utf8_replace;
}

/// P5 06 T9：TxClass 12 個 variant（P8 只消費 Flash／FlashProbe／FileTransfer，不新增）。
fn tx_class_variants(c: TxClass) -> &'static str {
    match c {
        TxClass::Flash => "flash",
        TxClass::FlashProbe => "flash_probe",
        TxClass::LeaseOwner => "lease_owner",
        TxClass::HumanRaw => "human_raw",
        TxClass::HumanReplay => "human_replay",
        TxClass::LoginProbe => "login_probe",
        TxClass::ReadyReconfirm => "ready_reconfirm",
        TxClass::Command => "command",
        TxClass::FileTransfer => "file_transfer",
        TxClass::SystemRecoverKey => "system_recover_key",
        TxClass::SelfTestProbe => "self_test_probe",
        TxClass::InteractiveOpenCmd => "interactive_open_cmd",
    }
}

/// P5 06 T9：`FlashMode::{Off, Probe(u64), Flashing}`。
fn flash_mode_variants(m: FlashMode) -> Option<u64> {
    match m {
        FlashMode::Off => None,
        FlashMode::Probe(id) => Some(id),
        FlashMode::Flashing => None,
    }
}

/// P5 06 T9＋P6 07 D-03／D-22：FenceReject 共 12 個 variant；計畫的 `StaleFsmGen` 實名為 `FsmMismatch`（DIFFERS #2）。
fn fence_reject_variants(r: FenceReject) -> &'static str {
    match r {
        FenceReject::StaleEpoch => "stale_epoch",
        FenceReject::Frozen => "frozen",
        FenceReject::Quiet => "quiet",
        FenceReject::ReconfirmPending => "reconfirm_pending",
        FenceReject::WalUnhealthy => "wal_unhealthy",
        FenceReject::Flash => "flash",
        FenceReject::OwnerMismatch => "owner_mismatch",
        FenceReject::FsmMismatch => "fsm_mismatch",
        FenceReject::Cancelled => "cancelled",
        FenceReject::HumanAdmissionClosed => "human_admission_closed",
        FenceReject::InputStale => "input_stale",
        FenceReject::ReplayRevoked => "replay_revoked",
    }
}

/// `TxPermit` 既有欄位（DIFFERS #1：`fsm_gen: u64` 非 Option、另有 `input_epoch`／`replay_hold`；`probe_id` 由 T8 新增，以 `..` 容許）。
fn tx_permit_fields(p: TxPermit) {
    let TxPermit { op_id, epoch, owner_gen, fsm_gen, class, source, cmd_id, log, input_epoch, replay_hold, .. } = p;
    let _: OpId = op_id;
    let _: u64 = epoch;
    let _: u64 = owner_gen;
    let _: u64 = fsm_gen;
    let _: TxClass = class;
    let _: String = source;
    let _: Option<String> = cmd_id;
    let _: bool = log;
    let _: Option<u64> = input_epoch;
    let _: Option<u64> = replay_hold;
}

/// `TxJob{permit, payload, reply}`（T5 追加 `aggregate`，以 `..` 容許）與 `TxReport{op_id, epoch, result, wal, reject}`（DIFFERS #3）。
fn tx_job_and_report(job: TxJob, rep: TxReport) {
    let TxJob { permit, payload, reply, .. } = job;
    let _: TxPermit = permit;
    let _: Vec<u8> = payload;
    let _: Sender<TxReport> = reply;
    let TxReport { op_id, epoch, result, wal, reject } = rep;
    let _: OpId = op_id;
    let _: u64 = epoch;
    let _: TxResult = result;
    let _: Option<WalAppend> = wal;
    let _: Option<FenceReject> = reject;
}

/// `Fence::{with, admit, revoke_ops}`（admit 為關聯函式：`Fence::admit(&FenceState, &TxPermit)`）。
fn fence_api(f: &Fence, p: &TxPermit) {
    let _: fn(&FenceState, &TxPermit) -> Result<(), FenceReject> = Fence::admit;
    let _: fn(&Fence, &[OpId]) = Fence::revoke_ops;
    let mode: FlashMode = f.with(|st| st.flash_mode);
    let _ = f.with(|st| Fence::admit(st, p));
    let _: fn() -> Arc<Fence> = Fence::new;
}

/// P5 06 T10＋P7 08 T3：`RxShared` 讀取／等待、`RunCancel`、`WaitOutcome`、`RxObserver`、`IoWorker::start_observed`。
fn rx_shared_api() {
    let _: fn(&RxShared) -> u64 = RxShared::snapshot_len;
    let _: fn(&RxShared, u64) -> String = RxShared::text_from;
    let _: fn(&RxShared, &PyRegex, u64, Duration, &dyn Clock) -> Option<RxMatch> = RxShared::wait_for_regex_from;
    let _: fn(&RxShared, &PyRegex, Option<u64>, Duration, &dyn Clock, &RunCancel) -> WaitOutcome =
        RxShared::wait_for_regex_cancellable;
    let _: fn(&Arc<RxShared>) -> RunCancel = RunCancel::new;
    let _: fn(Box<dyn SerialHandle>, IoWorkerArgs, Vec<Arc<dyn RxObserver>>) -> IoWorker = IoWorker::start_observed;
}

fn wait_outcome_variants(w: WaitOutcome) -> u8 {
    match w {
        WaitOutcome::Matched => 0,
        WaitOutcome::TimedOut => 1,
        WaitOutcome::Cancelled => 2,
    }
}

fn rx_observer_call(o: &dyn RxObserver) {
    o.on_rx("COM0", b"", 0);
}

/// P5 06 T14：`OpCtx` 欄位、`OpRunner::{spawn, run}`；`OpSpec` 目前 7 個 variant（無 FileTransfer，T6 新增 → `_ =>`）。
fn op_runner_api(ctx: OpCtx) {
    let OpCtx { com, sid, profile, rx, tx, wal, clock, control, rng, registry, .. } = ctx;
    let _: String = com;
    let _: String = sid;
    let _: SessionProfile = profile;
    let _: Arc<RxShared> = rx;
    let _: TxWriterHandle = tx;
    let _: Arc<WalWriter> = wal;
    let _: Arc<dyn Clock> = clock;
    let _: Sender<ControlEvent> = control;
    let _: Arc<dyn Fn(&mut [u8]) + Send + Sync> = rng;
    let _: Arc<CommandRegistry> = registry;
    let _: fn(OpCtx) -> OpRunner = OpRunner::spawn;
    let _: fn(&OpRunner, OpGrant, OpSpec) = OpRunner::run;
}

fn op_spec_known_variants(s: &OpSpec) -> &'static str {
    match s {
        OpSpec::Command(_) => "command",
        OpSpec::Recover { .. } => "recover",
        OpSpec::SelfTestProbe { .. } => "self_test_probe",
        OpSpec::InteractiveOpenCmd { .. } => "interactive_open_cmd",
        OpSpec::InteractiveSend { .. } => "interactive_send",
        OpSpec::RebootRecovery { .. } => "reboot_recovery",
        OpSpec::DetectTemplate { .. } => "detect_template",
        #[allow(unreachable_patterns)]
        _ => "p8-later",
    }
}

fn op_outcome_type(o: OpOutcome) -> OpOutcome {
    o
}

/// P5 06 T11：`OpGrant{op_id, epoch, owner_gen, fsm_gen, cmd_id}`、`Effects(pub Vec<Effect>)`、`SessionRequest`。
fn session_core_types(g: OpGrant, fx: Effects, req: SessionRequest) {
    let OpGrant { op_id, epoch, owner_gen, fsm_gen, cmd_id, .. } = g;
    let _: OpId = op_id;
    let _: u64 = epoch;
    let _: u64 = owner_gen;
    let _: u64 = fsm_gen;
    let _: Option<String> = cmd_id;
    let Effects(list) = fx;
    let _: Vec<Effect> = list;
    let _: SessionRequest = req;
}

/// P5 06 T11＋P6 07 T6（整合線上為實作，非 `todo!`）：`on_event`、前景／human suspend、BC-02 唯一入口。
fn session_core_methods() {
    let _: fn(&mut SessionCore, MailboxItem, Mono) -> Effects = SessionCore::on_event;
    let _: fn(&mut SessionCore, bool, &mut Effects) -> FgAdmission = SessionCore::fg_begin;
    let _: fn(&mut SessionCore, FgAdmission, &mut Effects) = SessionCore::fg_finish;
    let _: fn(&mut SessionCore, &mut Effects) -> bool = SessionCore::human_suspend_begin;
    let _: fn(&mut SessionCore, bool, &mut Effects) = SessionCore::human_suspend_end;
    let _: fn(&mut SessionCore, InvalidateReason, &mut Effects) = SessionCore::invalidate_human_input;
    let _ = InvalidateReason::Flash;
}

/// P5／P6 mailbox：`DataEvent::ConsoleInputBytes` 為 variant（DIFFERS #10）、`Reply` 型別、`OpenedBridge` 欄位、
/// `OpenFailure::{Held, AccessDenied, NotFound, Other}`（DIFFERS #11：PAL `OpenError` 只有 Held／AccessDenied）。
fn mailbox_types(ev: DataEvent, reply: Reply, ob: OpenedBridge, of: OpenFailure, item: MailboxItem, ctl: ControlEvent) {
    match ev {
        DataEvent::ConsoleInputBytes { client_id, client_lifetime, input_epoch, bytes } => {
            let _: String = client_id;
            let _: u64 = client_lifetime;
            let _: u64 = input_epoch;
            let _: Vec<u8> = bytes;
        }
        _ => {}
    }
    let _: Sender<Result<PyDict, PyErr>> = reply;
    let OpenedBridge { epoch, worker, real_path, rx, .. } = ob;
    let _: u64 = epoch;
    let _: IoWorker = worker;
    let _: String = real_path;
    let _: Arc<RxShared> = rx;
    match of {
        OpenFailure::Held(r) => {
            let _: HolderReport = r;
        }
        OpenFailure::AccessDenied(r) => {
            let _: HolderReport = r;
        }
        OpenFailure::NotFound => {}
        OpenFailure::Other(e) => {
            let _: PalError = e;
        }
    }
    let _: MailboxItem = item;
    let _: ControlEvent = ctl;
}

fn pal_open_error(e: OpenError) {
    match e {
        OpenError::Held(r) => {
            let _: HolderReport = r;
        }
        OpenError::AccessDenied { .. } => {}
        #[allow(unreachable_patterns)]
        _ => {}
    }
}

/// P6 human console：`InputRoute::DropFlash`（`human_console/ownership.rs`）。
fn input_route_drop_flash() -> InputRoute {
    InputRoute::DropFlash
}

/// P7 08：`submit_gate_reject(public_session, source)`（RPC 層）與 `execute_gate_reject(q, com, now)`（manager 層）。
fn quiet_gate_api() {
    let _: fn(&PyDict, &str) -> Option<PyDict> = submit_gate_reject;
    let _: fn(&mut QuietState, &str, Mono) -> Option<PyDict> = execute_gate_reject;
}

/// P7 08 T15：`EventEngine::{close_intake, stop(deadline)}`；P5 06 T16：`SessionManager` 停止序與查詢。
fn engine_and_manager_api() {
    let _: fn(&EventEngine) = EventEngine::close_intake;
    let _: fn(&EventEngine, Instant) -> DispatcherStopReport = EventEngine::stop;
    let _: fn(&SessionManager) = SessionManager::begin_shutdown;
    let _: fn(&SessionManager, Duration) -> Vec<(String, bool)> = SessionManager::shutdown;
    let _: fn(&SessionManager, &str, SessionRequest, Duration) -> Result<PyDict, PyErr> = SessionManager::route;
    let _: fn(&SessionManager) -> Vec<PyDict> = SessionManager::list_sessions;
    let _: fn(&SessionManager) -> Vec<PyDict> = SessionManager::list_devices;
    let _: fn(&SessionManager) -> Vec<PyDict> = SessionManager::flash_candidates;
}

/// P6 07：`PtyPair{master, slave_keepalive, slave_path, slave_ref}`、`PtySlaveRef{path}`、`create_pty`、`ConsoleIo` 既有方法。
fn pal_console_api(pair: PtyPair, f: &dyn ConsoleTransportFactory) {
    let PtyPair { master, slave_keepalive, slave_path, slave_ref } = pair;
    let _: Box<dyn ConsoleIo> = master;
    let _: KeepAlive = slave_keepalive;
    let _: String = slave_path;
    let PtySlaveRef { path } = slave_ref;
    let _: String = path;
    let _: PalResult<PtyPair> = f.create_pty();
}

fn console_io_methods(io: &mut dyn ConsoleIo) {
    let mut buf = [0u8; 8];
    let _ = io.read(&mut buf, Some(Duration::from_millis(1)));
    let _: PalResult<usize> = io.try_write(b"");
    let _: Arc<dyn IoWaker> = io.waker();
    let _: bool = io.has_external_peer(None);
    let _ = io.mux_handle();
}

fn console_io_close(io: Box<dyn ConsoleIo>) {
    io.close();
}

/// P5 06：`SerialHandle::{split, set_baud, mirror_termios_from, is_alive}`、`UartExclusive::probe_holders`、`IoWaker::wake`。
fn pal_serial_api(h: Box<dyn SerialHandle>, x: &dyn UartExclusive, w: &dyn IoWaker, slave: &PtySlaveRef) {
    let _: PalResult<()> = h.set_baud(115_200);
    let _: PalResult<()> = h.mirror_termios_from(slave, Some(115_200));
    let _: bool = h.is_alive();
    let (_reader, _writer, _waker, _close) = h.split();
    let _: HolderReport = x.probe_holders("/dev/null");
    w.wake();
}

/// P4／P5：`Platform{console, uart_excl, serial, service, …}`、`ServiceCtl::action_named`、`render_task_xml`、`WindowsTaskCtl::new`。
fn pal_platform_api(p: &Platform) {
    let _: &Arc<dyn ConsoleTransportFactory> = &p.console;
    let _: &Arc<dyn UartExclusive> = &p.uart_excl;
    let _: &Arc<dyn SerialBackend> = &p.serial;
    let _: &Arc<dyn ServiceCtl> = &p.service;
    let _: &Arc<dyn Clock> = &p.clock;
    let _: fn(&TaskXmlSpec) -> String = render_task_xml;
    let _: fn(Arc<dyn SysEnv>, Arc<dyn FsOps>, Arc<dyn Clock>) -> WindowsTaskCtl = WindowsTaskCtl::new;
}

fn service_action_named(s: &dyn ServiceCtl, mode: &SupervisionMode, action: ServiceAction) -> ServiceResult {
    s.action_named(mode, "serialwrap-throwaway-p8", action, false)
}

#[test]
fn compiles() {
    // 本檔的核對全在編譯期；執行期只確認幾個純函式的值，避免整檔被當成空測試。
    assert_eq!(tx_class_variants(TxClass::FileTransfer), "file_transfer");
    assert_eq!(flash_mode_variants(FlashMode::Probe(7)), Some(7));
    assert_eq!(fence_reject_variants(FenceReject::FsmMismatch), "fsm_mismatch");
    assert_eq!(wait_outcome_variants(WaitOutcome::Cancelled), 2);
}
