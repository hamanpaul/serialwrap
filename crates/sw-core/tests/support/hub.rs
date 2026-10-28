//! P6 Slice 2.2：ConsoleHub 測試 harness（07 Task 1 簽章；本體由 Task 9 補齊）。
//!
//! 以 `FakeConsoleFactory` 驅動真的 hub thread；hub 的 control／data lane 經容量 1 的 channel 交給 pump thread
//! 收集。`block_mailbox(true)` 等 pump 確認已離開 `recv`，再以佔位事件填滿 data channel，之後 data lane 的
//! `try_send` 一律 Full（D-17(1)；不會有 channel 緩衝或在途 `recv` 多收的那 1～2 筆）。佔位事件不計入 `data_events`。

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crossbeam_channel::bounded;
use sw_core::human_console::hub::{spawn_hub, HubCmd, HubHandle, HubStatus, HubTap, StallGate};
use sw_core::mailbox::{ControlEvent, DataEvent};
use sw_pal::fakes::clock::FakeClock;
use sw_pal::fakes::console::{FakeConsoleFactory, FakeConsoleRef, FakeIo};
use sw_pal::traits::Clock;
use sw_pal::ConsoleKind;

/// 以 `FakeConsoleFactory` 驅動真的 hub thread。
pub struct HubHarness {
    pub hub: HubHandle,
    pub fake: FakeConsoleFactory,
    pub clock: Arc<FakeClock>,
    control: Arc<Mutex<Vec<ControlEvent>>>,
    data: Arc<Mutex<Vec<DataEvent>>>,
    blocked: Arc<AtomicBool>,
    /// data pump 已觀察到 blocked 並停在 `recv` 之外。
    paused: Arc<AtomicBool>,
    /// block 時填滿 data channel 的入口（佔位事件）。
    data_fill: crossbeam_channel::Sender<DataEvent>,
    pumps_stop: Arc<AtomicBool>,
    tap: HubTap,
    reader_warnings: Arc<Mutex<Vec<String>>>,
    next_pty: AtomicU64,
    gate: Mutex<Option<Arc<StallGate>>>,
    start: u64,
}

impl Drop for HubHarness {
    fn drop(&mut self) {
        self.unstall();
        self.hub.shutdown();
        self.pumps_stop.store(true, Ordering::Release);
    }
}

/// `block_mailbox` 佔位事件的 client id（不會與 12-hex client id 衝突）。
const FILL_CID: &str = "__mailbox_fill__";

fn pump<T: Send + 'static>(
    rx: crossbeam_channel::Receiver<T>,
    sink: Arc<Mutex<Vec<T>>>,
    blocked: Arc<AtomicBool>,
    paused: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
) {
    std::thread::spawn(move || {
        while !stop.load(Ordering::Acquire) {
            if blocked.load(Ordering::Acquire) {
                paused.store(true, Ordering::Release);
                std::thread::sleep(Duration::from_millis(1));
                continue;
            }
            paused.store(false, Ordering::Release);
            if let Ok(ev) = rx.recv_timeout(Duration::from_millis(2)) {
                sink.lock().unwrap().push(ev);
            }
        }
    });
}

impl HubHarness {
    fn with_kind(com: &str, kind: ConsoleKind) -> HubHarness {
        let fake = FakeConsoleFactory::new(kind);
        let clock = FakeClock::at(5_000_000_000, 1_000_000);
        let platform = sw_pal::fakes::platform::platform()
            .console(Arc::new(fake.clone()))
            .clock(clock.clone())
            .build();
        let (ctl_tx, ctl_rx) = bounded::<ControlEvent>(1);
        let (data_tx, data_rx) = bounded::<DataEvent>(1);
        let data_fill = data_tx.clone();
        let control = Arc::new(Mutex::new(Vec::new()));
        let data = Arc::new(Mutex::new(Vec::new()));
        let blocked = Arc::new(AtomicBool::new(false));
        let paused = Arc::new(AtomicBool::new(false));
        let pumps_stop = Arc::new(AtomicBool::new(false));
        pump(
            ctl_rx,
            control.clone(),
            Arc::new(AtomicBool::new(false)),
            Arc::new(AtomicBool::new(false)),
            pumps_stop.clone(),
        );
        pump(data_rx, data.clone(), blocked.clone(), paused.clone(), pumps_stop.clone());
        let start = clock.mono_ns();
        let hub = spawn_hub(
            com.to_string(),
            &platform,
            ctl_tx,
            data_tx,
            clock.clone(),
            Arc::new(AtomicU64::new(0)),
        );
        let tap = hub.tap();
        HubHarness {
            hub,
            fake,
            clock,
            control,
            data,
            blocked,
            paused,
            data_fill,
            pumps_stop,
            tap,
            reader_warnings: Arc::new(Mutex::new(Vec::new())),
            next_pty: AtomicU64::new(0),
            gate: Mutex::new(None),
            start,
        }
    }

    pub fn tcp(com: &str) -> HubHarness {
        HubHarness::with_kind(com, ConsoleKind::TcpTelnet)
    }
    pub fn pty(com: &str) -> HubHarness {
        HubHarness::with_kind(com, ConsoleKind::Pty)
    }
    pub fn adopt_pty_with(&self, io: FakeIo) -> FakeConsoleRef {
        let r = io.reference();
        let n = self.next_pty.fetch_add(1, Ordering::Relaxed) + 1;
        let cid = format!("p{n}");
        r.set_client_id(&cid);
        let pair = self.fake.pty_pair_from(io);
        self.hub
            .cmd
            .try_send(HubCmd::AdoptPty {
                client_id: cid.clone(),
                client_lifetime: n,
                label: cid,
                pair,
            })
            .unwrap_or_else(|_| panic!("AdoptPty 送出失敗"));
        self.hub.waker.wake();
        r
    }
    /// 回 `try_send` 是否成功（與 reader 相同的 `HubTap::push`）。
    pub fn reader_push(&self, b: &[u8]) -> bool {
        let warnings = self.reader_warnings.clone();
        self.tap.push(b, &move |line: &str| {
            warnings.lock().unwrap().push(line.to_string())
        })
    }
    /// 等 hub 處理完目前所有 channel 內容（上限 2 s）。
    pub fn settle(&self) {
        let deadline = Instant::now() + Duration::from_secs(2);
        let status = &self.hub.status;
        let mut quiet_loops = 0;
        let mut last = status.loops.load(Ordering::Acquire);
        loop {
            self.hub.waker.wake();
            std::thread::sleep(Duration::from_millis(1));
            let now = status.loops.load(Ordering::Acquire);
            let drained = self.hub.cmd.is_empty()
                && self.hub.rx.is_empty()
                && (self.blocked.load(Ordering::Acquire)
                    || status.pending.load(Ordering::Acquire) == 0);
            if now > last && drained {
                quiet_loops += 1;
                if quiet_loops >= 3 {
                    break;
                }
            }
            last = now;
            if self.gate.lock().unwrap().is_some() || status.exited.load(Ordering::Acquire) {
                break;
            }
            assert!(Instant::now() < deadline, "HubHarness::settle 逾時");
        }
        std::thread::sleep(Duration::from_millis(5)); // pump thread 收取
    }
    pub fn control_events(&self) -> Vec<ControlEvent> {
        self.control
            .lock()
            .unwrap()
            .iter()
            .map(clone_control)
            .collect()
    }
    pub fn data_events(&self) -> Vec<DataEvent> {
        self.data
            .lock()
            .unwrap()
            .iter()
            .filter(|e| !matches!(e, DataEvent::ConsoleDisconnected { client_id, .. } if client_id == FILL_CID))
            .map(clone_data)
            .collect()
    }
    pub fn status(&self) -> &HubStatus {
        &self.hub.status
    }
    pub fn ring_display_bytes(&self, cid: &str) -> usize {
        self.hub
            .status
            .ring_display
            .lock()
            .unwrap()
            .get(cid)
            .copied()
            .unwrap_or(0)
    }
    pub fn block_mailbox(&self, on: bool) {
        self.blocked.store(on, Ordering::Release);
        if on {
            let deadline = Instant::now() + Duration::from_secs(2);
            while !self.paused.load(Ordering::Acquire) {
                assert!(Instant::now() < deadline, "data pump 未暫停");
                std::thread::sleep(Duration::from_millis(1));
            }
            // channel 若尚有空位，以佔位事件填滿（已滿則代表 block 前已送入一筆，照常計入）。
            let _ = self.data_fill.try_send(DataEvent::ConsoleDisconnected {
                client_id: FILL_CID.into(),
                client_lifetime: 0,
            });
        }
    }

    // ---- P6 Task 9 追加（Task 1 簽章之外） ----
    /// hub 在下一輪停住（`HubCmd::TestStall`），直到 `unstall`。
    pub fn stall(&self) {
        let gate = Arc::new(StallGate::default());
        self.hub
            .cmd
            .try_send(HubCmd::TestStall(gate.clone()))
            .unwrap_or_else(|_| panic!("TestStall 送出失敗"));
        self.hub.waker.wake();
        let deadline = Instant::now() + Duration::from_secs(2);
        while !gate.entered.load(Ordering::Acquire) {
            assert!(Instant::now() < deadline, "hub 未進入停住狀態");
            self.hub.waker.wake();
            std::thread::sleep(Duration::from_millis(1));
        }
        *self.gate.lock().unwrap() = Some(gate);
    }
    pub fn unstall(&self) {
        if let Some(gate) = self.gate.lock().unwrap().take() {
            *gate.released.lock().unwrap() = true;
            gate.cond.notify_all();
        }
    }
    /// hub 的 WARNING。
    pub fn warnings(&self) -> Vec<String> {
        self.hub
            .status
            .warnings
            .lock()
            .unwrap()
            .iter()
            .cloned()
            .collect()
    }
    /// `reader_push` 經 `HubTap::push` 產生的 WARNING。
    pub fn reader_warnings(&self) -> Vec<String> {
        self.reader_warnings.lock().unwrap().clone()
    }
    /// hub 迴圈輪數。
    pub fn loops(&self) -> u64 {
        self.hub.status.loops.load(Ordering::Acquire)
    }
    /// 推進注入的 FakeClock 並喚醒 hub。
    pub fn advance(&self, d: Duration) {
        self.clock.advance(d);
        self.hub.waker.wake();
    }
    /// harness 建立時的 monotonic 時刻。
    pub fn start_mono(&self) -> u64 {
        self.start
    }
    /// `shutdown` 後等 hub thread 結束（逾時 panic）。
    pub fn join_within(&self, d: Duration) {
        let deadline = Instant::now() + d;
        while !self.hub.status.exited.load(Ordering::Acquire) {
            assert!(Instant::now() < deadline, "hub thread 未於期限內結束");
            self.hub.waker.wake();
            std::thread::sleep(Duration::from_millis(1));
        }
    }
    /// 下一個 console 輸入處理 panic（#89）。
    pub fn panic_next_input_handler(&self) {
        self.hub
            .status
            .panic_next_input
            .store(true, Ordering::Release);
    }
}

fn clone_control(e: &ControlEvent) -> ControlEvent {
    match e {
        ControlEvent::ConsoleConnected {
            client_id,
            client_lifetime,
            label,
            vtty,
            kind,
            close_gen,
        } => ControlEvent::ConsoleConnected {
            client_id: client_id.clone(),
            client_lifetime: *client_lifetime,
            label: label.clone(),
            vtty: vtty.clone(),
            kind: *kind,
            close_gen: *close_gen,
        },
        ControlEvent::ConsoleScan { scan, source } => ControlEvent::ConsoleScan {
            scan: scan.clone(),
            source: source.clone(),
        },
        _ => ControlEvent::Tick,
    }
}

fn clone_data(e: &DataEvent) -> DataEvent {
    match e {
        DataEvent::UartRxChunk {
            epoch,
            rx_seq,
            wal_seq,
            bytes,
        } => DataEvent::UartRxChunk {
            epoch: *epoch,
            rx_seq: *rx_seq,
            wal_seq: *wal_seq,
            bytes: bytes.clone(),
        },
        DataEvent::ConsoleInputBytes {
            client_id,
            client_lifetime,
            input_epoch,
            bytes,
        } => DataEvent::ConsoleInputBytes {
            client_id: client_id.clone(),
            client_lifetime: *client_lifetime,
            input_epoch: *input_epoch,
            bytes: bytes.clone(),
        },
        DataEvent::ConsoleDisconnected {
            client_id,
            client_lifetime,
        } => DataEvent::ConsoleDisconnected {
            client_id: client_id.clone(),
            client_lifetime: *client_lifetime,
        },
    }
}
