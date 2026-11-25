//! P5-T10／T12：UART reader、RX 視窗與 IoWorker。
//!
//! T10 提供 reader thread、RX 視窗及哨兵 fan-out；IoWorker 的啟停與 TX 組裝由 T12 完成。

use std::collections::VecDeque;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use crossbeam_channel::{bounded, Sender};
use sw_pal::traits::{
    CancelToken, Clock, CloseToken, PalError, PalErrorKind, ReadError, ReadOutcome, SerialHandle,
    SerialReader, SerialWriter,
};
use sw_proto::pycompat::decode_utf8_replace;
use sw_proto::pyre::PyRegex;

use crate::drain::StopId;
use crate::mailbox::{ControlEvent, DataEvent};
use crate::tx_writer::{
    spawn_tx_writer, EvidenceGap, Fence, TxLedger, TxWriterHandle, TxWriterStop,
};
use crate::wal::{WalAppendReq, WalWriter};
use crate::Mono;

const IO_POLL: Duration = Duration::from_millis(200);
const RX_RATE_WINDOW_NS: u64 = 10_000_000_000;
const RX_RATE_WINDOW_MAX: usize = 4096;
const RX_WAIT_CLOCK_SLICE: Duration = Duration::from_millis(50);
const DATA_SEND_POLL: Duration = Duration::from_millis(50);

/// 哨兵 console（POSIX 內部 PTY）的 I/O 介面：與 PAL 的 `ConsoleIo`（arch §4.7）為同一型別。
pub use sw_pal::traits::ConsoleIo;

#[derive(Default)]
struct RxWindow {
    max_chars: usize,
    text: String,
    trimmed: u64,
    total_bytes: u64,
    last_rx_mono: Mono,
    watermark: (u64, u64),
    revision: u64,
    rate: VecDeque<(Mono, u64)>,
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
    changed: Condvar,
    /// P7 Task 3：`wait_for_regex_cancellable` 的喚醒通道（append／clear／`RunCancel::trigger` 時 `try_send`）。
    wakers: Mutex<Vec<Sender<()>>>,
    /// P7 Task 3：測試替身固定 `rx_stats` 的 bytes（`ScriptedBridge.rx_stats`）；`u64::MAX` 為未設定。
    stats_override: AtomicU64,
    /// P7 Task 10：agent log 的 reader 端寫入者（`append` 內同步呼叫；Python `_on_bridge_rx` 於 reader 執行緒寫檔）。
    capture: Mutex<Option<Arc<crate::capture::CaptureObserver>>>,
    /// 登入 FSM 提前武裝的內容遮罩器；reader 在任何出口前套用同一遮罩。
    secret_echo: Mutex<SecretEchoWindow>,
    /// secret 暫存改變時也喚醒 prompt waiter；RX 視窗不含尚未判定的前綴。
    secret_echo_revision: AtomicU64,
    /// 密碼提示狀態追蹤器；reader 在推給 console 前即更新，不依賴 actor 佇列時序。
    password_prompt: Mutex<PasswordPromptTracker>,
    /// P7 Task 7（SC-30）：login run 取消世代。`RunCancel` 建立時記下當時值，之後遞增即視為已取消
    /// （actor 在 quiet arm 的同一事件內呼叫 `cancel_login_runs`，不需新增 Effect 變體）。
    run_cancel_gen: AtomicU64,
    /// P8 Task 5：檔案傳輸等待的取消世代。drain Phase 1（`SessionCore::begin_stop`，所有 `StopKind`）遞增；
    /// 只影響以 [`RunCancel::watching_transfers`] 建立的取消旗標，login run（SC-30）不受影響。
    transfer_cancel_gen: AtomicU64,
}

#[derive(Default)]
struct SecretEchoWindow {
    armed: Option<ArmedSecretEcho>,
    released: VecDeque<ReleasedSecretByte>,
}

struct ReleasedSecretByte {
    byte: u8,
}

struct ArmedSecretEcho {
    secret: Vec<u8>,
    pending: Vec<u8>,
    dynamic: bool,
    expire_on_line_end: bool,
}

#[derive(Default)]
struct PasswordPromptTracker {
    password_regex: Option<sw_proto::pyre::PyRegex>,
    unlock_env: Option<String>,
    pending_bytes: Vec<u8>,
    current_line: String,
    active: bool,
    generation: u64,
}

impl PasswordPromptTracker {
    fn observe_bytes(&mut self, data: &[u8]) {
        if self.password_regex.is_none() && self.unlock_env.is_none() {
            return;
        }
        let mut slice = data;
        while !slice.is_empty() {
            if let Some(pos) = slice.iter().position(|&b| b == b'\r' || b == b'\n') {
                let part = &slice[..pos];
                if !part.is_empty() {
                    self.append_line_bytes(part);
                }
                self.pending_bytes.clear();
                self.current_line.clear();
                self.active = false;
                slice = &slice[pos + 1..];
            } else {
                self.append_line_bytes(slice);
                break;
            }
        }
    }

    fn append_line_bytes(&mut self, bytes: &[u8]) {
        self.pending_bytes.extend_from_slice(bytes);
        loop {
            match std::str::from_utf8(&self.pending_bytes) {
                Ok(s) => {
                    self.current_line.push_str(s);
                    self.pending_bytes.clear();
                    self.trim_line_and_check();
                    break;
                }
                Err(e) => {
                    let valid_up_to = e.valid_up_to();
                    if valid_up_to > 0 {
                        let valid_str = unsafe {
                            std::str::from_utf8_unchecked(&self.pending_bytes[..valid_up_to])
                        };
                        self.current_line.push_str(valid_str);
                        self.pending_bytes.drain(..valid_up_to);
                        self.trim_line_and_check();
                    }
                    if let Some(err_len) = e.error_len() {
                        self.pending_bytes.drain(..err_len);
                        continue;
                    }
                    break;
                }
            }
        }
    }

    fn trim_line_and_check(&mut self) {
        if self.current_line.len() > 4096 {
            let trim = self.current_line.len() - 4096;
            let boundary = self
                .current_line
                .char_indices()
                .find_map(|(i, _)| (i >= trim).then_some(i))
                .unwrap_or(self.current_line.len());
            self.current_line.drain(..boundary);
        }
        self.check_prompt();
    }

    fn check_prompt(&mut self) {
        let uncolored = sw_proto::pycompat::strip_ansi(&self.current_line);
        let profile_match = self.password_regex.as_ref().is_some_and(|re| {
            re.search(&uncolored).is_some() || re.search(&self.current_line).is_some()
        });
        let unlock_match = self.unlock_env.as_deref().is_some_and(|env| {
            if env.is_empty() {
                return false;
            }
            let line = uncolored.trim_end().to_ascii_lowercase();
            let prompt_end = line.ends_with(':') || line.ends_with('?') || line.ends_with('>');
            prompt_end
                && ["password", "passphrase", "unlock"]
                    .iter()
                    .any(|word| line.contains(word))
        });
        let matched = profile_match || unlock_match;
        if matched && !self.active {
            self.generation = self.generation.saturating_add(1);
        }
        self.active |= matched;
    }
}

struct MaskedRxChunk {
    bytes: Vec<u8>,
    rx_window: Vec<u8>,
}

/// 記錄 secret 啟用時的 RX 起點；遮罩由整個登入流程的 cleanup 結束。
pub struct SecretEchoGuard {
    start_from: u64,
}

impl SecretEchoGuard {
    pub(crate) fn start_from(&self) -> u64 {
        self.start_from
    }
}

impl RxShared {
    pub fn new(max_chars: usize, created_mono: u64) -> Arc<RxShared> {
        Arc::new(RxShared {
            inner: Mutex::new(RxWindow {
                max_chars,
                last_rx_mono: created_mono,
                ..RxWindow::default()
            }),
            changed: Condvar::new(),
            wakers: Mutex::new(Vec::new()),
            stats_override: AtomicU64::new(u64::MAX),
            capture: Mutex::new(None),
            secret_echo: Mutex::new(SecretEchoWindow::default()),
            secret_echo_revision: AtomicU64::new(0),
            password_prompt: Mutex::new(PasswordPromptTracker::default()),
            run_cancel_gen: AtomicU64::new(0),
            transfer_cancel_gen: AtomicU64::new(0),
        })
    }

    pub fn set_password_prompt_matcher(
        &self,
        password_regex: Option<sw_proto::pyre::PyRegex>,
        unlock_env: Option<String>,
    ) {
        let mut tracker = self.password_prompt.lock().unwrap_or_else(|e| e.into_inner());
        tracker.password_regex = password_regex;
        tracker.unlock_env = unlock_env;
        tracker.pending_bytes.clear();
        tracker.current_line.clear();
        tracker.active = false;
    }

    pub fn is_password_prompt_active(&self) -> bool {
        self.password_prompt
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .active
    }

    pub fn password_prompt_snapshot(&self) -> (bool, u64) {
        let tracker = self.password_prompt.lock().unwrap_or_else(|e| e.into_inner());
        (tracker.active, tracker.generation)
    }

    pub fn observe_password_prompt_bytes(&self, data: &[u8]) {
        let mut tracker = self.password_prompt.lock().unwrap_or_else(|e| e.into_inner());
        tracker.observe_bytes(data);
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

    /// P7 Task 10：掛上／卸下 agent log 寫入者（SessionCore 於 log_start／stop／bridge 交接時呼叫）。
    pub fn set_capture(&self, capture: Option<Arc<crate::capture::CaptureObserver>>) {
        *self.capture.lock().unwrap_or_else(|e| e.into_inner()) = capture;
    }

    /// 從目前 RX 偏移啟用內容遮罩；不依賴 TX 提交或 reader 時序。
    pub fn arm_secret_echo(&self, secret: &[u8]) -> SecretEchoGuard {
        let mut window = self.secret_echo.lock().unwrap_or_else(|e| e.into_inner());
        let start_from = self.snapshot_len();
        if let Some(mut previous) = window.armed.take() {
            Self::mask_pending(&mut window, &mut previous);
            previous.secret.fill(0);
        }
        if !secret.is_empty() {
            window.armed = Some(ArmedSecretEcho {
                secret: secret.to_vec(),
                pending: Vec::with_capacity(secret.len().saturating_sub(1)),
                dynamic: false,
                expire_on_line_end: false,
            });
        }
        drop(window);
        self.secret_echo_revision.fetch_add(1, Ordering::AcqRel);
        self.wake_all();
        SecretEchoGuard { start_from }
    }

    /// 啟動逐鍵 human password 的 echo matcher；後續 TX byte 會在送出前逐段加入。
    pub(crate) fn begin_human_secret_echo(&self) {
        let mut window = self.secret_echo.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(mut previous) = window.armed.take() {
            Self::mask_pending(&mut window, &mut previous);
            previous.secret.fill(0);
        }
        window.armed = Some(ArmedSecretEcho {
            secret: Vec::new(),
            pending: Vec::new(),
            dynamic: true,
            expire_on_line_end: true,
        });
        drop(window);
        self.secret_echo_revision.fetch_add(1, Ordering::AcqRel);
        self.wake_all();
    }

    /// 擴充逐鍵輸入的預期 echo；reader 會保留尚可能是密碼的前綴。
    pub(crate) fn extend_human_secret_echo(&self, bytes: &[u8]) {
        if bytes.is_empty() {
            return;
        }
        let mut window = self.secret_echo.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(armed) = window.armed.as_mut().filter(|armed| armed.dynamic) {
            armed.secret.extend_from_slice(bytes);
        }
        drop(window);
        self.secret_echo_revision.fetch_add(1, Ordering::AcqRel);
        self.wake_all();
    }

    /// 若 UART 僅確認了部分 TX，限制 echo matcher 到實際交給裝置的 secret 前綴。
    pub(crate) fn truncate_human_secret_echo(&self, len: usize) {
        let mut window = self.secret_echo.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(mut armed) = window.armed.take() {
            if armed.dynamic {
                armed.secret.truncate(len);
                if armed.pending.len() > armed.secret.len() {
                    Self::mask_pending(&mut window, &mut armed);
                    armed.secret.fill(0);
                } else {
                    window.armed = Some(armed);
                }
            } else {
                window.armed = Some(armed);
            }
        }
        drop(window);
        self.secret_echo_revision.fetch_add(1, Ordering::AcqRel);
        self.wake_all();
    }

    /// 封存本次 human password 的 echo matcher；行終止符之後不再遮罩。
    pub(crate) fn finish_human_secret_echo(&self) {
        let mut window = self.secret_echo.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(mut armed) = window.armed.take() {
            if armed.dynamic {
                armed.dynamic = false;
                if armed.secret.is_empty() {
                    Self::mask_pending(&mut window, &mut armed);
                    armed.secret.fill(0);
                } else {
                    window.armed = Some(armed);
                }
            } else {
                window.armed = Some(armed);
            }
        }
        drop(window);
        self.secret_echo_revision.fetch_add(1, Ordering::AcqRel);
        self.wake_all();
    }

    fn mask_pending(window: &mut SecretEchoWindow, armed: &mut ArmedSecretEcho) {
        let pending_len = armed.pending.len();
        armed.pending.fill(0);
        for _ in 0..pending_len {
            window
                .released
                .push_back(ReleasedSecretByte { byte: b'*' });
        }
        armed.pending.clear();
    }

    /// reader 專用：只發布無法再構成 secret 的 bytes；未決前綴留在 matcher，完整命中後停用。
    fn mask_secret_echo(&self, data: &[u8]) -> MaskedRxChunk {
        let mut window = self.secret_echo.lock().unwrap_or_else(|e| e.into_inner());
        let mut visible = Vec::new();
        let mut rx_window = Vec::new();
        for released in window.released.drain(..) {
            visible.push(released.byte);
            rx_window.push(released.byte);
        }
        let Some(mut armed) = window.armed.take() else {
            visible.extend_from_slice(data);
            rx_window.extend_from_slice(data);
            drop(window);
            if !data.is_empty() {
                self.secret_echo_revision.fetch_add(1, Ordering::AcqRel);
                self.wake_all();
            }
            return MaskedRxChunk {
                bytes: visible,
                rx_window,
            };
        };

        let mut matched = false;
        for (index, byte) in data.iter().enumerate() {
            if armed.expire_on_line_end && matches!(byte, b'\r' | b'\n') {
                if !armed.pending.is_empty() {
                    let pending_len = armed.pending.len();
                    visible.resize(visible.len().saturating_add(pending_len), b'*');
                    rx_window.resize(rx_window.len().saturating_add(pending_len), b'*');
                    armed.pending.fill(0);
                    armed.pending.clear();
                    armed.secret.fill(0);
                    visible.push(*byte);
                    rx_window.push(*byte);
                    visible.extend_from_slice(&data[index + 1..]);
                    rx_window.extend_from_slice(&data[index + 1..]);
                    matched = true;
                    break;
                }
                visible.push(*byte);
                rx_window.push(*byte);
                continue;
            }
            armed.pending.push(*byte);
            while !armed.secret.starts_with(&armed.pending) {
                let released = armed.pending.remove(0);
                visible.push(released);
                rx_window.push(released);
            }
            if armed.pending.len() == armed.secret.len() && !armed.dynamic {
                let matched_len = armed.secret.len();
                visible.resize(visible.len().saturating_add(matched_len), b'*');
                rx_window.resize(rx_window.len().saturating_add(matched_len), b'*');
                armed.pending.clear();
                armed.secret.fill(0);
                visible.extend_from_slice(&data[index + 1..]);
                rx_window.extend_from_slice(&data[index + 1..]);
                matched = true;
                break;
            }
        }
        if !matched {
            window.armed = Some(armed);
        }

        drop(window);
        if !data.is_empty() {
            self.secret_echo_revision.fetch_add(1, Ordering::AcqRel);
            self.wake_all();
        }
        MaskedRxChunk {
            bytes: visible,
            rx_window,
        }
    }

    /// guard 結束時未決前綴只以遮罩排出；不依逾時判定或發布原文。
    fn take_ready_secret_echo(&self) -> MaskedRxChunk {
        let mut window = self.secret_echo.lock().unwrap_or_else(|e| e.into_inner());
        let mut bytes = Vec::new();
        let mut rx_window = Vec::new();
        for released in window.released.drain(..) {
            bytes.push(released.byte);
            rx_window.push(released.byte);
        }
        drop(window);
        if !bytes.is_empty() {
            self.secret_echo_revision.fetch_add(1, Ordering::AcqRel);
            self.wake_all();
        }
        MaskedRxChunk { bytes, rx_window }
    }

    /// reader 停止時捨棄未決前綴並回報明確 RX evidence gap；不得以原文排出。
    fn finish_secret_echo_for_reader_exit(&self) -> (MaskedRxChunk, bool) {
        let mut window = self.secret_echo.lock().unwrap_or_else(|e| e.into_inner());
        let mut unresolved = false;
        if let Some(mut armed) = window.armed.take() {
            unresolved = !armed.pending.is_empty();
            armed.secret.fill(0);
            armed.pending.clear();
        }
        let mut bytes = Vec::new();
        let mut rx_window = Vec::new();
        for released in window.released.drain(..) {
            bytes.push(released.byte);
            rx_window.push(released.byte);
        }
        drop(window);
        self.secret_echo_revision.fetch_add(1, Ordering::AcqRel);
        self.wake_all();
        (MaskedRxChunk { bytes, rx_window }, unresolved)
    }

    pub(crate) fn disarm_secret_echo_for_login_end(&self) {
        let mut window = self.secret_echo.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(mut armed) = window.armed.take() {
            Self::mask_pending(&mut window, &mut armed);
            armed.secret.fill(0);
        }
        drop(window);
        self.secret_echo_revision.fetch_add(1, Ordering::AcqRel);
        self.wake_all();
    }

    #[cfg(test)]
    pub(crate) fn issue311_test_mask_secret_echo(&self, data: &[u8]) -> Vec<u8> {
        self.mask_secret_echo(data).bytes
    }

    /// 喚醒所有 `wait_for_regex_cancellable` 等待者（容量 1、滿則略過；已斷線者移除）。
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
        self.record_rx_activity(data.len(), now);
        self.append_sanitized(data, epoch, rx_seq);
    }

    /// raw RX 活動統計保留原始 byte 數與到達時間，即使 secret 前綴仍在 matcher 暫存中。
    fn record_rx_activity(&self, count: usize, now: Mono) {
        if count == 0 {
            return;
        }
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        window.total_bytes = window.total_bytes.saturating_add(count as u64);
        window.last_rx_mono = now;
        window.prune_rate(now);
        window.rate.push_back((now, count as u64));
        window.cap_rate();
        drop(window);
        self.wake_all();
    }

    /// 只把 reader 已通過 secret matcher 的 bytes 寫入可觀察 RX window 與 agent capture。
    fn append_sanitized(&self, data: &[u8], epoch: u64, rx_seq: u64) {
        self.append_sanitized_with_window(data, data, epoch, rx_seq);
    }

    /// 各輸出與 capture 收到完整遮罩後 RX；登入 matcher 可略過 clear 前已消耗的 prompt bytes。
    fn append_sanitized_with_window(
        &self,
        data: &[u8],
        window_data: &[u8],
        epoch: u64,
        rx_seq: u64,
    ) {
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        window.watermark = (epoch, rx_seq);
        window.revision = window.revision.saturating_add(1);
        if !window_data.is_empty() {
            window.text.push_str(&decode_utf8_replace(window_data));
            window.trim_to_limit();
        }
        drop(window);
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
        window.revision = window.revision.saturating_add(1);
        drop(window);
        self.wake_all();
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
    mut ctx: ReaderCtx,
    stop: Arc<AtomicBool>,
    after_rx_append: Option<Arc<dyn Fn() + Send + Sync>>,
    warning_sink: Arc<dyn Fn(&str) + Send + Sync>,
    observers: Vec<Arc<dyn RxObserver>>,
) -> std::thread::JoinHandle<ReaderExit> {
    std::thread::Builder::new()
        .name(format!("serialwrap-rx-{}-{}", ctx.com, ctx.epoch))
        .spawn(move || {
            let mut sentinel = std::mem::take(&mut ctx.sentinel);
            let mut buf = [0u8; 8192];
            let mut error = None;
            let mut publisher = ReaderPublisher {
                ctx: &ctx,
                stop: &stop,
                after_rx_append: after_rx_append.as_ref(),
                warning_sink: warning_sink.as_ref(),
                observers: &observers,
                sentinel: &mut sentinel,
                rx_seq: 0,
                queued_rx_seq: 0,
                unrecorded: Vec::new(),
            };

            loop {
                if stop.load(Ordering::Acquire) {
                    break;
                }
                match reader.read(&mut buf, Some(IO_POLL)) {
                    Ok(ReadOutcome::Data(0)) => {
                        if publisher.flush_secret_echo() {
                            break;
                        }
                        ctx.clock.sleep(IO_POLL);
                        publisher.drain_sentinel_input();
                    }
                    Ok(ReadOutcome::Data(count)) => {
                        let raw = &buf[..count.min(buf.len())];
                        let now = ctx.clock.mono_ns();
                        ctx.rx.record_rx_activity(raw.len(), now);
                        let visible = ctx.rx.mask_secret_echo(raw);
                        if publisher.publish(&visible) {
                            break;
                        }
                    }
                    Ok(ReadOutcome::Eof) => {
                        if publisher.flush_secret_echo() {
                            break;
                        }
                        ctx.clock.sleep(IO_POLL);
                    }
                    Ok(ReadOutcome::Woken | ReadOutcome::TimedOut) => {
                        if publisher.flush_secret_echo() {
                            break;
                        }
                        publisher.drain_sentinel_input();
                    }
                    Err(read_error) => {
                        let _ = publisher.flush_secret_echo();
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

            // 與 guard drop 原子結算：已解除前綴寫入 WAL；仍有歧義的前綴記成明確 evidence gap。
            let (ready, unresolved) = ctx.rx.finish_secret_echo_for_reader_exit();
            let _ = publisher.publish(&ready);
            if unresolved
                && publisher.unrecorded.len() < crate::tx_writer::TX_LEDGER_UNRESOLVED_GAPS_MAX
            {
                publisher.unrecorded.push(EvidenceGap {
                    op_id: None,
                    dir: "RX",
                    len: 0,
                    error: PalError::plain(
                        PalErrorKind::Io,
                        "secret echo prefix unresolved when RX reader stopped",
                    ),
                });
            }
            let rx_seq = publisher.queued_rx_seq;
            let unrecorded = std::mem::take(&mut publisher.unrecorded);
            drop(publisher);

            ReaderExit {
                reader,
                sentinel,
                error,
                rx_seq,
                unrecorded,
            }
        })
        .expect("建立 UART reader thread")
}

struct ReaderPublisher<'a> {
    ctx: &'a ReaderCtx,
    stop: &'a AtomicBool,
    after_rx_append: Option<&'a Arc<dyn Fn() + Send + Sync>>,
    warning_sink: &'a (dyn Fn(&str) + Send + Sync),
    observers: &'a [Arc<dyn RxObserver>],
    sentinel: &'a mut Option<Box<dyn ConsoleIo>>,
    rx_seq: u64,
    queued_rx_seq: u64,
    unrecorded: Vec<EvidenceGap>,
}

impl ReaderPublisher<'_> {
    fn publish(&mut self, chunk: &MaskedRxChunk) -> bool {
        if chunk.bytes.is_empty() {
            return false;
        }
        match catch_unwind(AssertUnwindSafe(|| self.publish_inner(chunk))) {
            Ok(should_stop) => should_stop,
            Err(_) => {
                warn_rx_handler_panic(&self.ctx.com, self.warning_sink);
                false
            }
        }
    }

    fn publish_inner(&mut self, chunk: &MaskedRxChunk) -> bool {
        let data = chunk.bytes.as_slice();
        // reader 是提示 tracker 的唯一 RX 寫入者；先更新，再通知 observer 或 actor。
        self.ctx.rx.observe_password_prompt_bytes(data);
        let wal_entry = self.ctx.wal.append(WalAppendReq {
            com: &self.ctx.com,
            dir: "RX",
            source: "device",
            payload: data,
            cmd_id: None,
            loss_flag: false,
            meta: None,
        });
        if wal_entry.loss_flag {
            let message = self
                .ctx
                .wal
                .health()
                .last_write_error
                .unwrap_or_else(|| "RX WAL append failed".to_string());
            if self.unrecorded.len() < crate::tx_writer::TX_LEDGER_UNRESOLVED_GAPS_MAX {
                self.unrecorded.push(EvidenceGap {
                    op_id: None,
                    dir: "RX",
                    len: data.len(),
                    error: PalError::plain(PalErrorKind::Io, message),
                });
            }
        }
        // DF-15：WAL append 之後依序通知觀察者（Python `_on_bridge_rx`：例外一律吞掉）。
        for obs in self.observers {
            let _ = catch_unwind(AssertUnwindSafe(|| {
                obs.on_rx(&self.ctx.com, data, wal_entry.seq)
            }));
        }
        self.rx_seq = self.rx_seq.saturating_add(1);
        self.ctx.rx.append_sanitized_with_window(
            data,
            &chunk.rx_window,
            self.ctx.epoch,
            self.rx_seq,
        );
        if let Some(hook) = self.after_rx_append {
            hook();
        }

        // 增補 #21（07 Task 9 Step 3 第 7 點）：WAL → hub `try_send`（滿載只計數）→ data lane。
        if let Some(tap) = self.ctx.hub.as_ref() {
            tap.push(data, self.warning_sink);
        }

        if let Some(console) = self.sentinel.as_mut() {
            if catch_unwind(AssertUnwindSafe(|| console.try_write(data))).is_err() {
                warn_rx_handler_panic(&self.ctx.com, self.warning_sink);
            }
        }

        let mut event = DataEvent::UartRxChunk {
            epoch: self.ctx.epoch,
            rx_seq: self.rx_seq,
            wal_seq: wal_entry.seq,
            bytes: data.to_vec(),
        };
        loop {
            if self.stop.load(Ordering::Acquire) {
                return true;
            }
            match self.ctx.data.send_timeout(event, DATA_SEND_POLL) {
                Ok(()) => {
                    self.queued_rx_seq = self.rx_seq;
                    return false;
                }
                Err(crossbeam_channel::SendTimeoutError::Timeout(retry)) => event = retry,
                Err(crossbeam_channel::SendTimeoutError::Disconnected(_)) => return true,
            }
        }
    }

    fn flush_secret_echo(&mut self) -> bool {
        let ready = self.ctx.rx.take_ready_secret_echo();
        self.publish(&ready)
    }

    fn drain_sentinel_input(&mut self) {
        drain_sentinel_input(self.sentinel);
    }
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
                clock: args.clock,
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
            Some(args.rx.clone()),
            args.epoch,
            args.control,
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
            let secret_revision = self.secret_echo_revision.load(Ordering::Acquire);
            let effective_from = from.unwrap_or(0);
            let (snapshot, revision) = {
                let window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
                (
                    window.text_from(effective_from).to_string(),
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
            if changed
                || self.secret_echo_revision.load(Ordering::Acquire) != secret_revision
                || cancel.is_triggered()
            {
                continue;
            }
            clock.wait_until_or_cancel(deadline, &wake_rx);
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
