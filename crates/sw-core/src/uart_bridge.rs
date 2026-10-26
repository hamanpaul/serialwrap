//! P5-T10／T12：UART reader、RX 視窗與 IoWorker。
//!
//! T10 提供 reader thread、RX 視窗及哨兵 fan-out；IoWorker 的啟停與 TX 組裝由 T12 完成。

use std::collections::VecDeque;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use crossbeam_channel::Sender;
use sw_pal::traits::{Clock, PalError, ReadError, ReadOutcome, SerialHandle, SerialReader};
use sw_proto::pycompat::decode_utf8_replace;
use sw_proto::pyre::PyRegex;

use crate::drain::StopId;
use crate::mailbox::{ControlEvent, DataEvent};
use crate::tx_writer::{Fence, TxWriterHandle};
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
        })
    }

    pub fn append(&self, data: &[u8], now: Mono, epoch: u64, rx_seq: u64) {
        let mut window = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        window.watermark = (epoch, rx_seq);
        window.revision = window.revision.saturating_add(1);
        if !data.is_empty() {
            window.total_bytes = window.total_bytes.saturating_add(data.len() as u64);
            window.last_rx_mono = now;
            window.prune_rate(now);
            window.rate.push_back((now, data.len() as u64));
            window.cap_rate();
            window.text.push_str(&decode_utf8_replace(data));
            window.trim_to_limit();
        }
        self.changed.notify_all();
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
        self.changed.notify_all();
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

    pub fn rx_stats(&self, now: Mono) -> (u64, u64) {
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
}

/// reader thread 結束時交回的資源（讀取錯誤與 reader 本體，供 IoWorker 收尾）。
pub struct ReaderExit {
    pub reader: Box<dyn SerialReader>,
    pub sentinel: Option<Box<dyn ConsoleIo>>,
    pub error: Option<PalError>,
    pub rx_seq: u64,
}

/// 啟動 reader thread（Task 10）：讀取 → WAL → RX 視窗 → data lane；`stop` 為協作式停止旗標。
pub fn spawn_reader(
    reader: Box<dyn SerialReader>,
    ctx: ReaderCtx,
    stop: Arc<AtomicBool>,
) -> std::thread::JoinHandle<ReaderExit> {
    spawn_reader_inner(
        reader,
        ctx,
        stop,
        None,
        Arc::new(|line| eprintln!("{line}")),
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
    spawn_reader_inner(reader, ctx, stop, Some(after_rx_append), warning_sink)
}

fn spawn_reader_inner(
    mut reader: Box<dyn SerialReader>,
    ctx: ReaderCtx,
    stop: Arc<AtomicBool>,
    after_rx_append: Option<Arc<dyn Fn() + Send + Sync>>,
    warning_sink: Arc<dyn Fn(&str) + Send + Sync>,
) -> std::thread::JoinHandle<ReaderExit> {
    std::thread::Builder::new()
        .name(format!("serialwrap-rx-{}-{}", ctx.com, ctx.epoch))
        .spawn(move || {
            let mut sentinel = ctx.sentinel;
            let mut rx_seq = 0u64;
            let mut buf = [0u8; 8192];
            let mut error = None;

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
                            let now = ctx.clock.mono_ns();
                            rx_seq = rx_seq.saturating_add(1);
                            ctx.rx.append(data, now, ctx.epoch, rx_seq);
                            if let Some(hook) = after_rx_append.as_ref() {
                                hook();
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
                            Ok(false) => {}
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
                rx_seq,
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
    pub data: Sender<DataEvent>,
    pub control: Sender<ControlEvent>,
    pub clock: Arc<dyn Clock>,
}

/// 單一 bridge 的 I/O worker（reader thread＋TxWriter thread）。
pub struct IoWorker {
    _stub: (),
}

impl IoWorker {
    #[cfg(feature = "test-fakes")]
    pub fn start(_handle: Box<dyn SerialHandle>, _args: IoWorkerArgs) -> IoWorker {
        IoWorker { _stub: () }
    }

    #[cfg(not(feature = "test-fakes"))]
    pub fn start(_handle: Box<dyn SerialHandle>, _args: IoWorkerArgs) -> IoWorker {
        s21_todo!("T12", "IoWorker::start")
    }

    pub fn tx(&self) -> &TxWriterHandle {
        s21_todo!("T12", "IoWorker::tx")
    }

    /// reader stop flag＋TxWriterStop::send＋cancel.cancel()＋waker.wake()。
    pub fn begin_stop(&self) {
        s21_todo!("T12", "IoWorker::begin_stop")
    }

    /// thread：join writer（取 TxLedger）→ join reader → close → `WorkerStopped`。
    pub fn finish_async(self, _stop_id: StopId, _control: Sender<ControlEvent>) {
        s21_todo!("T12", "IoWorker::finish_async")
    }
}
