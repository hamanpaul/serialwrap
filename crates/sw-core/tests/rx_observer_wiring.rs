//! P7 Task 16-wire（08-slice23-login-events.md）：reader 的 `RxObserver`（DF-15）——每個 RX chunk 在 WAL append 之後以
//! 該 chunk 的精確 `WalAppend.seq` 依序呼叫每個 observer；observer panic 被吞掉（Python `except Exception: pass`）。
//!
//! 偏離計畫原文：不在 06 `ReaderCtx` 加 `observers` 欄位（P5 測試以 struct literal 建構），改以
//! `spawn_reader_observed`／`IoWorker::start_observed` 另傳觀察者清單（reader 建立時固定）。
#![cfg(feature = "test-fakes")]

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crossbeam_channel::bounded;
use sw_core::mailbox::DataEvent;
use sw_core::uart_bridge::{spawn_reader_observed, ReaderCtx, RxObserver, RxShared};
use sw_core::wal::{WalAppendReq, WalConfig, WalWriter};
use sw_pal::fakes::clock::FakeClock;
use sw_pal::fakes::serial::FakeSerialBackend;
use sw_pal::fakes::wal_fs::FakeWalFs;
use sw_pal::traits::{Clock, DevicePath, SerialBackend, SerialConfig};

#[derive(Default)]
struct Recorder {
    calls: Mutex<Vec<(String, Vec<u8>, u64, u64)>>,
    wal: Mutex<Option<Arc<WalWriter>>>,
}

impl RxObserver for Recorder {
    fn on_rx(&self, com: &str, data: &[u8], wal_seq: u64) {
        let cur = self
            .wal
            .lock()
            .unwrap()
            .as_ref()
            .map_or(0, |w| w.current_seq());
        self.calls
            .lock()
            .unwrap()
            .push((com.to_string(), data.to_vec(), wal_seq, cur));
    }
}

struct Panicker;

impl RxObserver for Panicker {
    fn on_rx(&self, _com: &str, _data: &[u8], _wal_seq: u64) {
        panic!("observer 故障");
    }
}

struct Setup {
    dev: sw_pal::fakes::serial::FakeDevice,
    wal: Arc<WalWriter>,
    data_rx: crossbeam_channel::Receiver<DataEvent>,
    stop: Arc<AtomicBool>,
    waker: Arc<dyn sw_pal::traits::IoWaker>,
    worker: std::thread::JoinHandle<sw_core::uart_bridge::ReaderExit>,
    _close: sw_pal::traits::CloseToken,
}

fn setup(observers: Vec<Arc<dyn RxObserver>>) -> Setup {
    let clock: Arc<dyn Clock> = FakeClock::new();
    let wal = Arc::new(WalWriter::open(
        WalConfig {
            dir: "/w".into(),
            rotate_bytes: 1 << 26,
        },
        FakeWalFs::new(),
        clock.clone(),
    ));
    let backend = FakeSerialBackend::new();
    let dev = backend.add_device("/dev/f");
    let handle = backend
        .open_exclusive(
            &DevicePath("/dev/f".into()),
            &SerialConfig::from_uart_profile(115200, 8, "N", 1, "none", false),
        )
        .unwrap();
    let (reader, _writer, waker, close) = handle.split();
    let (data_tx, data_rx) = bounded(64);
    let (control_tx, _control_rx) = bounded(64);
    let stop = Arc::new(AtomicBool::new(false));
    let worker = spawn_reader_observed(
        reader,
        ReaderCtx {
            com: "COM0".into(),
            epoch: 3,
            wal: wal.clone(),
            rx: RxShared::new(131_072, 1),
            sentinel: None,
            data: data_tx,
            control: control_tx,
            clock,
        },
        stop.clone(),
        observers,
    );
    Setup {
        dev,
        wal,
        data_rx,
        stop,
        waker,
        worker,
        _close: close,
    }
}

fn chunk_seq(rx: &crossbeam_channel::Receiver<DataEvent>) -> (Vec<u8>, u64) {
    match rx.recv_timeout(Duration::from_secs(5)).expect("data lane") {
        DataEvent::UartRxChunk { bytes, wal_seq, .. } => (bytes, wal_seq),
        other => panic!("{other:?}"),
    }
}

fn finish(s: Setup) {
    s.stop.store(true, Ordering::Release);
    s.waker.wake();
    s.worker.join().unwrap();
}

#[test]
fn observer_receives_exact_wal_seq_per_chunk() {
    let rec = Arc::new(Recorder::default());
    let s = setup(vec![rec.clone()]);
    *rec.wal.lock().unwrap() = Some(s.wal.clone());
    let mut want = Vec::new();
    for (i, chunk) in [b"aa\n".as_slice(), b"bb", b"cc\n"].iter().enumerate() {
        // 期間另一 session 推進 WAL seq。
        s.wal.append(WalAppendReq {
            com: "COM1",
            dir: "RX",
            source: "device",
            payload: format!("other{i}").as_bytes(),
            cmd_id: None,
            loss_flag: false,
            meta: None,
        });
        s.dev.push_rx(chunk);
        want.push(chunk_seq(&s.data_rx));
    }
    let end = Instant::now() + Duration::from_secs(5);
    while rec.calls.lock().unwrap().len() < 3 {
        assert!(Instant::now() < end);
        std::thread::sleep(Duration::from_millis(2));
    }
    let calls = rec.calls.lock().unwrap().clone();
    for ((com, data, seq, cur), (bytes, wal_seq)) in calls.iter().zip(&want) {
        assert_eq!(com, "COM0");
        assert_eq!(data, bytes);
        assert_eq!(seq, wal_seq, "DF-15：observer 取該 chunk 的 WAL seq");
        assert!(
            cur >= seq,
            "observer_called_after_wal_append：呼叫時 current_seq 已含該 chunk"
        );
    }
    finish(s);
}

#[test]
fn observer_called_after_wal_append() {
    let rec = Arc::new(Recorder::default());
    let s = setup(vec![rec.clone()]);
    *rec.wal.lock().unwrap() = Some(s.wal.clone());
    s.dev.push_rx(b"x\n");
    let (_, seq) = chunk_seq(&s.data_rx);
    let end = Instant::now() + Duration::from_secs(5);
    while rec.calls.lock().unwrap().is_empty() {
        assert!(Instant::now() < end);
        std::thread::sleep(Duration::from_millis(2));
    }
    let (_, _, got, cur) = rec.calls.lock().unwrap()[0].clone();
    assert_eq!(got, seq);
    assert!(cur >= seq);
    finish(s);
}

#[test]
fn observer_panic_is_contained() {
    let rec = Arc::new(Recorder::default());
    let s = setup(vec![Arc::new(Panicker), rec.clone()]);
    s.dev.push_rx(b"one\n");
    let _ = chunk_seq(&s.data_rx);
    s.dev.push_rx(b"two\n");
    let _ = chunk_seq(&s.data_rx);
    let end = Instant::now() + Duration::from_secs(5);
    while rec.calls.lock().unwrap().len() < 2 {
        assert!(
            Instant::now() < end,
            "panic 的 observer 之後的 observer 仍收到、reader 不中止"
        );
        std::thread::sleep(Duration::from_millis(2));
    }
    finish(s);
}
