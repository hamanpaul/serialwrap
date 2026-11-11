//! P5-T11：`SessionActor` 執行緒與事件錯誤隔離。
//!
//! 每個 session 一條 thread（名稱 `serialwrap-session-<COM>`），以 `Mailbox::recv_next` 取事件、
//! `catch_unwind(core.on_event(..))` 隔離 panic，效果交 `EffectExecutor` 執行；1 s `Tick`。
//! I/O 效果交給注入的 executor；定時事件與單調時鐘 tick 由 actor 排程。

use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use crossbeam_channel::Sender;
#[cfg(feature = "test-fakes")]
use crossbeam_channel::{bounded, Receiver};
use sw_pal::traits::Clock;
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::PyDict;

use crate::mailbox::{ControlEvent, DataEvent, Mailbox, MailboxItem, TimerEvent};
use crate::session_core::{Effect, SessionCore, SessionRequest};

/// actor 的外部控制代碼（RPC 分派與測試共用）。
pub struct ActorHandle {
    pub control: Sender<ControlEvent>,
    pub safety: Sender<ControlEvent>,
    pub data: Sender<DataEvent>,
    pub join: JoinHandle<()>,
    /// 測試用（Task 12b）：事件延遲探針（`resource_limits.event_latency.l1_bound`）。
    #[cfg(feature = "test-fakes")]
    pub probe: Arc<ActorLatencyProbe>,
    /// 測試用：觀察 mailbox data lane 積壓，不取走事件。
    #[cfg(feature = "test-fakes")]
    pub data_observer: Receiver<DataEvent>,
    /// 測試用：觀察 safety lane 積壓，不取走事件。
    #[cfg(feature = "test-fakes")]
    pub safety_observer: Receiver<ControlEvent>,
}

/// 測試用（Task 12b）：記錄非 data 事件「在 lane 中等待期間」actor 又處理了幾個 data item。
///
/// 方法審閱 [High]（2026-10-29）後改為逐事件計數，不再共用一個會被其他 control 事件歸零的計數：
/// - mailbox 事件（control／safety lane、`stopped` 中的 receipt）：取 `Mailbox::take_control_witness()`——事件送入
///   mailbox 後、被取出前 mailbox 回傳的 data item 數；依賴暫存事件不追蹤（記 0）。
/// - actor 內部 timer／tick：到期時刻之後才開始處理的 data item 數（以 data 開始處理的單調時刻計）。
/// - OD-64（2026-10-29）：另記 `Mailbox::take_control_pre_dependency_data()`——receipt 因同 epoch 依賴移入 `stopped`
///   時已讀入 mailbox 的同 epoch data 筆數；drain_receipt 的界限為「見證數 ≤ 此數＋1」，其餘事件此數為 0（界限 1）。
#[cfg(feature = "test-fakes")]
#[derive(Default)]
pub struct ActorLatencyProbe {
    inner: std::sync::Mutex<ProbeInner>,
}

#[cfg(feature = "test-fakes")]
#[derive(Default)]
struct ProbeInner {
    data_items: u64,
    /// 最近處理的 data item 之開始時刻（單調 ns；上限 `PROBE_LOG_MAX`），timer／tick 見證數用。
    data_started: std::collections::VecDeque<u64>,
    log: std::collections::VecDeque<ProbeEntry>,
    pause: Option<ProbePause>,
}

#[cfg(feature = "test-fakes")]
struct ProbePause {
    label: &'static str,
    entered: Sender<()>,
    resume: Receiver<()>,
}

/// 探針紀錄上限：超過即丟最舊（長跑的 test-fakes daemon 不無限累積；審查 F49）。
#[cfg(feature = "test-fakes")]
pub const PROBE_LOG_MAX: usize = 4096;

/// 單一非 data 事件的延遲紀錄。
#[cfg(feature = "test-fakes")]
#[derive(Clone, Debug)]
pub struct ProbeEntry {
    pub label: &'static str,
    /// 此事件等待期間 actor 處理的 data item 數。
    pub data_while_waiting: u64,
    /// 處理此事件時 data lane 的積壓筆數。
    pub data_backlog: usize,
    /// OD-64：此事件依賴等待開始前已讀入 mailbox 的同 epoch data 筆數（無依賴等待者為 0）。
    pub pre_dependency_data: u64,
}

#[cfg(feature = "test-fakes")]
impl ActorLatencyProbe {
    /// 測試用：暫停下一筆指定標籤事件的 dispatch，回傳進入與繼續通道。
    pub fn pause_before_next(&self, label: &'static str) -> (Receiver<()>, Sender<()>) {
        let (entered_tx, entered_rx) = bounded(1);
        let (resume_tx, resume_rx) = bounded(1);
        self.inner.lock().unwrap_or_else(|e| e.into_inner()).pause = Some(ProbePause {
            label,
            entered: entered_tx,
            resume: resume_rx,
        });
        (entered_rx, resume_tx)
    }

    /// 累計處理的 data item 數。
    pub fn data_items(&self) -> u64 {
        self.inner.lock().unwrap_or_else(|e| e.into_inner()).data_items
    }

    /// 指定標籤的紀錄（`Release`、`CancelQueued`、`LeaseExpiry`、`Shutdown`、`WorkerStopped`、`Request`、`Timer`、`Tick`、`Control`）。
    pub fn entries(&self, label: &str) -> Vec<ProbeEntry> {
        self.inner
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .log
            .iter()
            .filter(|e| e.label == label)
            .cloned()
            .collect()
    }

    fn after_data(&self, started_ns: u64) {
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        g.data_items += 1;
        if g.data_started.len() >= PROBE_LOG_MAX {
            g.data_started.pop_front();
        }
        g.data_started.push_back(started_ns);
    }

    /// actor 內部 timer／tick：到期時刻 `due_ns` 之後才開始處理的 data item 數。
    fn data_since(&self, due_ns: u64) -> u64 {
        let g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        g.data_started.iter().filter(|at| **at >= due_ns).count() as u64
    }

    fn before_other(&self, label: &'static str, data_backlog: usize, data_while_waiting: u64, pre_dependency_data: u64) {
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if g.log.len() >= PROBE_LOG_MAX {
            g.log.pop_front();
        }
        g.log.push_back(ProbeEntry {
            label,
            data_while_waiting,
            data_backlog,
            pre_dependency_data,
        });
    }

    fn pause_before_dispatch(&self, label: &'static str) {
        let pause = {
            let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
            if inner
                .pause
                .as_ref()
                .is_some_and(|pause| pause.label == label)
            {
                inner.pause.take()
            } else {
                None
            }
        };
        if let Some(pause) = pause {
            let _ = pause.entered.send(());
            let _ = pause.resume.recv();
        }
    }
}

#[cfg(all(test, feature = "test-fakes"))]
mod probe_tests {
    use super::*;

    #[test]
    fn probe_log_is_bounded_and_keeps_newest() {
        let probe = ActorLatencyProbe::default();
        for _ in 0..PROBE_LOG_MAX + 10 {
            probe.before_other("Tick", 0, 0, 0);
        }
        probe.before_other("Release", 7, 0, 0);
        assert_eq!(probe.entries("Tick").len(), PROBE_LOG_MAX - 1);
        assert_eq!(probe.entries("Release")[0].data_backlog, 7);
    }
}

#[cfg(feature = "test-fakes")]
fn probe_label(item: &MailboxItem) -> Option<&'static str> {
    match item {
        MailboxItem::Data(_) => None,
        MailboxItem::Control(ControlEvent::Request { req, .. }) => Some(match req {
            SessionRequest::Release { .. } => "Release",
            SessionRequest::CancelQueued { .. } => "CancelQueued",
            _ => "Request",
        }),
        MailboxItem::Control(ControlEvent::Timer(ev)) => Some(timer_label(ev)),
        MailboxItem::Control(ControlEvent::Tick) => Some("Tick"),
        // OD-45：stop_request 的 L1 結構 gate（`at_most_one_data_item_before_control`）；drain_receipt 依 OD-64 為
        // `at_most_pre_dependency_data_plus_one`（見 `ProbeEntry::pre_dependency_data`）。
        MailboxItem::Control(ControlEvent::Shutdown { .. }) => Some("Shutdown"),
        MailboxItem::Control(ControlEvent::WorkerStopped(_)) => Some("WorkerStopped"),
        #[cfg(feature = "test-fakes")]
        MailboxItem::Control(ControlEvent::ArmPanic { .. }) => Some("ArmPanic"),
        MailboxItem::Control(_) => Some("Control"),
    }
}

#[cfg(feature = "test-fakes")]
fn probe_pause_label(item: &MailboxItem) -> Option<&'static str> {
    match item {
        MailboxItem::Control(ControlEvent::LoginFinished { .. }) => Some("LoginFinished"),
        _ => probe_label(item),
    }
}

#[cfg(feature = "test-fakes")]
fn timer_label(ev: &TimerEvent) -> &'static str {
    match ev {
        TimerEvent::LeaseExpiry { .. } => "LeaseExpiry",
        _ => "Timer",
    }
}

impl ActorHandle {
    /// 送出請求並等待回覆；逾時回 `PyErr("internal timeout")`；actor 已停止回 `PyErr("session actor stopped")`。
    pub fn request(&self, req: SessionRequest, timeout: Duration) -> Result<PyDict, PyErr> {
        let deadline = Instant::now() + timeout;
        let (reply, rx) = crossbeam_channel::bounded(1);
        let sender = if matches!(&req, SessionRequest::Release { .. }) {
            &self.safety
        } else {
            &self.control
        };
        match sender.send_timeout(ControlEvent::Request { req, reply }, timeout) {
            Ok(()) => {}
            Err(crossbeam_channel::SendTimeoutError::Timeout(_)) => {
                return Err(PyErr::value_error("internal timeout"));
            }
            Err(crossbeam_channel::SendTimeoutError::Disconnected(_)) => {
                return Err(PyErr::value_error("session actor stopped"));
            }
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        match rx.recv_timeout(remaining) {
            Ok(result) => result,
            Err(crossbeam_channel::RecvTimeoutError::Timeout) => {
                Err(PyErr::value_error("internal timeout"))
            }
            Err(crossbeam_channel::RecvTimeoutError::Disconnected) => {
                if self.join.is_finished() {
                    Err(PyErr::value_error("session actor stopped"))
                } else {
                    Err(PyErr::value_error("internal timeout"))
                }
            }
        }
    }
}

impl ActorHandle {
    /// 同 [`ActorHandle::request`]，但 `notice_at`（實時）到而仍未回覆時，於 safety lane 補送一次 `notice`（不等其回覆、
    /// lane 滿則略過），之後繼續等待原請求的回覆直到 `timeout`。供 release 的首次期限通知使用
    /// （`SessionRequest::ReleaseDeadlineElapsed`；Python 以實時等待作業 `done` 至期限，
    /// `origin/main:sw_core/session_manager.py:3188-3204`）。
    pub fn request_with_notice(
        &self,
        req: SessionRequest,
        timeout: Duration,
        notice_at: Instant,
        notice: SessionRequest,
    ) -> Result<PyDict, PyErr> {
        let deadline = Instant::now() + timeout;
        let (reply, rx) = crossbeam_channel::bounded(1);
        let sender = if matches!(&req, SessionRequest::Release { .. }) {
            &self.safety
        } else {
            &self.control
        };
        match sender.send_timeout(ControlEvent::Request { req, reply }, timeout) {
            Ok(()) => {}
            Err(crossbeam_channel::SendTimeoutError::Timeout(_)) => {
                return Err(PyErr::value_error("internal timeout"));
            }
            Err(crossbeam_channel::SendTimeoutError::Disconnected(_)) => {
                return Err(PyErr::value_error("session actor stopped"));
            }
        }
        if notice_at < deadline {
            match rx.recv_timeout(notice_at.saturating_duration_since(Instant::now())) {
                Ok(result) => return result,
                Err(crossbeam_channel::RecvTimeoutError::Disconnected) => {
                    return Err(self.disconnected_error());
                }
                Err(crossbeam_channel::RecvTimeoutError::Timeout) => {
                    let (notice_reply, _notice_rx) = crossbeam_channel::bounded(1);
                    let _ = self.safety.try_send(ControlEvent::Request {
                        req: notice,
                        reply: notice_reply,
                    });
                }
            }
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        match rx.recv_timeout(remaining) {
            Ok(result) => result,
            Err(crossbeam_channel::RecvTimeoutError::Timeout) => {
                Err(PyErr::value_error("internal timeout"))
            }
            Err(crossbeam_channel::RecvTimeoutError::Disconnected) => Err(self.disconnected_error()),
        }
    }

    fn disconnected_error(&self) -> PyErr {
        if self.join.is_finished() {
            PyErr::value_error("session actor stopped")
        } else {
            PyErr::value_error("internal timeout")
        }
    }
}

/// 效果執行器（executor 在 actor thread 外的 IO worker 側實作；測試以替身實作）。
pub trait EffectExecutor: Send {
    fn execute(&mut self, e: Effect, tx: &Sender<ControlEvent>, safety: &Sender<ControlEvent>);
    /// S2-PAR-wp510r R1: 效果立即回饋佇列（executor 推回的事件先於下一次 mailbox 收取被處理）。
    fn execute_with_feedback(
        &mut self,
        e: Effect,
        tx: &Sender<ControlEvent>,
        safety: &Sender<ControlEvent>,
        _feedback: &mut std::collections::VecDeque<ControlEvent>,
    ) {
        self.execute(e, tx, safety);
    }
}

/// 建立 session actor 失敗錯誤（DR-9：持鎖建立失敗回傳 Result，取代 expect panic 導致鎖中毒）。
#[derive(Debug)]
pub struct SpawnError(pub std::io::Error);

impl std::fmt::Display for SpawnError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "建立 session actor thread 失敗：{}", self.0)
    }
}

impl std::error::Error for SpawnError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.0)
    }
}

#[cfg(feature = "test-fakes")]
static INJECT_ACTOR_SPAWN_FAIL_COUNT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
#[cfg(feature = "test-fakes")]
static INJECT_ACTOR_SPAWN_FAIL_TARGET: std::sync::Mutex<Option<String>> = std::sync::Mutex::new(None);

#[cfg(feature = "test-fakes")]
pub fn inject_actor_spawn_fail(n: usize) {
    INJECT_ACTOR_SPAWN_FAIL_COUNT.store(n, std::sync::atomic::Ordering::SeqCst);
}

#[cfg(feature = "test-fakes")]
pub fn inject_actor_spawn_fail_for(sid: &str) {
    *INJECT_ACTOR_SPAWN_FAIL_TARGET.lock().unwrap() = Some(sid.to_string());
}

#[cfg(feature = "test-fakes")]
pub fn inject_actor_spawn_fail_clear() {
    *INJECT_ACTOR_SPAWN_FAIL_TARGET.lock().unwrap() = None;
    INJECT_ACTOR_SPAWN_FAIL_COUNT.store(0, std::sync::atomic::Ordering::SeqCst);
}

/// 嘗試啟動 actor thread（回傳 Result，呼叫端在 R2 全面切換）。
pub fn try_spawn_actor(
    core: SessionCore,
    mailbox: Mailbox,
    exec: Box<dyn EffectExecutor>,
    clock: Arc<dyn Clock>,
) -> Result<ActorHandle, SpawnError> {
    try_spawn_actor_with_reaper(core, mailbox, exec, clock, 0, None)
}

/// 支援帶世代號與回收者命令通道的 actor 啟動。
pub fn try_spawn_actor_with_reaper(
    mut core: SessionCore,
    mut mailbox: Mailbox,
    mut exec: Box<dyn EffectExecutor>,
    clock: Arc<dyn Clock>,
    inc: u64,
    reaper_cmd: Option<Sender<crate::custody::ReaperCmd>>,
) -> Result<ActorHandle, SpawnError> {
    #[cfg(feature = "test-fakes")]
    {
        let target = INJECT_ACTOR_SPAWN_FAIL_TARGET.lock().unwrap().clone();
        if let Some(target_sid) = target {
            if core.session_id() == target_sid {
                *INJECT_ACTOR_SPAWN_FAIL_TARGET.lock().unwrap() = None;
                return Err(SpawnError(std::io::Error::other(
                    "injected H-actor-spawn-fail",
                )));
            }
        } else {
            let count = INJECT_ACTOR_SPAWN_FAIL_COUNT.load(std::sync::atomic::Ordering::SeqCst);
            if count > 0 {
                INJECT_ACTOR_SPAWN_FAIL_COUNT.store(count - 1, std::sync::atomic::Ordering::SeqCst);
                return Err(SpawnError(std::io::Error::other(
                    "injected H-actor-spawn-fail",
                )));
            }
        }
    }

    let control = mailbox.control_sender();
    let safety = mailbox.safety_sender();
    let data = mailbox.data_sender();
    #[cfg(feature = "test-fakes")]
    let data_observer = mailbox.data_receiver_for_test();
    #[cfg(feature = "test-fakes")]
    let safety_observer = mailbox.safety_receiver_for_test();
    let thread_control = control.clone();
    let thread_safety = safety.clone();
    #[cfg(feature = "test-fakes")]
    let probe = Arc::new(ActorLatencyProbe::default());
    #[cfg(feature = "test-fakes")]
    let thread_probe = probe.clone();
    let com = core
        .session_id()
        .rsplit_once(':')
        .map(|(_, com)| com.to_string())
        .unwrap_or_else(|| core.session_id().to_string());
    let startup_effects = core.take_startup_effects();
    let join = std::thread::Builder::new()
        .name(format!("serialwrap-session-{com}"))
        .spawn(move || {
            // S2-PAR-wp510r: ExitReclaim 守衛只在 actor 執行緒 closure 內建立
            let _exit_reclaim = reaper_cmd.map(|tx| {
                crate::custody::ExitReclaim::new(core.session_id().to_string(), inc, tx)
            });

            let mut core = core;
            let mut timers: Vec<(u64, TimerEvent)> = Vec::new();
            let mut immediate_feedback: std::collections::VecDeque<ControlEvent> = std::collections::VecDeque::new();
            let mut processed = (0u64, 0u64);
            let mut next_tick = clock.mono_ns().saturating_add(1_000_000_000);
            let mut panic_type: Option<&'static str> = None;
            let mut panic_count = 0u8;

            // 啟動效果在進入 loop 前執行
            for effect in startup_effects {
                exec.execute_with_feedback(effect, &thread_control, &thread_safety, &mut immediate_feedback);
            }

            loop {
                let now = clock.mono_ns();

                // 立即回饋佇列最高優先級處理
                if let Some(event) = immediate_feedback.pop_front() {
                    if dispatch(
                        &mut core,
                        MailboxItem::Control(event),
                        now,
                        &thread_control,
                        &thread_safety,
                        &clock,
                        &mut exec,
                        &mut timers,
                        &mut processed,
                        &mut panic_type,
                        &mut panic_count,
                        &mut immediate_feedback,
                    ) {
                        break;
                    }
                    continue;
                }

                if let Some(index) = timers.iter().position(|(at, _)| *at <= now) {
                    let (_due, event) = timers.remove(index);
                    #[cfg(feature = "test-fakes")]
                    thread_probe.before_other(timer_label(&event), mailbox.data_backlog(), thread_probe.data_since(_due), 0);
                    if dispatch(
                        &mut core,
                        MailboxItem::Control(ControlEvent::Timer(event)),
                        now,
                        &thread_control,
                        &thread_safety,
                        &clock,
                        &mut exec,
                        &mut timers,
                        &mut processed,
                        &mut panic_type,
                        &mut panic_count,
                        &mut immediate_feedback,
                    ) {
                        break;
                    }
                    continue;
                }
                if now >= next_tick {
                    #[cfg(feature = "test-fakes")]
                    let tick_due = next_tick;
                    while next_tick <= now {
                        next_tick = next_tick.saturating_add(1_000_000_000);
                    }
                    #[cfg(feature = "test-fakes")]
                    thread_probe.before_other("Tick", mailbox.data_backlog(), thread_probe.data_since(tick_due), 0);
                    if dispatch(
                        &mut core,
                        MailboxItem::Control(ControlEvent::Tick),
                        now,
                        &thread_control,
                        &thread_safety,
                        &clock,
                        &mut exec,
                        &mut timers,
                        &mut processed,
                        &mut panic_type,
                        &mut panic_count,
                        &mut immediate_feedback,
                    ) {
                        break;
                    }
                    continue;
                }

                let deadline = Some(
                    timers
                        .iter()
                        .map(|(at, _)| *at)
                        .min()
                        .map_or(next_tick, |at| at.min(next_tick)),
                );
                let Some(item) =
                    mailbox.recv_next(processed, core.generations().conn_epoch, deadline, &*clock)
                else {
                    continue;
                };
                #[cfg(feature = "test-fakes")]
                if let MailboxItem::Control(ControlEvent::ArmPanic { count }) = &item {
                    core.arm_panic_on_next_events(*count);
                    continue;
                }
                #[cfg(feature = "test-fakes")]
                let label = probe_label(&item);
                #[cfg(feature = "test-fakes")]
                let pause_label = probe_pause_label(&item);
                #[cfg(feature = "test-fakes")]
                let data_started = clock.mono_ns();
                #[cfg(feature = "test-fakes")]
                if let Some(label) = label {
                    let witness = mailbox.take_control_witness().unwrap_or(0);
                    let pre = mailbox.take_control_pre_dependency_data().unwrap_or(0);
                    thread_probe.before_other(label, mailbox.data_backlog(), witness, pre);
                }
                #[cfg(feature = "test-fakes")]
                if let Some(label) = pause_label {
                    thread_probe.pause_before_dispatch(label);
                }
                let data_mark = match &item {
                    MailboxItem::Data(DataEvent::UartRxChunk { epoch, rx_seq, .. }) => {
                        Some((*epoch, *rx_seq))
                    }
                    _ => None,
                };
                let done = dispatch(
                    &mut core,
                    item,
                    clock.mono_ns(),
                    &thread_control,
                    &thread_safety,
                    &clock,
                    &mut exec,
                    &mut timers,
                    &mut processed,
                    &mut panic_type,
                    &mut panic_count,
                    &mut immediate_feedback,
                );
                if let Some(mark) = data_mark {
                    // 依賴事件只會在 current epoch 時判定；保留舊 epoch 的 dispatch watermark，
                    // 讓 OD-69 drain barrier 在 release 已清除 conn_epoch 後仍能確認 reader RX 已處理。
                    processed = processed.max(mark);
                }
                #[cfg(feature = "test-fakes")]
                if label.is_none() {
                    thread_probe.after_data(data_started);
                }
                if done {
                    break;
                }
            }
        })
        .map_err(SpawnError)?;

    Ok(ActorHandle {
        control,
        safety,
        data,
        join,
        #[cfg(feature = "test-fakes")]
        probe,
        #[cfg(feature = "test-fakes")]
        data_observer,
        #[cfg(feature = "test-fakes")]
        safety_observer,
    })
}

/// 啟動 actor thread。
pub fn spawn_actor(
    core: SessionCore,
    mailbox: Mailbox,
    exec: Box<dyn EffectExecutor>,
    clock: Arc<dyn Clock>,
) -> ActorHandle {
    try_spawn_actor(core, mailbox, exec, clock).expect("建立 session actor thread")
}

#[allow(clippy::too_many_arguments)]
fn dispatch(
    core: &mut SessionCore,
    item: MailboxItem,
    now: u64,
    control: &Sender<ControlEvent>,
    safety: &Sender<ControlEvent>,
    clock: &Arc<dyn Clock>,
    exec: &mut Box<dyn EffectExecutor>,
    timers: &mut Vec<(u64, TimerEvent)>,
    _processed: &mut (u64, u64),
    panic_type: &mut Option<&'static str>,
    panic_count: &mut u8,
    feedback: &mut std::collections::VecDeque<ControlEvent>,
) -> bool {
    let (reply, event_name) = match &item {
        MailboxItem::Control(ControlEvent::Request { reply, .. })
        | MailboxItem::Control(ControlEvent::Shutdown { reply }) => {
            (Some(reply.clone()), "Request")
        }
        MailboxItem::Control(ControlEvent::ConsoleConnected { .. }) => (None, "ConsoleConnected"),
        MailboxItem::Control(ControlEvent::ConsoleScan { .. }) => (None, "ConsoleScan"),
        MailboxItem::Control(ControlEvent::HumanTxCompleted { .. }) => (None, "HumanTxCompleted"),
        MailboxItem::Control(ControlEvent::DeviceAttached { .. }) => (None, "DeviceAttached"),
        MailboxItem::Control(ControlEvent::DeviceDetached { .. }) => (None, "DeviceDetached"),
        MailboxItem::Control(ControlEvent::DeviceMissing) => (None, "DeviceMissing"),
        MailboxItem::Control(ControlEvent::EvictCapture { .. }) => (None, "EvictCapture"),
        MailboxItem::Control(ControlEvent::ConsoleCountChanged { .. }) => {
            (None, "ConsoleCountChanged")
        }
        MailboxItem::Control(ControlEvent::BridgeOpened { .. }) => (None, "BridgeOpened"),
        MailboxItem::Control(ControlEvent::UartIoError { .. }) => (None, "UartIoError"),
        MailboxItem::Control(ControlEvent::UartTxCompleted { .. }) => (None, "UartTxCompleted"),
        MailboxItem::Control(ControlEvent::DrainDataBarrier { .. }) => (None, "DrainDataBarrier"),
        MailboxItem::Control(ControlEvent::WorkerStopped(_)) => (None, "WorkerStopped"),
        MailboxItem::Control(ControlEvent::WorkerDrainTimeout { .. }) => {
            (None, "WorkerDrainTimeout")
        }
        MailboxItem::Control(ControlEvent::OpFinished { .. }) => (None, "OpFinished"),
        MailboxItem::Control(ControlEvent::LoginFinished { .. }) => (None, "LoginFinished"),
        MailboxItem::Control(ControlEvent::Timer(_)) => (None, "Timer"),
        MailboxItem::Control(ControlEvent::StateCommitted { .. }) => (None, "StateCommitted"),
        MailboxItem::Control(ControlEvent::Tick) => (None, "Tick"),
        // P8 Task 8：flash 事件的 `FlashReply` 隨事件移入 core；panic 時被丟棄，呼叫端以通道斷線收斂。
        MailboxItem::Control(ControlEvent::FlashSyncProbeStart { .. }) => (None, "FlashSyncProbeStart"),
        MailboxItem::Control(ControlEvent::FlashProbeReleased { .. }) => (None, "FlashProbeReleased"),
        MailboxItem::Control(ControlEvent::FlashSyncMatched { .. }) => (None, "FlashSyncMatched"),
        MailboxItem::Control(ControlEvent::FlashHolderGone { .. }) => (None, "FlashHolderGone"),
        #[cfg(feature = "test-fakes")]
        MailboxItem::Control(ControlEvent::ArmPanic { .. }) => (None, "ArmPanic"),
        MailboxItem::Data(_) => (None, "Data"),
    };

    let result = catch_unwind(AssertUnwindSafe(|| core.on_event(item, now)));
    match result {
        Ok(effects) => {
            *panic_type = None;
            *panic_count = 0;
            for effect in effects.0 {
                if let Effect::Schedule { at, ev } = effect {
                    timers.push((at, ev));
                } else {
                    exec.execute_with_feedback(effect, control, safety, feedback);
                }
            }
        }
        Err(payload) => {
            let message = payload
                .downcast_ref::<String>()
                .cloned()
                .or_else(|| {
                    payload
                        .downcast_ref::<&'static str>()
                        .map(|s| (*s).to_string())
                })
                .unwrap_or_else(|| "unknown panic".to_string());
            if *panic_type == Some(event_name) {
                *panic_count = panic_count.saturating_add(1);
            } else {
                *panic_type = Some(event_name);
                *panic_count = 1;
            }
            if let Some(reply) = reply {
                let _ = reply.try_send(Err(PyErr::value_error(format!(
                    "internal error: {message}"
                ))));
            }
            let com = core
                .session_id()
                .rsplit_once(':')
                .map(|(_, com)| com)
                .unwrap_or(core.session_id());
            exec.execute(
                Effect::Warn(format!(
                    "session actor 事件處理例外（com={com}），略過此事件"
                )),
                control,
                safety,
            );
            if *panic_count >= 3 {
                let effects =
                    core.begin_stop(crate::drain::StopKind::IoError, None, clock.mono_ns());
                for effect in effects.0 {
                    if let Effect::Schedule { at, ev } = effect {
                        timers.push((at, ev));
                    } else {
                        exec.execute(effect, control, safety);
                    }
                }
                *panic_count = 0;
                *panic_type = None;
            }
        }
    }
    core.shutdown_complete()
}
