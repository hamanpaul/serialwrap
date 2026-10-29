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
        MailboxItem::Control(_) => Some("Control"),
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

/// 效果執行器（executor 在 actor thread 外的 IO worker 側實作；測試以替身實作）。
pub trait EffectExecutor: Send {
    fn execute(&mut self, e: Effect, tx: &Sender<ControlEvent>, safety: &Sender<ControlEvent>);
}

/// 啟動 actor thread。
pub fn spawn_actor(
    core: SessionCore,
    mut mailbox: Mailbox,
    mut exec: Box<dyn EffectExecutor>,
    clock: Arc<dyn Clock>,
) -> ActorHandle {
    let control = mailbox.control_sender();
    let safety = mailbox.safety_sender();
    let data = mailbox.data_sender();
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
    let join = std::thread::Builder::new()
        .name(format!("serialwrap-session-{com}"))
        .spawn(move || {
            let mut core = core;
            let mut timers: Vec<(u64, TimerEvent)> = Vec::new();
            let mut processed = (0u64, 0u64);
            let mut next_tick = clock.mono_ns().saturating_add(1_000_000_000);
            let mut panic_type: Option<&'static str> = None;
            let mut panic_count = 0u8;

            loop {
                let now = clock.mono_ns();
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
                let label = probe_label(&item);
                #[cfg(feature = "test-fakes")]
                let data_started = clock.mono_ns();
                #[cfg(feature = "test-fakes")]
                if let Some(label) = label {
                    let witness = mailbox.take_control_witness().unwrap_or(0);
                    let pre = mailbox.take_control_pre_dependency_data().unwrap_or(0);
                    thread_probe.before_other(label, mailbox.data_backlog(), witness, pre);
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
                );
                if let Some(mark) = data_mark {
                    if core.generations().conn_epoch == Some(mark.0) {
                        processed = processed.max(mark);
                    }
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
        .expect("建立 session actor thread");
    ActorHandle {
        control,
        safety,
        data,
        join,
        #[cfg(feature = "test-fakes")]
        probe,
    }
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
) -> bool {
    let (reply, event_name) = match &item {
        MailboxItem::Control(ControlEvent::Request { reply, .. })
        | MailboxItem::Control(ControlEvent::Shutdown { reply }) => {
            (Some(reply.clone()), "Request")
        }
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
        MailboxItem::Control(ControlEvent::WorkerStopped(_)) => (None, "WorkerStopped"),
        MailboxItem::Control(ControlEvent::WorkerDrainTimeout { .. }) => {
            (None, "WorkerDrainTimeout")
        }
        MailboxItem::Control(ControlEvent::OpFinished { .. }) => (None, "OpFinished"),
        MailboxItem::Control(ControlEvent::LoginFinished { .. }) => (None, "LoginFinished"),
        MailboxItem::Control(ControlEvent::Timer(_)) => (None, "Timer"),
        MailboxItem::Control(ControlEvent::StateCommitted { .. }) => (None, "StateCommitted"),
        MailboxItem::Control(ControlEvent::Tick) => (None, "Tick"),
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
                    exec.execute(effect, control, safety);
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
