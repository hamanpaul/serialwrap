//! P5-T18：`dispatch.rs` 的 `ServiceRig`（真 `SessionManager`＋`Service`，序列埠／WAL／state.json／行程掃描皆為替身）。
//!
//! 不起 daemon、不碰 OS：FakeSerialBackend 的 shell 假 DUT（回 `\n` → prompt、`__READY__<nonce>` → nonce＋prompt）、
//! FakeWalFs、記憶體 state.json（`RigFs`）、FakeClock、FakeProcScan。

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use sw_core::arbiter::CommandRegistry;
use sw_core::session_manager::{ManagerDeps, SessionManager};
use sw_core::state_store::{LoadedState, StateStore};
use sw_core::wal::{WalConfig, WalWriter};
use sw_daemon::service::{Service, ServiceDeps, TestAllowlist};
use sw_pal::ConsoleKind;
use sw_pal::fakes::clock::FakeClock;
use sw_pal::fakes::env::FakeEnv;
use sw_pal::fakes::proc::FakeProcScan;
use sw_pal::fakes::serial::{FakeDevice, FakeSerialBackend};
use sw_pal::fakes::wal_fs::FakeWalFs;
use sw_pal::traits::{
    ConsoleTransportFactory, DeviceInfo, FsOps, FsRead, HolderReport, HolderScanStatus, PalError,
    PalErrorKind, PalResult, UartExclusive,
};
use sw_proto::config_types::{ProfileFs, load_profiles};
use sw_proto::pycompat::{PyErr, PyOsError};
use sw_proto::pyjson::{self, PyDict, PyValue};
use sw_proto::pypath::PathFlavor;

/// 測試用 profile 目錄（只存在於記憶體 ProfileFs）。
const PROFILE_DIR: &str = "/fake/profiles";
/// state.json 路徑（只存在於 RigFs）。
pub const STATE_PATH: &str = "/fake/state/state.json";
/// WAL 目錄（只存在於 FakeWalFs）。
pub const WAL_DIR: &str = "/fake/wal";
/// 測試 daemon 的 pid（`health.status` 的 `pid`）。
pub const RIG_PID: u32 = 4242;
/// 測試 daemon 的版本（`version` 欄位）。
pub const RIG_VERSION: &str = "0.0.0-t18";
/// `started_at`。
pub const RIG_STARTED_AT: &str = "2026-10-27T00:00:00Z";
/// COM0 的 by-id 與實體路徑。
pub const BY_ID_A: &str = "/s/by-id/usb-A";
pub const REAL_A: &str = "/fake/tty-A";
pub const BY_ID_B: &str = "/s/by-id/usb-B";
pub const REAL_B: &str = "/fake/tty-B";

/// shell 模板 `t`（prompt `root@t:`）。
const TEMPLATES: &str = "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\n";

/// 記憶體 ProfileFs：單一 `profiles.yaml`。
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

/// 記憶體 `FsOps`（state.json 讀寫）。
#[derive(Default)]
pub struct RigFs {
    files: Mutex<BTreeMap<PathBuf, Vec<u8>>>,
    writes: Mutex<usize>,
}

impl RigFs {
    pub fn bytes(&self, path: &str) -> Vec<u8> {
        self.files
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(Path::new(path))
            .cloned()
            .unwrap_or_default()
    }

    pub fn writes(&self) -> usize {
        *self.writes.lock().unwrap_or_else(|e| e.into_inner())
    }
}

impl FsRead for RigFs {
    fn read(&self, p: &Path) -> PalResult<Vec<u8>> {
        self.files
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(p)
            .cloned()
            .ok_or_else(|| PalError::plain(PalErrorKind::NotFound, "No such file or directory"))
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
        self.files
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(p.to_path_buf(), data.to_vec());
        *self.writes.lock().unwrap_or_else(|e| e.into_inner()) += 1;
        Ok(())
    }
}

/// 可腳本化的獨占診斷（attach 前置 holder 檢查；預設無持有者）。
#[derive(Default)]
pub struct RigExcl {
    held: Mutex<BTreeMap<String, Vec<u32>>>,
}

impl RigExcl {
    pub fn hold(&self, real_path: &str, pids: Vec<u32>) {
        self.held
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(real_path.to_string(), pids);
    }
}

impl UartExclusive for RigExcl {
    fn probe_holders(&self, real_path: &str) -> HolderReport {
        let pids = self
            .held
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(real_path)
            .cloned()
            .unwrap_or_default();
        HolderReport {
            pids,
            holder_unknown: false,
            status: HolderScanStatus::Ok,
        }
    }
}

struct RigConsole;

/// P6 Task 10：PTY 替身工廠（委派 `FakeConsoleFactory`，並記錄每個 `create_pty` 的 io 參照供測試推輸入、讀輸出）。
pub struct RigPtyFactory {
    pub fake: sw_pal::fakes::console::FakeConsoleFactory,
    created: Mutex<Vec<(String, sw_pal::fakes::console::FakeConsoleRef)>>,
}

impl RigPtyFactory {
    /// 依建立順序的 (slave_path, io 參照)。
    pub fn created(&self) -> Vec<(String, sw_pal::fakes::console::FakeConsoleRef)> {
        self.created.lock().unwrap().clone()
    }
}

impl ConsoleTransportFactory for RigPtyFactory {
    fn kind(&self) -> ConsoleKind {
        self.fake.kind()
    }
    fn create_pty(&self) -> PalResult<sw_pal::PtyPair> {
        let io = sw_pal::fakes::console::FakeIo::unbounded();
        let r = io.reference();
        let pair = self.fake.pty_pair_from(io);
        self.created.lock().unwrap().push((pair.slave_path.clone(), r));
        Ok(pair)
    }
    fn bind_tcp_console(&self) -> PalResult<Box<dyn sw_pal::console_pty::ConsoleListener>> {
        self.fake.bind_tcp_console()
    }
    fn held_slave_paths(&self) -> Option<std::collections::HashSet<String>> {
        self.fake.held_slave_paths()
    }
    fn new_mux(&self) -> PalResult<Box<dyn sw_pal::console_mux::ConsoleMux>> {
        self.fake.new_mux()
    }
}

impl ConsoleTransportFactory for RigConsole {
    fn kind(&self) -> ConsoleKind {
        ConsoleKind::Pty
    }
}

/// shell 假 DUT：`\n` → prompt；`__READY__<nonce>` → nonce＋prompt。
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

/// 反覆檢查直到條件成立（真實時間；逾時 panic）。
pub fn wait_until(what: &str, mut cond: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !cond() {
        assert!(Instant::now() < deadline, "等待逾時：{what}");
        std::thread::sleep(Duration::from_millis(2));
    }
}

/// ServiceRig 組裝選項。
pub struct RigSpec {
    /// profiles.yaml 全文。
    pub yaml: String,
    /// 裝置快照（by_id, 實體路徑；依列舉順序）；每個裝置安裝 shell 回應。
    pub devices: Vec<(String, String)>,
    pub explicit_target_count: usize,
    pub allowlist: Option<TestAllowlist>,
    /// 等待這些 COM 進入 READY。
    pub wait_ready: Vec<String>,
}

/// `Service` 測試驅動（plan 06 Task 18 Step 1）。
pub struct ServiceRig {
    pub svc: Arc<Service>,
    pub mgr: Arc<SessionManager>,
    pub wal: Arc<WalWriter>,
    pub wfs: Arc<FakeWalFs>,
    pub fs: Arc<RigFs>,
    pub clock: Arc<FakeClock>,
    pub procs: Arc<FakeProcScan>,
    pub excl: Arc<RigExcl>,
    pub registry: Arc<CommandRegistry>,
    pub devices: BTreeMap<String, FakeDevice>,
    pub version: String,
    /// P6 Task 10：`with_console_backend` 的 console 替身工廠（其餘建構子為 `None`）。
    pub console: Option<Arc<RigPtyFactory>>,
}

/// `targets:` 一列。
fn target_line(com: &str, by_id: &str) -> String {
    format!("  - {{com: {com}, profile: t, device_by_id: {by_id}}}\n")
}

impl ServiceRig {
    pub fn build(spec: RigSpec) -> ServiceRig {
        ServiceRig::build_with_console(spec, None)
    }

    /// P6 Task 10：單一 shell target（COM0）已 READY，console 後端為 `kind`（`"pty"`｜`"tcp"`）的替身工廠。
    pub fn with_console_backend(kind: &str) -> ServiceRig {
        let kind = match kind {
            "pty" => ConsoleKind::Pty,
            "tcp" => ConsoleKind::TcpTelnet,
            other => panic!("未知 console 後端 {other}"),
        };
        let factory = Arc::new(RigPtyFactory {
            fake: sw_pal::fakes::console::FakeConsoleFactory::new(kind),
            created: Mutex::new(Vec::new()),
        });
        ServiceRig::build_with_console(
            RigSpec {
                yaml: format!("{TEMPLATES}targets:\n{}", target_line("COM0", BY_ID_A)),
                devices: vec![(BY_ID_A.into(), REAL_A.into())],
                explicit_target_count: 1,
                allowlist: None,
                wait_ready: vec!["COM0".into()],
            },
            Some(factory),
        )
    }

    fn build_with_console(spec: RigSpec, console: Option<Arc<RigPtyFactory>>) -> ServiceRig {
        let serial = FakeSerialBackend::new();
        let mut devices = BTreeMap::new();
        let mut snapshot = Vec::new();
        for (by_id, real) in &spec.devices {
            let dev = serial.add_device(real);
            install_shell_replies(&dev);
            devices.insert(real.clone(), dev);
            snapshot.push((
                by_id.clone(),
                DeviceInfo {
                    by_id: by_id.clone(),
                    real_path: real.clone(),
                },
            ));
        }
        let load = load_profiles(PROFILE_DIR, &MemProfileFs { yaml: spec.yaml })
            .expect("測試 YAML 應可載入");
        let fs = Arc::new(RigFs::default());
        let wfs = FakeWalFs::new();
        let clock = FakeClock::new();
        let wal = Arc::new(WalWriter::open(
            WalConfig {
                dir: PathBuf::from(WAL_DIR),
                rotate_bytes: 67_108_864,
            },
            wfs.clone(),
            clock.clone(),
        ));
        let registry = CommandRegistry::new();
        let excl = Arc::new(RigExcl::default());
        let mgr = SessionManager::new(ManagerDeps {
            serial: serial.clone(),
            uart_excl: excl.clone(),
            console: match &console {
                Some(f) => f.clone() as Arc<dyn ConsoleTransportFactory>,
                None => Arc::new(RigConsole),
            },
            caps: sw_pal::fakes::posix_caps(),
            wal: wal.clone(),
            registry: registry.clone(),
            state: StateStore::spawn(PathBuf::from(STATE_PATH), fs.clone()),
            loaded: LoadedState::default(),
            load,
            clock: clock.clone(),
            env: Arc::new(FakeEnv::default()),
            fs: fs.clone(),
            warn: Arc::new(|_msg: String| {}),
        });
        let procs = FakeProcScan::new(Vec::new());
        mgr.update_devices(&snapshot);
        mgr.bootstrap_attach();
        let svc = Arc::new(Service::new(ServiceDeps {
            manager: mgr.clone(),
            wal: wal.clone(),
            registry: registry.clone(),
            procs: procs.clone(),
            version: RIG_VERSION.to_string(),
            started_at: RIG_STARTED_AT.to_string(),
            explicit_target_count: spec.explicit_target_count,
            test_device_allowlist: spec.allowlist,
            pid: RIG_PID,
        }));
        let rig = ServiceRig {
            svc,
            mgr,
            wal,
            wfs,
            fs,
            clock,
            procs,
            excl,
            registry,
            devices,
            version: RIG_VERSION.to_string(),
            console,
        };
        for com in &spec.wait_ready {
            rig.wait_state(com, "READY");
        }
        rig.settle();
        rig
    }

    /// FakeSerial 單一 shell target（COM0，`t:COM0`）已 READY。
    pub fn ready_shell() -> ServiceRig {
        ServiceRig::build(RigSpec {
            yaml: format!("{TEMPLATES}targets:\n{}", target_line("COM0", BY_ID_A)),
            devices: vec![(BY_ID_A.into(), REAL_A.into())],
            explicit_target_count: 1,
            allowlist: None,
            wait_ready: vec!["COM0".into()],
        })
    }

    /// 兩個 shell target（COM0＝usb-A、COM1＝usb-B），裝置快照刻意以 usb-B 先列舉。
    pub fn two_shells() -> ServiceRig {
        ServiceRig::build(RigSpec {
            yaml: format!(
                "{TEMPLATES}targets:\n{}{}",
                target_line("COM1", BY_ID_B),
                target_line("COM0", BY_ID_A)
            ),
            devices: vec![
                (BY_ID_B.into(), REAL_B.into()),
                (BY_ID_A.into(), REAL_A.into()),
            ],
            explicit_target_count: 2,
            allowlist: None,
            wait_ready: vec!["COM0".into(), "COM1".into()],
        })
    }

    /// `ready_shell` 加上已啟用的 OD-11 allowlist（`source: env`）。
    pub fn with_allowlist(coms: &[&str]) -> ServiceRig {
        ServiceRig::build(RigSpec {
            yaml: format!("{TEMPLATES}targets:\n{}", target_line("COM0", BY_ID_A)),
            devices: vec![(BY_ID_A.into(), REAL_A.into())],
            explicit_target_count: 1,
            allowlist: Some(TestAllowlist {
                enabled: true,
                coms: coms.iter().map(|c| c.to_string()).collect(),
                source: "env",
                rejected_reason: None,
            }),
            wait_ready: vec!["COM0".into()],
        })
    }

    /// 只有模板（無 explicit target）；`devices` 為動態裝置快照（可為空）。
    pub fn templates_only(devices: &[(&str, &str)]) -> ServiceRig {
        ServiceRig::build(RigSpec {
            yaml: TEMPLATES.to_string(),
            devices: devices
                .iter()
                .map(|(b, r)| (b.to_string(), r.to_string()))
                .collect(),
            explicit_target_count: 0,
            allowlist: None,
            wait_ready: Vec::new(),
        })
    }

    /// 呼叫 `Service::rpc`（`params_json` 為 JSON 物件文字）；`Err` 時 panic。
    pub fn call(&self, method: &str, params_json: &str) -> PyDict {
        match self.call_raw(method, params_json) {
            Ok(d) => d,
            Err(e) => panic!("{method} 應回字典，實際 Err({})", e.message),
        }
    }

    /// 呼叫 `Service::rpc` 並預期 `Err(PyErr)`（framing 轉 `EXCEPTION`）。
    pub fn call_err(&self, method: &str, params_json: &str) -> PyErr {
        match self.call_raw(method, params_json) {
            Ok(d) => panic!(
                "{method} 應回 Err，實際 {}",
                pyjson::dumps(&PyValue::Dict(d), &pyjson::RPC_WIRE)
            ),
            Err(e) => e,
        }
    }

    pub fn call_raw(&self, method: &str, params_json: &str) -> Result<PyDict, PyErr> {
        let params = match pyjson::loads(params_json).expect("params 應為合法 JSON") {
            PyValue::Dict(d) => d,
            other => panic!("params 應為 object：{other:?}"),
        };
        self.svc.rpc(method, &params)
    }

    /// 副作用指紋：(WAL seq, state.json 內容, UART 寫入次數總和)。
    pub fn side_effect_fingerprint(&self) -> (u64, Vec<u8>, usize) {
        (
            self.wal.current_seq(),
            self.fs.bytes(STATE_PATH),
            self.devices.values().map(FakeDevice::write_calls).sum(),
        )
    }

    /// 等待背景寫入（state.json sticky 持久化、WAL META）穩定。
    pub fn settle(&self) {
        let mut last = (self.fs.writes(), self.wal.current_seq());
        let mut stable_since = Instant::now();
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
            let now = (self.fs.writes(), self.wal.current_seq());
            if now != last {
                last = now;
                stable_since = Instant::now();
            } else if stable_since.elapsed() >= Duration::from_millis(150) {
                return;
            }
        }
    }

    /// selector 的 session 公開欄位（`session.get_state`）；查無回 None。
    pub fn session(&self, sel: &str) -> Option<PyDict> {
        let r = self
            .svc
            .rpc(
                "session.get_state",
                &sw_proto::pydict! { "selector" => sel },
            )
            .ok()?;
        match r.get("session") {
            Some(PyValue::Dict(s)) => Some(s.clone()),
            _ => None,
        }
    }

    pub fn wait_state(&self, sel: &str, state: &str) {
        wait_until(&format!("{sel} state={state}"), || {
            self.session(sel)
                .and_then(|s| s.get("state").and_then(PyValue::as_str).map(str::to_string))
                .as_deref()
                == Some(state)
        });
    }

    pub fn device(&self, real_path: &str) -> &FakeDevice {
        self.devices.get(real_path).expect("未登錄的裝置")
    }
}

/// 自 `tests/acceptance/slice21_scope.yaml` 取出 `deferred_methods` 的 method 名稱（依檔案順序）。
pub fn deferred_methods(yaml: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut inside = false;
    for line in yaml.lines() {
        if line.starts_with("deferred_methods:") {
            inside = true;
            continue;
        }
        if !inside {
            continue;
        }
        let Some(rest) = line.trim_start().strip_prefix("- {method:") else {
            if !line.starts_with(' ') && !line.trim().is_empty() {
                break;
            }
            continue;
        };
        let name = rest.split(',').next().unwrap_or("").trim().to_string();
        out.push(name);
    }
    out
}
