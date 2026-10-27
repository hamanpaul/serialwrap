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
                    let (_, event) = timers.remove(index);
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
                    while next_tick <= now {
                        next_tick = next_tick.saturating_add(1_000_000_000);
                    }
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
