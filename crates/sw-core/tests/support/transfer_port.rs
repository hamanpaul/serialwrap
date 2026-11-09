//! P8 Task 5：`OpTransferPort` 測試台（`PortRig`）。
//!
//! 以真的 `IoWorker`（reader＋唯一 TxWriter）、`Fence`、`FakeSerialBackend`／`FakeDevice`、`FakeWalFs` 與
//! threaded `FakeClock`（只在測試推進時前進）組成。`FakeDut` 是外部 peer：背景 thread 讀取裝置已確認寫出的
//! bytes，依策略回 echo／輸出。`rig.dut.writes()` 為外部實際收到的寫入序列（每次 OS 提交一筆），外部行為
//! 一律以它斷言，不以 WAL 推論；`rig.tx_records()` 為 WAL 讀回。

use std::panic::resume_unwind;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use crossbeam_channel::{Receiver, Sender};
use sw_core::arbiter::CommandRegistry;
use sw_core::drain::{StopId, WorkerStoppedReceipt};
use sw_core::file_transfer::templates as t;
use sw_core::file_transfer::OpTransferPort;
use sw_core::mailbox::{ControlEvent, DataEvent};
use sw_core::op_runner::OpCtx;
use sw_core::resources::RX_WINDOW_MAX_CHARS;
use sw_core::session_core::OpGrant;
use sw_core::tx_writer::{Fence, OpId};
use sw_core::uart_bridge::{IoWorker, IoWorkerArgs, RunCancel, RxShared};
use sw_core::wal::{WalConfig, WalWriter};
use sw_pal::fakes::clock::FakeClock;
use sw_pal::fakes::serial::{FakeDevice, FakeSerialBackend, FakeWriteStep};
use sw_pal::fakes::wal_fs::FakeWalFs;
use sw_pal::traits::{Clock, DevicePath, PalError, SerialBackend, SerialConfig};
use sw_proto::pycompat::a2b_base64_lenient;
use sw_proto::pyjson::{loads, PyDict, PyValue};

/// rig 的 bridge epoch（fence `active_epoch`、permit epoch）。
pub const EPOCH: u64 = 7;
/// rig 的前景 op。
pub const OP: OpId = OpId(41);
/// 等待外部條件的實時上限（逾時即判失敗，不讓測試卡死）。
pub const REAL_WAIT: Duration = Duration::from_secs(10);

/// 外部 peer 的行為設定。
#[derive(Clone)]
pub struct FakeDut {
    /// 只 echo 串流中前 n 個字元（`None` 全部 echo）。
    echo_limit: Option<usize>,
    /// 完全不 echo。
    silent: bool,
    /// 每批 echo 之後插入的非同步輸出（printk 等）。
    noise: Option<String>,
    /// 在每批 echo 中間插入 `\x1b[0m`。
    ansi: bool,
    /// 寫入腳本（依序消耗；耗盡後恢復全部接受）。
    script: Vec<FakeWriteStep>,
    /// shell 模式：每行命令結束時回應（decoder probe → sentinel；其他 → prompt `# `）。
    shell: bool,
    /// shell 模式：以此開頭的行只 echo 前 n 個字元（模擬 echo 停滯）。
    stall_prefix: Option<(String, usize)>,
}

impl FakeDut {
    pub fn echo_all() -> FakeDut {
        FakeDut {
            echo_limit: None,
            silent: false,
            noise: None,
            ansi: false,
            script: Vec::new(),
            shell: false,
            stall_prefix: None,
        }
    }

    pub fn echo_first_n_chars(n: usize) -> FakeDut {
        FakeDut { echo_limit: Some(n), ..FakeDut::echo_all() }
    }

    pub fn silent() -> FakeDut {
        FakeDut { silent: true, ..FakeDut::echo_all() }
    }

    /// 模擬 busybox shell：echo 命令、回 decoder probe sentinel 與 prompt `# `。
    pub fn shell() -> FakeDut {
        FakeDut { shell: true, ..FakeDut::echo_all() }
    }

    pub fn with_noise(mut self, noise: &str) -> FakeDut {
        self.noise = Some(noise.to_string());
        self
    }

    pub fn with_ansi(mut self) -> FakeDut {
        self.ansi = true;
        self
    }

    /// 第 `nth` 次寫入（1 起算）只接受 `accept` bytes 後回錯誤（部分寫入）。
    pub fn partial_write_on(mut self, nth: usize, accept: usize) -> FakeDut {
        self.pad_script(nth);
        self.script.push(FakeWriteStep::AcceptThenError(
            accept,
            PalError::io(5, "[Errno 5] Input/output error"),
        ));
        self
    }

    /// 第 `nth` 次寫入（1 起算）結果不明（已確認 `confirmed` bytes）。
    pub fn unknown_on(mut self, nth: usize, confirmed: usize) -> FakeDut {
        self.pad_script(nth);
        self.script.push(FakeWriteStep::Unknown {
            confirmed,
            error: PalError::io(5, "[Errno 5] Input/output error"),
        });
        self
    }

    /// shell 模式：以 `prefix` 開頭的命令行只 echo 前 `n` 個字元。
    pub fn stall_lines_starting_with(mut self, prefix: &str, n: usize) -> FakeDut {
        self.stall_prefix = Some((prefix.to_string(), n));
        self
    }

    fn pad_script(&mut self, nth: usize) {
        assert!(nth >= 1 && self.script.len() < nth, "寫入腳本須依序設定");
        while self.script.len() + 1 < nth {
            self.script.push(FakeWriteStep::Accept(usize::MAX));
        }
    }
}

/// 外部 peer 的觀察端。
#[derive(Clone)]
pub struct DutHandle {
    pub dev: FakeDevice,
    /// 每次 OS 提交前：（外部已收到的 bytes，RX 視窗已收到的 bytes）。
    bounds: Arc<Mutex<Vec<(usize, u64)>>>,
    processed: Arc<Mutex<usize>>,
}

impl DutHandle {
    /// 外部實際收到的寫入序列（每次 OS 提交一筆；零長度提交略過）。
    pub fn writes(&self) -> Vec<Vec<u8>> {
        self.submits().into_iter().map(|(w, _)| w).collect()
    }

    /// 每次非空 OS 提交送出當下，RX 視窗已收到的 bytes（echo 流控斷言用）。
    pub fn rx_at_submit(&self) -> Vec<u64> {
        self.submits().into_iter().map(|(_, rx)| rx).collect()
    }

    fn submits(&self) -> Vec<(Vec<u8>, u64)> {
        let bounds = self.bounds.lock().unwrap().clone();
        let all = self.dev.tx_since(0);
        let mut out = Vec::new();
        for (i, (start, rx)) in bounds.iter().enumerate() {
            let end = bounds.get(i + 1).map_or(all.len(), |(s, _)| *s);
            let piece = all[*start..end.max(*start)].to_vec();
            if !piece.is_empty() {
                out.push((piece, *rx));
            }
        }
        out
    }

    /// 外部收到的全部 bytes。
    pub fn bytes(&self) -> Vec<u8> {
        self.dev.tx_since(0)
    }

    /// 等外部收到至少 `n` bytes 且 DUT thread 已處理完它們（實時上限 [`REAL_WAIT`]）。
    pub fn wait_bytes(&self, n: usize) {
        let deadline = Instant::now() + REAL_WAIT;
        while self.dev.tx_len() < n || *self.processed.lock().unwrap() < n {
            assert!(
                Instant::now() < deadline,
                "外部只收到 {} bytes，等不到 {n}",
                self.dev.tx_len()
            );
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    /// 等外部收到的串流出現 `needle`。
    pub fn wait_contains(&self, needle: &[u8]) {
        let deadline = Instant::now() + REAL_WAIT;
        loop {
            let all = self.dev.tx_since(0);
            if all.windows(needle.len()).any(|w| w == needle) {
                return;
            }
            assert!(Instant::now() < deadline, "外部串流等不到 {needle:?}");
            std::thread::sleep(Duration::from_millis(1));
        }
    }
}

/// WAL 讀回的一筆紀錄。
#[derive(Clone, Debug)]
pub struct WalRec {
    pub seq: u64,
    pub dir: String,
    pub source: String,
    pub cmd_id: Option<String>,
    pub payload: Vec<u8>,
    pub meta: Option<PyDict>,
}

/// 收尾回條與其抵達當下的 WAL 狀態（回條送達時立即讀取）。
pub struct ReceiptAt {
    pub receipt: WorkerStoppedReceipt,
    /// 回條抵達當下 WAL 已配出的最大 seq。
    pub wal_seq_at_receipt: u64,
    /// 回條抵達當下 WAL 檔已有的紀錄。
    pub records_at_receipt: Vec<WalRec>,
}

/// 背景執行中的 port 呼叫。
pub struct PortTask<T> {
    join: Option<JoinHandle<T>>,
}

impl<T> PortTask<T> {
    pub fn is_finished(&self) -> bool {
        self.join.as_ref().is_none_or(JoinHandle::is_finished)
    }

    /// 等結果（實時上限 `limit`；逾時即判失敗）。thread 內 panic 原樣上拋。
    pub fn join_within(mut self, limit: Duration) -> T {
        let deadline = Instant::now() + limit;
        let join = self.join.take().expect("PortTask 只能 join 一次");
        while !join.is_finished() {
            assert!(Instant::now() < deadline, "port 呼叫在 {limit:?} 內未返回");
            std::thread::sleep(Duration::from_millis(1));
        }
        match join.join() {
            Ok(v) => v,
            Err(panic) => resume_unwind(panic),
        }
    }

    pub fn join(self) -> T {
        self.join_within(REAL_WAIT)
    }
}

pub struct PortRig {
    pub com: String,
    pub clock: Arc<FakeClock>,
    pub fence: Arc<Fence>,
    pub rx: Arc<RxShared>,
    pub wal: Arc<WalWriter>,
    pub wfs: Arc<FakeWalFs>,
    pub dut: DutHandle,
    pub ctx: OpCtx,
    pub grant: OpGrant,
    pub cancel: RunCancel,
    worker: Option<IoWorker>,
    ctl_tx: Sender<ControlEvent>,
    receipts: Receiver<ReceiptAt>,
    stop: Arc<AtomicBool>,
}

fn parse_records(bytes: &[u8]) -> Vec<WalRec> {
    String::from_utf8_lossy(bytes)
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| {
            let PyValue::Dict(d) = loads(l.trim_end_matches('\r')).expect("WAL 行應為合法 JSON") else {
                panic!("WAL 行應為 object：{l}");
            };
            let text = |k: &str| d.get(k).and_then(PyValue::as_str).map(str::to_string);
            WalRec {
                seq: match d.get("seq") {
                    Some(PyValue::Int(sw_proto::pyjson::PyInt::Small(v))) => *v as u64,
                    other => panic!("WAL seq 應為整數：{other:?}"),
                },
                dir: text("dir").unwrap_or_default(),
                source: text("source").unwrap_or_default(),
                cmd_id: text("cmd_id"),
                payload: a2b_base64_lenient(text("payload_b64").unwrap_or_default().as_bytes())
                    .expect("payload_b64 應可解碼"),
                meta: match d.get("meta") {
                    Some(PyValue::Dict(m)) => Some(m.clone()),
                    _ => None,
                },
            }
        })
        .collect()
}

/// 外部 peer thread：讀取裝置已確認的 TX，依 `spec` 回 RX。
fn spawn_dut(dev: FakeDevice, spec: FakeDut, stop: Arc<AtomicBool>, processed: Arc<Mutex<usize>>) {
    std::thread::spawn(move || {
        let mut cursor = 0usize;
        let mut echoed_chars = 0usize;
        let mut line: Vec<u8> = Vec::new();
        // shell 模式下本行的 echo 上限（字元）。
        let mut line_limit: Option<usize> = None;
        let mut line_echoed = 0usize;
        while !stop.load(Ordering::Acquire) {
            let new = dev.tx_since(cursor);
            if new.is_empty() {
                std::thread::sleep(Duration::from_millis(1));
                continue;
            }
            cursor += new.len();
            let mut echo: Vec<u8> = Vec::new();
            let mut out: Vec<u8> = Vec::new();
            let mut echo_cur = false;
            for (i, &b) in new.iter().enumerate() {
                let lead = b & 0xC0 != 0x80;
                if spec.shell && line.is_empty() && lead {
                    line_limit = spec
                        .stall_prefix
                        .as_ref()
                        .filter(|(p, _)| new[i..].starts_with(p.as_bytes()))
                        .map(|(_, n)| *n);
                    line_echoed = 0;
                }
                if b == 0x15 {
                    line.clear();
                    line_limit = None;
                    continue;
                }
                if b == b'\n' {
                    if !spec.silent {
                        echo.extend_from_slice(b"\r\n");
                    }
                    if spec.shell {
                        let cmd = String::from_utf8_lossy(&line).to_string();
                        if cmd == t::decoder_probe_command() {
                            out.extend_from_slice(format!("{}\r\n", t::DECODER_B64_SENTINEL).as_bytes());
                        }
                        out.extend_from_slice(b"# ");
                    }
                    line.clear();
                    line_limit = None;
                    continue;
                }
                line.push(b);
                if lead {
                    let global_ok = spec.echo_limit.is_none_or(|n| echoed_chars < n);
                    let line_ok = line_limit.is_none_or(|n| line_echoed < n);
                    echo_cur = !spec.silent && global_ok && line_ok;
                    if echo_cur {
                        echoed_chars += 1;
                        line_echoed += 1;
                    }
                }
                if echo_cur {
                    echo.push(b);
                }
            }
            let mut rx = Vec::new();
            if spec.ansi && !echo.is_empty() {
                let mut mid = echo.len() / 2;
                while mid > 0 && echo[mid] & 0xC0 == 0x80 {
                    mid -= 1;
                }
                rx.extend_from_slice(&echo[..mid]);
                rx.extend_from_slice(b"\x1b[0m");
                rx.extend_from_slice(&echo[mid..]);
            } else {
                rx.extend_from_slice(&echo);
            }
            if let Some(noise) = spec.noise.as_ref().filter(|_| !echo.is_empty()) {
                rx.extend_from_slice(noise.as_bytes());
            }
            rx.extend_from_slice(&out);
            if !rx.is_empty() {
                dev.push_rx(&rx);
            }
            *processed.lock().unwrap() = cursor;
        }
    });
}

impl PortRig {
    /// `com` 的 rig；fence 已就緒（`active_epoch == EPOCH`），無 quiet／flash。
    pub fn new(com: &str, spec: FakeDut) -> PortRig {
        let clock = FakeClock::at(1_000_000_000, 1_700_000_000_000_000);
        let rx = RxShared::new(RX_WINDOW_MAX_CHARS, clock.mono_ns());
        let be = FakeSerialBackend::new();
        let dev = be.add_device("/dev/fake-dut");
        dev.script_write(spec.script.clone());
        let bounds = Arc::new(Mutex::new(Vec::new()));
        {
            let bounds = Arc::clone(&bounds);
            let probe = dev.clone();
            let rx = Arc::clone(&rx);
            dev.on_os_submit(Box::new(move || {
                bounds.lock().unwrap().push((probe.tx_len(), rx.total_bytes()));
            }));
        }
        let processed = Arc::new(Mutex::new(0usize));
        let stop = Arc::new(AtomicBool::new(false));
        spawn_dut(dev.clone(), spec, Arc::clone(&stop), Arc::clone(&processed));

        let handle = be
            .open_exclusive(
                &DevicePath("/dev/fake-dut".into()),
                &SerialConfig::from_uart_profile(115200, 8, "N", 1, "none", false),
            )
            .expect("開啟假裝置");
        let wfs = FakeWalFs::new();
        let wal = Arc::new(WalWriter::open(
            WalConfig { dir: "/w".into(), rotate_bytes: 1 << 26 },
            wfs.clone(),
            clock.clone(),
        ));
        let fence = Fence::new();
        fence.with(|f| f.active_epoch = Some(EPOCH));

        let (data_tx, data_rx) = crossbeam_channel::bounded::<DataEvent>(1024);
        std::thread::spawn(move || for _ in data_rx.iter() {});
        let (ctl_tx, ctl_rx) = crossbeam_channel::bounded::<ControlEvent>(1024);
        let (receipt_tx, receipts) = crossbeam_channel::bounded::<ReceiptAt>(4);
        {
            let wal = Arc::clone(&wal);
            let wfs = Arc::clone(&wfs);
            std::thread::spawn(move || {
                for ev in ctl_rx.iter() {
                    match ev {
                        ControlEvent::DrainDataBarrier { reply, .. } => {
                            let _ = reply.send(());
                        }
                        ControlEvent::WorkerStopped(receipt) => {
                            let wal_seq_at_receipt = wal.current_seq();
                            let bytes = wfs.contents(&wal.wal_path()).unwrap_or_default();
                            let _ = receipt_tx.send(ReceiptAt {
                                receipt,
                                wal_seq_at_receipt,
                                records_at_receipt: parse_records(&bytes),
                            });
                        }
                        _ => {}
                    }
                }
            });
        }

        let clock_dyn: Arc<dyn Clock> = clock.clone();
        let worker = IoWorker::start(
            handle,
            IoWorkerArgs {
                com: com.to_string(),
                epoch: EPOCH,
                wal: Arc::clone(&wal),
                rx: Arc::clone(&rx),
                fence: Arc::clone(&fence),
                sentinel: None,
                sentinel_keepalive: None,
                data: data_tx,
                control: ctl_tx.clone(),
                clock: Arc::clone(&clock_dyn),
                hub: None,
            },
        );
        let ctx = OpCtx {
            com: com.to_string(),
            sid: format!("sid-{com}"),
            profile: super::profile_of(super::SHELL_PROFILE),
            rx: Arc::clone(&rx),
            tx: worker.tx().clone(),
            wal: Arc::clone(&wal),
            clock: clock_dyn,
            control: ctl_tx.clone(),
            rng: Arc::new(|buf: &mut [u8]| buf.fill(0x5a)),
            registry: CommandRegistry::new(),
        };
        let grant = OpGrant { op_id: OP, epoch: EPOCH, owner_gen: 0, fsm_gen: 0, cmd_id: None };
        let cancel = RunCancel::new(&rx);
        PortRig {
            com: com.to_string(),
            clock,
            fence,
            rx,
            wal,
            wfs,
            dut: DutHandle { dev, bounds, processed },
            ctx,
            grant,
            cancel,
            worker: Some(worker),
            ctl_tx,
            receipts,
            stop,
        }
    }

    /// 同步呼叫用的 port（只用於不會等待的方法）。
    pub fn port(&self) -> OpTransferPort<'_> {
        OpTransferPort::new(&self.ctx, &self.grant, &self.cancel)
    }

    /// 在背景 thread 建立 port 並執行 `f`（會等待的呼叫一律走這裡，避免卡死測試）。
    pub fn spawn<T: Send + 'static>(
        &self,
        f: impl FnOnce(&mut OpTransferPort<'_>) -> T + Send + 'static,
    ) -> PortTask<T> {
        let ctx = self.ctx.clone();
        let grant = self.grant.clone();
        let cancel = self.cancel.clone();
        let (ready_tx, ready_rx) = crossbeam_channel::bounded::<()>(1);
        let join = std::thread::spawn(move || {
            let mut port = OpTransferPort::new(&ctx, &grant, &cancel);
            let _ = ready_tx.send(());
            f(&mut port)
        });
        // port 建立後才返回（其取消世代自此起算）。
        let _ = ready_rx.recv_timeout(REAL_WAIT);
        PortTask { join: Some(join) }
    }

    /// 反覆推進 FakeClock（每次 `step`）直到 `done()` 成立（實時上限 [`REAL_WAIT`]）。
    pub fn pump_clock_until(&self, step: Duration, mut done: impl FnMut() -> bool) {
        let deadline = Instant::now() + REAL_WAIT;
        while !done() {
            assert!(Instant::now() < deadline, "推進 FakeClock 仍未達成條件");
            self.clock.advance(step);
            std::thread::sleep(Duration::from_millis(2));
        }
    }

    /// 模擬 drain Phase 1（`SessionCore::begin_stop` 的 fence 凍結＋取消傳輸等待）。
    pub fn phase1(&self) {
        self.fence.with(|f| {
            f.frozen = true;
            f.active_epoch = None;
        });
        self.rx.cancel_transfer_runs();
    }

    /// Phase 2：IoWorker 收尾，回傳 `WorkerStoppedReceipt` 與其抵達當下的 WAL 狀態。
    pub fn finish_worker(&mut self) -> ReceiptAt {
        let worker = self.worker.take().expect("worker 只能收尾一次");
        worker.finish_async(StopId(1), self.ctl_tx.clone());
        self.receipts.recv_timeout(REAL_WAIT).expect("等不到 WorkerStoppedReceipt")
    }

    /// 注入外部輸出（經 reader → WAL → RX 視窗），並等 RX 視窗收到。
    pub fn push_rx(&self, bytes: &[u8]) {
        let before = self.rx.total_bytes();
        self.dut.dev.push_rx(bytes);
        let deadline = Instant::now() + REAL_WAIT;
        while self.rx.total_bytes() < before + bytes.len() as u64 {
            assert!(Instant::now() < deadline, "RX 視窗等不到注入的 bytes");
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    pub fn records(&self) -> Vec<WalRec> {
        parse_records(&self.wfs.contents(&self.wal.wal_path()).unwrap_or_default())
    }

    /// WAL 讀回的 TX 紀錄（本 rig 的 com）。
    pub fn tx_records(&self) -> Vec<WalRec> {
        self.records().into_iter().filter(|r| r.dir == "TX").collect()
    }

    pub fn meta_records(&self) -> Vec<WalRec> {
        self.records().into_iter().filter(|r| r.dir == "META").collect()
    }
}

impl Drop for PortRig {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() {
            worker.finish_async(StopId(u64::MAX), self.ctl_tx.clone());
        }
    }
}
