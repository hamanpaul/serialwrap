//! Slice 2.1 Task 10：UART RX WAL、視窗、reader 與哨兵 fan-out。

#![cfg(feature = "test-fakes")]

use std::collections::VecDeque;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use crossbeam_channel::{Sender, bounded};
use sw_core::mailbox::{ControlEvent, DataEvent};
use sw_core::uart_bridge::{
    ReaderCtx, ReaderExit, RxShared, spawn_reader, spawn_reader_with_test_hooks,
};
use sw_core::wal::{WalAppendReq, WalConfig, WalWriter};
use sw_pal::fakes::clock::FakeClock;
use sw_pal::fakes::serial::{FakeDevice, FakeSerialBackend};
use sw_pal::fakes::wal_fs::FakeWalFs;
use sw_pal::traits::{
    Clock, ConsoleIo, DevicePath, IoWaker, PalError, PalErrorKind, ReadOutcome, SerialBackend,
    SerialConfig, SerialReader,
};
use sw_proto::pyre::{PyFlags, compile};

const IO_POLL: Duration = Duration::from_millis(200);

struct FakePort {
    backend: Arc<FakeSerialBackend>,
    dev: FakeDevice,
    reader: Box<dyn SerialReader>,
    waker: Arc<dyn IoWaker>,
    _close: sw_pal::traits::CloseToken,
}

fn fake_port() -> FakePort {
    let backend = FakeSerialBackend::new();
    let dev = backend.add_device("/dev/f");
    let handle = backend
        .open_exclusive(
            &DevicePath("/dev/f".into()),
            &SerialConfig::from_uart_profile(115200, 8, "N", 1, "none", false),
        )
        .expect("開啟 fake serial");
    let (reader, _writer, waker, close) = handle.split();
    FakePort {
        backend,
        dev,
        reader,
        waker,
        _close: close,
    }
}

fn fake_wal(clock: Arc<dyn Clock>) -> (Arc<WalWriter>, Arc<FakeWalFs>) {
    let fs = FakeWalFs::new();
    let wal = Arc::new(WalWriter::open(
        WalConfig {
            dir: "/w".into(),
            rotate_bytes: 1 << 26,
        },
        fs.clone(),
        clock,
    ));
    (wal, fs)
}

fn reader_ctx(
    wal: Arc<WalWriter>,
    rx: Arc<RxShared>,
    sentinel: Option<Box<dyn ConsoleIo>>,
    data: Sender<DataEvent>,
    control: Sender<ControlEvent>,
    clock: Arc<dyn Clock>,
) -> ReaderCtx {
    ReaderCtx {
        com: "COM0".into(),
        epoch: 3,
        wal,
        rx,
        sentinel,
        data,
        control,
        clock,
    }
}

fn stop_and_join(
    stop: &Arc<AtomicBool>,
    waker: &Arc<dyn IoWaker>,
    worker: thread::JoinHandle<ReaderExit>,
) -> ReaderExit {
    stop.store(true, Ordering::Release);
    waker.wake();
    worker.join().expect("reader thread 應正常結束")
}

#[derive(Default)]
struct ConsoleState {
    input: VecDeque<u8>,
    output: Vec<u8>,
    reads: usize,
    write_zero: bool,
    panic_once: bool,
}

struct TestWaker;

impl IoWaker for TestWaker {
    fn wake(&self) {}
}

struct TestConsole {
    state: Arc<Mutex<ConsoleState>>,
    waker: Arc<dyn IoWaker>,
}

impl TestConsole {
    fn new(state: Arc<Mutex<ConsoleState>>) -> TestConsole {
        TestConsole {
            state,
            waker: Arc::new(TestWaker),
        }
    }
}

impl ConsoleIo for TestConsole {
    fn read(
        &mut self,
        buf: &mut [u8],
        _timeout: Option<Duration>,
    ) -> sw_pal::PalResult<ReadOutcome> {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.reads += 1;
        if state.input.is_empty() {
            return Ok(ReadOutcome::TimedOut);
        }
        let count = buf.len().min(state.input.len());
        for byte in &mut buf[..count] {
            *byte = state.input.pop_front().expect("input 長度已確認");
        }
        Ok(ReadOutcome::Data(count))
    }

    fn try_write(&mut self, data: &[u8]) -> sw_pal::PalResult<usize> {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if state.panic_once {
            state.panic_once = false;
            panic!("injected sentinel RX panic");
        }
        state.output.extend_from_slice(data);
        if state.write_zero {
            Ok(0)
        } else {
            Ok(data.len())
        }
    }

    fn waker(&self) -> Arc<dyn IoWaker> {
        Arc::clone(&self.waker)
    }

    fn has_external_peer(&self, _held: Option<&std::collections::HashSet<String>>) -> bool {
        true
    }

    fn close(self: Box<Self>) {}
}

enum ScriptStep {
    Outcome(ReadOutcome),
    Bytes(&'static [u8]),
    Error(sw_pal::traits::ReadError),
}

struct ScriptReader {
    steps: VecDeque<ScriptStep>,
    calls: Option<Sender<()>>,
}

impl SerialReader for ScriptReader {
    fn read(
        &mut self,
        buf: &mut [u8],
        _timeout: Option<Duration>,
    ) -> Result<ReadOutcome, sw_pal::traits::ReadError> {
        if let Some(calls) = &self.calls {
            let _ = calls.try_send(());
        }
        match self.steps.pop_front() {
            Some(ScriptStep::Outcome(ReadOutcome::Data(n))) => {
                buf[..n].copy_from_slice(&vec![b'x'; n]);
                Ok(ReadOutcome::Data(n))
            }
            Some(ScriptStep::Bytes(data)) => {
                buf[..data.len()].copy_from_slice(data);
                Ok(ReadOutcome::Data(data.len()))
            }
            Some(ScriptStep::Outcome(outcome)) => Ok(outcome),
            Some(ScriptStep::Error(error)) => Err(error),
            None => Ok(ReadOutcome::TimedOut),
        }
    }
}

fn rx_error(code: i32) -> PalError {
    PalError::new(
        PalErrorKind::Io,
        Some(code),
        None,
        "Input/output error",
        format!("[Errno {code}] Input/output error"),
    )
}

fn script_reader(steps: impl IntoIterator<Item = ScriptStep>) -> Box<dyn SerialReader> {
    Box::new(ScriptReader {
        steps: steps.into_iter().collect(),
        calls: None,
    })
}

fn script_reader_with_calls(
    steps: impl IntoIterator<Item = ScriptStep>,
) -> (Box<dyn SerialReader>, crossbeam_channel::Receiver<()>) {
    let (calls_tx, calls_rx) = bounded(16);
    (
        Box::new(ScriptReader {
            steps: steps.into_iter().collect(),
            calls: Some(calls_tx),
        }),
        calls_rx,
    )
}

#[test]
fn rx_is_in_wal_before_actor_sees_it_with_exact_seq() {
    let fake_clock = FakeClock::at(1, 1);
    let clock: Arc<dyn Clock> = fake_clock;
    let (wal, wfs) = fake_wal(Arc::clone(&clock));
    wal.append(WalAppendReq {
        com: "COM1",
        dir: "TX",
        source: "x",
        payload: b"z",
        cmd_id: None,
        loss_flag: false,
        meta: None,
    });
    let (data_tx, data_rx) = bounded(1024);
    let (control_tx, _control_rx) = bounded(256);
    let rx = RxShared::new(131_072, 1);
    let port = fake_port();
    let stop = Arc::new(AtomicBool::new(false));
    let worker = spawn_reader(
        port.reader,
        reader_ctx(wal, Arc::clone(&rx), None, data_tx, control_tx, clock),
        Arc::clone(&stop),
    );

    port.dev.push_rx(b"login: ");
    match data_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("reader 應送出 RX")
    {
        DataEvent::UartRxChunk {
            epoch,
            rx_seq,
            wal_seq,
            bytes,
        } => assert_eq!(
            (epoch, rx_seq, wal_seq, bytes.as_slice()),
            (3, 1, 2, &b"login: "[..])
        ),
    }
    let text = String::from_utf8(
        wfs.contents(Path::new("/w/raw.wal.ndjson"))
            .expect("WAL 內容"),
    )
    .expect("WAL 為 UTF-8");
    assert!(
        text.lines()
            .nth(1)
            .unwrap()
            .contains("\"source\":\"device\"")
    );
    assert_eq!(rx.snapshot_len(), 7);
    let _ = stop_and_join(&stop, &port.waker, worker);
}

#[test]
fn rx_window_trims_head_and_keeps_absolute_offsets() {
    let rx = RxShared::new(10, 0);
    rx.append(b"0123456789", 1, 1, 1);
    let offset = rx.snapshot_len();
    rx.append(b"abcde", 2, 1, 2);
    assert_eq!((rx.trimmed(), rx.snapshot_len()), (5, 15));
    assert_eq!(rx.text_from(offset), "abcde");
    assert_eq!(rx.text_from(0), "56789abcde");
    rx.clear();
    assert_eq!(
        (rx.trimmed(), rx.snapshot_len(), rx.total_bytes()),
        (15, 15, 15)
    );
}

#[test]
fn per_chunk_utf8_decode_replaces_split_multibyte() {
    let rx = RxShared::new(32, 0);
    rx.append(b"\xe4\xb8", 1, 1, 1);
    rx.append(b"\xad", 2, 1, 2);
    assert_eq!(rx.tail(0), "\u{FFFD}\u{FFFD}");
}

#[test]
fn rx_stats_window_prunes_strictly_older_than_10s() {
    let rx = RxShared::new(32, 0);
    rx.append(&[b'x'; 100], 0, 1, 1);
    rx.append(&[b'y'; 50], 10_000_000_000, 1, 2);
    assert_eq!(rx.rx_stats(10_000_000_000), (150, 15));
    assert_eq!(rx.rx_stats(10_000_000_001), (50, 5));
}

#[test]
fn idle_counts_from_creation() {
    let rx = RxShared::new(32, 5_000_000_000);
    assert_eq!(rx.idle_s(8_000_000_000), 3.0);
}

#[test]
fn wait_for_regex_zero_timeout_is_false() {
    let rx = RxShared::new(32, 0);
    rx.append(b"ready", 1, 3, 7);
    let regex = compile("ready", PyFlags::default()).unwrap();
    let clock = FakeClock::at(10, 0);
    assert!(rx.wait_for_regex(&regex, Duration::ZERO, &*clock).is_none());
}

#[test]
fn wait_for_regex_from_wakes_on_append() {
    let rx = RxShared::new(64, 0);
    let regex = compile(r"# $", PyFlags::default()).unwrap();
    let clock = FakeClock::at(1, 0);
    let waiting_rx = Arc::clone(&rx);
    let waiting_clock = Arc::clone(&clock);
    let started = Instant::now();
    let waiter = thread::spawn(move || {
        waiting_rx.wait_for_regex_from(&regex, 0, Duration::from_secs(1), &*waiting_clock)
    });
    thread::sleep(Duration::from_millis(50));
    rx.append(b"# ", 2, 3, 7);
    let matched = waiter.join().expect("waiter 應正常結束");
    assert!(started.elapsed() < Duration::from_millis(200));
    assert_eq!(matched.map(|m| (m.epoch, m.rx_seq)), Some((3, 7)));
}

#[test]
fn match_watermark_is_atomic_with_text() {
    let rx = RxShared::new(16_384, 0);
    let regex = compile("marker-500", PyFlags::default()).unwrap();
    let writing_rx = Arc::clone(&rx);
    let writer = thread::spawn(move || {
        for seq in 1..=1_000 {
            writing_rx.append(format!("marker-{seq} ").as_bytes(), seq, 1, seq);
        }
    });
    let clock = FakeClock::at(1, 0);
    let matched = rx
        .wait_for_regex(&regex, Duration::from_secs(1), &*clock)
        .expect("應命中 marker-500");
    writer.join().expect("append thread 應正常結束");
    assert_eq!(matched.epoch, 1);
    assert!(matched.rx_seq >= 500, "命中文字的 watermark 不得較舊");
}

#[test]
fn wait_for_regex_does_not_hold_rx_mutex_during_search() {
    let rx = RxShared::new(64, 0);
    rx.append(b"first", 1, 3, 1);
    let clock = FakeClock::at(1, 0);
    let waiting_rx = Arc::clone(&rx);
    let waiting_clock = Arc::clone(&clock);
    let (search_started_tx, search_started_rx) = bounded(1);
    let (release_search_tx, release_search_rx) = bounded(1);
    let waiter = thread::spawn(move || {
        waiting_rx.wait_for_regex_from_with(0, Duration::from_secs(1), &*waiting_clock, |text| {
            assert_eq!(text, "first");
            search_started_tx.send(()).expect("搜尋開始通知");
            release_search_rx
                .recv_timeout(Duration::from_secs(1))
                .expect("測試釋放搜尋");
            true
        })
    });
    search_started_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("搜尋 closure 應執行");

    let appending_rx = Arc::clone(&rx);
    let (append_done_tx, append_done_rx) = bounded(1);
    let appender = thread::spawn(move || {
        appending_rx.append(b"second", 2, 3, 2);
        append_done_tx.send(()).expect("append 完成通知");
    });
    let append_completed_while_searching = append_done_rx
        .recv_timeout(Duration::from_millis(100))
        .is_ok();
    release_search_tx.send(()).expect("釋放搜尋");
    let matched = waiter.join().expect("waiter 應正常結束");
    appender.join().expect("append thread 應正常結束");

    assert!(
        append_completed_while_searching,
        "RX append 不得等待 regex 搜尋"
    );
    assert_eq!(matched.map(|m| (m.epoch, m.rx_seq)), Some((3, 1)));
}

#[test]
fn completion_cannot_overtake_its_rx_chunk() {
    let clock: Arc<dyn Clock> = FakeClock::at(1, 0);
    let (wal, _wfs) = fake_wal(Arc::clone(&clock));
    let rx = RxShared::new(64, 0);
    let (data_tx, data_rx) = bounded(1);
    let (control_tx, control_rx) = bounded(1);
    let stop = Arc::new(AtomicBool::new(false));
    let worker = spawn_reader(
        script_reader([ScriptStep::Bytes(b"prompt")]),
        reader_ctx(
            wal,
            Arc::clone(&rx),
            None,
            data_tx,
            control_tx.clone(),
            Arc::clone(&clock),
        ),
        Arc::clone(&stop),
    );

    let regex = compile("prompt$", PyFlags::default()).unwrap();
    let matched = rx
        .wait_for_regex(&regex, Duration::from_secs(1), &*clock)
        .expect("reader RX 應命中 prompt");
    assert_eq!((matched.epoch, matched.rx_seq), (3, 1));
    assert!(matches!(
        data_rx.recv_timeout(Duration::from_secs(1)).expect("RX chunk 送達 data lane"),
        DataEvent::UartRxChunk { epoch: 3, rx_seq: 1, .. }
    ));

    control_tx
        .send(ControlEvent::LoginFinished {
            epoch: matched.epoch,
            fsm_gen: 1,
            result: sw_core::login_fsm::LoginOutcome::Ready,
            after_rx_seq: Some(matched.rx_seq),
        })
        .expect("completion 送出");
    assert!(matches!(
        control_rx.recv_timeout(Duration::from_secs(1)).expect("completion 送達 control lane"),
        ControlEvent::LoginFinished { after_rx_seq: Some(1), .. }
    ));

    stop.store(true, Ordering::Release);
    worker.join().expect("reader 應正常停止");
}

#[test]
fn op_finished_waits_for_reader_rx_match_watermark() {
    let fake_clock = FakeClock::at(1, 0);
    let clock: Arc<dyn Clock> = fake_clock;
    let (wal, _wfs) = fake_wal(Arc::clone(&clock));
    let rx = RxShared::new(64, 0);
    let (data_tx, data_rx) = bounded(16);
    let (control_tx, control_rx) = bounded(16);
    let reader = script_reader([
        ScriptStep::Bytes(b"x"),
        ScriptStep::Bytes(b"x"),
        ScriptStep::Bytes(b"x"),
        ScriptStep::Bytes(b"x"),
        ScriptStep::Bytes(b"ready"),
    ]);
    let stop = Arc::new(AtomicBool::new(false));
    let worker = spawn_reader(
        reader,
        reader_ctx(
            wal,
            Arc::clone(&rx),
            None,
            data_tx,
            control_tx.clone(),
            Arc::clone(&clock),
        ),
        Arc::clone(&stop),
    );
    let regex = compile("ready$", PyFlags::default()).unwrap();
    let matched = rx
        .wait_for_regex(&regex, Duration::from_secs(1), &*clock)
        .expect("實際 reader RX 應命中 ready");
    assert_eq!((matched.epoch, matched.rx_seq), (3, 5));
    for expected_rx_seq in 1..=5 {
        assert!(matches!(
            data_rx.recv_timeout(Duration::from_secs(1)).expect("RX data lane chunk"),
            DataEvent::UartRxChunk { epoch: 3, rx_seq, .. }
                if rx_seq == expected_rx_seq
        ));
    }
    control_tx
        .send(ControlEvent::OpFinished {
            epoch: matched.epoch,
            op_id: sw_core::tx_writer::OpId(7),
            outcome: sw_core::op_runner::OpOutcome::SendFailed,
            after_rx_seq: Some(matched.rx_seq),
        })
        .expect("OpFinished 送出");
    assert!(matches!(
        control_rx.recv_timeout(Duration::from_secs(1)).expect("OpFinished control lane"),
        ControlEvent::OpFinished {
            after_rx_seq: Some(5),
            ..
        }
    ));

    stop.store(true, Ordering::Release);
    worker.join().expect("reader 應正常停止");
}

#[test]
fn rx_rate_window_capped_4096() {
    let rx = RxShared::new(16_384, 0);
    for index in 0..10_000u64 {
        rx.append(b"x", index * 100, 1, index + 1);
    }
    assert_eq!(rx.rate_window_len(), 4096);
    assert_eq!(rx.rx_stats(999_900).0, 10_000);
}

#[test]
fn rx_handler_panic_skips_chunk_keeps_reader() {
    let port = fake_port();
    let clock = FakeClock::at(1, 0);
    let (wal, _wfs) = fake_wal(Arc::clone(&clock) as Arc<dyn Clock>);
    let rx = RxShared::new(64, 0);
    let (data_tx, data_rx) = bounded(16);
    let (control_tx, control_rx) = bounded(16);
    let stop = Arc::new(AtomicBool::new(false));
    let panic_once = Arc::new(AtomicBool::new(true));
    let handler_panic_once = Arc::clone(&panic_once);
    let after_rx_append: Arc<dyn Fn() + Send + Sync> = Arc::new(move || {
        if handler_panic_once.swap(false, Ordering::AcqRel) {
            panic!("injected primary RX handler panic");
        }
    });
    let warnings = Arc::new(Mutex::new(Vec::<String>::new()));
    let warning_lines = Arc::clone(&warnings);
    let warning_sink: Arc<dyn Fn(&str) + Send + Sync> = Arc::new(move |line| {
        warning_lines
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .push(line.to_string());
    });
    let worker = spawn_reader_with_test_hooks(
        port.reader,
        reader_ctx(wal, Arc::clone(&rx), None, data_tx, control_tx, clock),
        Arc::clone(&stop),
        after_rx_append,
        warning_sink,
    );

    port.dev.push_rx(b"first");
    let deadline = Instant::now() + Duration::from_secs(1);
    while rx.total_bytes() < 5 && Instant::now() < deadline {
        thread::yield_now();
    }
    assert_eq!(rx.total_bytes(), 5, "panic 前已完成的 RX 視窗更新應保留");
    assert!(
        data_rx.recv_timeout(Duration::from_millis(100)).is_err(),
        "primary RX handler panic 時不得分送該 chunk"
    );
    port.dev.push_rx(b"second");
    assert!(matches!(
        data_rx
        .recv_timeout(Duration::from_secs(1))
            .expect("primary panic 後 reader 應續行"),
        DataEvent::UartRxChunk { ref bytes, rx_seq: 2, .. } if bytes == b"second"
    ));
    assert_eq!(rx.total_bytes(), 11);
    assert_eq!(
        *warnings.lock().unwrap_or_else(|error| error.into_inner()),
        ["WARNING uart reader RX handler 例外（com=COM0），略過此塊、reader 續行"]
    );
    assert!(
        control_rx.try_recv().is_err(),
        "單一 primary RX handler panic 不得使 bridge down"
    );
    let _ = stop_and_join(&stop, &port.waker, worker);
}

#[test]
fn wait_for_regex_from_recomputes_rel_after_trim() {
    let rx = RxShared::new(4, 0);
    rx.append(b"old!", 1, 3, 1);
    let regex = compile("new", PyFlags::default()).unwrap();
    let waiting_rx = Arc::clone(&rx);
    let clock = FakeClock::at(1, 0);
    let waiting_clock = Arc::clone(&clock);
    let waiter = thread::spawn(move || {
        waiting_rx.wait_for_regex_from(&regex, 2, Duration::from_secs(1), &*waiting_clock)
    });
    thread::sleep(Duration::from_millis(25));
    rx.append(b"new", 2, 3, 2);
    assert_eq!(
        waiter.join().unwrap().map(|m| (m.epoch, m.rx_seq)),
        Some((3, 2))
    );
}

#[test]
fn read_error_reports_serial_read_errno() {
    let port = fake_port();
    let clock = FakeClock::at(1, 0);
    let (wal, _wfs) = fake_wal(Arc::clone(&clock) as Arc<dyn Clock>);
    let (data_tx, _data_rx) = bounded(16);
    let (control_tx, control_rx) = bounded(16);
    let worker = spawn_reader(
        port.reader,
        reader_ctx(wal, RxShared::new(64, 0), None, data_tx, control_tx, clock),
        Arc::new(AtomicBool::new(false)),
    );
    port.backend.remove_device("/dev/f");
    assert!(matches!(
        control_rx.recv_timeout(Duration::from_secs(1)).expect("UartIoError"),
        ControlEvent::UartIoError { epoch: 3, reason } if reason == "SERIAL_READ:5"
    ));
    assert_eq!(
        worker
            .join()
            .unwrap()
            .error
            .as_ref()
            .and_then(|error| error.os_code),
        Some(5)
    );
}

#[test]
fn reader_wait_error_reason_is_select_errno() {
    let clock = FakeClock::at(1, 0);
    let (wal, _wfs) = fake_wal(Arc::clone(&clock) as Arc<dyn Clock>);
    let error = rx_error(5);
    let reader = script_reader([ScriptStep::Error(sw_pal::traits::ReadError::Wait(
        error.clone(),
    ))]);
    let (data_tx, _data_rx) = bounded(16);
    let (control_tx, control_rx) = bounded(16);
    let worker = spawn_reader(
        reader,
        reader_ctx(wal, RxShared::new(64, 0), None, data_tx, control_tx, clock),
        Arc::new(AtomicBool::new(false)),
    );
    assert!(matches!(
        control_rx.recv_timeout(Duration::from_secs(1)).expect("UartIoError"),
        ControlEvent::UartIoError { epoch: 3, reason } if reason == "SELECT:5"
    ));
    assert_eq!(worker.join().unwrap().error, Some(error));
}

#[test]
fn eof_does_not_report_bridge_down() {
    let clock = FakeClock::at(1, 0);
    let (wal, _wfs) = fake_wal(Arc::clone(&clock) as Arc<dyn Clock>);
    let (calls_tx, calls_rx) = bounded(16);
    let reader = Box::new(ScriptReader {
        steps: (0..5)
            .map(|_| ScriptStep::Outcome(ReadOutcome::Eof))
            .chain([ScriptStep::Outcome(ReadOutcome::TimedOut)])
            .collect(),
        calls: Some(calls_tx),
    });
    let (data_tx, _data_rx) = bounded(16);
    let (control_tx, control_rx) = bounded(16);
    let stop = Arc::new(AtomicBool::new(false));
    let worker = spawn_reader(
        reader,
        reader_ctx(
            wal,
            RxShared::new(64, 0),
            None,
            data_tx,
            control_tx,
            clock.clone(),
        ),
        Arc::clone(&stop),
    );
    for count in 1..=5 {
        calls_rx
            .recv_timeout(Duration::from_secs(1))
            .expect("讀取 EOF");
        let deadline = Instant::now() + Duration::from_secs(1);
        while clock.sleeps.lock().unwrap().len() < count && Instant::now() < deadline {
            thread::yield_now();
        }
        assert!(
            clock.sleeps.lock().unwrap().len() >= count,
            "EOF 後須等待 200 ms"
        );
        clock.advance(IO_POLL);
    }
    calls_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("第五次 EOF 後 reader 仍存活");
    assert!(control_rx.try_recv().is_err(), "EOF 不得送出 UartIoError");
    stop.store(true, Ordering::Release);
    let exit = worker.join().expect("reader 應正常結束");
    assert!(exit.error.is_none());
}

#[test]
fn woken_and_stop_flag_exits() {
    let port = fake_port();
    let clock = FakeClock::at(1, 0);
    let (wal, _wfs) = fake_wal(Arc::clone(&clock) as Arc<dyn Clock>);
    let (data_tx, _data_rx) = bounded(16);
    let (control_tx, _control_rx) = bounded(16);
    let stop = Arc::new(AtomicBool::new(false));
    let worker = spawn_reader(
        port.reader,
        reader_ctx(wal, RxShared::new(64, 0), None, data_tx, control_tx, clock),
        Arc::clone(&stop),
    );
    let exit = stop_and_join(&stop, &port.waker, worker);
    assert!(exit.error.is_none());
}

#[test]
fn sentinel_receives_raw_bytes_and_eagain_drops() {
    let port = fake_port();
    let clock = FakeClock::at(1, 0);
    let (wal, _wfs) = fake_wal(Arc::clone(&clock) as Arc<dyn Clock>);
    let state = Arc::new(Mutex::new(ConsoleState {
        write_zero: true,
        ..ConsoleState::default()
    }));
    let console: Box<dyn ConsoleIo> = Box::new(TestConsole::new(Arc::clone(&state)));
    let (data_tx, data_rx) = bounded(16);
    let (control_tx, _control_rx) = bounded(16);
    let stop = Arc::new(AtomicBool::new(false));
    let worker = spawn_reader(
        port.reader,
        reader_ctx(
            wal,
            RxShared::new(64, 0),
            Some(console),
            data_tx,
            control_tx,
            clock,
        ),
        Arc::clone(&stop),
    );
    port.dev.push_rx(b"raw\xffbytes");
    let event = data_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("RX event");
    assert!(matches!(event, DataEvent::UartRxChunk { ref bytes, .. } if bytes == b"raw\xffbytes"));
    let state = state.lock().unwrap();
    assert_eq!(state.output, b"raw\xffbytes");
    drop(state);
    let _ = stop_and_join(&stop, &port.waker, worker);
}

#[test]
fn data_lane_backpressure_blocks_reader_after_wal() {
    let fake_clock = FakeClock::at(1, 0);
    let clock: Arc<dyn Clock> = fake_clock;
    let (wal, _wfs) = fake_wal(Arc::clone(&clock));
    let (data_tx, data_rx) = bounded(1);
    let (control_tx, _control_rx) = bounded(16);
    let (reader, calls_rx) = script_reader_with_calls([
        ScriptStep::Outcome(ReadOutcome::Data(1)),
        ScriptStep::Outcome(ReadOutcome::Data(1)),
        ScriptStep::Outcome(ReadOutcome::Data(1)),
    ]);
    let stop = Arc::new(AtomicBool::new(false));
    let worker = spawn_reader(
        reader,
        reader_ctx(
            wal.clone(),
            RxShared::new(64, 0),
            None,
            data_tx,
            control_tx,
            Arc::clone(&clock),
        ),
        Arc::clone(&stop),
    );
    calls_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("第一輪 read");
    calls_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("第二輪 read");
    let deadline = Instant::now() + Duration::from_secs(1);
    while wal.current_seq() < 2 && Instant::now() < deadline {
        thread::yield_now();
    }
    assert_eq!(wal.current_seq(), 2, "兩塊 RX 均須先落 WAL");
    let third_read_started = calls_rx.recv_timeout(Duration::from_millis(100)).is_ok();
    let third_wal_written = wal.current_seq() > 2;

    stop.store(true, Ordering::Release);
    let (joined_tx, joined_rx) = bounded(1);
    let joiner = thread::spawn(move || {
        let _ = joined_tx.send(worker.join().is_ok());
    });
    let stopped_while_lane_full = joined_rx
        .recv_timeout(Duration::from_millis(200))
        .unwrap_or(false);
    drop(data_rx);
    if !stopped_while_lane_full {
        assert!(
            joined_rx
                .recv_timeout(Duration::from_secs(1))
                .expect("關閉 data lane 後 reader 應解除阻塞")
        );
    }
    joiner.join().expect("join observer 應正常結束");

    assert!(!third_read_started, "data lane 滿時 reader 不得讀第三塊");
    assert!(!third_wal_written, "第三塊不得先於 data lane 空位寫入 WAL");
    assert!(stopped_while_lane_full, "stop 應解除等待中的 data send");
}

#[test]
fn sentinel_input_discarded() {
    let port = fake_port();
    let clock = FakeClock::at(1, 0);
    let (wal, _wfs) = fake_wal(Arc::clone(&clock) as Arc<dyn Clock>);
    let state = Arc::new(Mutex::new(ConsoleState {
        input: b"human input".iter().copied().collect(),
        ..ConsoleState::default()
    }));
    let console: Box<dyn ConsoleIo> = Box::new(TestConsole::new(Arc::clone(&state)));
    let (data_tx, data_rx) = bounded(16);
    let (control_tx, _control_rx) = bounded(16);
    let stop = Arc::new(AtomicBool::new(false));
    let worker = spawn_reader(
        port.reader,
        reader_ctx(
            wal.clone(),
            RxShared::new(64, 0),
            Some(console),
            data_tx,
            control_tx,
            clock,
        ),
        Arc::clone(&stop),
    );
    let deadline = Instant::now() + Duration::from_secs(1);
    while state.lock().unwrap().reads == 0 && Instant::now() < deadline {
        thread::yield_now();
    }
    assert!(
        state.lock().unwrap().reads > 0,
        "reader 應排空 sentinel 輸入"
    );
    assert!(data_rx.try_recv().is_err(), "human 輸入不得成為 UART RX");
    assert!(port.dev.take_tx().is_empty(), "human 輸入不得送往 UART");
    assert_eq!(wal.current_seq(), 0, "human 輸入不得寫入 WAL");
    let _ = stop_and_join(&stop, &port.waker, worker);
}

#[test]
fn rate_window_merge_oldest_keeps_byte_sum() {
    let rx = RxShared::new(8_192, 0);
    for index in 0..4_097u64 {
        rx.append(b"x", index, 1, index + 1);
    }
    assert_eq!(rx.rate_window_len(), 4096);
    assert_eq!(rx.rx_stats(10_000_000_001).0, 4097);
    assert_eq!(rx.rx_stats(10_000_000_002).0, 4095);
}

#[test]
fn rx_observer_panic_isolated_reader_continues() {
    let port = fake_port();
    let clock = FakeClock::at(1, 0);
    let (wal, _wfs) = fake_wal(Arc::clone(&clock) as Arc<dyn Clock>);
    let rx = RxShared::new(64, 0);
    let state = Arc::new(Mutex::new(ConsoleState {
        panic_once: true,
        ..ConsoleState::default()
    }));
    let console: Box<dyn ConsoleIo> = Box::new(TestConsole::new(Arc::clone(&state)));
    let (data_tx, data_rx) = bounded(16);
    let (control_tx, control_rx) = bounded(16);
    let stop = Arc::new(AtomicBool::new(false));
    let warnings = Arc::new(Mutex::new(Vec::<String>::new()));
    let warning_lines = Arc::clone(&warnings);
    let warning_sink: Arc<dyn Fn(&str) + Send + Sync> = Arc::new(move |line| {
        warning_lines
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .push(line.to_string());
    });
    let worker = spawn_reader_with_test_hooks(
        port.reader,
        reader_ctx(
            wal,
            Arc::clone(&rx),
            Some(console),
            data_tx,
            control_tx,
            clock,
        ),
        Arc::clone(&stop),
        Arc::new(|| {}),
        warning_sink,
    );
    port.dev.push_rx(b"first");
    assert!(matches!(
        data_rx
        .recv_timeout(Duration::from_secs(1))
            .expect("observer panic 不得丟棄 primary RX event"),
        DataEvent::UartRxChunk { ref bytes, rx_seq: 1, .. } if bytes == b"first"
    ));
    port.dev.push_rx(b"second");
    assert!(matches!(
        data_rx.recv_timeout(Duration::from_secs(1)).expect("第二塊 RX"),
        DataEvent::UartRxChunk { ref bytes, .. } if bytes == b"second"
    ));
    assert_eq!(rx.total_bytes(), 11);
    assert_eq!(
        *warnings.lock().unwrap_or_else(|error| error.into_inner()),
        ["WARNING uart reader RX handler 例外（com=COM0），略過此塊、reader 續行"]
    );
    assert!(control_rx.try_recv().is_err());
    let _ = stop_and_join(&stop, &port.waker, worker);
}
