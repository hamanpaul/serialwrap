//! P5-T10／T12：UART reader、RX 視窗與 IoWorker。
//!
//! T10 提供 reader thread、RX 視窗及哨兵 fan-out；IoWorker 的啟停與 TX 組裝由 T12 完成。

use std::collections::VecDeque;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, Weak};
use std::time::{Duration, Instant};

use crossbeam_channel::{bounded, Sender};
use sw_pal::traits::{
    CancelToken, Clock, CloseToken, PalError, PalErrorKind, ReadError, ReadOutcome, SerialHandle,
    SerialReader, SerialWriter, SubmitStep,
};
use sw_proto::pycompat::{clean_text, clean_text_with_source_ranges, decode_utf8_replace};
use sw_proto::pyre::{compile, PyFlags, PyRegex};

use crate::drain::StopId;
use crate::mailbox::{ControlEvent, DataEvent};
use crate::tx_writer::{
    spawn_tx_writer, EvidenceGap, Fence, TxLedger, TxWriterContext, TxWriterHandle, TxWriterStop,
};
use crate::wal::{WalAppendReq, WalWriter};
use crate::Mono;

#[cfg(feature = "test-fakes")]
type SnapshotWaitHook = Arc<dyn Fn(&RxSnapshot) + Send + Sync>;
#[cfg(feature = "test-fakes")]
type SnapshotWaitEnterHook = Arc<dyn Fn() + Send + Sync>;
#[cfg(feature = "test-fakes")]
type SnapshotWaitDeadlineHook = Arc<dyn Fn() + Send + Sync>;
#[cfg(feature = "test-fakes")]
type ActorDispatchGateHook = Arc<dyn Fn(ActorDispatchGateEvent) + Send + Sync>;

/// actor dispatch 與 timeout 救援判讀之間的線性化事件（測試觀察）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ActorDispatchGateEvent {
    ActorWaiting,
    ActorEntered,
    ActorExited,
    DecisionWaiting,
    DecisionEntered,
    DecisionExited,
}

#[derive(Default)]
struct ActorDispatchGateState {
    dispatching: bool,
    decision_waiters: usize,
    decision_active: bool,
    human_enqueue_active: bool,
    rescue_pending: usize,
    human_submit_start_count: u64,
    human_in_flight: usize,
    human_tx_generation: u64,
    human_tx_epoch: Option<u64>,
    last_human_tx_rx_offset: u64,
    human_raw_line_open: bool,
    pending_human: bool,
    /// 最新已寫出 human TX 的 RX epoch 與絕對 RX 字元邊界。
    pending_human_epoch: Option<u64>,
    pending_human_rx_offset: Option<u64>,
    prompt_revision: u64,
    /// 此 session 當前接管的 RX window；舊 writer 的晚到回報也在這個 window 線性化。
    active_rx: Option<Weak<RxShared>>,
    active_rx_epoch: Option<u64>,
    completion_capture_op_id: Option<u64>,
    human_state_events: VecDeque<HumanStateEvent>,
    #[cfg(feature = "test-fakes")]
    actor_waits: u64,
    #[cfg(feature = "test-fakes")]
    decision_waits: u64,
}

/// actor transition 到實際 HumanTx guard 登記之間的排他區段。
///
/// timeout waiter 先發布等待旗標；actor 之後不會插隊開始另一個 dispatch。兩邊只在進入／離開時
/// 短暫持有 `state` mutex，transition 與 timeout 訊號讀取期間不持有該 mutex。
pub struct ActorDispatchGate {
    state: Mutex<ActorDispatchGateState>,
    changed: Condvar,
    prompt_regex: Mutex<Option<Arc<PyRegex>>>,
    #[cfg(feature = "test-fakes")]
    hook: Mutex<Option<ActorDispatchGateHook>>,
}

impl ActorDispatchGate {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            state: Mutex::new(ActorDispatchGateState::default()),
            changed: Condvar::new(),
            prompt_regex: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            hook: Mutex::new(None),
        })
    }

    pub(crate) fn new_for_session(prompt_pattern: &str) -> Arc<Self> {
        let gate = Self::new();
        gate.set_prompt_regex(prompt_pattern);
        gate
    }

    fn set_prompt_regex(&self, pattern: &str) {
        let compiled = compile(pattern, PyFlags(0))
            .or_else(|_| compile("(?!)", PyFlags(0)))
            .ok()
            .map(Arc::new);
        *self
            .prompt_regex
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = compiled;
    }

    fn prompt_regex(&self) -> Option<Arc<PyRegex>> {
        self.prompt_regex
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
    }

    fn event(&self, event: ActorDispatchGateEvent) {
        #[cfg(feature = "test-fakes")]
        if let Some(hook) = self.hook.lock().unwrap_or_else(|error| error.into_inner()).clone() {
            hook(event);
        }
        #[cfg(not(feature = "test-fakes"))]
        let _ = event;
    }

    pub fn enter_actor_dispatch(self: &Arc<Self>) -> ActorDispatchGuard {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if state.dispatching || state.decision_waiters > 0 || state.decision_active {
            #[cfg(feature = "test-fakes")]
            {
                state.actor_waits = state.actor_waits.saturating_add(1);
            }
            self.event(ActorDispatchGateEvent::ActorWaiting);
        }
        while state.dispatching || state.decision_waiters > 0 || state.decision_active {
            state = self
                .changed
                .wait(state)
                .unwrap_or_else(|error| error.into_inner());
        }
        state.dispatching = true;
        self.event(ActorDispatchGateEvent::ActorEntered);
        ActorDispatchGuard {
            gate: Arc::clone(self),
            decision: false,
        }
    }

    fn enter_timeout_decision(self: &Arc<Self>) -> ActorDispatchGuard {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        state.decision_waiters = state.decision_waiters.saturating_add(1);
        if state.dispatching || state.decision_active || state.human_enqueue_active {
            #[cfg(feature = "test-fakes")]
            {
                state.decision_waits = state.decision_waits.saturating_add(1);
            }
            self.event(ActorDispatchGateEvent::DecisionWaiting);
        }
        while state.dispatching || state.decision_active || state.human_enqueue_active {
            state = self
                .changed
                .wait(state)
                .unwrap_or_else(|error| error.into_inner());
        }
        state.decision_waiters = state.decision_waiters.saturating_sub(1);
        state.decision_active = true;
        self.event(ActorDispatchGateEvent::DecisionEntered);
        ActorDispatchGuard {
            gate: Arc::clone(self),
            decision: true,
        }
    }

    fn enter_human_enqueue(self: &Arc<Self>) -> HumanEnqueueGuard {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        while state.decision_waiters > 0
            || state.decision_active
            || state.rescue_pending > 0
            || state.human_enqueue_active
        {
            state = self
                .changed
                .wait(state)
                .unwrap_or_else(|error| error.into_inner());
        }
        state.human_enqueue_active = true;
        HumanEnqueueGuard { gate: Arc::clone(self) }
    }

    fn reserve_rescue(self: &Arc<Self>) -> HumanSubmitRescueGuard {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        state.rescue_pending = state.rescue_pending.saturating_add(1);
        HumanSubmitRescueGuard { gate: Arc::clone(self), active: true }
    }

    fn note_human_in_flight(&self, delta: isize) {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if delta >= 0 {
            state.human_in_flight = state.human_in_flight.saturating_add(delta as usize);
        } else {
            state.human_in_flight = state.human_in_flight.saturating_sub(delta.unsigned_abs());
        }
    }

    fn note_human_submit_start(&self) {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        state.human_submit_start_count = state.human_submit_start_count.saturating_add(1);
    }

    #[cfg(feature = "test-fakes")]
    fn human_signals(&self) -> (u64, usize, u64) {
        let state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        (state.human_submit_start_count, state.human_in_flight, state.human_tx_generation)
    }

    #[cfg(feature = "test-fakes")]
    fn set_hook(&self, hook: Option<ActorDispatchGateHook>) {
        *self.hook.lock().unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    fn waits_for_test(&self) -> (u64, u64) {
        let state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        (state.decision_waits, state.actor_waits)
    }
}

pub struct ActorDispatchGuard {
    gate: Arc<ActorDispatchGate>,
    decision: bool,
}

struct HumanEnqueueGuard {
    gate: Arc<ActorDispatchGate>,
}

impl Drop for HumanEnqueueGuard {
    fn drop(&mut self) {
        let mut state = self.gate.state.lock().unwrap_or_else(|error| error.into_inner());
        state.human_enqueue_active = false;
        drop(state);
        self.gate.changed.notify_all();
    }
}

impl Drop for ActorDispatchGuard {
    fn drop(&mut self) {
        let mut state = self
            .gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if self.decision {
            state.decision_active = false;
            self.gate.event(ActorDispatchGateEvent::DecisionExited);
        } else {
            state.dispatching = false;
            self.gate.event(ActorDispatchGateEvent::ActorExited);
        }
        drop(state);
        self.gate.changed.notify_all();
    }
}

const IO_POLL: Duration = Duration::from_millis(200);
const RX_RATE_WINDOW_NS: u64 = 10_000_000_000;
const RX_RATE_WINDOW_MAX: usize = 4096;
const RX_WAIT_CLOCK_SLICE: Duration = Duration::from_millis(50);
const DATA_SEND_POLL: Duration = Duration::from_millis(50);

#[derive(Clone)]
struct RxEvent {
    start_offset: u64,
    end_offset: u64,
    mono_ns: Mono,
    watermark: (u64, u64),
    text: String,
}

#[derive(Clone, Copy)]
struct HumanStateEvent {
    mono_ns: Mono,
    state: HumanTxState,
    kind: HumanStateEventKind,
}

/// human pending 狀態結案事件的來源。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HumanStateEventKind {
    TxRegistered,
    PromptSettled,
    TimeoutConsumed,
}

/// 哨兵 console（POSIX 內部 PTY）的 I/O 介面：與 PAL 的 `ConsoleIo`（arch §4.7）為同一型別。
pub use sw_pal::traits::ConsoleIo;

#[derive(Default)]
struct RxWindow {
    max_chars: usize,
    text: String,
    prompt_evidence: Option<PromptEvidence>,
    trimmed: u64,
    total_bytes: u64,
    last_rx_mono: Mono,
    watermark: (u64, u64),
    revision: u64,
    rate: VecDeque<(Mono, u64)>,
    rx_events: VecDeque<RxEvent>,
    rx_event_chars: usize,
    completion_capture_op_id: Option<u64>,
    completion_deadline: Option<Mono>,
    command_tx_boundary: Option<(u64, CommandTxBoundary)>,
    tx_submit_pending: bool,
    tx_submit_pending_started_at: Option<Mono>,
    completion_tx_submit_crossed_deadline: bool,
}

impl RxWindow {
    fn text_from(&self, from: u64) -> &str {
        if from <= self.trimmed {
            return &self.text;
        }
        let relative = usize::try_from(from - self.trimmed).unwrap_or(usize::MAX);
        let byte_index = self
            .text
            .char_indices()
            .nth(relative)
            .map_or(self.text.len(), |(index, _)| index);
        &self.text[byte_index..]
    }

    fn trim_to_limit(&mut self) {
        let char_count = self.text.chars().count();
        let overflow = char_count.saturating_sub(self.max_chars);
        if overflow == 0 {
            return;
        }
        let byte_index = self
            .text
            .char_indices()
            .nth(overflow)
            .map_or(self.text.len(), |(index, _)| index);
        self.text.drain(..byte_index);
        self.trimmed = self.trimmed.saturating_add(overflow as u64);
    }

    fn push_rx_event(&mut self, event: RxEvent) {
        self.rx_event_chars = self
            .rx_event_chars
            .saturating_add(event.text.chars().count());
        self.rx_events.push_back(event);
        self.trim_rx_events_to_limit();
    }

    fn trim_rx_events_to_limit(&mut self) {
        let overflow = self.rx_event_chars.saturating_sub(self.max_chars);
        let mut remaining = overflow;
        while remaining > 0 {
            let Some(front) = self.rx_events.front_mut() else {
                self.rx_event_chars = 0;
                break;
            };
            let front_chars = front.text.chars().count();
            if remaining >= front_chars {
                remaining -= front_chars;
                self.rx_event_chars = self.rx_event_chars.saturating_sub(front_chars);
                self.rx_events.pop_front();
                continue;
            }
            let byte_index = front
                .text
                .char_indices()
                .nth(remaining)
                .map_or(front.text.len(), |(index, _)| index);
            front.text.drain(..byte_index);
            front.start_offset = front.start_offset.saturating_add(remaining as u64);
            self.rx_event_chars = self.rx_event_chars.saturating_sub(remaining);
            remaining = 0;
        }
    }

    fn clear_rx_events(&mut self) {
        self.rx_events.clear();
        self.rx_event_chars = 0;
    }

    fn prune_rate(&mut self, now: Mono) {
        while self
            .rate
            .front()
            .is_some_and(|(at, _)| now.saturating_sub(*at) > RX_RATE_WINDOW_NS)
        {
            self.rate.pop_front();
        }
    }

    fn cap_rate(&mut self) {
        if self.rate.len() <= RX_RATE_WINDOW_MAX {
            return;
        }
        let Some((first_at, first_bytes)) = self.rate.pop_front() else {
            return;
        };
        let Some((second_at, second_bytes)) = self.rate.pop_front() else {
            self.rate.push_front((first_at, first_bytes));
            return;
        };
        self.rate.push_front((
            first_at.max(second_at),
            first_bytes.saturating_add(second_bytes),
        ));
    }
}

/// RX 視窗（絕對偏移；跨 epoch 的 watermark 以 `(epoch, rx_seq)` 表示）。
pub struct RxShared {
    inner: Mutex<RxWindow>,
    actor_dispatch_gate: Arc<ActorDispatchGate>,
    changed: Condvar,
    /// P7 Task 3：`wait_for_regex_cancellable` 的喚醒通道（append／clear／`RunCancel::trigger` 時 `try_send`）。
    wakers: Mutex<Vec<Sender<()>>>,
    /// P7 Task 3：測試替身固定 `rx_stats` 的 bytes（`ScriptedBridge.rx_stats`）；`u64::MAX` 為未設定。
    stats_override: AtomicU64,
    /// P7 Task 10：agent log 的 reader 端寫入者（`append` 內同步呼叫；Python `_on_bridge_rx` 於 reader 執行緒寫檔）。
    capture: Mutex<Option<Arc<crate::capture::CaptureObserver>>>,
    /// P7 Task 7（SC-30）：login run 取消世代。`RunCancel` 建立時記下當時值，之後遞增即視為已取消
    /// （actor 在 quiet arm 的同一事件內呼叫 `cancel_login_runs`，不需新增 Effect 變體）。
    run_cancel_gen: AtomicU64,
    /// P8 Task 5：檔案傳輸等待的取消世代。drain Phase 1（`SessionCore::begin_stop`，所有 `StopKind`）遞增；
    /// 只影響以 [`RunCancel::watching_transfers`] 建立的取消旗標，login run（SC-30）不受影響。
    transfer_cancel_gen: AtomicU64,
    /// issue 297：daemon Shutdown 專用、單向的 RX 等待取消狀態；不與 login／transfer 世代共用。
    shutdown_waits_cancelled: AtomicBool,
    /// 測試觀察：記錄 regex 等待入口，供 self-test nonce／prompt 等待的決定性同步。
    #[cfg(feature = "test-fakes")]
    regex_wait_starts: AtomicU64,
    /// 測試觀察：regex 等待實際檢查 RX snapshot 的次數。
    #[cfg(feature = "test-fakes")]
    regex_wait_checks: AtomicU64,
    /// 測試觀察：regex 等待已檢查的最後一個 RX 絕對字元位置。
    #[cfg(feature = "test-fakes")]
    regex_wait_snapshot_end: AtomicU64,
    /// 測試觀察：snapshot predicate 已檢查的次數與絕對尾端，用於 reader／evaluator processed barrier。
    #[cfg(feature = "test-fakes")]
    snapshot_wait_checks: AtomicU64,
    #[cfg(feature = "test-fakes")]
    snapshot_wait_end: AtomicU64,
    /// 測試交錯：命中 completion snapshot 後、waiter 收到結果前暫停。
    #[cfg(feature = "test-fakes")]
    snapshot_wait_match_hook: Mutex<Option<SnapshotWaitHook>>,
    /// 測試交錯：外層 deadline 已計算、內層等待器尚未建立期限時暫停。
    #[cfg(feature = "test-fakes")]
    snapshot_wait_enter_hook: Mutex<Option<SnapshotWaitEnterHook>>,
    /// 測試交錯：predicate 已依 snapshot 完成評估、等待器尚未重取 RX 鎖時暫停。
    #[cfg(feature = "test-fakes")]
    snapshot_wait_evaluated_hook: Mutex<Option<SnapshotWaitHook>>,
    /// 測試交錯：evaluator 已辨識新 prompt、證據寫回與完成 stale recheck 前暫停。
    #[cfg(feature = "test-fakes")]
    prompt_evidence_before_write_hook: Mutex<Option<SnapshotWaitHook>>,
    /// 測試交錯：snapshot wait 確認逾時後、呼叫端收回結果前暫停。
    #[cfg(feature = "test-fakes")]
    snapshot_wait_deadline_hook: Mutex<Option<Arc<dyn Fn() + Send + Sync>>>,
    /// 測試交錯：期限已到、deadline snapshot 尚未取得 RX 鎖時暫停。
    #[cfg(feature = "test-fakes")]
    snapshot_wait_before_deadline_snapshot_hook: Mutex<Option<SnapshotWaitDeadlineHook>>,
    /// 測試交錯：timeout 已判定無 human、尚未送第一個救援鍵時暫停。
    #[cfg(feature = "test-fakes")]
    timeout_rescue_decision_hook: Mutex<Option<SnapshotWaitDeadlineHook>>,
    /// 測試交錯：human writer 已建立最終 report、尚未交給 submitter 前暫停。
    #[cfg(feature = "test-fakes")]
    human_tx_report_hook: Mutex<Option<SnapshotWaitDeadlineHook>>,
    /// 測試交錯：actor transition 已產生 HumanTx effect、executor 尚未處理 effect 前暫停。
    #[cfg(feature = "test-fakes")]
    human_tx_before_effect_hook: Mutex<Option<SnapshotWaitEnterHook>>,
    /// 測試交錯：transition 已回傳 effects、實際 HumanTx guard 尚未登記前暫停。
    #[cfg(feature = "test-fakes")]
    dispatch_before_guard_registration_hook: Mutex<Option<SnapshotWaitEnterHook>>,
    /// 測試觀察：timeout 已開始取得救援判讀線性化點。
    #[cfg(feature = "test-fakes")]
    timeout_rescue_attempt_hook: Mutex<Option<SnapshotWaitEnterHook>>,
    /// 測試觀察：timeout 已讀取完整 human tuple、即將離開 dispatch gate。
    #[cfg(feature = "test-fakes")]
    timeout_rescue_read_hook: Mutex<Option<SnapshotWaitEnterHook>>,
    /// 測試交錯：timeout 已辨識 human TX、即將回傳 CompletionUncertain 前暫停。
    #[cfg(feature = "test-fakes")]
    human_timeout_decision_hook: Mutex<Option<SnapshotWaitEnterHook>>,
    /// 測試交錯：human pump 在取件前與取件後分別暫停。
    #[cfg(feature = "test-fakes")]
    human_pump_before_take_hook: Mutex<Option<SnapshotWaitDeadlineHook>>,
    #[cfg(feature = "test-fakes")]
    human_pump_after_take_hook: Mutex<Option<SnapshotWaitDeadlineHook>>,
    /// 測試觀察：human pump 已開始 submit、正等待 rescue gate 或 writer queue。
    #[cfg(feature = "test-fakes")]
    human_submit_attempts: AtomicU64,
    /// 測試觀察：human job 已成功進入 pump channel 的數量。
    #[cfg(feature = "test-fakes")]
    human_pump_submissions: AtomicU64,
}

impl RxShared {
    pub fn new(max_chars: usize, created_mono: u64) -> Arc<RxShared> {
        Self::new_with_actor_dispatch_gate(max_chars, created_mono, ActorDispatchGate::new())
    }

    /// 建立同一 session 的新 epoch RX；actor dispatch 與 timeout 決策沿用 session gate。
    pub fn new_with_actor_dispatch_gate(
        max_chars: usize,
        created_mono: u64,
        actor_dispatch_gate: Arc<ActorDispatchGate>,
    ) -> Arc<RxShared> {
        let rx = Arc::new(RxShared {
            inner: Mutex::new(RxWindow {
                max_chars,
                last_rx_mono: created_mono,
                ..RxWindow::default()
            }),
            actor_dispatch_gate: actor_dispatch_gate.clone(),
            changed: Condvar::new(),
            wakers: Mutex::new(Vec::new()),
            stats_override: AtomicU64::new(u64::MAX),
            capture: Mutex::new(None),
            run_cancel_gen: AtomicU64::new(0),
            transfer_cancel_gen: AtomicU64::new(0),
            shutdown_waits_cancelled: AtomicBool::new(false),
            #[cfg(feature = "test-fakes")]
            regex_wait_starts: AtomicU64::new(0),
            #[cfg(feature = "test-fakes")]
            regex_wait_checks: AtomicU64::new(0),
            #[cfg(feature = "test-fakes")]
            regex_wait_snapshot_end: AtomicU64::new(0),
            #[cfg(feature = "test-fakes")]
            snapshot_wait_checks: AtomicU64::new(0),
            #[cfg(feature = "test-fakes")]
            snapshot_wait_end: AtomicU64::new(0),
            #[cfg(feature = "test-fakes")]
            snapshot_wait_match_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            snapshot_wait_enter_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            snapshot_wait_evaluated_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            prompt_evidence_before_write_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            snapshot_wait_deadline_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            snapshot_wait_before_deadline_snapshot_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            timeout_rescue_decision_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            human_tx_report_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            human_tx_before_effect_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            dispatch_before_guard_registration_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            timeout_rescue_attempt_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            timeout_rescue_read_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            human_timeout_decision_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            human_pump_before_take_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            human_pump_after_take_hook: Mutex::new(None),
            #[cfg(feature = "test-fakes")]
            human_submit_attempts: AtomicU64::new(0),
            #[cfg(feature = "test-fakes")]
            human_pump_submissions: AtomicU64::new(0),
        });
        {
            let mut state = actor_dispatch_gate
                .state
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            if state.active_rx.as_ref().and_then(Weak::upgrade).is_none() {
                state.active_rx = Some(Arc::downgrade(&rx));
            }
        }
        rx
    }

    #[doc(hidden)]
    pub fn actor_dispatch_gate(&self) -> Arc<ActorDispatchGate> {
        Arc::clone(&self.actor_dispatch_gate)
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_human_prompt_regex_for_test(&self, pattern: &str) {
        self.set_human_prompt_regex(pattern);
    }

    /// 同步更新 session gate 使用的 prompt 身分。鎖序為 gate state → prompt regex → RX window，
    /// 與 RX append／epoch 接管一致，避免 append 以舊 regex 配對已切換的 profile。
    pub(crate) fn set_human_prompt_regex(&self, pattern: &str) {
        let mut state = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        self.actor_dispatch_gate.set_prompt_regex(pattern);
        state.prompt_revision = state.prompt_revision.saturating_add(1);
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        window.prompt_evidence = None;
        window.revision = window.revision.saturating_add(1);
        drop(window);
        drop(state);
        self.actor_dispatch_gate.changed.notify_all();
    }

    /// 將 session gate 的 active window 線性化到已接管的 bridge epoch。
    pub(crate) fn activate_epoch(self: &Arc<Self>, epoch: u64) {
        let mut human = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        human.active_rx = Some(Arc::downgrade(self));
        human.active_rx_epoch = Some(epoch);
        if human.pending_human {
            let (boundary_epoch, boundary_offset) =
                advance_pending_human_boundary(&mut human, epoch, 0);
            human.human_tx_epoch = Some(boundary_epoch);
            human.last_human_tx_rx_offset = boundary_offset;
        }
        let prompt_regex = self.actor_dispatch_gate.prompt_regex();
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        window.watermark.0 = epoch;
        window.prompt_evidence = None;
        window.revision = window.revision.saturating_add(1);
        let now = window.last_rx_mono;
        settle_pending_human_locked(&mut human, &window, prompt_regex.as_deref(), epoch, now);
        drop(window);
        drop(human);
        self.wake_all();
    }

    #[cfg(feature = "test-fakes")]
    pub fn activate_epoch_for_test(self: &Arc<Self>, epoch: u64) {
        self.activate_epoch(epoch);
    }

    /// human line submit 的在途 guard；實際成功排入 TX writer 後才更新 submit 起點計數。
    #[doc(hidden)]
    pub fn begin_human_submit(self: &Arc<Self>) -> HumanInFlightGuard {
        #[cfg(feature = "test-fakes")]
        self.human_submit_attempts.fetch_add(1, Ordering::AcqRel);
        self.register_human_in_flight();
        self.new_human_in_flight_guard(false)
    }

    /// actor 只為 transition 實際產生的每個 HumanTx effect 建立 guard。
    #[doc(hidden)]
    pub fn begin_human_tx_effect(self: &Arc<Self>) -> HumanInFlightGuard {
        self.register_human_in_flight();
        self.new_human_in_flight_guard(false)
    }

    /// 對 writer enqueue 與 timeout rescue 決策互斥。計數只在 enqueue 成功後更新；guard 已提供
    /// 在途訊號，故 writer 可在計數更新前開始處理而不會被誤判為無 human。
    pub(crate) fn with_human_submit_gate<T>(
        &self,
        in_flight: &mut HumanInFlightGuard,
        mut job: T,
        mut try_enqueue: impl FnMut(T) -> Result<(), crate::tx_writer::QueueEnqueueError<T>>,
        mut capacity_generation: impl FnMut() -> u64,
        mut wait_for_capacity: impl FnMut(u64),
    ) -> Result<(), ()> {
        loop {
            // gate 內只執行非阻塞 try_send。
            let enqueue_guard = self.actor_dispatch_gate.enter_human_enqueue();
            let observed_capacity = capacity_generation();
            match try_enqueue(job) {
                Ok(()) => {
                    in_flight.mark_human_submit_started();
                    drop(enqueue_guard);
                    return Ok(());
                }
                Err(crate::tx_writer::QueueEnqueueError::Full(returned)) => {
                    job = *returned;
                    drop(enqueue_guard);
                    // 等待 writer 容量時不持有救援 gate 或 dispatch gate。
                    wait_for_capacity(observed_capacity);
                }
                Err(crate::tx_writer::QueueEnqueueError::Disconnected) => {
                    drop(enqueue_guard);
                    return Err(());
                }
            }
        }
    }

    fn register_human_in_flight(&self) {
        self.actor_dispatch_gate.note_human_in_flight(1);
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        window.revision = window.revision.saturating_add(1);
        drop(window);
        self.wake_all();
    }

    fn unregister_human_in_flight(&self) {
        let mut human = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        human.human_in_flight = human.human_in_flight.saturating_sub(1);
        let prompt_regex = self.actor_dispatch_gate.prompt_regex();
        let active_rx = human.active_rx.as_ref().and_then(Weak::upgrade);
        let active_window = active_rx.as_deref().unwrap_or(self);
        let mut window = active_window
            .inner
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        window.revision = window.revision.saturating_add(1);
        let now = window.last_rx_mono;
        let epoch = human.active_rx_epoch.unwrap_or(window.watermark.0);
        settle_pending_human_locked(
            &mut human,
            &window,
            prompt_regex.as_deref(),
            epoch,
            now,
        );
        drop(window);
        drop(human);
        self.wake_all();
        if let Some(active_rx) = active_rx {
            if !std::ptr::eq(Arc::as_ptr(&active_rx), self) {
                active_rx.wake_all();
            }
        }
    }

    fn register_human_submit_start(&self) {
        self.actor_dispatch_gate.note_human_submit_start();
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        window.revision = window.revision.saturating_add(1);
        drop(window);
        self.wake_all();
    }

    fn new_human_in_flight_guard(self: &Arc<Self>, submit_started: bool) -> HumanInFlightGuard {
        HumanInFlightGuard {
            rx: Arc::clone(self),
            submit_started,
        }
    }

    /// Timeout 決策與第一個救援鍵排入 writer 之間，阻止新的 human submit 起點插入。
    pub(crate) fn begin_timeout_rescue(
        self: &Arc<Self>,
        command_start_count: u64,
        command_human_generation: u64,
    ) -> (HumanSubmitRescueGuard, bool) {
        #[cfg(feature = "test-fakes")]
        self.timeout_rescue_attempt_hook_for_test();
        // 等待 dispatch 或 human enqueue 前先發布 timeout 意圖；後續 human producer 必須讓此決策先選定結果。
        let dispatch_guard = self.actor_dispatch_gate.enter_timeout_decision();
        // 全 session human counters 與目前 RX 的 submit pending 在 gate → RX 鎖序中一次讀取。
        let state = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        let human_submit_seen = state.human_submit_start_count > command_start_count
            || state.human_in_flight > 0
            || window.tx_submit_pending
            || state.human_tx_generation > command_human_generation;
        drop(window);
        drop(state);
        let rescue_guard = if human_submit_seen {
            HumanSubmitRescueGuard { gate: Arc::clone(&self.actor_dispatch_gate), active: false }
        } else {
            self.actor_dispatch_gate.reserve_rescue()
        };
        drop(dispatch_guard);
        #[cfg(feature = "test-fakes")]
        self.timeout_rescue_read_hook_for_test();
        (rescue_guard, human_submit_seen)
    }

    /// 決定 echo timeout 是否走 human partial；穩定的舊 pending 只在此決策點消耗一次。
    pub(crate) fn begin_timeout_decision(
        self: &Arc<Self>,
        command_start_count: u64,
        command_human_before: HumanTxState,
        pending_human_before: bool,
        mono_ns: Mono,
    ) -> (HumanSubmitRescueGuard, bool) {
        #[cfg(feature = "test-fakes")]
        self.timeout_rescue_attempt_hook_for_test();
        let dispatch_guard = self.actor_dispatch_gate.enter_timeout_decision();
        let mut state = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let active_rx = state.active_rx.as_ref().and_then(Weak::upgrade);
        let active_window_rx = active_rx.as_deref().unwrap_or(self);
        let mut window = active_window_rx
            .inner
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let current_boundary = state
            .pending_human_epoch
            .zip(state.pending_human_rx_offset);
        let start_boundary = command_human_before
            .epoch
            .map(|epoch| (epoch, command_human_before.rx_offset));
        let no_new_boundary = state.human_tx_generation == command_human_before.generation
            && current_boundary == start_boundary;
        let human_submit_seen = state.human_submit_start_count > command_start_count
            || state.human_in_flight > 0
            || window.tx_submit_pending
            || state.human_tx_generation > command_human_before.generation;
        let mut pending_consumed = false;
        if pending_human_before
            && state.pending_human
            && state.human_submit_start_count == command_start_count
            && no_new_boundary
            && state.human_in_flight == 0
            && !window.tx_submit_pending
        {
            state.pending_human = false;
            state.pending_human_epoch = None;
            state.pending_human_rx_offset = None;
            push_human_state_event(&mut state, mono_ns, HumanStateEventKind::TimeoutConsumed);
            window.revision = window.revision.saturating_add(1);
            pending_consumed = true;
        }
        drop(window);
        let rescue_guard = if pending_human_before || human_submit_seen {
            HumanSubmitRescueGuard {
                gate: Arc::clone(&self.actor_dispatch_gate),
                active: false,
            }
        } else {
            state.rescue_pending = state.rescue_pending.saturating_add(1);
            HumanSubmitRescueGuard {
                gate: Arc::clone(&self.actor_dispatch_gate),
                active: true,
            }
        };
        drop(state);
        drop(dispatch_guard);
        if pending_consumed {
            active_window_rx.wake_all();
            if !std::ptr::eq(active_window_rx, self.as_ref()) {
                self.wake_all();
            }
        }
        #[cfg(feature = "test-fakes")]
        self.timeout_rescue_read_hook_for_test();
        (rescue_guard, human_submit_seen)
    }

    /// 以正式 timeout 線性化讀取器驗證任一 bridge epoch 的 session 訊號。
    #[cfg(feature = "test-fakes")]
    pub fn timeout_rescue_human_seen_for_test(
        self: &Arc<Self>,
        command_start_count: u64,
        command_human_generation: u64,
    ) -> bool {
        let (rescue_guard, human_seen) =
            self.begin_timeout_rescue(command_start_count, command_human_generation);
        drop(rescue_guard);
        human_seen
    }

    pub(crate) fn human_submit_start_count(&self) -> u64 {
        self.actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .human_submit_start_count
    }

    #[cfg(test)]
    pub(crate) fn tx_submit_pending(&self) -> bool {
        self.inner
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .tx_submit_pending
    }

    #[cfg(feature = "test-fakes")]
    pub fn human_submit_attempts_for_test(&self) -> u64 {
        self.human_submit_attempts.load(Ordering::Acquire)
    }

    #[cfg(feature = "test-fakes")]
    pub fn human_submit_start_count_for_test(&self) -> u64 {
        self.actor_dispatch_gate.human_signals().0
    }

    #[cfg(feature = "test-fakes")]
    pub fn human_in_flight_for_test(&self) -> usize {
        self.actor_dispatch_gate.human_signals().1
    }

    #[cfg(feature = "test-fakes")]
    pub fn human_pump_submissions_for_test(&self) -> u64 {
        self.human_pump_submissions.load(Ordering::Acquire)
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_human_pump_before_take_hook_for_test(
        &self,
        hook: Option<SnapshotWaitDeadlineHook>,
    ) {
        *self
            .human_pump_before_take_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_human_pump_after_take_hook_for_test(
        &self,
        hook: Option<SnapshotWaitDeadlineHook>,
    ) {
        *self
            .human_pump_after_take_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    pub(crate) fn note_human_pump_submission_for_test(&self) {
        self.human_pump_submissions.fetch_add(1, Ordering::AcqRel);
    }

    #[cfg(feature = "test-fakes")]
    pub(crate) fn human_pump_before_take_for_test(&self) {
        if let Some(hook) = self
            .human_pump_before_take_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
        {
            hook();
        }
    }

    #[cfg(feature = "test-fakes")]
    pub(crate) fn human_pump_after_take_for_test(&self) {
        if let Some(hook) = self
            .human_pump_after_take_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
        {
            hook();
        }
    }

    #[cfg(feature = "test-fakes")]
    pub fn submit_human_job_for_test(
        self: &Arc<Self>,
        tx: &crate::tx_writer::TxWriterHandle,
        job: crate::tx_writer::TxJob,
    ) -> Option<((), HumanInFlightGuard)> {
        let mut guard = self.begin_human_submit();
        self.with_human_submit_gate(
            &mut guard,
            job,
            |job| tx.try_enqueue_job(job),
            || tx.capacity_generation(),
            |generation| tx.wait_for_capacity(generation),
        )
        .ok()
        .map(|()| ((), guard))
    }

    #[cfg(feature = "test-fakes")]
    pub fn timeout_rescue_gate_available_for_test(&self) -> bool {
        let state = self.actor_dispatch_gate.state.lock().unwrap_or_else(|e| e.into_inner());
        !state.human_enqueue_active && state.rescue_pending == 0
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_snapshot_wait_before_deadline_snapshot_hook_for_test(
        &self,
        hook: Option<SnapshotWaitDeadlineHook>,
    ) {
        *self
            .snapshot_wait_before_deadline_snapshot_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_timeout_rescue_decision_hook_for_test(
        &self,
        hook: Option<SnapshotWaitDeadlineHook>,
    ) {
        *self
            .timeout_rescue_decision_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_human_tx_before_effect_hook_for_test(
        &self,
        hook: Option<SnapshotWaitEnterHook>,
    ) {
        *self
            .human_tx_before_effect_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_dispatch_before_guard_registration_hook_for_test(
        &self,
        hook: Option<SnapshotWaitEnterHook>,
    ) {
        *self
            .dispatch_before_guard_registration_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    pub fn dispatch_before_guard_registration_hook_for_test(&self) {
        if let Some(hook) = self
            .dispatch_before_guard_registration_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
        {
            hook();
        }
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_actor_dispatch_gate_hook_for_test(
        &self,
        hook: Option<Arc<dyn Fn(ActorDispatchGateEvent) + Send + Sync>>,
    ) {
        self.actor_dispatch_gate.set_hook(hook);
    }

    #[cfg(feature = "test-fakes")]
    pub fn actor_dispatch_gate_waits_for_test(&self) -> (u64, u64) {
        self.actor_dispatch_gate.waits_for_test()
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_timeout_rescue_attempt_hook_for_test(&self, hook: Option<SnapshotWaitEnterHook>) {
        *self
            .timeout_rescue_attempt_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    fn timeout_rescue_attempt_hook_for_test(&self) {
        if let Some(hook) = self
            .timeout_rescue_attempt_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
        {
            hook();
        }
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_timeout_rescue_read_hook_for_test(&self, hook: Option<SnapshotWaitEnterHook>) {
        *self
            .timeout_rescue_read_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    fn timeout_rescue_read_hook_for_test(&self) {
        if let Some(hook) = self
            .timeout_rescue_read_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
        {
            hook();
        }
    }

    #[cfg(feature = "test-fakes")]
    pub fn human_tx_before_effect_hook_for_test(&self) {
        if let Some(hook) = self
            .human_tx_before_effect_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
        {
            hook();
        }
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_human_timeout_decision_hook_for_test(
        &self,
        hook: Option<SnapshotWaitEnterHook>,
    ) {
        *self
            .human_timeout_decision_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    pub fn human_timeout_decision_hook_for_test(&self) {
        if let Some(hook) = self
            .human_timeout_decision_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
        {
            hook();
        }
    }

    #[cfg(feature = "test-fakes")]
    pub fn timeout_rescue_decision_hook_for_test(&self) {
        if let Some(hook) = self
            .timeout_rescue_decision_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
        {
            hook();
        }
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_human_tx_report_hook_for_test(&self, hook: Option<SnapshotWaitDeadlineHook>) {
        *self
            .human_tx_report_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    pub(crate) fn human_tx_report_hook_for_test(&self) {
        if let Some(hook) = self
            .human_tx_report_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
        {
            hook();
        }
    }

    #[cfg(feature = "test-fakes")]
    pub fn regex_wait_starts_for_test(&self) -> u64 {
        self.regex_wait_starts.load(Ordering::Acquire)
    }

    #[cfg(feature = "test-fakes")]
    pub fn regex_wait_checks_for_test(&self) -> u64 {
        self.regex_wait_checks.load(Ordering::Acquire)
    }

    #[cfg(feature = "test-fakes")]
    pub fn regex_wait_snapshot_end_for_test(&self) -> u64 {
        self.regex_wait_snapshot_end.load(Ordering::Acquire)
    }

    #[cfg(feature = "test-fakes")]
    pub fn snapshot_wait_progress_for_test(&self) -> (u64, u64) {
        (
            self.snapshot_wait_checks.load(Ordering::Acquire),
            self.snapshot_wait_end.load(Ordering::Acquire),
        )
    }

    #[cfg(feature = "test-fakes")]
    pub fn wait_for_reader_bytes_for_test(&self, expected: u64, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        while window.total_bytes < expected {
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            let (next, timed_out) = self
                .changed
                .wait_timeout(window, deadline.saturating_duration_since(now))
                .unwrap_or_else(|error| error.into_inner());
            window = next;
            if timed_out.timed_out() && window.total_bytes < expected {
                return false;
            }
        }
        true
    }

    #[cfg(feature = "test-fakes")]
    pub fn wait_for_snapshot_wait_progress_for_test(
        &self,
        after_checks: u64,
        expected_end: u64,
        timeout: Duration,
    ) -> bool {
        let deadline = Instant::now() + timeout;
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        loop {
            if self.snapshot_wait_checks.load(Ordering::Acquire) > after_checks
                && self.snapshot_wait_end.load(Ordering::Acquire) >= expected_end
            {
                return true;
            }
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            let (next, timed_out) = self
                .changed
                .wait_timeout(window, deadline.saturating_duration_since(now))
                .unwrap_or_else(|error| error.into_inner());
            window = next;
            if timed_out.timed_out()
                && (self.snapshot_wait_checks.load(Ordering::Acquire) <= after_checks
                    || self.snapshot_wait_end.load(Ordering::Acquire) < expected_end)
            {
                return false;
            }
        }
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_snapshot_wait_match_hook_for_test(
        &self,
        hook: Option<SnapshotWaitHook>,
    ) {
        *self
            .snapshot_wait_match_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_snapshot_wait_enter_hook_for_test(
        &self,
        hook: Option<SnapshotWaitEnterHook>,
    ) {
        *self
            .snapshot_wait_enter_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_snapshot_wait_evaluated_hook_for_test(
        &self,
        hook: Option<SnapshotWaitHook>,
    ) {
        *self
            .snapshot_wait_evaluated_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_prompt_evidence_before_write_hook_for_test(
        &self,
        hook: Option<SnapshotWaitHook>,
    ) {
        *self
            .prompt_evidence_before_write_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    #[cfg(feature = "test-fakes")]
    pub(crate) fn prompt_evidence_before_write_hook_for_test(&self, snapshot: &RxSnapshot) {
        if let Some(hook) = self
            .prompt_evidence_before_write_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
        {
            hook(snapshot);
        }
    }

    #[cfg(feature = "test-fakes")]
    pub fn set_snapshot_wait_deadline_hook_for_test(
        &self,
        hook: Option<Arc<dyn Fn() + Send + Sync>>,
    ) {
        *self
            .snapshot_wait_deadline_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = hook;
    }

    /// P8 Task 5：取消本 bridge 上進行中的檔案傳輸等待（watching_transfers 的 `RunCancel` 轉真）並喚醒等待者。
    /// 不動 login run 的取消世代（P7 SC-30 行為不變）。
    pub fn cancel_transfer_runs(&self) {
        self.transfer_cancel_gen.fetch_add(1, Ordering::AcqRel);
        self.wake_all();
    }

    /// SC-30：取消本 bridge 上所有既存的 login run（其 `RunCancel::is_triggered` 轉真）並喚醒等待者。
    pub fn cancel_login_runs(&self) {
        self.run_cancel_gen.fetch_add(1, Ordering::AcqRel);
        self.wake_all();
    }

    /// issue 297：Phase 1 凍結 Shutdown RX 等待；狀態只會由 false 轉為 true。
    pub fn cancel_shutdown_waits(&self) {
        self.shutdown_waits_cancelled.store(true, Ordering::Release);
        self.wake_all();
    }

    /// P7 Task 10：掛上／卸下 agent log 寫入者（SessionCore 於 log_start／stop／bridge 交接時呼叫）。
    pub fn set_capture(&self, capture: Option<Arc<crate::capture::CaptureObserver>>) {
        *self.capture.lock().unwrap_or_else(|e| e.into_inner()) = capture;
    }

    /// 喚醒所有可取消的 regex 等待者（容量 1、滿則略過；已斷線者移除）。
    fn wake_all(&self) {
        self.changed.notify_all();
        let mut wakers = self
            .wakers
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        wakers.retain(|tx| {
            !matches!(
                tx.try_send(()),
                Err(crossbeam_channel::TrySendError::Disconnected(()))
            )
        });
    }

    pub fn append(&self, data: &[u8], now: Mono, epoch: u64, rx_seq: u64) {
        self.append_with_time(data, || now, epoch, rx_seq);
    }

    /// 使用同一 monotonic clock 在 RX 視窗鎖內記錄追加時間與 watermark。
    pub(crate) fn append_with_clock(
        &self,
        data: &[u8],
        clock: &dyn Clock,
        epoch: u64,
        rx_seq: u64,
    ) {
        self.append_with_time(data, || clock.mono_ns(), epoch, rx_seq);
    }

    fn append_with_time(
        &self,
        data: &[u8],
        event_time: impl FnOnce() -> Mono,
        epoch: u64,
        rx_seq: u64,
    ) {
        let mut human = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let prompt_regex = self.actor_dispatch_gate.prompt_regex();
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        if human.active_rx_epoch.is_none()
            && human
                .active_rx
                .as_ref()
                .and_then(Weak::upgrade)
                .is_some_and(|active| std::ptr::eq(Arc::as_ptr(&active), self))
        {
            human.active_rx_epoch = Some(epoch);
        }
        let now = event_time();
        window.watermark = (epoch, rx_seq);
        window.revision = window.revision.saturating_add(1);
        if !data.is_empty() {
            window.total_bytes = window.total_bytes.saturating_add(data.len() as u64);
            window.last_rx_mono = now;
            window.prune_rate(now);
            window.rate.push_back((now, data.len() as u64));
            window.cap_rate();
            let decoded = decode_utf8_replace(data);
            let start_offset = window
                .trimmed
                .saturating_add(window.text.chars().count() as u64);
            window.text.push_str(&decoded);
            window.trim_to_limit();
            if window.completion_capture_op_id.is_some()
                && window
                    .completion_deadline
                    .is_none_or(|deadline| now <= deadline)
            {
                let end_offset = start_offset.saturating_add(decoded.chars().count() as u64);
                let watermark = window.watermark;
                window.push_rx_event(RxEvent {
                    start_offset,
                    end_offset,
                    mono_ns: now,
                    watermark,
                    text: decoded,
                });
            }
        }
        if !data.is_empty() {
            settle_pending_human_locked(&mut human, &window, prompt_regex.as_deref(), epoch, now);
        }
        drop(window);
        drop(human);
        if !data.is_empty() {
            let capture = self
                .capture
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .clone();
            if let Some(capture) = capture {
                capture.write_rx(data);
            }
        }
        self.wake_all();
    }

    pub fn watermark(&self) -> (u64, u64) {
        self.inner
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .watermark
    }

    pub fn clear(&self) {
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        window.trimmed = window
            .trimmed
            .saturating_add(window.text.chars().count() as u64);
        window.text.clear();
        window.prompt_evidence = None;
        window.clear_rx_events();
        window.revision = window.revision.saturating_add(1);
        drop(window);
        self.wake_all();
    }

    /// READY probe 的獨立 nonce 輸出後、以完整清理尾行確認 prompt；只記錄，不改登入判準。
    pub(crate) fn observe_ready_prompt_evidence(
        &self,
        prompt: &PyRegex,
        probe: &str,
        marker: &str,
        before_probe: &str,
        before_probe_offset: u64,
    ) {
        let state = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let prompt_revision = state.prompt_revision;
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        window.prompt_evidence = None;
        let probe_output = (before_probe_offset >= window.trimmed)
            .then(|| window.text_from(before_probe_offset).to_string());
        let Some(probe_output) = probe_output else {
            return;
        };
        if let Some(literal) = ready_prompt_literal_after_marker(
            &probe_output,
            probe,
            marker,
            before_probe,
            prompt,
        ) {
            window.prompt_evidence = Some(PromptEvidence {
                literal,
                watermark: window.watermark,
                prompt_revision,
            });
        }
    }

    /// 只把與同一 snapshot／profile revision 一致的完整 echo 前綴寫回 epoch 證據。
    pub(crate) fn remember_prompt_evidence(
        self: &Arc<Self>,
        literal: String,
        window_revision: u64,
        watermark: (u64, u64),
        prompt_revision: u64,
    ) {
        if !prompt_evidence_literal_is_identifiable(&literal) {
            return;
        }
        let state = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if state.prompt_revision != prompt_revision
            || state
                .active_rx
                .as_ref()
                .and_then(Weak::upgrade)
                .is_some_and(|active| !std::ptr::eq(Arc::as_ptr(&active), Arc::as_ptr(self)))
        {
            return;
        }
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        if window.revision != window_revision
            || window.watermark != watermark
            || literal.is_empty()
        {
            return;
        }
        window.prompt_evidence = Some(PromptEvidence {
            literal,
            watermark,
            prompt_revision,
        });
    }

    pub fn snapshot_len(&self) -> u64 {
        let window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        window
            .trimmed
            .saturating_add(window.text.chars().count() as u64)
    }

    pub fn text_from(&self, from: u64) -> String {
        self.inner
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .text_from(from)
            .to_string()
    }

    /// 取得同一個 RX watermark 下的 snapshot；若窗口已修剪會把 `base` 推進到保留資料起點。
    pub fn snapshot_from(&self, from: u64) -> RxSnapshot {
        let human = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        snapshot_from_window(&window, from, &human)
    }

    /// 回傳最近可信 human TX 世代、其 TX 前 RX offset 與目前 raw 輸入是否仍是半行。
    pub fn human_tx_state(&self) -> HumanTxState {
        let human = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        human_tx_state(&human, window.watermark.0)
    }

    #[cfg(feature = "test-fakes")]
    pub fn last_human_state_event_kind_for_test(&self) -> Option<HumanStateEventKind> {
        self.actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .human_state_events
            .back()
            .map(|event| event.kind)
    }

    /// 測試控制尚未完成的 submit 窗口；用於重現 OS submit 與 human TX observer 登錄間的交錯。
    #[cfg(feature = "test-fakes")]
    pub fn set_tx_submit_pending_for_test(&self, pending: bool) {
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        window.tx_submit_pending = pending;
        window.revision = window.revision.saturating_add(1);
        drop(window);
        self.wake_all();
    }

    #[cfg(feature = "test-fakes")]
    pub fn record_human_tx_for_test(self: &Arc<Self>, payload: &[u8], clock: &dyn Clock) {
        let mut submit_guard = self.begin_human_submit();
        submit_guard.mark_human_submit_started();
        let mut human = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        let event_mono = clock.mono_ns();
        let epoch = human.active_rx_epoch.unwrap_or(window.watermark.0);
        let rx_offset = window
            .trimmed
            .saturating_add(window.text.chars().count() as u64);
        record_human_tx_locked(
            &mut human,
            payload,
            true,
            rx_offset,
            epoch,
            event_mono,
        );
        window.revision = window.revision.saturating_add(1);
        drop(window);
        drop(human);
        drop(submit_guard);
        self.wake_all();
    }

    /// agent command 邊界於 OS submit 前候選擷取；human TX 邊界則在成功 submit 返回後取 active RX watermark。
    /// WouldBlock／NotSubmitted 不建立 human TX 邊界，也不固定 command 起點，下一次實際提交會重新取 watermark。
    pub(crate) fn observe_tx_submit(
        &self,
        command_op_id: Option<u64>,
        command_boundary_recorded: bool,
        writer_epoch: u64,
        human_payload: Option<(&[u8], bool, usize)>,
        clock: &dyn Clock,
        submit: &mut dyn FnMut() -> SubmitStep,
    ) -> (SubmitStep, bool) {
        // agent command 的候選起點在 submit 前取值；human 的有效邊界稍後在 submit 成功返回後取值。
        // 此處先發布 submit-pending，隨即釋放鎖再呼叫 OS。human guard／submit-pending 使交錯 RX
        // 不能在 write 結果確定前清除 pending；OS write 可能等待 RX reader，因此不可跨 write closure
        // 持有 gate 或 RX mutex。
        let candidate_boundary = {
            let human = self
                .actor_dispatch_gate
                .state
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
            let boundary = command_op_id.filter(|_| !command_boundary_recorded).map(|_| {
                command_boundary(&human, &window)
            });
            window.tx_submit_pending = true;
            window.tx_submit_pending_started_at = Some(clock.mono_ns());
            drop(window);
            boundary
        };
        let mut pending_guard = TxSubmitPendingGuard {
            rx: self,
            clock,
            human_submit: human_payload.is_some(),
            accepted_or_pending: false,
        };
        let step = submit();
        let accepted = matches!(&step, SubmitStep::Accepted(count) if *count > 0);
        let submitted = accepted || matches!(&step, SubmitStep::Pending);
        pending_guard.accepted_or_pending = submitted;
        let mut boundary_recorded = command_boundary_recorded;

        if submitted {
            let mut human = self
                .actor_dispatch_gate
                .state
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            let human_boundary = human_payload
                .map(|_| human_boundary_after_submit(self, &human, writer_epoch));
            {
                let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
                let event_mono = clock.mono_ns();
                if let (Some(op_id), Some(boundary)) = (command_op_id, candidate_boundary) {
                    if !command_boundary_recorded {
                        window.command_tx_boundary = Some((op_id, boundary));
                        boundary_recorded = true;
                    }
                }
                match (human_payload, &step) {
                    (Some((payload, raw_input, offset)), SubmitStep::Accepted(count)) if *count > 0 => {
                        let start = offset.min(payload.len());
                        let end = start.saturating_add(*count).min(payload.len());
                        if end > start {
                            let (epoch, rx_offset) = human_boundary
                                .expect("human submit 成功返回後應擷取 RX watermark");
                            record_human_tx_locked(
                                &mut human,
                                &payload[start..end],
                                raw_input,
                                rx_offset,
                                epoch,
                                event_mono,
                            );
                        }
                    }
                    (Some((_, raw_input, _)), SubmitStep::Pending) => {
                        let (epoch, rx_offset) = human_boundary
                            .expect("human submit 成功返回後應擷取 RX watermark");
                        record_human_tx_unknown_locked(
                            &mut human,
                            raw_input,
                            rx_offset,
                            epoch,
                            event_mono,
                        );
                    }
                    _ => {}
                }
            }
        }

        drop(pending_guard);
        (step, boundary_recorded)
    }

    /// TX writer 完成後以確認的 human raw bytes 校正半行；結果不明時保持保守開啟。
    pub(crate) fn finish_human_raw_tx(&self, payload: &[u8], confirmed: usize, unknown: bool) {
        let mut human = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        if unknown {
            human.human_raw_line_open = true;
        } else {
            let accepted = confirmed.min(payload.len());
            if accepted > 0 {
                update_raw_line_state(&mut human, &payload[..accepted]);
            }
        }
        let state = human_tx_state(&human, window.watermark.0);
        if let Some(event) = human.human_state_events.back_mut() {
            if event.state.generation == state.generation {
                event.state = state;
            }
        }
        window.revision = window.revision.saturating_add(1);
        drop(window);
        drop(human);
        self.wake_all();
    }

    /// agent 命令確實送到換行時，該換行已結束由 human 開啟的板端半行。
    pub(crate) fn finish_command_tx(&self, payload: &[u8], confirmed: usize) {
        let Some(line_end) = payload
            .iter()
            .position(|byte| matches!(*byte, b'\r' | b'\n'))
            .map(|index| index + 1)
        else {
            return;
        };
        if confirmed < line_end {
            return;
        }
        let mut human = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        human.human_raw_line_open = false;
        window.revision = window.revision.saturating_add(1);
        drop(window);
        drop(human);
        self.wake_all();
    }

    /// 讀取指定 op 的 command TX 邊界；下一筆 agent command 會覆寫此單一在途記錄。
    pub fn command_tx_boundary(&self, op_id: u64) -> Option<CommandTxBoundary> {
        let _human = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        window
            .command_tx_boundary
            .as_ref()
            .filter(|(recorded_op, _)| *recorded_op == op_id)
            .map(|(_, boundary)| boundary.clone())
    }

    pub(crate) fn begin_command_completion_observation(&self, op_id: u64) {
        let mut human = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        human.completion_capture_op_id = Some(op_id);
        human.human_state_events.clear();
        window.clear_rx_events();
        window.completion_capture_op_id = Some(op_id);
        window.completion_deadline = None;
        window.completion_tx_submit_crossed_deadline = false;
    }

    pub(crate) fn set_command_completion_deadline(&self, op_id: u64, deadline: Mono) {
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        if window.completion_capture_op_id != Some(op_id) {
            return;
        }
        window.completion_deadline = Some(deadline);
        window.completion_tx_submit_crossed_deadline = false;
        window.rx_events.retain(|event| event.mono_ns <= deadline);
        window.rx_event_chars = window
            .rx_events
            .iter()
            .map(|event| event.text.chars().count())
            .sum();
    }

    pub(crate) fn finish_command_completion_observation(&self, op_id: u64) {
        let mut human = self
            .actor_dispatch_gate
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        if human.completion_capture_op_id == Some(op_id) {
            human.completion_capture_op_id = None;
            human.human_state_events.clear();
        }
        if window.completion_capture_op_id == Some(op_id) {
            window.completion_capture_op_id = None;
            window.completion_deadline = None;
            window.completion_tx_submit_crossed_deadline = false;
            window.clear_rx_events();
        }
        if window
            .command_tx_boundary
            .as_ref()
            .is_some_and(|(recorded_op, _)| *recorded_op == op_id)
        {
            window.command_tx_boundary = None;
        }
        drop(window);
        drop(human);
        self.wake_all();
    }

    pub fn tail(&self, max_chars: usize) -> String {
        let window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        if max_chars == 0 {
            return window.text.clone();
        }
        let skip = window.text.chars().count().saturating_sub(max_chars);
        let byte_index = window
            .text
            .char_indices()
            .nth(skip)
            .map_or(window.text.len(), |(index, _)| index);
        window.text[byte_index..].to_string()
    }

    /// P6 增補 #19：Python `text[-n:]`（n>0 尾 n 字元、n==0 全窗、n<0 去掉開頭 |n| 字元）。
    pub fn tail_py(&self, n: i128) -> String {
        if n >= 0 {
            return self.tail(n as usize);
        }
        let text = self.tail(0);
        let skip = n.unsigned_abs().min(usize::MAX as u128) as usize;
        text.chars().skip(skip).collect()
    }

    pub fn rx_stats(&self, now: Mono) -> (u64, u64) {
        let fixed = self.stats_override.load(Ordering::Acquire);
        if fixed != u64::MAX {
            return (fixed, fixed / 10);
        }
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        window.prune_rate(now);
        let total = window
            .rate
            .iter()
            .fold(0u64, |sum, (_, bytes)| sum.saturating_add(*bytes));
        (total, total / 10)
    }

    pub fn total_bytes(&self) -> u64 {
        self.inner
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .total_bytes
    }

    pub fn idle_s(&self, now: Mono) -> f64 {
        let last_rx = self
            .inner
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .last_rx_mono;
        now.saturating_sub(last_rx) as f64 / 1_000_000_000.0
    }

    pub fn last_rx_mono(&self) -> u64 {
        self.inner
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .last_rx_mono
    }

    pub fn trimmed(&self) -> u64 {
        self.inner
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .trimmed
    }

    /// 以 condvar 等待 `re` 命中（非輪詢）；逾時回 `None`。
    pub fn wait_for_regex(
        &self,
        re: &PyRegex,
        timeout: Duration,
        clock: &dyn Clock,
    ) -> Option<RxMatch> {
        self.wait_for_regex_from(re, 0, timeout, clock)
    }

    /// 同 `wait_for_regex`，但只比對絕對偏移 `from` 之後的文字。
    pub fn wait_for_regex_from(
        &self,
        re: &PyRegex,
        from: u64,
        timeout: Duration,
        clock: &dyn Clock,
    ) -> Option<RxMatch> {
        self.wait_for_match_from(from, timeout, clock, |text| re.search(text).is_some())
    }

    /// 等待 predicate 成功，或依指定期限重建截止水位 snapshot。
    pub fn wait_for_snapshot_from<T>(
        &self,
        from: u64,
        timeout: Duration,
        clock: &dyn Clock,
        matches: impl FnMut(&RxSnapshot) -> Option<T>,
    ) -> SnapshotWaitResult<T> {
        let timeout_ns = timeout.as_nanos().min(u64::MAX as u128) as u64;
        let deadline = clock.mono_ns().saturating_add(timeout_ns);
        let (human_tx_before, watermark_before) = {
            let human = self
                .actor_dispatch_gate
                .state
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            let window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
            (
                human_tx_state(&human, window.watermark.0),
                window.watermark,
            )
        };
        self.wait_for_snapshot_until_from(
            from,
            deadline,
            human_tx_before,
            watermark_before,
            clock,
            matches,
        )
    }

    /// 等待 predicate 到絕對 monotonic deadline；逾時結算依事件時間重建 deadline snapshot。
    pub fn wait_for_snapshot_until_from<T>(
        &self,
        from: u64,
        deadline: Mono,
        human_tx_before: HumanTxState,
        watermark_before: (u64, u64),
        clock: &dyn Clock,
        mut matches: impl FnMut(&RxSnapshot) -> Option<T>,
    ) -> SnapshotWaitResult<T> {
        #[cfg(feature = "test-fakes")]
        if let Some(hook) = self
            .snapshot_wait_enter_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
        {
            hook();
        }

        loop {
            let human = self
                .actor_dispatch_gate
                .state
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            let window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
            if clock.mono_ns() >= deadline {
                drop(window);
                drop(human);
                break;
            }
            if window.tx_submit_pending {
                let remaining = Duration::from_nanos(deadline.saturating_sub(clock.mono_ns()));
                let wait_for = remaining.min(RX_WAIT_CLOCK_SLICE);
                drop(human);
                let (next, _) = self
                    .changed
                    .wait_timeout(window, wait_for)
                    .unwrap_or_else(|error| error.into_inner());
                drop(next);
                continue;
            }
            let snapshot = snapshot_from_window(&window, from, &human);
            let revision = window.revision;
            drop(window);
            drop(human);

            let value = matches(&snapshot);
            #[cfg(feature = "test-fakes")]
            {
                if let Some(hook) = self
                    .snapshot_wait_evaluated_hook
                    .lock()
                    .unwrap_or_else(|error| error.into_inner())
                    .take()
                {
                    hook(&snapshot);
                }
                let _human = self
                    .actor_dispatch_gate
                    .state
                    .lock()
                    .unwrap_or_else(|error| error.into_inner());
                let _window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
                self.snapshot_wait_end.store(
                    snapshot
                        .base
                        .saturating_add(snapshot.text.chars().count() as u64),
                    Ordering::Release,
                );
                self.snapshot_wait_checks.fetch_add(1, Ordering::AcqRel);
                self.changed.notify_all();
            }
            let human = self
                .actor_dispatch_gate
                .state
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            let window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
            if clock.mono_ns() >= deadline {
                drop(window);
                drop(human);
                break;
            }
            let stale_snapshot = window.revision != revision
                || window.revision != snapshot.revision
                || human.prompt_revision != snapshot.prompt_revision
                || human
                    .active_rx
                    .as_ref()
                    .and_then(Weak::upgrade)
                    .is_some_and(|active| !std::ptr::eq(active.as_ref(), self))
                || human_tx_state(&human, window.watermark.0) != snapshot.human_tx_state
                || window.tx_submit_pending != snapshot.tx_submit_pending
                || human.human_submit_start_count != snapshot.human_submit_start_count
                || human.human_in_flight != snapshot.human_in_flight;
            if stale_snapshot {
                drop(window);
                drop(human);
                continue;
            }
            if let Some(value) = value {
                drop(window);
                drop(human);
                #[cfg(feature = "test-fakes")]
                if let Some(hook) = self
                    .snapshot_wait_match_hook
                    .lock()
                    .unwrap_or_else(|error| error.into_inner())
                    .clone()
                {
                    hook(&snapshot);
                }
                return SnapshotWaitResult::Matched { snapshot, value };
            }
            let remaining = Duration::from_nanos(deadline.saturating_sub(clock.mono_ns()));
            let wait_for = remaining.min(RX_WAIT_CLOCK_SLICE);
            drop(human);
            let (next, _) = self
                .changed
                .wait_timeout(window, wait_for)
                .unwrap_or_else(|error| error.into_inner());
            drop(next);
        }
        #[cfg(feature = "test-fakes")]
        if let Some(hook) = self
            .snapshot_wait_before_deadline_snapshot_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
        {
            hook();
        }
        let deadline_snapshot = {
            let human = self
                .actor_dispatch_gate
                .state
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            let window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
            snapshot_from_window_at(
                &window,
                from,
                deadline,
                human_tx_before,
                watermark_before,
                &human,
            )
        };
        #[cfg(feature = "test-fakes")]
        if let Some(hook) = self
            .snapshot_wait_deadline_hook
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
        {
            hook();
        }
        SnapshotWaitResult::TimedOut {
            last_snapshot: Some(deadline_snapshot),
        }
    }

    #[cfg(feature = "test-fakes")]
    pub fn wait_for_regex_from_with<F>(
        &self,
        from: u64,
        timeout: Duration,
        clock: &dyn Clock,
        matches: F,
    ) -> Option<RxMatch>
    where
        F: Fn(&str) -> bool,
    {
        self.wait_for_match_from(from, timeout, clock, matches)
    }

    fn wait_for_match_from<F>(
        &self,
        from: u64,
        timeout: Duration,
        clock: &dyn Clock,
        matches: F,
    ) -> Option<RxMatch>
    where
        F: Fn(&str) -> bool,
    {
        #[cfg(feature = "test-fakes")]
        self.regex_wait_starts.fetch_add(1, Ordering::AcqRel);
        let timeout_ns = timeout.as_nanos().min(u64::MAX as u128) as u64;
        let deadline = clock.mono_ns().saturating_add(timeout_ns);
        if clock.mono_ns() >= deadline {
            return None;
        }

        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        while clock.mono_ns() < deadline {
            let snapshot = window.text_from(from).to_string();
            let watermark = window.watermark;
            let revision = window.revision;
            drop(window);

            if matches(&snapshot) {
                return Some(RxMatch {
                    epoch: watermark.0,
                    rx_seq: watermark.1,
                });
            }

            window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
            if window.revision != revision {
                continue;
            }
            let remaining = Duration::from_nanos(deadline.saturating_sub(clock.mono_ns()));
            let wait_for = remaining.min(RX_WAIT_CLOCK_SLICE);
            let (next, _) = self
                .changed
                .wait_timeout(window, wait_for)
                .unwrap_or_else(|error| error.into_inner());
            window = next;
        }
        None
    }

    #[cfg(feature = "test-fakes")]
    pub fn rate_window_len(&self) -> usize {
        self.inner
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .rate
            .len()
    }
}

/// 命中當下（同一把鎖）的 watermark；`OpFinished`／`LoginFinished` 的 `after_rx_seq` 一律取此值。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RxMatch {
    pub epoch: u64,
    pub rx_seq: u64,
}

#[derive(Debug, Clone)]
pub struct RxSnapshot {
    /// 此 snapshot 實際保留文字的絕對起點。
    pub base: u64,
    /// 已解碼的 RX 文字。
    pub text: String,
    /// 同一把鎖下讀取的 `(epoch, rx_seq)`。
    pub watermark: (u64, u64),
    /// 同一個 RX watermark 下的 human TX 狀態。
    pub human_tx_state: HumanTxState,
    /// 同一個 RX watermark 下的單調 human submit 開始計數。
    pub human_submit_start_count: u64,
    /// 同一個 RX watermark 下的 session human 在途數。
    pub human_in_flight: usize,
    /// snapshot 建立時是否仍有尚未完成 observer 登錄的 OS submit。
    pub tx_submit_pending: bool,
    /// `base > requested_from` 時，所需的早期 RX 已被窗口修剪。
    pub trimmed_before_from: bool,
    /// snapshot 當下可用的完整 prompt 字面與 profile revision。
    pub revision: u64,
    pub prompt_revision: u64,
    pub prompt_evidence: Option<PromptEvidence>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PromptEvidence {
    pub(crate) literal: String,
    watermark: (u64, u64),
    prompt_revision: u64,
}

/// 一次 completion snapshot 等待的結果。逾時時附上只含事件時間不晚於 deadline 的 snapshot。
pub enum SnapshotWaitResult<T> {
    Matched { snapshot: RxSnapshot, value: T },
    TimedOut { last_snapshot: Option<RxSnapshot> },
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct HumanTxState {
    pub generation: u64,
    /// 最近 human TX 所屬 epoch；跨 epoch snapshot 不會沿用其 RX offset。
    pub epoch: Option<u64>,
    pub rx_offset: u64,
    pub raw_line_open: bool,
    /// session 是否仍有 human TX 等待其所屬 epoch 的 prompt。
    pub pending_human: bool,
}

#[derive(Debug, Clone)]
pub struct CommandTxBoundary {
    /// OS submit 之前的絕對 RX 字元偏移。
    pub rx_offset: u64,
    /// 同一 RX watermark 下的可信 human TX 狀態。
    pub human_tx_state: HumanTxState,
    /// command OS submit 起點當下的 human submit 開始計數。
    pub human_submit_start_count: u64,
    /// command TX 前 RX watermark，供 deadline settlement 作為無後續事件時的基線。
    pub watermark: (u64, u64),
    /// command TX 前的 profile revision；profile 切換後舊 evaluator 不可完成。
    pub prompt_revision: u64,
}

struct TxSubmitPendingGuard<'a> {
    rx: &'a RxShared,
    clock: &'a dyn Clock,
    human_submit: bool,
    accepted_or_pending: bool,
}

/// human writer job 的在途身分；涵蓋佇列等待、OS submit 與 writer report 等待。
#[doc(hidden)]
pub struct HumanInFlightGuard {
    rx: Arc<RxShared>,
    submit_started: bool,
}

/// human submit 起點與 timeout 救援排程共用的短期仲裁鎖。
pub(crate) struct HumanSubmitRescueGuard {
    gate: Arc<ActorDispatchGate>,
    active: bool,
}

impl Drop for TxSubmitPendingGuard<'_> {
    fn drop(&mut self) {
        let completed_at = self.clock.mono_ns();
        let mut window = self
            .rx
            .inner
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if self.human_submit
            && self.accepted_or_pending
            && window.completion_deadline.is_some_and(|deadline| {
                window
                    .tx_submit_pending_started_at
                    .is_some_and(|started_at| started_at <= deadline && completed_at > deadline)
            })
        {
            // Submit 在 deadline 前開始、deadline 後才完成觀察登錄；即使 waiter 取得鎖前 pending 已清除，
            // deadline snapshot 仍須把這段未結算的 human TX 視為窗口內活動。
            window.completion_tx_submit_crossed_deadline = true;
        }
        window.tx_submit_pending = false;
        window.tx_submit_pending_started_at = None;
        window.revision = window.revision.saturating_add(1);
        drop(window);
        self.rx.wake_all();
    }
}

impl Drop for HumanInFlightGuard {
    fn drop(&mut self) {
        self.rx.unregister_human_in_flight();
    }
}

impl HumanInFlightGuard {
    /// 成功排入 writer 後記錄實際 human submit 起點；失敗、取消與未入列都不增加計數。
    pub fn mark_human_submit_started(&mut self) {
        if self.submit_started {
            return;
        }
        self.rx.register_human_submit_start();
        #[cfg(feature = "test-fakes")]
        self.rx.human_submit_attempts.fetch_add(1, Ordering::AcqRel);
        self.submit_started = true;
    }

    pub(crate) fn belongs_to(&self, rx: &Arc<RxShared>) -> bool {
        Arc::ptr_eq(&self.rx, rx)
    }
}

impl Drop for HumanSubmitRescueGuard {
    fn drop(&mut self) {
        if !self.active {
            return;
        }
        let mut state = self.gate.state.lock().unwrap_or_else(|error| error.into_inner());
        state.rescue_pending = state.rescue_pending.saturating_sub(1);
        drop(state);
        self.gate.changed.notify_all();
    }
}

fn snapshot_from_window(
    window: &RxWindow,
    from: u64,
    human: &ActorDispatchGateState,
) -> RxSnapshot {
    let base = from.max(window.trimmed);
    let watermark = window.watermark;
    let prompt_revision = human.prompt_revision;
    RxSnapshot {
        base,
        text: window.text_from(base).to_string(),
        watermark,
        human_tx_state: human_tx_state(human, window.watermark.0),
        human_submit_start_count: human.human_submit_start_count,
        human_in_flight: human.human_in_flight,
        tx_submit_pending: window.tx_submit_pending,
        trimmed_before_from: base > from,
        revision: window.revision,
        prompt_revision,
        prompt_evidence: snapshot_prompt_evidence(window, watermark, prompt_revision),
    }
}

fn snapshot_from_window_at(
    window: &RxWindow,
    from: u64,
    deadline: Mono,
    human_tx_before: HumanTxState,
    watermark_before: (u64, u64),
    human: &ActorDispatchGateState,
) -> RxSnapshot {
    let events: Vec<_> = window
        .rx_events
        .iter()
        .filter(|event| event.mono_ns <= deadline && event.end_offset > from)
        .collect();
    let first_event = events.first();
    let last_event = events.last();
    let history_start = first_event.map_or(from, |event| event.start_offset);
    let base = from.max(history_start);
    let end = last_event.map_or(base, |event| event.end_offset).max(base);
    let history: String = events.iter().flat_map(|event| event.text.chars()).collect();
    let skip_chars = usize::try_from(base.saturating_sub(history_start)).unwrap_or(usize::MAX);
    let take_chars = usize::try_from(end.saturating_sub(base)).unwrap_or(usize::MAX);
    let text: String = history.chars().skip(skip_chars).take(take_chars).collect();
    let human_tx_state = human
        .human_state_events
        .iter()
        .rev()
        .find(|event| event.mono_ns <= deadline)
        .map(|event| event.state)
        .unwrap_or(human_tx_before);
    let watermark = last_event
        .map(|event| event.watermark)
        .unwrap_or(watermark_before);
    let prompt_revision = human.prompt_revision;
    RxSnapshot {
        base,
        text,
        watermark,
        human_tx_state: human_tx_state_for_epoch(human_tx_state, window.watermark.0),
        human_submit_start_count: human.human_submit_start_count,
        human_in_flight: human.human_in_flight,
        tx_submit_pending: window.tx_submit_pending || window.completion_tx_submit_crossed_deadline,
        trimmed_before_from: base > from,
        revision: window.revision,
        prompt_revision,
        prompt_evidence: snapshot_prompt_evidence(window, watermark, prompt_revision),
    }
}

fn snapshot_prompt_evidence(
    window: &RxWindow,
    watermark: (u64, u64),
    prompt_revision: u64,
) -> Option<PromptEvidence> {
    window.prompt_evidence.as_ref().and_then(|evidence| {
        (evidence.watermark <= watermark && evidence.prompt_revision == prompt_revision)
            .then(|| evidence.clone())
    })
}

fn full_last_line_literal(raw: &str, prompt: &PyRegex) -> Option<String> {
    let cleaned = clean_text(raw);
    let literal = cleaned
        .rsplit_once('\n')
        .map_or(cleaned.as_str(), |(_, tail)| tail);
    if literal.is_empty()
        || !prompt_evidence_literal_is_identifiable(literal)
        || !prompt_full_literal_matches(literal, prompt)
    {
        return None;
    }

    Some(literal.to_string())
}

fn ready_prompt_literal_after_marker(
    raw: &str,
    probe: &str,
    marker: &str,
    before_probe: &str,
    prompt: &PyRegex,
) -> Option<String> {
    if marker.is_empty() {
        return None;
    }
    let probe = clean_text(probe);
    let default_marker = format!("__READY__{marker}");
    if probe.trim_end_matches(['\r', '\n']) == marker
        || probe.trim_end_matches(['\r', '\n']) == default_marker
    {
        return None;
    }
    let expected = full_last_line_literal(before_probe, prompt)?;
    let cleaned = clean_text(raw);
    let mut line_start = 0;
    while line_start < cleaned.len() {
        let line_end = cleaned[line_start..]
            .find('\n')
            .map_or(cleaned.len(), |offset| line_start + offset);
        let line = &cleaned[line_start..line_end];
        let output_without_line_echo =
            line.strip_prefix(&expected).is_some_and(|suffix| {
                suffix == marker || suffix == default_marker
            });
        if line == marker || line == default_marker || output_without_line_echo {
            let prompt_start = if line_end < cleaned.len() {
                line_end.saturating_add(1)
            } else {
                line_end
            };
            if prompt_start < cleaned.len() && cleaned[prompt_start..] == expected {
                return Some(expected);
            }
        }
        if line_end == cleaned.len() {
            break;
        }
        line_start = line_end.saturating_add(1);
    }
    None
}

pub(crate) fn prompt_evidence_literal_is_identifiable(literal: &str) -> bool {
    !literal.is_empty()
        && !literal.contains(['\0', '\r', '\n'])
        && literal.chars().any(char::is_alphanumeric)
}

fn prompt_full_literal_matches(literal: &str, prompt: &PyRegex) -> bool {
    if prompt.search(literal).is_some_and(|matched| {
        matched.start == 0 && matched.end == literal.chars().count()
    }) {
        return true;
    }
    let with_newline = format!("\n{literal}");
    prompt.search(&with_newline).is_some_and(|matched| {
        matched.start == 0 && matched.end == with_newline.chars().count()
    })
}

fn human_tx_state(human: &ActorDispatchGateState, rx_epoch: u64) -> HumanTxState {
    human_tx_state_for_epoch(
        HumanTxState {
            generation: human.human_tx_generation,
            epoch: human.human_tx_epoch,
            rx_offset: human.last_human_tx_rx_offset,
            raw_line_open: human.human_raw_line_open,
            pending_human: human.pending_human,
        },
        rx_epoch,
    )
}

fn human_tx_state_for_epoch(mut state: HumanTxState, rx_epoch: u64) -> HumanTxState {
    if state.epoch != Some(rx_epoch) {
        state.rx_offset = 0;
    }
    state
}

fn command_boundary(human: &ActorDispatchGateState, window: &RxWindow) -> CommandTxBoundary {
    let human_tx_state = human_tx_state(human, window.watermark.0);
    CommandTxBoundary {
        rx_offset: window
            .trimmed
            .saturating_add(window.text.chars().count() as u64),
        human_tx_state,
        human_submit_start_count: human.human_submit_start_count,
        watermark: window.watermark,
        prompt_revision: human.prompt_revision,
    }
}

fn human_boundary_after_submit(
    rx: &RxShared,
    human: &ActorDispatchGateState,
    writer_epoch: u64,
) -> (u64, u64) {
    let active_rx = human.active_rx.as_ref().and_then(Weak::upgrade);
    let active_window = active_rx.as_deref().unwrap_or(rx);
    let window = active_window
        .inner
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    let active_epoch = human.active_rx_epoch.unwrap_or(window.watermark.0);
    if writer_epoch < active_epoch {
        // 舊 handle 關閉先於新 handle 開啟；舊 epoch 的已接受 TX 必定位於新 epoch RX 之前。
        (active_epoch, 0)
    } else {
        let end_offset = window
            .trimmed
            .saturating_add(window.text.chars().count() as u64);
        if writer_epoch == active_epoch {
            (active_epoch, end_offset)
        } else {
            // Fence 不允許未來 epoch 的 permit；若內部狀態違反此前提，保守要求後續新行才可結案。
            (active_epoch, end_offset.saturating_add(1))
        }
    }
}

fn advance_pending_human_boundary(
    human: &mut ActorDispatchGateState,
    epoch: u64,
    rx_offset: u64,
) -> (u64, u64) {
    let current = human
        .pending_human_epoch
        .zip(human.pending_human_rx_offset);
    let candidate = (epoch, rx_offset);
    let latest = current.map_or(candidate, |current| current.max(candidate));
    human.pending_human_epoch = Some(latest.0);
    human.pending_human_rx_offset = Some(latest.1);
    latest
}

fn record_human_tx_locked(
    human: &mut ActorDispatchGateState,
    payload: &[u8],
    raw_input: bool,
    rx_offset: u64,
    epoch: u64,
    mono_ns: Mono,
) {
    human.human_tx_generation = human.human_tx_generation.saturating_add(1);
    human.pending_human = true;
    let (boundary_epoch, boundary_offset) =
        advance_pending_human_boundary(human, epoch, rx_offset);
    human.human_tx_epoch = Some(boundary_epoch);
    human.last_human_tx_rx_offset = boundary_offset;
    if raw_input && !payload.is_empty() {
        update_raw_line_state(human, payload);
    }
    push_human_state_event(human, mono_ns, HumanStateEventKind::TxRegistered);
}

fn has_prompt_after_offset(
    window: &RxWindow,
    epoch: u64,
    rx_offset: u64,
    prompt: &PyRegex,
) -> bool {
    let end_offset = window
        .trimmed
        .saturating_add(window.text.chars().count() as u64);
    if window.watermark.0 != epoch || rx_offset > end_offset {
        return false;
    }
    let (cleaned, sources) = clean_text_with_source_ranges(&window.text);
    let cleaned_chars = cleaned.chars().collect::<Vec<_>>();
    let Some(line_start) = cleaned_chars
        .iter()
        .rposition(|ch| *ch == '\n')
        .map(|index| index + 1)
        .or_else(|| (!cleaned_chars.is_empty()).then_some(0))
    else {
        return false;
    };
    let line_start_absolute = if line_start == 0 {
        if window.trimmed > 0 {
            // 修剪後無法證明目前最後一行的真正行首仍在窗口內。
            return false;
        }
        window.trimmed
    } else {
        let newline_source_end = sources[line_start - 1].end;
        window
            .trimmed
            .saturating_add(window.text[..newline_source_end].chars().count() as u64)
    };
    if line_start_absolute < rx_offset {
        return false;
    }

    let line_start_byte = cleaned
        .char_indices()
        .nth(line_start)
        .map_or(cleaned.len(), |(byte, _)| byte);
    let tail = &cleaned[line_start_byte..];
    prompt
        .search(tail)
        .is_some_and(|matched| matched.start == 0 && matched.end == tail.chars().count())
}

fn settle_pending_human_locked(
    human: &mut ActorDispatchGateState,
    window: &RxWindow,
    prompt: Option<&PyRegex>,
    epoch: u64,
    mono_ns: Mono,
) -> bool {
    if human.human_in_flight != 0 || !human.pending_human {
        return false;
    }
    let Some(boundary_epoch) = human.pending_human_epoch else {
        return false;
    };
    let Some(rx_offset) = human.pending_human_rx_offset else {
        return false;
    };
    if boundary_epoch != epoch
        || !prompt.is_some_and(|prompt| {
            has_prompt_after_offset(window, boundary_epoch, rx_offset, prompt)
        })
    {
        return false;
    }
    human.pending_human = false;
    human.pending_human_epoch = None;
    human.pending_human_rx_offset = None;
    push_human_state_event(human, mono_ns, HumanStateEventKind::PromptSettled);
    true
}

fn record_human_tx_unknown_locked(
    human: &mut ActorDispatchGateState,
    raw_input: bool,
    rx_offset: u64,
    epoch: u64,
    mono_ns: Mono,
) {
    human.human_tx_generation = human.human_tx_generation.saturating_add(1);
    human.pending_human = true;
    let (boundary_epoch, boundary_offset) =
        advance_pending_human_boundary(human, epoch, rx_offset);
    human.human_tx_epoch = Some(boundary_epoch);
    human.last_human_tx_rx_offset = boundary_offset;
    if raw_input {
        human.human_raw_line_open = true;
    }
    push_human_state_event(human, mono_ns, HumanStateEventKind::TxRegistered);
}

fn update_raw_line_state(human: &mut ActorDispatchGateState, payload: &[u8]) {
    if let Some(last_line_break) = payload.iter().rposition(|byte| matches!(*byte, b'\r' | b'\n')) {
        human.human_raw_line_open = last_line_break + 1 < payload.len();
    } else if !payload.is_empty() {
        human.human_raw_line_open = true;
    }
}

fn push_human_state_event(
    human: &mut ActorDispatchGateState,
    mono_ns: Mono,
    kind: HumanStateEventKind,
) {
    if human.completion_capture_op_id.is_none() {
        return;
    }
    human.human_state_events.push_back(HumanStateEvent {
        mono_ns,
        kind,
        state: HumanTxState {
            generation: human.human_tx_generation,
            epoch: human.human_tx_epoch,
            rx_offset: human.last_human_tx_rx_offset,
            raw_line_open: human.human_raw_line_open,
            pending_human: human.pending_human,
        },
    });
}

/// reader thread 的依賴（Task 10 Produces）。
pub struct ReaderCtx {
    pub com: String,
    pub epoch: u64,
    pub wal: Arc<WalWriter>,
    pub rx: Arc<RxShared>,
    pub sentinel: Option<Box<dyn ConsoleIo>>,
    pub data: Sender<crate::mailbox::DataEvent>,
    pub control: Sender<crate::mailbox::ControlEvent>,
    pub clock: Arc<dyn Clock>,
    /// P6 增補 #21：RX 於 WAL 之後 `try_send` 給 ConsoleHub（`None`＝無 hub；P6 起 `sentinel` 一律 `None`）。
    pub hub: Option<crate::human_console::hub::HubTap>,
}

/// reader thread 結束時交回的資源（讀取錯誤與 reader 本體，供 IoWorker 收尾）。
pub struct ReaderExit {
    pub reader: Box<dyn SerialReader>,
    pub sentinel: Option<Box<dyn ConsoleIo>>,
    pub error: Option<PalError>,
    /// 最後一筆成功排入 mailbox data lane 的 RX watermark。
    pub rx_seq: u64,
    pub unrecorded: Vec<EvidenceGap>,
}

/// 啟動 reader thread（Task 10）：讀取 → WAL → RX 視窗 → data lane；`stop` 為協作式停止旗標。
pub fn spawn_reader(
    reader: Box<dyn SerialReader>,
    ctx: ReaderCtx,
    stop: Arc<AtomicBool>,
) -> std::thread::JoinHandle<ReaderExit> {
    spawn_reader_observed(reader, ctx, stop, Vec::new())
}

/// 同 [`spawn_reader`]，另帶 RX 觀察者（P7 Task 16-wire，DF-15）：每個 chunk 在 WAL append 之後依序以該 chunk 的
/// `WalAppend.seq` 呼叫；清單於 reader 建立時固定（熱路徑不加鎖）。
pub fn spawn_reader_observed(
    reader: Box<dyn SerialReader>,
    ctx: ReaderCtx,
    stop: Arc<AtomicBool>,
    observers: Vec<Arc<dyn RxObserver>>,
) -> std::thread::JoinHandle<ReaderExit> {
    spawn_reader_inner(
        reader,
        ctx,
        stop,
        None,
        Arc::new(|line| eprintln!("{line}")),
        observers,
    )
}

#[cfg(feature = "test-fakes")]
pub fn spawn_reader_with_test_hooks(
    reader: Box<dyn SerialReader>,
    ctx: ReaderCtx,
    stop: Arc<AtomicBool>,
    after_rx_append: Arc<dyn Fn() + Send + Sync>,
    warning_sink: Arc<dyn Fn(&str) + Send + Sync>,
) -> std::thread::JoinHandle<ReaderExit> {
    spawn_reader_inner(
        reader,
        ctx,
        stop,
        Some(after_rx_append),
        warning_sink,
        Vec::new(),
    )
}

fn spawn_reader_inner(
    mut reader: Box<dyn SerialReader>,
    ctx: ReaderCtx,
    stop: Arc<AtomicBool>,
    after_rx_append: Option<Arc<dyn Fn() + Send + Sync>>,
    warning_sink: Arc<dyn Fn(&str) + Send + Sync>,
    observers: Vec<Arc<dyn RxObserver>>,
) -> std::thread::JoinHandle<ReaderExit> {
    std::thread::Builder::new()
        .name(format!("serialwrap-rx-{}-{}", ctx.com, ctx.epoch))
        .spawn(move || {
            let mut sentinel = ctx.sentinel;
            let mut rx_seq = 0u64;
            let mut queued_rx_seq = 0u64;
            let mut buf = [0u8; 8192];
            let mut error = None;
            let mut unrecorded = Vec::new();

            loop {
                if stop.load(Ordering::Acquire) {
                    break;
                }
                match reader.read(&mut buf, Some(IO_POLL)) {
                    Ok(ReadOutcome::Data(0)) => {
                        ctx.clock.sleep(IO_POLL);
                        drain_sentinel_input(&mut sentinel);
                    }
                    Ok(ReadOutcome::Data(count)) => {
                        let data = &buf[..count.min(buf.len())];
                        let should_stop = catch_unwind(AssertUnwindSafe(|| {
                            let wal_entry = ctx.wal.append(WalAppendReq {
                                com: &ctx.com,
                                dir: "RX",
                                source: "device",
                                payload: data,
                                cmd_id: None,
                                loss_flag: false,
                                meta: None,
                            });
                            if wal_entry.loss_flag {
                                let message = ctx
                                    .wal
                                    .health()
                                    .last_write_error
                                    .unwrap_or_else(|| "RX WAL append failed".to_string());
                                if unrecorded.len()
                                    < crate::tx_writer::TX_LEDGER_UNRESOLVED_GAPS_MAX
                                {
                                    unrecorded.push(EvidenceGap {
                                        op_id: None,
                                        dir: "RX",
                                        len: data.len(),
                                        error: PalError::plain(PalErrorKind::Io, message),
                                    });
                                }
                            }
                            // DF-15：WAL append 之後依序通知觀察者（Python `_on_bridge_rx`：例外一律吞掉）。
                            for obs in &observers {
                                let _ = catch_unwind(AssertUnwindSafe(|| {
                                    obs.on_rx(&ctx.com, data, wal_entry.seq)
                                }));
                            }
                            rx_seq = rx_seq.saturating_add(1);
                            ctx.rx.append_with_clock(data, &*ctx.clock, ctx.epoch, rx_seq);
                            if let Some(hook) = after_rx_append.as_ref() {
                                hook();
                            }

                            // 增補 #21（07 Task 9 Step 3 第 7 點）：WAL → hub `try_send`（滿載只計數）→ data lane。
                            if let Some(tap) = ctx.hub.as_ref() {
                                tap.push(data, warning_sink.as_ref());
                            }

                            if let Some(console) = sentinel.as_mut() {
                                if catch_unwind(AssertUnwindSafe(|| console.try_write(data)))
                                    .is_err()
                                {
                                    warn_rx_handler_panic(&ctx.com, warning_sink.as_ref());
                                }
                            }

                            let mut event = DataEvent::UartRxChunk {
                                epoch: ctx.epoch,
                                rx_seq,
                                wal_seq: wal_entry.seq,
                                bytes: data.to_vec(),
                            };
                            loop {
                                if stop.load(Ordering::Acquire) {
                                    return true;
                                }
                                match ctx.data.send_timeout(event, DATA_SEND_POLL) {
                                    Ok(()) => return false,
                                    Err(crossbeam_channel::SendTimeoutError::Timeout(retry)) => {
                                        event = retry;
                                    }
                                    Err(crossbeam_channel::SendTimeoutError::Disconnected(_)) => {
                                        return true;
                                    }
                                }
                            }
                        }));
                        match should_stop {
                            Ok(true) => break,
                            Ok(false) => queued_rx_seq = rx_seq,
                            Err(_) => warn_rx_handler_panic(&ctx.com, warning_sink.as_ref()),
                        }
                    }
                    Ok(ReadOutcome::Eof) => ctx.clock.sleep(IO_POLL),
                    Ok(ReadOutcome::Woken | ReadOutcome::TimedOut) => {
                        drain_sentinel_input(&mut sentinel);
                    }
                    Err(read_error) => {
                        let (prefix, pal_error) = match read_error {
                            ReadError::Wait(error) => ("SELECT", error),
                            ReadError::Read(error) => ("SERIAL_READ", error),
                        };
                        let reason = format!(
                            "{prefix}:{}",
                            pal_error
                                .os_code
                                .map(|code| code.to_string())
                                .unwrap_or_else(|| pal_error.py_exc_name().to_string())
                        );
                        let _ = ctx.control.send(ControlEvent::UartIoError {
                            epoch: ctx.epoch,
                            reason,
                        });
                        error = Some(pal_error);
                        break;
                    }
                }
            }

            ReaderExit {
                reader,
                sentinel,
                error,
                rx_seq: queued_rx_seq,
                unrecorded,
            }
        })
        .expect("建立 UART reader thread")
}

fn drain_sentinel_input(sentinel: &mut Option<Box<dyn ConsoleIo>>) {
    let Some(console) = sentinel.as_mut() else {
        return;
    };
    let mut buf = [0u8; 8192];
    loop {
        match console.read(&mut buf, Some(Duration::ZERO)) {
            Ok(ReadOutcome::Data(count)) if count > 0 => {}
            Ok(
                ReadOutcome::Data(_)
                | ReadOutcome::Eof
                | ReadOutcome::Woken
                | ReadOutcome::TimedOut,
            )
            | Err(_) => break,
        }
    }
}

fn warn_rx_handler_panic(com: &str, warning_sink: &dyn Fn(&str)) {
    let line = format!("WARNING uart reader RX handler 例外（com={com}），略過此塊、reader 續行");
    warning_sink(&line);
}

/// `IoWorker::start` 的參數（reader＋唯一 TxWriter 的依賴）。
pub struct IoWorkerArgs {
    pub com: String,
    pub epoch: u64,
    pub wal: Arc<WalWriter>,
    pub rx: Arc<RxShared>,
    pub fence: Arc<Fence>,
    pub sentinel: Option<Box<dyn ConsoleIo>>,
    /// 哨兵 PTY slave 的 keepalive（與 master 同生命週期；worker 收尾關閉 master 後一併釋放）。
    pub sentinel_keepalive: Option<sw_pal::traits::KeepAlive>,
    pub data: Sender<DataEvent>,
    pub control: Sender<ControlEvent>,
    pub clock: Arc<dyn Clock>,
    /// P6 增補 #21：ConsoleHub tap（見 `ReaderCtx.hub`）。
    pub hub: Option<crate::human_console::hub::HubTap>,
}

/// 單一 bridge 的 I/O worker（reader thread＋TxWriter thread）。
pub struct IoWorker {
    tx: TxWriterHandle,
    tx_stop: TxWriterStop,
    reader_stop: Arc<AtomicBool>,
    cancel: CancelToken,
    reader: std::thread::JoinHandle<ReaderExit>,
    writer: std::thread::JoinHandle<(Box<dyn SerialWriter>, TxLedger)>,
    close: CloseToken,
    wal: Arc<WalWriter>,
    epoch: u64,
    sentinel_keepalive: Option<sw_pal::traits::KeepAlive>,
    /// P8 Task 9：序列線控制（`split` 前自 `SerialHandle::line_control()` 取得；`/dev/ttyMCU` 的 baud 鏡射）。
    line_control: Arc<dyn sw_pal::traits::SerialLineControl>,
}

/// IoWorker 對外快照（P5-T12 parity-amendments #41）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct WorkerSnapshot {
    pub serial_alive: bool,
    pub running: bool,
}

impl IoWorker {
    pub fn epoch(&self) -> u64 {
        self.epoch
    }

    pub fn start(handle: Box<dyn SerialHandle>, args: IoWorkerArgs) -> IoWorker {
        IoWorker::start_observed(handle, args, Vec::new())
    }

    /// 同 [`IoWorker::start`]，reader 另帶 RX 觀察者（P7 Task 16-wire）。
    pub fn start_observed(
        handle: Box<dyn SerialHandle>,
        args: IoWorkerArgs,
        observers: Vec<Arc<dyn RxObserver>>,
    ) -> IoWorker {
        // P8 Task 9：`split` 會消耗 handle，線控制必須先取出（split 後仍可持有並調整速率）。
        let line_control = handle.line_control();
        let (reader, writer, waker, close) = handle.split();
        let cancel = CancelToken::new(waker.clone());
        let reader_stop = Arc::new(AtomicBool::new(false));
        let reader = spawn_reader_observed(
            reader,
            ReaderCtx {
                com: args.com.clone(),
                epoch: args.epoch,
                wal: args.wal.clone(),
                rx: args.rx.clone(),
                sentinel: args.sentinel,
                data: args.data,
                control: args.control.clone(),
                clock: args.clock.clone(),
                hub: args.hub,
            },
            reader_stop.clone(),
            observers,
        );
        let wal = args.wal.clone();
        let epoch = args.epoch;
        let (tx, tx_stop, writer) = spawn_tx_writer(
            args.com,
            writer,
            cancel.clone(),
            args.fence,
            args.wal,
            TxWriterContext {
                rx: args.rx,
                epoch: args.epoch,
                control: args.control,
                clock: args.clock,
            },
        );
        IoWorker {
            tx,
            tx_stop,
            reader_stop,
            cancel,
            reader,
            writer,
            close,
            wal,
            epoch,
            sentinel_keepalive: args.sentinel_keepalive,
            line_control,
        }
    }

    /// S2-PAR-wp510r W3': 當真實 IoWorker 已登記至帳本保管時，提供給過渡期 OpenedBridge 之佔位 worker。
    pub fn dummy(epoch: u64, wal: Arc<WalWriter>, fence: Arc<Fence>) -> IoWorker {
        struct DummyWaker;
        impl sw_pal::traits::IoWaker for DummyWaker {
            fn wake(&self) {}
        }
        struct DummyClose;
        impl sw_pal::traits::CloseImpl for DummyClose {
            fn is_alive(&self) -> bool {
                false
            }
            fn configure(
                &self,
                _cfg: &sw_pal::traits::SerialConfig,
            ) -> sw_pal::traits::PalResult<()> {
                Ok(())
            }
            fn close(self: Box<Self>) -> sw_pal::traits::CloseReceipt {
                sw_pal::traits::CloseReceipt {
                    closed: true,
                    excl_cleared: true,
                    error: None,
                }
            }
        }
        struct DummyReader;
        impl sw_pal::traits::SerialReader for DummyReader {
            fn read(
                &mut self,
                _buf: &mut [u8],
                _timeout: Option<Duration>,
            ) -> Result<sw_pal::traits::ReadOutcome, sw_pal::traits::ReadError> {
                Ok(sw_pal::traits::ReadOutcome::Eof)
            }
        }
        struct DummyWriter;
        impl sw_pal::traits::SerialWriter for DummyWriter {
            fn write_gated(
                &mut self,
                _data: &[u8],
                _cancel: &CancelToken,
                _gate: &dyn sw_pal::traits::SubmitGate,
            ) -> sw_pal::traits::TxResult {
                sw_pal::traits::TxResult {
                    outcome: sw_pal::traits::TxOutcome::Completed(0),
                    error: None,
                }
            }
        }

        let (job_tx, _) = crossbeam_channel::bounded(1);
        let (settle_tx, _) = crossbeam_channel::bounded(1);
        let (stop_tx, _) = crossbeam_channel::bounded(1);
        let gauge = Arc::new(crate::tx_writer::LedgerGaugeCells::default());
        let tx = crate::tx_writer::TxWriterHandle::new(job_tx, settle_tx, gauge, fence);
        let tx_stop = crate::tx_writer::TxWriterStop(stop_tx);
        let cancel = CancelToken::new(Arc::new(DummyWaker));
        let reader_stop = Arc::new(AtomicBool::new(true));
        let reader = std::thread::spawn(|| ReaderExit {
            reader: Box::new(DummyReader),
            sentinel: None,
            error: None,
            rx_seq: 0,
            unrecorded: Vec::new(),
        });
        let writer = std::thread::spawn(|| {
            (
                Box::new(DummyWriter) as Box<dyn SerialWriter>,
                crate::tx_writer::TxLedger::default(),
            )
        });
        let close = CloseToken::new(Box::new(DummyClose));
        IoWorker {
            tx,
            tx_stop,
            reader_stop,
            cancel,
            reader,
            writer,
            close,
            wal,
            epoch,
            sentinel_keepalive: None,
            line_control: Arc::new(sw_pal::traits::UnsupportedLineControl),
        }
    }

    pub fn tx(&self) -> &TxWriterHandle {
        &self.tx
    }

    /// P8 Task 9：本 bridge 實體 UART 的線控制（`SessionManager::line_control_for` 的來源）。
    pub fn line_control(&self) -> Arc<dyn sw_pal::traits::SerialLineControl> {
        Arc::clone(&self.line_control)
    }

    /// reader stop flag＋TxWriterStop::send＋cancel.cancel()＋waker.wake()。
    pub fn begin_stop(&self) {
        self.reader_stop.store(true, Ordering::Release);
        self.tx_stop.send();
        self.cancel.cancel();
    }

    /// 回報目前仍由 worker 持有的 serial handle 與 reader thread 狀態。
    pub fn snapshot(&self) -> WorkerSnapshot {
        WorkerSnapshot {
            serial_alive: self.close.is_alive(),
            running: !self.reader.is_finished(),
        }
    }

    /// thread：join writer（取 TxLedger）→ join reader → close → 等 RX dispatch barrier → `WorkerStopped`。
    pub fn finish_async(self, stop_id: StopId, control: Sender<ControlEvent>) {
        self.begin_stop();
        let Self {
            tx: _,
            tx_stop: _,
            reader_stop: _,
            cancel: _,
            reader,
            writer,
            close,
            wal,
            epoch,
            sentinel_keepalive,
            line_control: _,
        } = self;
        std::thread::Builder::new()
            .name(format!("serialwrap-drain-{}", stop_id.0))
            .spawn(move || {
                let writer_result = writer.join();
                let reader_result = reader.join();
                let (ledger, reader_exit, reader_rx_seq, close_receipt) =
                    match (writer_result, reader_result) {
                        (Ok((writer, ledger)), Ok(reader_exit)) => {
                            let ReaderExit {
                                reader,
                                sentinel,
                                error: _,
                                rx_seq,
                                unrecorded,
                            } = reader_exit;
                            drop(sentinel);
                            drop(sentinel_keepalive);
                            let close_receipt = close.close(reader, writer);
                            (ledger, Some(unrecorded), Some(rx_seq), Some(close_receipt))
                        }
                        _ => {
                            let error = PalError::plain(
                                PalErrorKind::Io,
                                "UART worker thread panicked before returning its handle",
                            );
                            let receipt = crate::drain::WorkerStoppedReceipt {
                                epoch,
                                stop_id,
                                ledger: TxLedger::default(),
                                unrecorded: Vec::new(),
                                wal_error: Some(error.clone()),
                                handle_closed: false,
                                close_error: Some(error),
                            };
                            let _ = control.send(ControlEvent::WorkerStopped(receipt));
                            return;
                        }
                    };

                let mut unrecorded = reader_exit.unwrap_or_default();
                for gap in &ledger.unresolved_gaps {
                    unrecorded.push(gap.clone());
                }
                for entry in ledger.ops.values() {
                    unrecorded.extend(entry.wal_gaps.iter().cloned());
                }
                if let Some(rx_seq) = reader_rx_seq.filter(|rx_seq| *rx_seq > 0) {
                    if let Err(error) = wait_for_rx_dispatch(&control, epoch, rx_seq) {
                        unrecorded.push(EvidenceGap {
                            op_id: None,
                            dir: "RX",
                            len: 0,
                            error,
                        });
                    }
                }
                let close_receipt = close_receipt.expect("成功 join 後必有 close receipt");
                let health = wal.health();
                let wal_error = (!health.healthy).then(|| {
                    PalError::plain(
                        PalErrorKind::Io,
                        health
                            .last_write_error
                            .unwrap_or_else(|| "WAL unhealthy during UART drain".to_string()),
                    )
                });
                let receipt = crate::drain::WorkerStoppedReceipt {
                    epoch,
                    stop_id,
                    ledger,
                    unrecorded,
                    wal_error,
                    handle_closed: close_receipt.closed,
                    close_error: close_receipt.error,
                };
                let _ = control.send(ControlEvent::WorkerStopped(receipt));
            })
            .expect("建立 UART drain finisher thread");
    }
}

// ── P7 Task 0（08-slice23-login-events.md）凍結的新增公開項；本體由標示的 Task 實作 ──

/// login run 的取消旗標（SC-30）：`Arc<AtomicBool>`＋喚醒所屬 `RxShared` 的等待者；不得使用 PAL `CancelToken`。
#[derive(Clone)]
pub struct RunCancel {
    flag: Arc<AtomicBool>,
    rx: Arc<RxShared>,
    /// 建立當下的 `RxShared::run_cancel_gen`；之後 `cancel_login_runs` 使其不符即視為取消。
    start_gen: u64,
    /// P8 Task 5：`Some(建立當下的 transfer_cancel_gen)` 表示另監看檔案傳輸取消（drain Phase 1）。
    transfer_gen: Option<u64>,
    /// issue 282：額外監看軟喚醒旗標；不合併其世代或喚醒來源。
    also_flag: Option<Arc<AtomicBool>>,
}

impl RunCancel {
    pub fn new(rx: &Arc<RxShared>) -> Self {
        RunCancel {
            flag: Arc::new(AtomicBool::new(false)),
            rx: Arc::clone(rx),
            start_gen: rx.run_cancel_gen.load(Ordering::Acquire),
            transfer_gen: None,
            also_flag: None,
        }
    }

    /// P8 Task 5：同一取消旗標與 login 世代，另監看 [`RxShared::cancel_transfer_runs`]（自呼叫當下起；本身已在監看時
    /// 沿用原起點，不遮蔽已發生的取消）。
    pub fn watching_transfers(&self) -> RunCancel {
        RunCancel {
            flag: Arc::clone(&self.flag),
            rx: Arc::clone(&self.rx),
            start_gen: self.start_gen,
            transfer_gen: self
                .transfer_gen
                .or_else(|| Some(self.rx.transfer_cancel_gen.load(Ordering::Acquire))),
            also_flag: self.also_flag.clone(),
        }
    }

    /// 回傳一個也會在 `other` 自身旗標觸發時取消的 run；不改動任一原始旗標或合併 `other` 的世代。
    pub fn also_flag_of(&self, other: &RunCancel) -> RunCancel {
        RunCancel {
            flag: Arc::clone(&self.flag),
            rx: Arc::clone(&self.rx),
            start_gen: self.start_gen,
            transfer_gen: self.transfer_gen,
            also_flag: Some(Arc::clone(&other.flag)),
        }
    }
    /// 設旗標並喚醒等待中的 `wait_for_regex_cancellable`（P7 Task 3 先行；Task 7 由 `Effect::CancelLogin` 觸發）。
    pub fn trigger(&self) {
        self.flag.store(true, Ordering::Release);
        self.rx.wake_all();
    }
    pub fn is_triggered(&self) -> bool {
        self.flag.load(Ordering::Acquire)
            || self.rx.run_cancel_gen.load(Ordering::Acquire) != self.start_gen
            || self
                .transfer_gen
                .is_some_and(|start| self.rx.transfer_cancel_gen.load(Ordering::Acquire) != start)
            || self
                .also_flag
                .as_ref()
                .is_some_and(|flag| flag.load(Ordering::Acquire))
    }
}

/// `wait_for_regex_cancellable` 的結果。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WaitOutcome {
    Matched,
    TimedOut,
    Cancelled,
}

/// shutdown-cancellable regex 等待結果，match 結束位置以絕對 Unicode 字元座標表示。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WaitMatchOutcome {
    Matched { end: u64 },
    TimedOut,
    Cancelled,
}

impl RxShared {
    /// Python `wait_for_regex`（整窗或自絕對偏移 `from` 起）＋SC-30 取消：先比對、再看期限；未命中時以
    /// `Clock::wait_until_or_cancel` 等到期限或「RX／clear／取消」喚醒（P7 Task 3 先行實作）。
    pub fn wait_for_regex_cancellable(
        &self,
        re: &PyRegex,
        from: Option<u64>,
        timeout: Duration,
        clock: &dyn Clock,
        cancel: &RunCancel,
    ) -> WaitOutcome {
        let timeout_ns = timeout.as_nanos().min(u64::MAX as u128) as u64;
        let deadline = clock.mono_ns().saturating_add(timeout_ns);
        let (wake_tx, wake_rx) = crossbeam_channel::bounded(1);
        self.wakers
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .push(wake_tx);
        loop {
            if cancel.is_triggered() {
                return WaitOutcome::Cancelled;
            }
            let (snapshot, revision) = {
                let window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
                (
                    window.text_from(from.unwrap_or(0)).to_string(),
                    window.revision,
                )
            };
            if re.search(&snapshot).is_some() {
                return WaitOutcome::Matched;
            }
            if clock.mono_ns() >= deadline {
                return WaitOutcome::TimedOut;
            }
            while wake_rx.try_recv().is_ok() {}
            let changed = self
                .inner
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .revision
                != revision;
            if changed || cancel.is_triggered() {
                continue;
            }
            clock.wait_until_or_cancel(deadline, &wake_rx);
        }
    }

    /// 從絕對偏移等待 regex，並只監看本 bridge 的單向 Shutdown 取消狀態。
    /// 此介面刻意不使用 [`RunCancel`]，以免改變 login／file-transfer 既有取消範圍。
    pub fn wait_for_regex_from_shutdown_cancellable(
        &self,
        re: &PyRegex,
        from: u64,
        timeout: Duration,
        clock: &dyn Clock,
    ) -> WaitOutcome {
        match self.wait_for_regex_end_from_shutdown_cancellable(re, from, timeout, clock) {
            WaitMatchOutcome::Matched { .. } => WaitOutcome::Matched,
            WaitMatchOutcome::TimedOut => WaitOutcome::TimedOut,
            WaitMatchOutcome::Cancelled => WaitOutcome::Cancelled,
        }
    }

    /// 同 shutdown-cancellable 等待，但命中時回傳 match 結束的絕對 Unicode 字元位置。
    pub fn wait_for_regex_end_from_shutdown_cancellable(
        &self,
        re: &PyRegex,
        from: u64,
        timeout: Duration,
        clock: &dyn Clock,
    ) -> WaitMatchOutcome {
        self.wait_for_regex_end_after_from_shutdown_cancellable_inner(
            re, from, None, timeout, clock,
        )
    }

    /// 從絕對偏移等待 regex，並略過結尾未超過 `after_end` 的命中。
    /// 搜尋保留 `from` 的原始上下文，供 prompt pattern 需要 marker 行界、但命中須延伸到 marker 後的情況使用。
    pub fn wait_for_regex_end_after_from_shutdown_cancellable(
        &self,
        re: &PyRegex,
        from: u64,
        after_end: u64,
        timeout: Duration,
        clock: &dyn Clock,
    ) -> WaitMatchOutcome {
        self.wait_for_regex_end_after_from_shutdown_cancellable_inner(
            re,
            from,
            Some(after_end),
            timeout,
            clock,
        )
    }

    fn wait_for_regex_end_after_from_shutdown_cancellable_inner(
        &self,
        re: &PyRegex,
        from: u64,
        after_end: Option<u64>,
        timeout: Duration,
        clock: &dyn Clock,
    ) -> WaitMatchOutcome {
        #[cfg(feature = "test-fakes")]
        self.regex_wait_starts.fetch_add(1, Ordering::AcqRel);
        if self.shutdown_waits_cancelled.load(Ordering::Acquire) {
            return WaitMatchOutcome::Cancelled;
        }
        let timeout_ns = timeout.as_nanos().min(u64::MAX as u128) as u64;
        let deadline = clock.mono_ns().saturating_add(timeout_ns);
        if clock.mono_ns() >= deadline {
            return WaitMatchOutcome::TimedOut;
        }
        let (wake_tx, wake_rx) = bounded(1);
        self.wakers
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .push(wake_tx);

        loop {
            if self.shutdown_waits_cancelled.load(Ordering::Acquire) {
                return WaitMatchOutcome::Cancelled;
            }
            if clock.mono_ns() >= deadline {
                return WaitMatchOutcome::TimedOut;
            }
            let (snapshot, trimmed, revision) = {
                let window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
                (
                    window.text_from(from).to_string(),
                    window.trimmed,
                    window.revision,
                )
            };
            if self.shutdown_waits_cancelled.load(Ordering::Acquire) {
                return WaitMatchOutcome::Cancelled;
            }
            if clock.mono_ns() >= deadline {
                return WaitMatchOutcome::TimedOut;
            }
            let base = from.max(trimmed);
            let found = if let Some(after_end) = after_end {
                let snapshot_len = snapshot.chars().count();
                let mut search_from = 0usize;
                loop {
                    match re.search_from(&snapshot, search_from) {
                        Some(found) if base.saturating_add(found.end as u64) > after_end => {
                            break Some(found);
                        }
                        Some(found) => {
                            let next = found.start.saturating_add(1);
                            if next > snapshot_len {
                                break None;
                            }
                            search_from = next;
                        }
                        None => break None,
                    }
                }
            } else {
                re.search(&snapshot)
            };
            #[cfg(feature = "test-fakes")]
            self.regex_wait_checks.fetch_add(1, Ordering::AcqRel);
            #[cfg(feature = "test-fakes")]
            self.regex_wait_snapshot_end.store(
                base.saturating_add(snapshot.chars().count() as u64),
                Ordering::Release,
            );
            if let Some(found) = found {
                return if self.shutdown_waits_cancelled.load(Ordering::Acquire) {
                    WaitMatchOutcome::Cancelled
                } else {
                    WaitMatchOutcome::Matched {
                        end: base.saturating_add(found.end as u64),
                    }
                };
            }
            if self.shutdown_waits_cancelled.load(Ordering::Acquire) {
                return WaitMatchOutcome::Cancelled;
            }
            if clock.mono_ns() >= deadline {
                return WaitMatchOutcome::TimedOut;
            }
            while wake_rx.try_recv().is_ok() {}
            let changed = self
                .inner
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .revision
                != revision;
            if changed || self.shutdown_waits_cancelled.load(Ordering::Acquire) {
                continue;
            }
            let _ = clock.wait_until_or_cancel(deadline, &wake_rx);
        }
    }

    /// P7 Task 3（測試替身）：固定 `rx_stats` 回報的 10 s bytes（`None` 恢復實際統計）。
    #[cfg(feature = "test-fakes")]
    pub fn set_rx_stats_override(&self, bytes: Option<u64>) {
        self.stats_override
            .store(bytes.unwrap_or(u64::MAX), Ordering::Release);
    }
}

/// reader 在 WAL append 後依序呼叫的 RX 觀察者（DF-15）；不得阻塞於跨 thread 等待、不得回呼 SessionManager。
/// 經 [`spawn_reader_observed`]／[`IoWorker::start_observed`] 傳入（06 `ReaderCtx` 欄位不變）。
pub trait RxObserver: Send + Sync {
    fn on_rx(&self, com: &str, data: &[u8], wal_seq: u64);
}

/// 等 actor 排空 reader 已成功排入 data lane 的同 epoch RX；到期後保留 receipt 並回報排序證據缺口。
fn wait_for_rx_dispatch(
    safety: &Sender<ControlEvent>,
    epoch: u64,
    rx_seq: u64,
) -> Result<(), PalError> {
    wait_for_rx_dispatch_with_timeout(
        safety,
        epoch,
        rx_seq,
        Duration::from_millis(crate::resources::DRAIN_TIMEOUT_MS),
    )
}

fn wait_for_rx_dispatch_with_timeout(
    safety: &Sender<ControlEvent>,
    epoch: u64,
    rx_seq: u64,
    timeout: Duration,
) -> Result<(), PalError> {
    let (reply, ack) = bounded(1);
    let expired = Arc::new(AtomicBool::new(false));
    let deadline = Instant::now() + timeout;
    if let Err(error) = safety.send_timeout(
        ControlEvent::DrainDataBarrier {
            epoch,
            rx_seq,
            reply,
            expired: expired.clone(),
        },
        deadline.saturating_duration_since(Instant::now()),
    ) {
        expired.store(true, Ordering::Release);
        return Err(PalError::plain(
            PalErrorKind::Io,
            format!("RX drain barrier 未能送入 actor：{error:?}"),
        ));
    }
    match ack.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
        Ok(()) => Ok(()),
        Err(crossbeam_channel::RecvTimeoutError::Disconnected) => {
            expired.store(true, Ordering::Release);
            Err(PalError::plain(
                PalErrorKind::Io,
                "回收者排空，無 actor 確認 RX dispatch".to_string(),
            ))
        }
        Err(crossbeam_channel::RecvTimeoutError::Timeout) => {
            expired.store(true, Ordering::Release);
            Err(PalError::plain(
                PalErrorKind::Io,
                "RX drain barrier 等待逾時，capture 順序未確認：timed out waiting on channel"
                    .to_string(),
            ))
        }
    }
}

#[doc(hidden)]
pub fn wait_for_rx_dispatch_with_timeout_for_test(
    safety: &Sender<ControlEvent>,
    epoch: u64,
    rx_seq: u64,
    timeout: Duration,
) -> Result<(), PalError> {
    wait_for_rx_dispatch_with_timeout(safety, epoch, rx_seq, timeout)
}

#[cfg(all(test, feature = "test-fakes"))]
mod r15_tests {
    use super::RxShared;
    use crossbeam_channel::bounded;
    use std::sync::Arc;
    use std::time::Duration;

    #[test]
    fn i294_r15_blocked_human_enqueue_does_not_hold_rescue_gate() {
        let rx = RxShared::new(1024, 0);
        let (jobs_tx, jobs_rx) = bounded(1);
        jobs_tx.send(10_u8).expect("先佔滿 writer queue");
        let (capacity_wait_tx, capacity_wait_rx) = bounded(1);
        let (capacity_release_tx, capacity_release_rx) = bounded(1);
        let producer_rx = Arc::clone(&rx);
        let producer = std::thread::spawn(move || {
            let mut in_flight = producer_rx.begin_human_submit();
            let result = producer_rx.with_human_submit_gate(
                &mut in_flight,
                20_u8,
                |job| {
                    jobs_tx.try_send(job).map_err(|error| match error {
                        crossbeam_channel::TrySendError::Full(job) => {
                            crate::tx_writer::QueueEnqueueError::Full(Box::new(job))
                        }
                        crossbeam_channel::TrySendError::Disconnected(_) => {
                            crate::tx_writer::QueueEnqueueError::Disconnected
                        }
                    })
                },
                || 0,
                |_| {
                    capacity_wait_tx.send(()).expect("容量等待 hook");
                    capacity_release_rx.recv().expect("釋放容量等待");
                },
            );
            drop(in_flight);
            result
        });

        capacity_wait_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("writer queue 滿載後 producer 已在 gate 外等待容量");
        assert!(rx.timeout_rescue_gate_available_for_test());

        let decision_rx = Arc::clone(&rx);
        let decision = std::thread::spawn(move || {
            decision_rx.timeout_rescue_human_seen_for_test(0, 0)
        });
        assert!(
            decision.join().expect("timeout decision thread 正常結束"),
            "尚未排入 writer 的 HumanInFlightGuard 仍須使決策選 human tuple"
        );
        assert_eq!(jobs_rx.recv().expect("取出 queue blocker"), 10);
        capacity_release_tx.send(()).expect("producer 等待容量中");
        assert_eq!(
            jobs_rx.recv_timeout(Duration::from_secs(5)).expect("human job 重試後入列"),
            20
        );
        assert!(producer.join().expect("producer thread 正常結束").is_ok());
    }
}
