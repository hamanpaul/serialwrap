//! Slice 2.1 測試支援（Task 11 建立；Tasks 12～16 共用，之後只追加、不改既有簽章）。
//!
//! `CoreHarness` 只透過 `SessionCore::on_event` 驅動，不起任何 thread：
//! - `Effect::Reply` 由 harness 立即送回（等同 actor executor），其餘效果原樣回傳給測試；
//! - `Effect::Schedule` 由 harness 內部排程，`advance` 依 FakeClock 推進並投遞到期 timer 與每 1 s 一次 `ControlEvent::Tick`；
//! - `BridgeOpened` 用 `FakeSerialBackend` 開出真的 `IoWorker`（Task 10／12 實作後才有行為）。

#![allow(dead_code)]

use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crossbeam_channel::{Receiver, Sender, bounded};
use indexmap::IndexMap;
use sw_core::Mono;
use sw_core::actor::{ActorHandle, EffectExecutor, spawn_actor};
use sw_core::arbiter::{CommandRegistry, SubmitReq};
use sw_core::drain::{StopId, WorkerStoppedReceipt};
use sw_core::generations::EpochAllocator;
use sw_core::login_fsm::LoginOutcome;
use sw_core::mailbox::{
    ControlEvent, DataEvent, Mailbox, MailboxItem, OpenFailure, OpenedBridge, TimerEvent,
};
use sw_core::op_runner::{OpCtx, OpOutcome, OpRunner};
use sw_core::session_core::{
    CoreDeps, Effect, PublicCtx, SessionCore, SessionRequest, SessionState,
};
use sw_core::state_store::StateDelta;
use sw_core::tx_writer::{Fence, TxLedger, TxLedgerGauge, TxWriterHandle};
use sw_core::uart_bridge::{IoWorker, IoWorkerArgs, RxShared};
use sw_core::wal::{WalAppendReq, WalConfig, WalHealthFanout, WalWriter};
use sw_pal::fakes::clock::FakeClock;
use sw_pal::fakes::env::FakeEnv;
use sw_pal::fakes::serial::{FakeDevice, FakeSerialBackend};
use sw_pal::fakes::wal_fs::FakeWalFs;
use sw_pal::traits::{
    Clock, DevicePath, FlowControl, OpenError, PalError, Parity, ReadError, SerialBackend,
    SerialConfig,
};
use sw_proto::config_types::{ProfileFs, SessionProfile, load_profiles};
use sw_proto::pycompat::{PyErr, PyOsError};
use sw_proto::pyjson::{PyDict, PyValue, loads};
use sw_proto::pypath::PathFlavor;

/// 測試用 profile 目錄（只存在於記憶體 ProfileFs）。
const PROFILE_DIR: &str = "/fake/profiles";
/// 測試用 by-id 路徑與實體路徑（不是真裝置；FakeSerialBackend 不碰 OS）。
pub const DEVICE_BY_ID: &str = "/s/by-id/usb-A";
pub const REAL_PATH: &str = "/fake/tty-A";
const START_MONO_NS: u64 = 1_000_000_000;
const START_WALL_US: i64 = 1_790_000_000_000_000;

pub const SHELL_PROFILE: &str = "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: t, act_no: 1, alias: t+1, device_by_id: /s/by-id/usb-A}\n";
pub const PASSTHROUGH_PROFILE: &str = "profiles:\n  p:\n    platform: passthrough\n    ready_probe: ''\ntargets:\n  - {com: COM0, profile: p, act_no: 1, alias: p+1, device_by_id: /s/by-id/usb-A}\n";
// SC-35：others-template 等價（prompt_regex '.*' 不可用 → 完成規則 3）
pub const OTHERS_PASSTHROUGH_PROFILE: &str = "profiles:\n  o:\n    platform: passthrough\n    prompt_regex: '.*'\n    login_regex: '$^'\n    password_regex: '$^'\n    ready_probe: ''\ntargets:\n  - {com: COM0, profile: o, act_no: 1, alias: o+1, device_by_id: /s/by-id/usb-A}\n";
// SC-35：uboot 類（ready_probe 非空、prompt 可用 → 規則 2，且仍不 probe）
pub const UBOOT_PASSTHROUGH_PROFILE: &str = "profiles:\n  u:\n    platform: passthrough\n    prompt_regex: '(?mi)^(=>|u-boot>|cfe>) $'\n    login_regex: '$^'\n    password_regex: '$^'\n    ready_probe: 'echo __READY__${nonce}'\ntargets:\n  - {com: COM0, profile: u, act_no: 1, alias: u+1, device_by_id: /s/by-id/usb-A}\n";
// 非 passthrough 的空 ready_probe（維持 PROFILE_NOT_COMMAND_CAPABLE）
pub const SHELL_NO_PROBE_PROFILE: &str = "profiles:\n  n:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    ready_probe: ''\ntargets:\n  - {com: COM0, profile: n, act_no: 1, alias: n+1, device_by_id: /s/by-id/usb-A}\n";
pub const CRED_PROFILE: &str = "profiles:\n  c:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    login_regex: 'login: ?$'\n    password_regex: 'Password: ?$'\n    user_env: SW_T_U\n    pass_env: SW_T_P\n    env_file: /nonexistent/s21.env\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: c, act_no: 1, alias: c+1, device_by_id: /s/by-id/usb-A}\n";

/// 測試用 agent 命令（source "agent"、mode "line"、timeout 10.0、priority 10）。
pub fn agent_submit(cmd: &str) -> SubmitReq {
    SubmitReq {
        session_id: String::new(),
        command: cmd.into(),
        source: "agent".into(),
        mode: "line".into(),
        timeout_s: 10.0,
        priority: 10,
        expected_duration_s: None,
        completion_regex: None,
    }
}

/// 記憶體 ProfileFs：單一 `profiles.yaml`，內容即傳入的 YAML 字串。
struct MemProfileFs {
    yaml: String,
}

impl ProfileFs for MemProfileFs {
    fn flavor(&self) -> PathFlavor {
        PathFlavor::Posix
    }
    fn is_dir(&self, p: &str) -> bool {
        p == PROFILE_DIR
    }
    fn list_dir(&self, _p: &str) -> Result<Vec<String>, PyOsError> {
        Ok(vec!["profiles.yaml".to_string()])
    }
    fn read_bytes(&self, _p: &str) -> Result<Vec<u8>, PyOsError> {
        Ok(self.yaml.clone().into_bytes())
    }
    fn expanduser(&self, p: &str) -> String {
        p.to_string()
    }
}

/// 驅動 `SessionCore` 的測試 harness（無 thread；見模組說明）。
pub struct CoreHarness {
    pub core: SessionCore,
    pub clock: Arc<FakeClock>,
    pub fence: Arc<Fence>,
    pub reg: Arc<CommandRegistry>,
    pub env: Arc<FakeEnv>,
    pub wal: Arc<WalWriter>,
    pub wfs: Arc<FakeWalFs>,
    pub rx: Arc<RxShared>,
    pub epochs: Arc<EpochAllocator>,
    pub state_store: FakeStateStore,
    pub fake_device: FakeDevice,
    serial: Arc<FakeSerialBackend>,
    credential_files: Arc<Mutex<BTreeMap<String, String>>>,
    wall_us: Arc<AtomicI64>,
    scheduled: Vec<(Mono, TimerEvent)>,
    rx_seq: u64,
    wal_seq: u64,
    data_tx: Sender<DataEvent>,
    ctl_tx: Sender<ControlEvent>,
    _keep: (Receiver<DataEvent>, Receiver<ControlEvent>),
    workers: HarnessWorkers,
    finish_tx: Sender<ControlEvent>,
    finish_rx: Receiver<ControlEvent>,
    last_stop: Option<(u64, StopId)>,
    wal_metas: Vec<PyDict>,
    defer_state_commits: bool,
    pending_state_commits: Vec<(u64, StateDelta)>,
}

struct HarnessWorkers {
    items: HashMap<u64, IoWorker>,
    finish_tx: Sender<ControlEvent>,
}

impl HarnessWorkers {
    fn new(finish_tx: Sender<ControlEvent>) -> HarnessWorkers {
        HarnessWorkers {
            items: HashMap::new(),
            finish_tx,
        }
    }

    fn insert(&mut self, epoch: u64, worker: IoWorker) {
        self.items.insert(epoch, worker);
    }

    fn get(&self, epoch: &u64) -> Option<&IoWorker> {
        self.items.get(epoch)
    }

    fn remove(&mut self, epoch: u64) -> Option<IoWorker> {
        self.items.remove(&epoch)
    }
}

impl Drop for HarnessWorkers {
    fn drop(&mut self) {
        for (epoch, worker) in std::mem::take(&mut self.items) {
            worker.finish_async(
                StopId(u64::MAX.saturating_sub(epoch)),
                self.finish_tx.clone(),
            );
        }
    }
}

impl CoreHarness {
    /// 解析單一 target 的 profiles YAML（經 P3 `load_profiles`，不手刻 `SessionProfile`）後建立 `SessionCore`。
    pub fn new(profile_yaml: &str) -> CoreHarness {
        let fs = MemProfileFs {
            yaml: profile_yaml.to_string(),
        };
        let loaded = load_profiles(PROFILE_DIR, &fs).expect("測試 profile YAML 應可載入");
        assert_eq!(loaded.profiles.len(), 1, "targets 必須恰好一個");
        let profile = loaded.profiles.into_iter().next().expect("一個 target");

        let clock = FakeClock::at(START_MONO_NS, START_WALL_US);
        let fence = Fence::new();
        let reg = CommandRegistry::new();
        let wfs = FakeWalFs::new();
        let wal = Arc::new(WalWriter::open(
            WalConfig {
                dir: PathBuf::from("/fake/wal"),
                rotate_bytes: 67_108_864,
            },
            wfs.clone(),
            clock.clone(),
        ));
        let fanout = WalHealthFanout::new();
        fanout.register(fence.clone());
        wal.set_health_sink(fanout);

        let rx = RxShared::new(131_072, clock.mono_ns());
        let epochs = EpochAllocator::new();
        let serial = FakeSerialBackend::new();
        let fake_device = serial.add_device(REAL_PATH);
        let state_store = FakeStateStore::new();
        let env = Arc::new(FakeEnv::default());
        let credential_files = Arc::new(Mutex::new(BTreeMap::<String, String>::new()));
        let files_for_read = Arc::clone(&credential_files);
        let wall_us = Arc::new(AtomicI64::new(START_WALL_US));
        let (data_tx, data_rx) = bounded::<DataEvent>(1024);
        let (ctl_tx, ctl_rx) = bounded::<ControlEvent>(256);
        let (finish_tx, finish_rx) = bounded::<ControlEvent>(16);

        let wall_for_iso = Arc::clone(&wall_us);
        let deps = CoreDeps {
            fence: fence.clone(),
            registry: reg.clone(),
            wal: wal.clone(),
            rx: rx.clone(),
            epochs: epochs.clone(),
            wall_iso: Arc::new(move || format!("wall-us-{}", wall_for_iso.load(Ordering::SeqCst))),
            rng: Arc::new(|buf: &mut [u8]| {
                for (i, b) in buf.iter_mut().enumerate() {
                    *b = i as u8;
                }
            }),
            env: env.clone(),
            read_env_file: Arc::new(move |path: &str| -> Option<std::io::Result<String>> {
                files_for_read
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .get(path)
                    .cloned()
                    .map(Ok)
            }),
        };
        let core = SessionCore::new(profile, "yaml-target", deps);

        CoreHarness {
            core,
            clock,
            fence,
            reg,
            env,
            wal,
            wfs,
            rx,
            epochs,
            state_store,
            fake_device,
            serial,
            credential_files,
            wall_us,
            scheduled: Vec::new(),
            rx_seq: 0,
            wal_seq: 0,
            data_tx,
            ctl_tx,
            _keep: (data_rx, ctl_rx),
            workers: HarnessWorkers::new(finish_tx.clone()),
            finish_tx,
            finish_rx,
            last_stop: None,
            wal_metas: Vec::new(),
            defer_state_commits: false,
            pending_state_commits: Vec::new(),
        }
    }

    /// 暫扣 state.json commit，讓測試可插入並行 waiter 再完成或失敗 commit。
    pub fn defer_state_commits(&mut self) {
        self.defer_state_commits = true;
    }

    /// 尚未完成的 commit 識別碼。
    pub fn pending_state_commit_ids(&self) -> Vec<u64> {
        self.pending_state_commits
            .iter()
            .map(|(commit_id, _)| *commit_id)
            .collect()
    }

    /// 執行一筆暫扣 commit，並將結果交回 SessionCore。
    pub fn complete_state_commit(&mut self, commit_id: u64) -> Vec<Effect> {
        let position = self
            .pending_state_commits
            .iter()
            .position(|(id, _)| *id == commit_id)
            .expect("暫扣 state commit 應存在");
        let (_, delta) = self.pending_state_commits.remove(position);
        let result = self.state_store.apply(delta);
        self.deliver(MailboxItem::Control(ControlEvent::StateCommitted {
            commit_id,
            result,
        }))
    }

    /// 設定測試用 process env。
    pub fn set_env(&self, key: &str, value: &str) {
        self.env
            .vars
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(key.to_string(), value.to_string());
    }

    /// 設定測試用 env 檔內容；路徑需與 profile 的 `env_file` 相同。
    pub fn set_env_file(&self, path: &str, contents: &str) {
        self.credential_files
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(path.to_string(), contents.to_string());
    }

    /// 目前 bridge 的 conn_epoch（尚未開出 bridge 時 panic）。
    pub fn epoch(&self) -> u64 {
        self.core
            .generations()
            .conn_epoch
            .expect("目前沒有 bridge epoch")
    }

    /// 同時推進 wall（SystemTime 跳動）與 FakeClock 的 wall；mono 不動（Review Focus 4）。
    pub fn jump_wall_us(&mut self, delta_us: i64) {
        let next = self.wall_us.load(Ordering::SeqCst) + delta_us;
        self.wall_us.store(next, Ordering::SeqCst);
        self.clock.set_wall_us(next);
    }

    /// 把 wall 設為絕對值（例如 `set_wall_us(0)` 模擬倒退到 1970）；mono 不動。
    pub fn set_wall_us(&mut self, wall_us: i64) {
        self.wall_us.store(wall_us, Ordering::SeqCst);
        self.clock.set_wall_us(wall_us);
    }

    /// 目前 wall（微秒）；供期望值計算。
    pub fn wall_us(&self) -> i64 {
        self.wall_us.load(Ordering::SeqCst)
    }

    /// 注入 DeviceAttached → BridgeOpened(ok) → LoginFinished(Ready)，達到 READY（不依賴 Task 13 login）。
    pub fn to_ready(&mut self) -> &mut Self {
        self.attach_device();
        self.bridge_ok();
        let (epoch, fsm_gen) = (self.epoch(), self.core.generations().fsm_gen);
        self.feed(ControlEvent::LoginFinished {
            epoch,
            fsm_gen,
            result: LoginOutcome::Ready,
            after_rx_seq: None,
        });
        assert_eq!(
            self.core.state(),
            SessionState::Ready,
            "to_ready 應達 READY"
        );
        self
    }

    /// 達到 ATTACHED：shell 類以 `LoginFinished(NotReady(last_error))` 收尾；passthrough 由 bridge 採用直接 ATTACHED。
    pub fn to_attached(&mut self, last_error: Option<&str>) -> &mut Self {
        self.attach_device();
        self.bridge_ok();
        if self.core.state() == SessionState::Attaching {
            let (epoch, fsm_gen) = (self.epoch(), self.core.generations().fsm_gen);
            let reason = last_error.unwrap_or("PROMPT_UNAVAILABLE").to_string();
            self.feed(ControlEvent::LoginFinished {
                epoch,
                fsm_gen,
                result: LoginOutcome::NotReady(reason),
                after_rx_seq: None,
            });
        }
        assert_eq!(
            self.core.state(),
            SessionState::Attached,
            "to_attached 應達 ATTACHED"
        );
        self
    }

    fn attach_device(&mut self) {
        self.feed(ControlEvent::DeviceAttached {
            by_id: DEVICE_BY_ID.into(),
            real_path: REAL_PATH.into(),
        });
    }

    /// 模擬 executor 成功開啟 bridge：`epochs.next()` 配號後送 `BridgeOpened(ok)`。
    pub fn bridge_ok(&mut self) -> Vec<Effect> {
        let epoch = self.epochs.next();
        let worker = self.start_worker(epoch);
        let opened = OpenedBridge {
            epoch,
            worker,
            real_path: REAL_PATH.into(),
        };
        self.feed(ControlEvent::BridgeOpened {
            epoch_hint: 0,
            result: Ok(opened),
        })
    }

    /// 由 admission 出列並取得正式 `StartOp` grant，供 drain BC-03 案例使用。
    pub fn start_foreground_command(&mut self, cmd: &str) -> StartedCmd {
        let (response, effects) = self.rpc_full(SessionRequest::Submit(agent_submit(cmd)));
        let response = response.expect("命令 admission 應成功");
        let cmd_id = response
            .get("cmd_id")
            .and_then(PyValue::as_str)
            .expect("admission 回應含 cmd_id")
            .to_string();
        let op_id = effects
            .iter()
            .find_map(|effect| match effect {
                Effect::StartOp { grant, .. } => Some(grant.op_id),
                _ => None,
            })
            .expect("前景命令應產生 StartOp");
        StartedCmd { op_id, cmd_id }
    }

    /// 最近一次 Phase 1 的 `(epoch, stop_id)`。
    pub fn pending_stop(&self) -> (u64, StopId) {
        self.last_stop.expect("尚未產生 BeginDrain")
    }

    /// 建立可鏈式修改的 WorkerStopped receipt。
    pub fn receipt(&mut self, epoch: u64, stop_id: StopId) -> ReceiptBuilder<'_> {
        ReceiptBuilder {
            harness: self,
            receipt: WorkerStoppedReceipt {
                epoch,
                stop_id,
                ledger: TxLedger {
                    epoch,
                    ..TxLedger::default()
                },
                unrecorded: Vec::new(),
                wal_error: None,
                handle_closed: true,
                close_error: None,
            },
        }
    }

    /// harness 已執行並寫入 WAL 的 META 效果。
    pub fn wal_meta_events(&self) -> Vec<PyDict> {
        self.wal_metas.clone()
    }

    /// 丟棄無需斷言的 worker 控制回報，供大量 TX 測試避免填滿測試 mailbox。
    pub fn drain_control_events(&self) {
        while self._keep.1.try_recv().is_ok() {}
    }

    fn start_worker(&self, epoch: u64) -> IoWorker {
        self.start_worker_at(epoch, REAL_PATH)
    }

    fn start_worker_at(&self, epoch: u64, real_path: &str) -> IoWorker {
        let cfg = SerialConfig {
            baud: 115_200,
            data_bits: 8,
            parity: Parity::None,
            stop_bits: 1,
            flow: FlowControl::None,
            xonxoff: false,
        };
        let handle = self
            .serial
            .open_exclusive(&DevicePath(real_path.into()), &cfg)
            .expect("fake serial open");
        IoWorker::start(
            handle,
            IoWorkerArgs {
                com: "COM0".into(),
                epoch,
                wal: self.wal.clone(),
                rx: self.rx.clone(),
                fence: self.fence.clone(),
                sentinel: None,
                data: self.data_tx.clone(),
                control: self.ctl_tx.clone(),
                clock: self.clock.clone(),
            },
        )
    }

    /// 建立指定 fake 路徑的 opened bridge，用於延遲／過期開啟回歸案例。
    pub fn opened_bridge(&mut self, real_path: &str) -> OpenedBridge {
        let _ = self.serial.add_device(real_path);
        let epoch = self.epochs.next();
        let worker = self.start_worker_at(epoch, real_path);
        OpenedBridge {
            epoch,
            worker,
            real_path: real_path.to_string(),
        }
    }

    /// 送出 RPC 並回傳 (回應, 同次事件產生的其他效果)；未同步回覆時 panic。
    pub fn rpc_full(&mut self, req: SessionRequest) -> (Result<PyDict, PyErr>, Vec<Effect>) {
        let (tx, rx) = bounded::<Result<PyDict, PyErr>>(1);
        let effects = self.deliver(MailboxItem::Control(ControlEvent::Request {
            req,
            reply: tx,
        }));
        let reply = rx
            .try_recv()
            .expect("RPC 應於同步路徑回覆（否則請改用 rpc_deferred）");
        (reply, effects)
    }

    pub fn rpc(&mut self, req: SessionRequest) -> Result<PyDict, PyErr> {
        self.rpc_full(req).0
    }

    /// 送出 RPC 但不期待同步回覆；回傳 (接收端, 同次事件的其他效果)，由測試於之後的事件後 `try_recv`。
    pub fn rpc_deferred(
        &mut self,
        req: SessionRequest,
    ) -> (Receiver<Result<PyDict, PyErr>>, Vec<Effect>) {
        let (tx, rx) = bounded::<Result<PyDict, PyErr>>(1);
        let effects = self.deliver(MailboxItem::Control(ControlEvent::Request {
            req,
            reply: tx,
        }));
        (rx, effects)
    }

    pub fn feed(&mut self, ev: ControlEvent) -> Vec<Effect> {
        self.deliver(MailboxItem::Control(ev))
    }

    pub fn data(&mut self, ev: DataEvent) -> Vec<Effect> {
        self.deliver(MailboxItem::Data(ev))
    }

    /// 目前 epoch 的一個 RX chunk（rx_seq／wal_seq 自動遞增）。
    pub fn rx(&mut self, bytes: &[u8]) -> Vec<Effect> {
        let epoch = self.epoch();
        self.rx_seq += 1;
        self.wal_seq += 1;
        let ev = DataEvent::UartRxChunk {
            epoch,
            rx_seq: self.rx_seq,
            wal_seq: self.wal_seq,
            bytes: bytes.to_vec(),
        };
        self.data(ev)
    }

    /// 推進 FakeClock（以不超過 1 s 的步長），每步投遞到期 timer 與一次 `Tick`。
    pub fn advance(&mut self, d: Duration) -> Vec<Effect> {
        let mut out = Vec::new();
        let mut left = d;
        while left > Duration::ZERO {
            let step = left.min(Duration::from_secs(1));
            left -= step;
            self.clock.advance(step);
            let now = self.clock.mono_ns();
            let mut due = Vec::new();
            let mut i = 0;
            while i < self.scheduled.len() {
                if self.scheduled[i].0 <= now {
                    due.push(self.scheduled.remove(i).1);
                } else {
                    i += 1;
                }
            }
            for ev in due {
                out.extend(self.deliver(MailboxItem::Control(ControlEvent::Timer(ev))));
            }
            out.extend(self.deliver(MailboxItem::Control(ControlEvent::Tick)));
        }
        out
    }

    pub fn public(&self) -> PyDict {
        let ctx = PublicCtx {
            console_count: 0,
            vtty: None,
            console_endpoint: None,
        };
        self.core.public(self.clock.mono_ns(), &ctx)
    }

    /// 只供碼映射測試（例如 FLASHING）；2.1 不會自然進入該狀態。
    pub fn force_state(&mut self, s: SessionState) {
        self.core.force_state(s);
    }

    /// 送入事件並處理效果：Reply 立即送回、Schedule 內部排程，其餘回傳。
    fn deliver(&mut self, item: MailboxItem) -> Vec<Effect> {
        let mut rest = Vec::new();
        if let MailboxItem::Control(ControlEvent::WorkerStopped(receipt)) = &item {
            if let Some(worker) = self.workers.remove(receipt.epoch) {
                worker.finish_async(receipt.stop_id, self.finish_tx.clone());
                let deadline = Instant::now() + Duration::from_millis(250);
                while Instant::now() < deadline {
                    match self.finish_rx.recv_timeout(Duration::from_millis(10)) {
                        Ok(ControlEvent::WorkerStopped(actual))
                            if actual.epoch == receipt.epoch
                                && actual.stop_id == receipt.stop_id =>
                        {
                            break;
                        }
                        Ok(_) => {}
                        Err(_) => {}
                    }
                }
            }
        }
        let mut pending = vec![item];
        while let Some(item) = pending.pop() {
            let effects = self.core.on_event(item, self.clock.mono_ns()).0;
            for effect in effects {
                match effect {
                    Effect::Reply(tx, result) => {
                        let _ = tx.try_send(result);
                    }
                    Effect::Schedule { at, ev } => {
                        if let TimerEvent::DrainTimeout { epoch, stop_id } = &ev {
                            self.last_stop = Some((*epoch, *stop_id));
                        }
                        self.scheduled.push((at, ev));
                    }
                    Effect::AdoptBridge { epoch, worker, .. } => {
                        self.workers.insert(epoch, worker);
                    }
                    Effect::BeginDrain { stop_id, epoch } => {
                        self.last_stop = Some((epoch, stop_id));
                        if let Some(worker) = self.workers.get(&epoch) {
                            worker.begin_stop();
                        }
                        rest.push(Effect::BeginDrain { stop_id, epoch });
                    }
                    Effect::Persist { commit_id, delta } => {
                        if self.defer_state_commits {
                            self.pending_state_commits.push((commit_id, delta));
                        } else {
                            let result = self.state_store.apply(delta);
                            pending.push(MailboxItem::Control(ControlEvent::StateCommitted {
                                commit_id,
                                result,
                            }));
                        }
                    }
                    Effect::WalMeta { cmd_id, meta } => {
                        let _ = self.wal.append(WalAppendReq {
                            com: "COM0",
                            dir: "META",
                            source: "system",
                            payload: &[],
                            cmd_id: cmd_id.as_deref(),
                            loss_flag: false,
                            meta: Some(meta.clone()),
                        });
                        self.wal_metas.push(meta.clone());
                        rest.push(Effect::WalMeta { cmd_id, meta });
                    }
                    Effect::SettleTx { epoch, op_id } => {
                        if let Some(worker) = self.workers.get(&epoch) {
                            let _ = worker.tx().settle(op_id);
                        }
                        rest.push(Effect::SettleTx { epoch, op_id });
                    }
                    other => rest.push(other),
                }
            }
        }
        rest
    }
}

pub struct StartedCmd {
    pub op_id: sw_core::tx_writer::OpId,
    pub cmd_id: String,
}

pub struct ReceiptBuilder<'a> {
    harness: &'a mut CoreHarness,
    receipt: WorkerStoppedReceipt,
}

impl ReceiptBuilder<'_> {
    pub fn confirmed(mut self, op_id: sw_core::tx_writer::OpId, bytes: usize) -> Self {
        let entry = self.receipt.ledger.ops.entry(op_id).or_default();
        entry.confirmed_bytes = bytes;
        entry.unknown = false;
        self
    }

    pub fn unknown(mut self, op_id: sw_core::tx_writer::OpId) -> Self {
        let entry = self.receipt.ledger.ops.entry(op_id).or_default();
        entry.unknown = true;
        self
    }

    pub fn handle_closed(mut self, closed: bool) -> Self {
        self.receipt.handle_closed = closed;
        self
    }

    pub fn gap(
        mut self,
        op_id: Option<sw_core::tx_writer::OpId>,
        dir: &'static str,
        len: usize,
    ) -> Self {
        self.receipt
            .unrecorded
            .push(sw_core::tx_writer::EvidenceGap {
                op_id,
                dir,
                len,
                error: PalError::io(28, "injected WAL write failure"),
            });
        self
    }

    pub fn feed(self) -> Vec<Effect> {
        let Self { harness, receipt } = self;
        harness.feed(ControlEvent::WorkerStopped(receipt))
    }
}

/// `ActorHandle` 測試用 executor：把 `Effect::Reply` 送回請求者，其餘效果忽略。
pub struct ReplyExec;

impl sw_core::actor::EffectExecutor for ReplyExec {
    fn execute(&mut self, e: Effect, _tx: &Sender<ControlEvent>, _safety: &Sender<ControlEvent>) {
        if let Effect::Reply(reply, result) = e {
            let _ = reply.try_send(result);
        }
    }
}

/// `ActorHandle` 測試用 executor：完全不回覆（用於逾時測試）。
pub struct SilentExec;

impl sw_core::actor::EffectExecutor for SilentExec {
    fn execute(&mut self, _e: Effect, _tx: &Sender<ControlEvent>, _safety: &Sender<ControlEvent>) {}
}

/// 以指定 reason 組成 `WorkerStopped` 收據（BC-03 帳本為空、handle 已關閉）。
pub fn receipt(epoch: u64, stop: u64) -> WorkerStoppedReceipt {
    WorkerStoppedReceipt {
        epoch,
        stop_id: StopId(stop),
        ledger: TxLedger {
            epoch,
            ..TxLedger::default()
        },
        unrecorded: Vec::new(),
        wal_error: None,
        handle_closed: true,
        close_error: None,
    }
}

/// 測試用 `PalError`（只供事件參數；不代表真實 OS 錯誤）。
pub fn io_error(msg: &str) -> PalError {
    PalError::io(5, msg)
}

/// 取回 RPC 錯誤碼（`error_code` 欄位；無則空字串）。
pub fn error_code(r: &Result<PyDict, PyErr>) -> String {
    r.as_ref()
        .expect("預期 ok:false 的回應，不是 PyErr")
        .get("error_code")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string()
}

// ---------------------------------------------------------------------------
// Task 13（P5-T13）追加：`profile_of`、`FakeStateStore`（Task 12 Step 0 的最小替身）、`ActorRig`。
// 只追加；既有 `CoreHarness` 簽章不變。
// ---------------------------------------------------------------------------

/// 解析單一 target 的 profiles YAML 為 `SessionProfile`（與 `CoreHarness::new` 相同的 P3 載入路徑）。
pub fn profile_of(profile_yaml: &str) -> SessionProfile {
    let fs = MemProfileFs {
        yaml: profile_yaml.to_string(),
    };
    let loaded = load_profiles(PROFILE_DIR, &fs).expect("測試 profile YAML 應可載入");
    loaded
        .profiles
        .into_iter()
        .next()
        .expect("targets 必須恰好一個")
}

/// 標準 base64 解碼（WAL `payload_b64` 讀回用；遇非字母表字元略過）。
fn b64_decode(s: &str) -> Vec<u8> {
    const T: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = Vec::new();
    let mut buf: u32 = 0;
    let mut bits: u32 = 0;
    for c in s.bytes() {
        if c == b'=' {
            break;
        }
        let Some(v) = T.iter().position(|&x| x == c) else {
            continue;
        };
        buf = (buf << 6) | v as u32;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push(((buf >> bits) & 0xFF) as u8);
        }
    }
    out
}

#[derive(Default)]
struct StoreInner {
    released: IndexMap<String, PyDict>,
    commits: usize,
    fail_next: Option<PalError>,
}

/// 記憶體版 `state.json` commit 替身（Task 12 Step 0）：只記錄 commit 次數與 released 列。
#[derive(Clone, Default)]
pub struct FakeStateStore {
    inner: Arc<Mutex<StoreInner>>,
}

impl FakeStateStore {
    pub fn new() -> FakeStateStore {
        FakeStateStore::default()
    }

    /// 目前 `released` 列（sid → row dict）。
    pub fn last_released(&self) -> IndexMap<String, PyDict> {
        self.inner
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .released
            .clone()
    }

    /// 已成功套用的 commit 次數。
    pub fn commits(&self) -> usize {
        self.inner.lock().unwrap_or_else(|e| e.into_inner()).commits
    }

    /// 下一次 commit 失敗（一次性）。
    pub fn fail_next_commit(&self, err: PalError) {
        self.inner
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .fail_next = Some(err);
    }

    /// `Effect::Persist` 的替身：套用增量；若已設定失敗則回傳該錯誤。
    pub fn apply(&self, delta: StateDelta) -> Result<(), PalError> {
        let mut g = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(e) = g.fail_next.take() {
            return Err(e);
        }
        g.commits += 1;
        match delta {
            StateDelta::Released { sid, row } => {
                let mut d = PyDict::default();
                d.insert("by_id", row.by_id.clone());
                d.insert("released_by", row.released_by.clone());
                d.insert("released_at", row.released_at.clone());
                d.insert("reason", row.reason.clone());
                g.released.insert(sid, d);
            }
            StateDelta::Unreleased { sid } => {
                g.released.shift_remove(&sid);
            }
            _ => {}
        }
        Ok(())
    }
}

/// `ActorRig` 的效果執行器：把 `SessionCore` 的效果落到替身上（OS 寫入經 IoWorker／FakeSerial）。
struct RigExec {
    serial: Arc<FakeSerialBackend>,
    wal: Arc<WalWriter>,
    rx: Arc<RxShared>,
    fence: Arc<Fence>,
    epochs: Arc<EpochAllocator>,
    clock: Arc<FakeClock>,
    data: Sender<DataEvent>,
    stderr: Arc<Mutex<Vec<String>>>,
    state: FakeStateStore,
    workers: HashMap<u64, IoWorker>,
    /// Task 13：login FSM 執行所需（profile、帳密 fallback env）。
    profile: SessionProfile,
    env: Arc<FakeEnv>,
    /// Task 14：每個 epoch 一個 OpRunner（ForegroundSlot）。
    runners: HashMap<u64, OpRunner>,
    /// Task 14：現行 worker 的 TX 入口（`tx_ledger_gauge()` 用）。
    current_tx: Arc<Mutex<Option<TxWriterHandle>>>,
}

/// 測試用 nonce（8 位 hex；每次呼叫遞增，對應 Python `uuid4().hex[:8]`）。
fn rig_nonce() -> String {
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0x5eed_0000);
    format!("{:08x}", NEXT.fetch_add(1, Ordering::Relaxed) & 0xffff_ffff)
}

impl EffectExecutor for RigExec {
    fn execute(&mut self, e: Effect, tx: &Sender<ControlEvent>, _safety: &Sender<ControlEvent>) {
        match e {
            Effect::Reply(reply, result) => {
                let _ = reply.try_send(result);
            }
            Effect::OpenBridge {
                dev,
                cfg,
                precheck_holders: _,
            } => {
                let result = match self.serial.open_exclusive(&dev, &cfg) {
                    Ok(handle) => {
                        let epoch = self.epochs.next();
                        let worker = IoWorker::start(
                            handle,
                            IoWorkerArgs {
                                com: "COM0".into(),
                                epoch,
                                wal: self.wal.clone(),
                                rx: self.rx.clone(),
                                fence: self.fence.clone(),
                                sentinel: None,
                                data: self.data.clone(),
                                control: tx.clone(),
                                clock: self.clock.clone(),
                            },
                        );
                        Ok(OpenedBridge {
                            epoch,
                            worker,
                            real_path: dev.0.clone(),
                        })
                    }
                    Err(OpenError::Held(holder)) => Err(OpenFailure::Held(holder)),
                    Err(OpenError::AccessDenied { holder, .. }) => {
                        Err(OpenFailure::AccessDenied(holder))
                    }
                    Err(OpenError::NotFound(_)) => Err(OpenFailure::NotFound),
                    Err(OpenError::Other(error)) => Err(OpenFailure::Other(error)),
                };
                let _ = tx.send(ControlEvent::BridgeOpened {
                    epoch_hint: 0,
                    result,
                });
            }
            Effect::AdoptBridge { epoch, worker, .. } => {
                *self.current_tx.lock().unwrap_or_else(|e| e.into_inner()) =
                    Some(worker.tx().clone());
                self.workers.insert(epoch, worker);
            }
            Effect::WalMeta { cmd_id, meta } => {
                let _ = self.wal.append(WalAppendReq {
                    com: "COM0",
                    dir: "META",
                    source: "system",
                    payload: &[],
                    cmd_id: cmd_id.as_deref(),
                    loss_flag: false,
                    meta: Some(meta),
                });
            }
            Effect::Persist { commit_id, delta } => {
                let result = self.state.apply(delta);
                let _ = tx.send(ControlEvent::StateCommitted { commit_id, result });
            }
            Effect::Warn(msg) => {
                self.stderr
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .push(msg);
            }
            Effect::Notify(_) => {}
            Effect::Schedule { at, ev } => {
                // 以 FakeClock 輪詢到期後投遞（測試替身；期限一律依 monotonic）。
                let clock = self.clock.clone();
                let tx = tx.clone();
                std::thread::spawn(move || {
                    while clock.mono_ns() < at {
                        std::thread::sleep(Duration::from_millis(1));
                    }
                    let _ = tx.send(ControlEvent::Timer(ev));
                });
            }
            Effect::BeginDrain { stop_id, epoch } => {
                if let Some(worker) = self.workers.remove(&epoch) {
                    worker.finish_async(stop_id, tx.clone());
                }
            }
            Effect::StartLogin {
                epoch,
                fsm_gen,
                plan,
            } => {
                // Task 13 Step 0：login FSM 於獨立 thread 執行（替身 OpRunner）；TX 一律經該 epoch 的 TxWriter。
                if matches!(
                    plan,
                    sw_core::login_fsm::LoginPlan::None
                        | sw_core::login_fsm::LoginPlan::CredentialsUnresolved { .. }
                ) {
                    return;
                }
                let Some(worker) = self.workers.get(&epoch) else {
                    return;
                };
                let handle = worker.tx().clone();
                let rx = self.rx.clone();
                let clock = self.clock.clone();
                let profile = self.profile.clone();
                let env = self.env.clone();
                let control = tx.clone();
                std::thread::spawn(move || {
                    let send = sw_core::login_fsm::login_tx_sender(handle, epoch, 0, fsm_gen);
                    let nonce = rig_nonce;
                    let io = sw_core::login_fsm::LoginIo {
                        rx: &rx,
                        tx: &send,
                        clock: &*clock,
                        nonce: &nonce,
                    };
                    let Some(result) =
                        sw_core::login_fsm::run_login_plan(&io, &profile, &plan, &*env)
                    else {
                        return;
                    };
                    let (wm_epoch, wm_seq) = rx.watermark();
                    let _ = control.send(ControlEvent::LoginFinished {
                        epoch,
                        fsm_gen,
                        result,
                        after_rx_seq: (wm_epoch == epoch).then_some(wm_seq),
                    });
                });
            }
            Effect::StartOp { grant, spec } => {
                // Task 14：op 於該 epoch 的 OpRunner thread 執行（TX 一律經該 epoch 的 TxWriter）。
                let epoch = grant.epoch;
                if !self.runners.contains_key(&epoch) {
                    let Some(worker) = self.workers.get(&epoch) else {
                        let _ = tx.send(ControlEvent::OpFinished {
                            op_id: grant.op_id,
                            epoch,
                            outcome: OpOutcome::SendFailed,
                            after_rx_seq: None,
                        });
                        return;
                    };
                    let runner = OpRunner::spawn(OpCtx {
                        com: "COM0".into(),
                        sid: String::new(),
                        profile: self.profile.clone(),
                        rx: self.rx.clone(),
                        tx: worker.tx().clone(),
                        wal: self.wal.clone(),
                        clock: self.clock.clone(),
                        control: tx.clone(),
                        rng: Arc::new(|buf: &mut [u8]| {
                            for (i, b) in buf.iter_mut().enumerate() {
                                *b = i as u8;
                            }
                        }),
                    });
                    self.runners.insert(epoch, runner);
                }
                if let Some(runner) = self.runners.get(&epoch) {
                    runner.run(grant, spec);
                }
            }
            Effect::SettleTx { epoch, op_id } => {
                if let Some(worker) = self.workers.get(&epoch) {
                    let _ = worker.tx().settle(op_id);
                }
            }
        }
    }
}

/// 真 actor thread 的測試驅動（Task 13 Step 0）：`SessionActor`＋`IoWorker`＋FakeSerial＋FakeWalFs＋FakeClock＋FakeStateStore。
pub struct ActorRig {
    pub dev: FakeDevice,
    pub clock: Arc<FakeClock>,
    pub state_store: FakeStateStore,
    pub env: Arc<FakeEnv>,
    /// Task 14：TX fence（`quiet_active` 等斷言用）。
    pub fence: Arc<Fence>,
    /// Task 14：RX 視窗（`screen` 等斷言用）。
    pub rx: Arc<RxShared>,
    current_tx: Arc<Mutex<Option<TxWriterHandle>>>,
    wal: Arc<WalWriter>,
    wfs: Arc<FakeWalFs>,
    reg: Arc<CommandRegistry>,
    stderr: Arc<Mutex<Vec<String>>>,
    files: Arc<Mutex<BTreeMap<String, String>>>,
    actor: ActorHandle,
}

const RIG_WAIT: Duration = Duration::from_secs(5);
/// 「之後所有 WAL 寫入失敗」的失敗次數（FakeWalFs 逐筆預排；`usize::MAX` 會預配置到 OOM）。
const WAL_FAIL_FOREVER: usize = 4096;

impl ActorRig {
    /// 以 profiles YAML 建立 actor（尚未 attach；bridge 未開）。
    pub fn new(profile_yaml: &str) -> ActorRig {
        ActorRig::new_with(profile_yaml, |_| {})
    }

    /// 同 `new`，但在 actor 啟動前可調整 `SessionCore`（Task 14：capture env、self-test 觀測注入）。
    pub fn new_with(profile_yaml: &str, setup: impl FnOnce(&mut SessionCore)) -> ActorRig {
        let profile = profile_of(profile_yaml);
        let clock = FakeClock::new();
        let fence = Fence::new();
        let reg = CommandRegistry::new();
        let wfs = FakeWalFs::new();
        let wal = Arc::new(WalWriter::open(
            WalConfig {
                dir: PathBuf::from("/fake/wal"),
                rotate_bytes: 67_108_864,
            },
            wfs.clone(),
            clock.clone(),
        ));
        let fanout = WalHealthFanout::new();
        fanout.register(fence.clone());
        wal.set_health_sink(fanout);
        let rx = RxShared::new(131_072, clock.mono_ns());
        let epochs = EpochAllocator::new();
        let serial = FakeSerialBackend::new();
        let dev = serial.add_device(REAL_PATH);
        let env = Arc::new(FakeEnv::default());
        let files: Arc<Mutex<BTreeMap<String, String>>> = Arc::new(Mutex::new(BTreeMap::new()));
        let files_for_read = Arc::clone(&files);
        let deps = CoreDeps {
            fence: fence.clone(),
            registry: reg.clone(),
            wal: wal.clone(),
            rx: rx.clone(),
            epochs: epochs.clone(),
            wall_iso: Arc::new(|| "wall-rig".to_string()),
            rng: Arc::new(|buf: &mut [u8]| {
                for (i, b) in buf.iter_mut().enumerate() {
                    *b = i as u8;
                }
            }),
            env: env.clone(),
            read_env_file: Arc::new(move |p: &str| -> Option<std::io::Result<String>> {
                files_for_read
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .get(p)
                    .map(|t| Ok(t.clone()))
            }),
        };
        let exec_profile = profile.clone();
        let mut core = SessionCore::new(profile, "yaml-target", deps);
        setup(&mut core);
        let current_tx: Arc<Mutex<Option<TxWriterHandle>>> = Arc::new(Mutex::new(None));
        let mailbox = Mailbox::new(256, 1024);
        let data = mailbox.data_sender();
        let stderr = Arc::new(Mutex::new(Vec::new()));
        let state_store = FakeStateStore::new();
        let rig_fence = fence.clone();
        let rig_rx = rx.clone();
        let exec = RigExec {
            serial,
            wal: wal.clone(),
            rx,
            fence,
            epochs,
            clock: clock.clone(),
            data,
            stderr: stderr.clone(),
            state: state_store.clone(),
            workers: HashMap::new(),
            profile: exec_profile,
            env: env.clone(),
            runners: HashMap::new(),
            current_tx: current_tx.clone(),
        };
        let actor = spawn_actor(core, mailbox, Box::new(exec), clock.clone());
        ActorRig {
            dev,
            clock,
            state_store,
            env,
            fence: rig_fence,
            rx: rig_rx,
            current_tx,
            wal,
            wfs,
            reg,
            stderr,
            files,
            actor,
        }
    }

    /// 設定 process env（`resolve_session_auth` 讀取；attach／recover 時重新解析）。
    pub fn set_env(&self, key: &str, value: &str) {
        self.env
            .vars
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(key.to_string(), value.to_string());
    }

    /// 設定 env 檔內容（路徑需與 profile 的 `env_file` 相同）。
    pub fn set_env_file(&self, path: &str, text: &str) {
        self.files
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(path.to_string(), text.to_string());
    }

    /// 推進 FakeClock（每 1 s 步長投遞一次 `ControlEvent::Tick`），最後以同步 RPC 收斂。
    pub fn advance(&self, d: Duration) {
        let mut left = d;
        while left > Duration::ZERO {
            let step = left.min(Duration::from_secs(1));
            left -= step;
            self.clock.advance(step);
            let _ = self.actor.control.send(ControlEvent::Tick);
        }
        let _ = self.get_state();
    }

    /// 注入 RX 並等待讀取執行緒消化（reader 非同步；時間戳以注入當下的 FakeClock 為準）。
    pub fn inject_rx(&self, bytes: &[u8]) {
        self.dev.push_rx(bytes);
        std::thread::sleep(Duration::from_millis(30));
    }

    /// 讓之後所有 WAL 寫入失敗（磁碟滿替身；Review Focus 3）。
    pub fn fail_wal_writes(&self, err: PalError) {
        self.wfs
            .fail_next_writes(&self.wal.wal_path(), WAL_FAIL_FOREVER, err);
    }

    /// WAL 累計寫入失敗次數（`WalWriter::health().write_failures`；Review Focus 3 同步點）。
    pub fn wal_write_failures(&self) -> u64 {
        self.wal.health().write_failures
    }

    /// Task 12b：WAL 健康快照（`healthy` 等斷言用）。
    pub fn wal_health(&self) -> sw_core::wal::WalHealth {
        self.wal.health()
    }

    /// Task 12b：actor 事件延遲探針（test-fakes）。
    pub fn actor_probe(&self) -> Arc<sw_core::actor::ActorLatencyProbe> {
        self.actor.probe.clone()
    }

    /// Task 12b：actor data lane 的送出端（滿載測試直接注入 RX chunk）。
    pub fn data_sender(&self) -> Sender<DataEvent> {
        self.actor.data.clone()
    }

    /// Task 14：直接投遞 control 事件（例如另一路徑的 `DeviceAttached`）。
    pub fn send_control(&self, ev: ControlEvent) {
        let _ = self.actor.control.send(ev);
    }

    /// Task 14（R1-F8）：現行 worker 的 TX 帳本 gauge。
    pub fn tx_ledger_gauge(&self) -> TxLedgerGauge {
        self.current_tx
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
            .map(|handle| handle.ledger_gauge())
            .expect("尚未開出 bridge")
    }

    /// Task 14：WAL 全部紀錄（dict）。
    pub fn wal_all_rows(&self) -> Vec<PyDict> {
        self.wal_rows()
    }

    /// Task 14：以 RPC 等待條件成立（每輪送一次 GetState，讓 actor 處理積壓事件）。
    pub fn wait_for(&self, what: &str, mut cond: impl FnMut(&ActorRig) -> bool) {
        let deadline = Instant::now() + RIG_WAIT;
        loop {
            let _ = self.get_state();
            if cond(self) {
                return;
            }
            assert!(Instant::now() < deadline, "等待逾時：{what}");
            std::thread::sleep(Duration::from_millis(2));
        }
    }

    /// 只送 `DeviceAttached`，不等 READY。
    pub fn attach(self) -> ActorRig {
        let _ = self.actor.control.send(ControlEvent::DeviceAttached {
            by_id: DEVICE_BY_ID.into(),
            real_path: REAL_PATH.into(),
        });
        self
    }

    /// attach 並以 FakeDevice 回應 login probe（`\n`→prompt）與 nonce 探針（回顯 nonce＋prompt）直到 READY。
    pub fn ready(self) -> ActorRig {
        self.dev.on_tx_reply(b"\n", b"root@t:/# ");
        self.dev.on_tx_match(
            r"__READY__[0-9a-f]+",
            Box::new(|line: &[u8]| {
                let text = String::from_utf8_lossy(line).into_owned();
                let token = text
                    .split("__READY__")
                    .nth(1)
                    .unwrap_or("")
                    .trim()
                    .to_string();
                format!("__READY__{token}\r\nroot@t:/# ").into_bytes()
            }),
        );
        let rig = self.attach();
        rig.wait_state("READY");
        rig
    }

    /// 送出 agent 命令（預設 `agent_submit`）；回傳 `cmd_id`。
    pub fn submit_ok(&self, cmd: &str) -> String {
        self.submit_with(cmd, |_| {})
    }

    /// 送出 agent 命令並允許調整 `SubmitReq`；回傳 `cmd_id`。
    pub fn submit_with(&self, cmd: &str, f: impl FnOnce(&mut SubmitReq)) -> String {
        let mut req = agent_submit(cmd);
        f(&mut req);
        let resp = self.request(SessionRequest::Submit(req));
        assert_eq!(
            resp.get("ok"),
            Some(&PyValue::Bool(true)),
            "submit 應成功：{resp:?}"
        );
        resp.get("cmd_id")
            .and_then(|v| v.as_str())
            .expect("submit 回應應含 cmd_id")
            .to_string()
    }

    /// 等命令進入 `running`。
    pub fn wait_running(&self, cmd_id: &str) {
        let deadline = Instant::now() + RIG_WAIT;
        loop {
            let cur = self.command_get(cmd_id);
            if cur.get("status").and_then(|v| v.as_str()) == Some("running") {
                return;
            }
            assert!(
                Instant::now() < deadline,
                "等待 {cmd_id} running 逾時：{cur:?}"
            );
            std::thread::sleep(Duration::from_millis(2));
        }
    }

    /// 等命令完成（紀錄出現 `done_at`），回傳 `command` dict。
    pub fn wait_done(&self, cmd_id: &str) -> PyDict {
        let deadline = Instant::now() + RIG_WAIT;
        loop {
            let cur = self.command_get(cmd_id);
            if cur
                .get("done_at")
                .is_some_and(|v| !matches!(v, PyValue::Null))
            {
                return cur;
            }
            assert!(Instant::now() < deadline, "等待 {cmd_id} 完成逾時：{cur:?}");
            std::thread::sleep(Duration::from_millis(2));
        }
    }

    /// 同步 RPC（逾時或 actor 停止即 panic）。
    pub fn request(&self, req: SessionRequest) -> PyDict {
        match self.actor.request(req, RIG_WAIT) {
            Ok(d) => d,
            Err(e) => panic!("session RPC 失敗：{}", e.message),
        }
    }

    /// 非同步 RPC：回傳接收端（之後以 `try_recv`／`recv_timeout` 取回）。
    pub fn request_async(&self, req: SessionRequest) -> Receiver<Result<PyDict, PyErr>> {
        let (tx, rx) = bounded::<Result<PyDict, PyErr>>(1);
        let sender = if matches!(&req, SessionRequest::Release { .. }) {
            &self.actor.safety
        } else {
            &self.actor.control
        };
        let _ = sender.send(ControlEvent::Request { req, reply: tx });
        rx
    }

    /// `session` 子 dict（02 §6.1 欄位＋SC-04 兩欄）。
    pub fn get_state(&self) -> PyDict {
        let resp = self.request(SessionRequest::GetState);
        match resp.get("session") {
            Some(PyValue::Dict(s)) => s.clone(),
            other => panic!("GetState 回應應含 session dict，實際 {other:?}"),
        }
    }

    /// `command.get` 等價查詢（命令登錄表）。
    pub fn command_get(&self, id: &str) -> PyDict {
        self.reg
            .get(id)
            .unwrap_or_else(|| panic!("找不到命令 {id}"))
    }

    fn wal_rows(&self) -> Vec<PyDict> {
        let bytes = self.wfs.contents(&self.wal.wal_path()).unwrap_or_default();
        String::from_utf8_lossy(&bytes)
            .lines()
            .filter(|l| !l.trim().is_empty())
            .map(|l| match loads(l).expect("WAL 行應為合法 JSON") {
                PyValue::Dict(d) => d,
                other => panic!("WAL 行應為 object，實際 {other:?}"),
            })
            .collect()
    }

    /// WAL 內所有 `dir == "TX"` 紀錄的原始 payload（base64 解碼後）。
    pub fn wal_tx_payloads(&self) -> Vec<Vec<u8>> {
        self.wal_rows()
            .into_iter()
            .filter(|r| r.get("dir").and_then(|v| v.as_str()) == Some("TX"))
            .map(|r| {
                let b64 = r.get("payload_b64").and_then(|v| v.as_str()).unwrap_or("");
                b64_decode(b64)
            })
            .collect()
    }

    /// WAL 內 `dir == "META"` 紀錄的 `meta` 物件。
    pub fn wal_meta_events(&self) -> Vec<PyDict> {
        self.wal_rows()
            .into_iter()
            .filter(|r| r.get("dir").and_then(|v| v.as_str()) == Some("META"))
            .filter_map(|r| match r.get("meta") {
                Some(PyValue::Dict(m)) => Some(m.clone()),
                _ => None,
            })
            .collect()
    }

    /// daemon stderr WARNING（`Effect::Warn`）收集結果。
    pub fn stderr_lines(&self) -> Vec<String> {
        self.stderr
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    /// 等 `session.state` 變為 `s`（逾時 panic，訊息含目前狀態）。
    pub fn wait_state(&self, s: &str) {
        let deadline = Instant::now() + RIG_WAIT;
        loop {
            let cur = self
                .get_state()
                .get("state")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            if cur == s {
                return;
            }
            assert!(
                Instant::now() < deadline,
                "等待 state={s} 逾時（目前 {cur}）"
            );
            std::thread::sleep(Duration::from_millis(2));
        }
    }
}

// ===========================================================================
// P5-T16：SessionManager 測試支援（ManagerRig、RigSerial、RigFs；plan 06 Task 16 Step 0）。
//
// `ManagerRig` 以真的 `SessionManager::new` 組裝（不起 daemon、不碰 OS）：
// - 序列埠：`RigSerial` 包住 `FakeSerialBackend`，記錄 open／close 時序（`events()`）、open 次數，
//   並提供 `block_open` 閘門（plan「FakeSerial::block_open」）；open 以**真實路徑**鍵控（by-id 本身不進入 open）。
// - 檔案系統：`RigFs` 記憶體版 `FsOps`（state.json 寫入次數、一次性寫入失敗），避免依賴 P4 的 MemFs 寫入路徑。
// - UART 獨占診斷與 console 工廠：固定回傳無持有者（測試只需要 PTY 形式）。
// ===========================================================================

use std::collections::BTreeSet;
use std::path::Path;
use std::sync::Condvar;

use sw_core::session_manager::{ManagerDeps, SessionManager};
use sw_core::state_store::{LoadedState, StateStore, load_state};
use sw_pal::ConsoleKind;
use sw_pal::traits::{
    CancelToken, CloseImpl, CloseReceipt, CloseToken, ConsoleTransportFactory, DeviceInfo, FsOps,
    FsRead, HolderReport, HolderScanStatus, IoWaker, PalErrorKind, PalResult, ReadOutcome,
    SerialHandle, SerialReader, SerialWriter, SubmitGate, TxResult, UartExclusive,
};

/// 管理者測試的 state.json 路徑（只存在於 RigFs）。
pub const STATE_PATH: &str = "/fake/state/state.json";
/// 管理者測試的 WAL 目錄（只存在於 FakeWalFs）。
const RIG_WAL_DIR: &str = "/fake/wal";
/// 管理者測試用模板：`prpl-template`（prpl，prompt `root@prpl:`）、`others-template`（passthrough，SC-35 等價）、`t`（shell，prompt `root@t:`）。
const MANAGER_TEMPLATES: &str = "profiles:\n  prpl-template:\n    platform: prpl\n    prompt_regex: '(?m)^root@prpl:.*# '\n    timeout_s: 2.0\n  others-template:\n    platform: passthrough\n    prompt_regex: '.*'\n    login_regex: '$^'\n    password_regex: '$^'\n    ready_probe: ''\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\n";
/// 一般 RPC 等待上限（真實時間；FakeClock 不影響 actor 等待）。
const RIG_RPC_TIMEOUT: Duration = Duration::from_secs(5);

/// YAML：`MANAGER_TEMPLATES` 加上給定的 target 列（`{...}` 行內字典字串）；無 target 時省略 `targets:`。
pub fn manager_yaml(targets: &[&str]) -> String {
    let mut s = MANAGER_TEMPLATES.to_string();
    if !targets.is_empty() {
        s.push_str("targets:\n");
        for t in targets {
            s.push_str(&format!("  - {t}\n"));
        }
    }
    s
}

/// `state.json` 內容中是否含有指定字串（PyValue 遞迴搜尋）。
pub fn has_str(v: &PyValue, needle: &str) -> bool {
    match v {
        PyValue::Str(s) => s == needle,
        PyValue::List(l) => l.iter().any(|x| has_str(x, needle)),
        PyValue::Dict(d) => d.0.values().any(|x| has_str(x, needle)),
        _ => false,
    }
}

/// PyDict 字串欄位（不存在或非字串回 None）。
pub fn s_of(d: &PyDict, key: &str) -> Option<String> {
    d.get(key).and_then(|v| v.as_str()).map(str::to_string)
}

/// 反覆檢查直到條件成立（真實時間；逾時 panic 並附上描述）。
pub fn wait_until(what: &str, mut cond: impl FnMut() -> bool) {
    let deadline = Instant::now() + RIG_RPC_TIMEOUT;
    while !cond() {
        assert!(Instant::now() < deadline, "等待逾時：{what}");
        std::thread::sleep(Duration::from_millis(2));
    }
}

/// shell target 的 login probe 與 ready_probe nonce 回應（與 `ActorRig::ready` 相同規則）。
pub fn install_shell_replies(dev: &FakeDevice) {
    dev.on_tx_reply(b"\n", b"root@t:/# ");
    dev.on_tx_match(
        r"__READY__[0-9a-f]+",
        Box::new(|line: &[u8]| {
            let text = String::from_utf8_lossy(line).into_owned();
            let token = text
                .split("__READY__")
                .nth(1)
                .unwrap_or("")
                .trim()
                .to_string();
            format!("__READY__{token}\r\nroot@t:/# ").into_bytes()
        }),
    );
}

fn rig_not_found() -> PalError {
    PalError::plain(PalErrorKind::NotFound, "No such file or directory")
}

/// 記憶體 `FsOps`：只實作 state.json 會用到的讀寫；寫入次數與一次性失敗可由測試觀測／注入。
#[derive(Default)]
pub struct RigFs {
    files: Mutex<BTreeMap<PathBuf, Vec<u8>>>,
    writes: Mutex<BTreeMap<PathBuf, usize>>,
    fail_next: Mutex<Option<PalError>>,
}

impl RigFs {
    pub fn new() -> Arc<RigFs> {
        Arc::new(RigFs::default())
    }

    /// 檔案目前內容（不存在回 None）。
    pub fn bytes(&self, path: &str) -> Option<Vec<u8>> {
        self.files
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(Path::new(path))
            .cloned()
    }

    /// 對指定路徑的成功 `atomic_write` 次數。
    pub fn write_count(&self, path: &str) -> usize {
        self.writes
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(Path::new(path))
            .copied()
            .unwrap_or(0)
    }

    /// 下一次 `atomic_write` 失敗（一次性；state.json commit 失敗替身）。
    pub fn fail_next_atomic_write(&self, err: PalError) {
        *self.fail_next.lock().unwrap_or_else(|e| e.into_inner()) = Some(err);
    }
}

impl FsRead for RigFs {
    fn read(&self, p: &Path) -> PalResult<Vec<u8>> {
        self.files
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(p)
            .cloned()
            .ok_or_else(rig_not_found)
    }

    fn exists(&self, p: &Path) -> bool {
        self.files
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .contains_key(p)
    }

    fn is_dir(&self, _p: &Path) -> bool {
        false
    }

    fn is_file(&self, p: &Path) -> bool {
        self.exists(p)
    }

    fn list_dir(&self, _p: &Path) -> PalResult<Vec<String>> {
        Ok(Vec::new())
    }
}

impl FsOps for RigFs {
    fn atomic_write(&self, p: &Path, data: &[u8], _mode: Option<u32>) -> PalResult<()> {
        if let Some(err) = self
            .fail_next
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take()
        {
            return Err(err);
        }
        self.files
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(p.to_path_buf(), data.to_vec());
        *self
            .writes
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .entry(p.to_path_buf())
            .or_insert(0) += 1;
        Ok(())
    }
}

/// 無持有者的獨占診斷（測試只需要 PTY 形式）。
pub struct RigExcl;

impl UartExclusive for RigExcl {
    fn probe_holders(&self, _real_path: &str) -> HolderReport {
        HolderReport {
            pids: Vec::new(),
            holder_unknown: false,
            status: HolderScanStatus::Ok,
        }
    }
}

/// POSIX PTY 形式的 console 工廠（測試替身）。
pub struct RigConsole;

impl ConsoleTransportFactory for RigConsole {
    fn kind(&self) -> ConsoleKind {
        ConsoleKind::Pty
    }
}

/// 只用於 drop 的 reader／writer（`CloseToken::close` 需要實體；真正的 reader／writer 已由外層先行 drop）。
struct NoopReader;

impl SerialReader for NoopReader {
    fn read(
        &mut self,
        _buf: &mut [u8],
        _timeout: Option<Duration>,
    ) -> Result<ReadOutcome, ReadError> {
        unreachable!("NoopReader 只用於 drop")
    }
}

struct NoopWriter;

impl SerialWriter for NoopWriter {
    fn write_gated(
        &mut self,
        _data: &[u8],
        _cancel: &CancelToken,
        _gate: &dyn SubmitGate,
    ) -> TxResult {
        unreachable!("NoopWriter 只用於 drop")
    }
}

/// 記錄 close 時序；`excl_fail` 路徑的 close 回報 `excl_cleared:false`，並讓下一次 open 得 `Held`（外部持有者出現）。
struct LoggedClose {
    path: String,
    log: Arc<Mutex<Vec<String>>>,
    excl_fail: bool,
    held: Arc<Mutex<BTreeSet<String>>>,
    inner: CloseToken,
}

impl CloseImpl for LoggedClose {
    fn is_alive(&self) -> bool {
        self.inner.is_alive()
    }

    fn close(self: Box<Self>) -> CloseReceipt {
        let me = *self;
        me.log
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(format!("close:{}", me.path));
        let mut receipt = me.inner.close(Box::new(NoopReader), Box::new(NoopWriter));
        if me.excl_fail {
            receipt.excl_cleared = false;
            me.held
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .insert(me.path.clone());
        }
        receipt
    }
}

struct RigHandle {
    inner: Box<dyn SerialHandle>,
    path: String,
    log: Arc<Mutex<Vec<String>>>,
    excl_fail: Arc<Mutex<BTreeSet<String>>>,
    held: Arc<Mutex<BTreeSet<String>>>,
}

impl SerialHandle for RigHandle {
    fn split(
        self: Box<Self>,
    ) -> (
        Box<dyn SerialReader>,
        Box<dyn SerialWriter>,
        Arc<dyn IoWaker>,
        CloseToken,
    ) {
        let me = *self;
        let (reader, writer, waker, token) = me.inner.split();
        let excl_fail = me
            .excl_fail
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .contains(&me.path);
        let wrapped = CloseToken::new(Box::new(LoggedClose {
            path: me.path,
            log: me.log,
            excl_fail,
            held: me.held,
            inner: token,
        }));
        (reader, writer, waker, wrapped)
    }

    fn set_baud(&self, baud: u32) -> PalResult<()> {
        self.inner.set_baud(baud)
    }

    fn is_alive(&self) -> bool {
        self.inner.is_alive()
    }
}

/// `FakeSerialBackend` 的觀測包裝（Task 16 Step 0）：open／close 時序、open 次數、`block_open` 閘門。
pub struct RigSerial {
    inner: Arc<FakeSerialBackend>,
    log: Arc<Mutex<Vec<String>>>,
    attempts: Mutex<BTreeMap<String, usize>>,
    gates: Mutex<BTreeSet<String>>,
    gate_cv: Condvar,
    excl_fail: Arc<Mutex<BTreeSet<String>>>,
    held: Arc<Mutex<BTreeSet<String>>>,
    devices: Mutex<BTreeMap<String, FakeDevice>>,
}

impl RigSerial {
    pub fn new() -> Arc<RigSerial> {
        Arc::new(RigSerial {
            inner: FakeSerialBackend::new(),
            log: Arc::new(Mutex::new(Vec::new())),
            attempts: Mutex::new(BTreeMap::new()),
            gates: Mutex::new(BTreeSet::new()),
            gate_cv: Condvar::new(),
            excl_fail: Arc::new(Mutex::new(BTreeSet::new())),
            held: Arc::new(Mutex::new(BTreeSet::new())),
            devices: Mutex::new(BTreeMap::new()),
        })
    }

    /// 在 FakeSerialBackend 加入實體裝置（以真實路徑鍵控）。
    pub fn add_device(&self, real_path: &str) -> FakeDevice {
        let dev = self.inner.add_device(real_path);
        self.devices
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(real_path.to_string(), dev.clone());
        dev
    }

    /// 以真實路徑取回已加入的裝置（未知路徑 panic）。
    pub fn device(&self, real_path: &str) -> FakeDevice {
        self.devices
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(real_path)
            .cloned()
            .unwrap_or_else(|| panic!("未登錄的裝置 {real_path}"))
    }

    /// 之後對該路徑的 `open_exclusive` 會阻塞於閘門（已越過 manager 的 gate 檢查之後）直到 `release_open`。
    pub fn block_open(&self, real_path: &str) {
        self.gates
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(real_path.to_string());
    }

    /// 放行 `block_open` 的閘門。
    pub fn release_open(&self, real_path: &str) {
        self.gates
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(real_path);
        self.gate_cv.notify_all();
    }

    /// `open_exclusive` 進入次數（閘門之前計數，含被阻塞者）。
    pub fn open_attempts(&self, real_path: &str) -> usize {
        self.attempts
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(real_path)
            .copied()
            .unwrap_or(0)
    }

    /// 之後該路徑的 close 回報 `excl_cleared:false`，且下一次 open 得 `Held`（plan probe 變體）。
    pub fn fail_excl_clear(&self, real_path: &str) {
        self.excl_fail
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(real_path.to_string());
    }

    /// open／close 時序（`open:<路徑>`／`close:<路徑>`，依發生順序）。
    pub fn events(&self) -> Vec<String> {
        self.log.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }
}

impl SerialBackend for RigSerial {
    fn open_exclusive(
        &self,
        dev: &DevicePath,
        cfg: &SerialConfig,
    ) -> Result<Box<dyn SerialHandle>, OpenError> {
        let key = dev.0.clone();
        *self
            .attempts
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .entry(key.clone())
            .or_insert(0) += 1;
        {
            let mut gates = self.gates.lock().unwrap_or_else(|e| e.into_inner());
            while gates.contains(&key) {
                gates = self.gate_cv.wait(gates).unwrap_or_else(|e| e.into_inner());
            }
        }
        let held_once = self
            .held
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&key);
        if held_once {
            return Err(OpenError::Held(HolderReport {
                pids: vec![4242],
                holder_unknown: false,
                status: HolderScanStatus::Ok,
            }));
        }
        let handle = self.inner.open_exclusive(dev, cfg)?;
        self.log
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(format!("open:{key}"));
        Ok(Box::new(RigHandle {
            inner: handle,
            path: key,
            log: self.log.clone(),
            excl_fail: self.excl_fail.clone(),
            held: self.held.clone(),
        }))
    }
}

/// 組裝一個 `SessionManager`（真實 `new`）與其觀測面；`ManagerRig::with` 與 `restart` 共用。
fn assemble_manager(
    yaml: &str,
    loaded: LoadedState,
    fs: Arc<RigFs>,
    wfs: Arc<FakeWalFs>,
    clock: Arc<FakeClock>,
    serial: Arc<RigSerial>,
    warnings: Arc<Mutex<Vec<String>>>,
) -> (Arc<SessionManager>, Arc<WalWriter>) {
    let load = load_profiles(
        PROFILE_DIR,
        &MemProfileFs {
            yaml: yaml.to_string(),
        },
    )
    .expect("測試 manager YAML 應可載入");
    let wal = Arc::new(WalWriter::open(
        WalConfig {
            dir: PathBuf::from(RIG_WAL_DIR),
            rotate_bytes: 67_108_864,
        },
        wfs.clone(),
        clock.clone(),
    ));
    let warn_sink = warnings.clone();
    let deps = ManagerDeps {
        serial: serial.clone(),
        uart_excl: Arc::new(RigExcl),
        console: Arc::new(RigConsole),
        caps: sw_pal::fakes::posix_caps(),
        wal: wal.clone(),
        registry: CommandRegistry::new(),
        state: StateStore::spawn(PathBuf::from(STATE_PATH), fs.clone()),
        loaded,
        load,
        clock: clock.clone(),
        env: Arc::new(FakeEnv::default()),
        fs: fs.clone(),
        warn: Arc::new(move |msg: String| {
            warn_sink
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .push(msg)
        }),
    };
    (SessionManager::new(deps), wal)
}

/// `SessionManager` 測試驅動（plan 06 Task 16 Step 0）：真 `SessionManager`＋RigSerial＋RigFs＋FakeWalFs＋FakeClock。
pub struct ManagerRig {
    pub mgr: Arc<SessionManager>,
    /// 第一個 target 的裝置（`primary` 未指定時為未登錄於快照的備用裝置）。
    pub dev: FakeDevice,
    pub clock: Arc<FakeClock>,
    pub wal: Arc<WalWriter>,
    pub serial: Arc<RigSerial>,
    pub fs: Arc<RigFs>,
    pub wfs: Arc<FakeWalFs>,
    pub state_path: String,
    yaml: String,
    warnings: Arc<Mutex<Vec<String>>>,
    shared: Arc<RigShared>,
}

/// 快照與實體路徑登錄（restart 之間共享）。
#[derive(Default)]
struct RigShared {
    /// 裝置快照（by_id → DeviceInfo），順序＝列舉順序。
    devices: Mutex<Vec<(String, DeviceInfo)>>,
    /// 由測試產生的實體路徑計數（`/dev/ttyRIG<n>`）。
    next_real: Mutex<usize>,
}

impl ManagerRig {
    /// 以 YAML 與 `state.json` 初值建立；`primary` 為 `(by_id, 實體路徑)`，會登錄到快照並安裝 shell 回應。
    pub fn with(yaml: &str, loaded: LoadedState, primary: Option<(&str, &str)>) -> ManagerRig {
        let serial = RigSerial::new();
        let shared = Arc::new(RigShared::default());
        let dev = match primary {
            Some((by_id, real)) => {
                let dev = serial.add_device(real);
                install_shell_replies(&dev);
                shared
                    .devices
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .push((
                        by_id.to_string(),
                        DeviceInfo {
                            by_id: by_id.to_string(),
                            real_path: real.to_string(),
                        },
                    ));
                dev
            }
            None => serial.add_device("/dev/ttyUNUSED"),
        };
        let fs = RigFs::new();
        let wfs = FakeWalFs::new();
        let clock = FakeClock::new();
        let warnings = Arc::new(Mutex::new(Vec::new()));
        let (mgr, wal) = assemble_manager(
            yaml,
            loaded,
            fs.clone(),
            wfs.clone(),
            clock.clone(),
            serial.clone(),
            warnings.clone(),
        );
        ManagerRig {
            mgr,
            dev,
            clock,
            wal,
            serial,
            fs,
            wfs,
            state_path: STATE_PATH.to_string(),
            yaml: yaml.to_string(),
            warnings,
            shared,
        }
    }

    /// 只有 `manager_yaml` 模板、無 target（rank／動態 session 測試用）。
    pub fn templates_only() -> ManagerRig {
        ManagerRig::with(&manager_yaml(&[]), LoadedState::default(), None)
    }

    /// 單一 target（by_id `/s/by-id/usb-A`，實體路徑 `/dev/ttyOLD`），shell 回應已安裝；尚未 attach。
    pub fn single_target(yaml: &str) -> ManagerRig {
        ManagerRig::with(
            yaml,
            LoadedState::default(),
            Some(("/s/by-id/usb-A", "/dev/ttyOLD")),
        )
    }

    /// 快照 → `update_devices` → `bootstrap_attach` → 等 COM0 READY。
    pub fn ready(self) -> ManagerRig {
        self.mgr.update_devices(&self.snapshot());
        self.mgr.bootstrap_attach();
        self.wait_state("COM0", "READY");
        self
    }

    /// 以測試替身登錄動態裝置（實體路徑自動配號；不安裝回應，預設偵測會落到 fallback）。
    pub fn devices(&self, keys: &[&str]) {
        for key in keys {
            let n = {
                let mut next = self
                    .shared
                    .next_real
                    .lock()
                    .unwrap_or_else(|e| e.into_inner());
                let n = *next;
                *next += 1;
                n
            };
            self.add_device_at(key, &format!("/dev/ttyRIG{n}"));
        }
    }

    /// 登錄指定 by_id 與實體路徑的裝置（實體裝置＋快照；不安裝回應）；回傳實體裝置替身。
    pub fn add_device_at(&self, by_id: &str, real_path: &str) -> FakeDevice {
        let dev = self.serial.add_device(real_path);
        self.shared
            .devices
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push((
                by_id.to_string(),
                DeviceInfo {
                    by_id: by_id.to_string(),
                    real_path: real_path.to_string(),
                },
            ));
        dev
    }

    /// 快照中的 by_id 清單（列舉順序）。
    pub fn device_keys(&self) -> Vec<String> {
        self.snapshot().into_iter().map(|(k, _)| k).collect()
    }

    /// 目前裝置快照（`update_devices` 的輸入）。
    pub fn snapshot(&self) -> Vec<(String, DeviceInfo)> {
        self.shared
            .devices
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    /// 快照中某 by_id 的實體路徑。
    pub fn real_of(&self, by_id: &str) -> String {
        self.snapshot()
            .into_iter()
            .find(|(k, _)| k == by_id)
            .map(|(_, info)| info.real_path)
            .unwrap_or_else(|| panic!("快照中無 {by_id}"))
    }

    /// 實體路徑對應的裝置替身。
    pub fn device(&self, real_path: &str) -> FakeDevice {
        self.serial.device(real_path)
    }

    /// 讓該實體裝置在偵測探針（`\n`）時回應 `prompt`。
    pub fn reply_probe(&self, real_path: &str, prompt: &[u8]) {
        self.device(real_path).on_tx_reply(b"\n", prompt);
    }

    /// 等 `counts().0 >= n`（session 數）。
    pub fn wait_sessions(&self, n: usize) {
        wait_until(&format!("session 數 >= {n}"), || self.mgr.counts().0 >= n);
    }

    /// 等 session 的 `profile_source` 出現並回傳（偵測／pin／sticky 結果）。
    pub fn wait_profile_source(&self, sel: &str) -> String {
        wait_until(&format!("{sel} profile_source"), || {
            self.find_session(sel)
                .and_then(|s| s_of(&s, "profile_source"))
                .is_some()
        });
        s_of(&self.session(sel), "profile_source").expect("profile_source")
    }

    /// `device_by_id → com` 對應（僅含有 device_by_id 的 session）。
    pub fn com_by_id(&self) -> HashMap<String, String> {
        self.mgr
            .list_sessions()
            .iter()
            .filter_map(|s| Some((s_of(s, "device_by_id")?, s_of(s, "com")?)))
            .collect()
    }

    /// 熱插拔替身：以新實體路徑取代快照中 by_id 的實體路徑，安裝 shell 回應，送 `update_devices`，
    /// 並等到新路徑被 open（drain 與重開的順序由 `events()` 斷言）。
    pub fn retarget_by_id(&self, by_id: &str, new_real: &str) {
        let dev = self.serial.add_device(new_real);
        install_shell_replies(&dev);
        {
            let mut devs = self
                .shared
                .devices
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            for (key, info) in devs.iter_mut() {
                if key == by_id {
                    info.real_path = new_real.to_string();
                }
            }
        }
        self.mgr.update_devices(&self.snapshot());
        let want = format!("open:{new_real}");
        wait_until(&want, || self.events().iter().any(|e| *e == want));
    }

    /// 以 selector 找 session 的 `state`（找不到回 None）。
    pub fn state_of(&self, sel: &str) -> Option<String> {
        self.find_session(sel).and_then(|s| s_of(&s, "state"))
    }

    /// 等 selector 的 session `state == state`。
    pub fn wait_state(&self, com: &str, state: &str) {
        wait_until(&format!("{com} state={state}"), || {
            self.state_of(com).as_deref() == Some(state)
        });
    }

    /// open／close 時序（`RigSerial::events`）。
    pub fn events(&self) -> Vec<String> {
        self.serial.events()
    }

    /// selector 對應的 session 公開欄位（找不到 panic）。
    pub fn session(&self, sel: &str) -> PyDict {
        self.find_session(sel)
            .unwrap_or_else(|| panic!("找不到 session {sel}"))
    }

    fn find_session(&self, sel: &str) -> Option<PyDict> {
        let sid = self.mgr.resolve(sel)?;
        self.mgr
            .list_sessions()
            .into_iter()
            .find(|s| s_of(s, "session_id").as_deref() == Some(sid.as_str()))
    }

    /// selector 對應 session 的 `last_error`（字串；None 表示 null 或查無）。
    pub fn session_last_error(&self, sel: &str) -> Option<String> {
        self.find_session(sel).and_then(|s| s_of(&s, "last_error"))
    }

    /// session 的 `last_error` 變更歷史（test-fakes 掛鉤）。
    pub fn last_error_history(&self, com: &str) -> Vec<String> {
        self.mgr.last_error_history(com)
    }

    /// daemon stderr WARNING 收集（`ManagerDeps.warn`）。
    pub fn warnings(&self) -> Vec<String> {
        self.warnings
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    /// `state.json` 原始文字（不存在回空字串）。
    pub fn state_text(&self) -> String {
        String::from_utf8_lossy(&self.fs.bytes(&self.state_path).unwrap_or_default()).into_owned()
    }

    /// `state.json` 寫入次數（成功的 atomic_write）。
    pub fn state_writes(&self) -> usize {
        self.fs.write_count(&self.state_path)
    }

    /// 以 `load_state` 讀回目前磁碟上的 `state.json`（不存在時為預設值）。
    pub fn load_state_now(&self) -> LoadedState {
        load_state(Path::new(&self.state_path), &*self.fs)
            .map(|(s, _)| s)
            .unwrap_or_default()
    }

    /// 讓之後所有 WAL 寫入失敗（磁碟滿替身；Review Focus 3）。
    pub fn fail_wal_writes(&self, err: PalError) {
        self.wfs
            .fail_next_writes(&self.wal.wal_path(), WAL_FAIL_FOREVER, err);
    }

    /// WAL 內所有行（JSON 物件）。
    pub fn wal_rows(&self) -> Vec<PyDict> {
        let bytes = self.wfs.contents(&self.wal.wal_path()).unwrap_or_default();
        String::from_utf8_lossy(&bytes)
            .lines()
            .filter(|l| !l.trim().is_empty())
            .map(|l| match loads(l).expect("WAL 行應為合法 JSON") {
                PyValue::Dict(d) => d,
                other => panic!("WAL 行應為 object，實際 {other:?}"),
            })
            .collect()
    }

    /// 以同一 `state.json`、同一裝置與 WAL 目錄重建 manager（「重啟」）；快照沿用。
    pub fn restart(&self) -> ManagerRig {
        let (loaded, _) =
            load_state(Path::new(&self.state_path), &*self.fs).expect("state.json 應可載入");
        let warnings = Arc::new(Mutex::new(Vec::new()));
        let (mgr, wal) = assemble_manager(
            &self.yaml,
            loaded,
            self.fs.clone(),
            self.wfs.clone(),
            self.clock.clone(),
            self.serial.clone(),
            warnings.clone(),
        );
        ManagerRig {
            mgr,
            dev: self.dev.clone(),
            clock: self.clock.clone(),
            wal,
            serial: self.serial.clone(),
            fs: self.fs.clone(),
            wfs: self.wfs.clone(),
            state_path: self.state_path.clone(),
            yaml: self.yaml.clone(),
            warnings,
            shared: self.shared.clone(),
        }
    }
}
