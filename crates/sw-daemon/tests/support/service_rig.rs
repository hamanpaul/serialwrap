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
    PalErrorKind, PalResult, SerialBackend, UartExclusive,
};
use sw_proto::config_types::{ProfileFs, RuntimeConfig, SerialConsoleMap, load_profiles};
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
    next_write_error: Mutex<Option<PalError>>,
    /// 接續 `next_write_error` 之後的連續失敗（錯誤、剩餘次數）；模擬持續 ENOSPC。
    repeat_write_error: Mutex<Option<(PalError, usize)>>,
    before_write: Mutex<Option<Box<dyn FnOnce() + Send>>>,
}

impl RigFs {
    /// 下一次落盤前在鎖外暫停，讓 RPC 可交錯在 binding 與外部 attach 之間。
    pub fn before_next_atomic_write(&self, hook: Box<dyn FnOnce() + Send>) {
        *self.before_write.lock().unwrap_or_else(|e| e.into_inner()) = Some(hook);
    }

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

    pub fn fail_next_atomic_write(&self, error: PalError) {
        *self
            .next_write_error
            .lock()
            .unwrap_or_else(|e| e.into_inner()) = Some(error);
    }

    /// 接下來連續 `n` 次 `atomic_write` 都以 `error` 失敗（持續 ENOSPC 替身）。
    pub fn fail_next_atomic_writes(&self, error: PalError, n: usize) {
        *self
            .repeat_write_error
            .lock()
            .unwrap_or_else(|e| e.into_inner()) = (n > 0).then_some((error, n));
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
        let hook = self.before_write.lock().unwrap_or_else(|e| e.into_inner()).take();
        if let Some(hook) = hook {
            hook();
        }
        if let Some(error) = self
            .next_write_error
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take()
        {
            return Err(error);
        }
        {
            let mut repeat = self
                .repeat_write_error
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            if let Some((error, remaining)) = repeat.as_mut() {
                let error = error.clone();
                *remaining -= 1;
                if *remaining == 0 {
                    *repeat = None;
                }
                return Err(error);
            }
        }
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
    native_tcp: bool,
    native_com0com: bool,
    serial_console: Option<SerialConsoleMap>,
    opened_serial_consoles: Mutex<Vec<String>>,
}

impl RigPtyFactory {
    /// 依建立順序的 (slave_path, io 參照)。
    pub fn created(&self) -> Vec<(String, sw_pal::fakes::console::FakeConsoleRef)> {
        self.created.lock().unwrap().clone()
    }

    pub fn opened_serial_consoles(&self) -> Vec<String> {
        self.opened_serial_consoles.lock().unwrap().clone()
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
        if self.native_tcp {
            sw_pal::console_pty::NativeConsoleFactory::new(ConsoleKind::TcpTelnet).bind_tcp_console()
        } else {
            self.fake.bind_tcp_console()
        }
    }
    fn held_slave_paths(&self) -> Option<std::collections::HashSet<String>> {
        self.fake.held_slave_paths()
    }
    fn slave_path_exists(&self, path: &str) -> bool {
        self.fake.slave_path_exists(path)
    }
    fn new_mux(&self) -> PalResult<Box<dyn sw_pal::console_mux::ConsoleMux>> {
        if self.native_tcp {
            sw_pal::console_pty::NativeConsoleFactory::new(ConsoleKind::TcpTelnet).new_mux()
        } else {
            self.fake.new_mux()
        }
    }
    fn open_com0com_console(&self, port: &str) -> PalResult<Box<dyn sw_pal::console_pty::ConsoleIo>> {
        if self.native_com0com {
            let io = sw_pal::console_pty::NativeConsoleFactory::new(ConsoleKind::TcpTelnet)
                .open_com0com_console(port)?;
            self.opened_serial_consoles.lock().unwrap().push(port.to_string());
            Ok(io)
        } else if self.native_tcp {
            // 原生 TCP 測試只探測設定是否送到 factory；POSIX 沒有 com0com 埠，故模擬平台拒絕開啟。
            self.opened_serial_consoles.lock().unwrap().push(port.to_string());
            Err(PalError::plain(
                PalErrorKind::Unsupported,
                "測試使用原生 TCP factory；POSIX 不提供 com0com",
            ))
        } else {
            let io = self.fake.open_com0com_console(port)?;
            self.opened_serial_consoles
                .lock()
                .unwrap()
                .push(port.to_string());
            Ok(io)
        }
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
    pub env: Arc<FakeEnv>,
    /// P6 Task 10：`with_console_backend` 的 console 替身工廠（其餘建構子為 `None`）。
    pub console: Option<Arc<RigPtyFactory>>,
    /// P8 Task 10：`with_flash` 的 flash 端點組件（其餘建構子為 `None`）。
    pub flash: Option<FlashParts>,
}

/// `targets:` 一列。
fn target_line(com: &str, by_id: &str) -> String {
    format!("  - {{com: {com}, profile: t, device_by_id: {by_id}}}\n")
}

impl ServiceRig {
    pub fn build(spec: RigSpec) -> ServiceRig {
        ServiceRig::build_with_console(spec, None)
    }

    /// 以指定環境變數建構 rig，供憑證與 console unlock 測試使用。
    pub fn build_with_env(spec: RigSpec, env: Arc<FakeEnv>) -> ServiceRig {
        ServiceRig::build_with_code_identity(
            spec,
            None,
            None,
            Some("/fake/bundle".to_string()),
            None,
            LoadedState::default(),
            Some(env),
        )
    }

    /// 以指定的載入狀態建構 rig；不需在 `RigSpec` 增加跨既有測試的必填欄位。
    pub fn build_with_loaded_state(spec: RigSpec, loaded: LoadedState) -> ServiceRig {
        ServiceRig::build_with_code_identity(
            spec,
            None,
            None,
            Some("/fake/bundle".to_string()),
            None,
            loaded,
            None,
        )
    }

    /// 以載入狀態與自訂 serial 替身建構 rig，供 dispatch 測試注入開埠錯誤。
    pub fn build_with_loaded_state_and_serial_backend(
        spec: RigSpec,
        loaded: LoadedState,
        serial_backend: Arc<dyn SerialBackend>,
    ) -> ServiceRig {
        ServiceRig::build_with_code_identity(
            spec,
            None,
            None,
            Some("/fake/bundle".to_string()),
            Some(serial_backend),
            loaded,
            None,
        )
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
            native_tcp: false,
            native_com0com: false,
            serial_console: None,
            opened_serial_consoles: Mutex::new(Vec::new()),
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

    /// 原生 loopback TCP listener 與 ConsoleMux，UART 仍使用 FakeSerialBackend。
    pub fn with_native_tcp_console() -> ServiceRig {
        Self::with_native_tcp_console_config(None, false)
    }

    /// 原生 TCP listener 加 `windows.serial_console` map；測試可選擇 fake 或實際 com0com console 埠。
    pub fn with_native_tcp_console_and_serial_console(
        console_port: &str,
        peer_port: &str,
        native_com0com: bool,
    ) -> ServiceRig {
        let yaml = format!(
            "windows:\n  serial_console:\n    {BY_ID_A}: {{console_port: {console_port}, peer_port: {peer_port}}}\n"
        );
        let map = RuntimeConfig::from_text(Some(&yaml))
            .expect("測試 windows.serial_console YAML")
            .windows_serial_console()
            .expect("map 存在")
            .expect("測試 windows.serial_console 結構正確");
        Self::with_native_tcp_console_config(Some(map), native_com0com)
    }

    /// Fake TCP hub 與 fake com0com console；供不依賴實體 COM pair 的生命週期測試使用。
    pub fn with_fake_tcp_console_and_serial_console(
        console_port: &str,
        peer_port: &str,
    ) -> ServiceRig {
        let yaml = format!(
            "windows:\n  serial_console:\n    {BY_ID_A}: {{console_port: {console_port}, peer_port: {peer_port}}}\n"
        );
        let map = RuntimeConfig::from_text(Some(&yaml))
            .expect("測試 windows.serial_console YAML")
            .windows_serial_console()
            .expect("map 存在")
            .expect("測試 windows.serial_console 結構正確");
        let factory = Arc::new(RigPtyFactory {
            fake: sw_pal::fakes::console::FakeConsoleFactory::new(ConsoleKind::TcpTelnet),
            created: Mutex::new(Vec::new()),
            native_tcp: false,
            native_com0com: false,
            serial_console: Some(map),
            opened_serial_consoles: Mutex::new(Vec::new()),
        });
        ServiceRig::build_with_console(
            RigSpec {
                yaml: format!(
                    "profiles:\n  t:\n    platform: passthrough\n    timeout_s: 2.0\nwindows:\n  serial_console:\n    {BY_ID_A}: {{console_port: {console_port}, peer_port: {peer_port}}}\ntargets:\n{}",
                    target_line("COM0", BY_ID_A)
                ),
                devices: vec![(BY_ID_A.into(), REAL_A.into())],
                explicit_target_count: 1,
                allowlist: None,
                wait_ready: Vec::new(),
            },
            Some(factory),
        )
    }

    fn with_native_tcp_console_config(
        serial_console: Option<SerialConsoleMap>,
        native_com0com: bool,
    ) -> ServiceRig {
        let factory = Arc::new(RigPtyFactory {
            fake: sw_pal::fakes::console::FakeConsoleFactory::new(ConsoleKind::TcpTelnet),
            created: Mutex::new(Vec::new()),
            native_tcp: true,
            native_com0com,
            serial_console,
            opened_serial_consoles: Mutex::new(Vec::new()),
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
        ServiceRig::build_with_code_identity(
            spec,
            console,
            None,
            Some("/fake/bundle".to_string()),
            None,
            LoadedState::default(),
            None,
        )
    }

    /// 以指定序列埠後端建構 rig；`None` 沿用預設 FakeSerialBackend。
    pub fn build_with_serial_backend(
        spec: RigSpec,
        serial_backend: Option<Arc<dyn SerialBackend>>,
    ) -> ServiceRig {
        ServiceRig::build_with_code_identity(
            spec,
            None,
            None,
            Some("/fake/bundle".to_string()),
            serial_backend,
            LoadedState::default(),
            None,
        )
    }

    fn build_with_code_identity(
        spec: RigSpec,
        console: Option<Arc<RigPtyFactory>>,
        code_fingerprint: Option<String>,
        code_path: Option<String>,
        serial_backend: Option<Arc<dyn SerialBackend>>,
        loaded: LoadedState,
        env: Option<Arc<FakeEnv>>,
    ) -> ServiceRig {
        let env = env.unwrap_or_else(|| Arc::new(FakeEnv::default()));
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
        let manager_serial: Arc<dyn SerialBackend> =
            serial_backend.unwrap_or_else(|| serial.clone());
        let mut caps = sw_pal::fakes::posix_caps();
        caps.com0com_console = console
            .as_ref()
            .is_some_and(|factory| factory.serial_console.is_some());
        let mgr = SessionManager::new(ManagerDeps {
            serial: manager_serial,
            uart_excl: excl.clone(),
            console: match &console {
                Some(f) => f.clone() as Arc<dyn ConsoleTransportFactory>,
                None => Arc::new(RigConsole),
            },
            windows_serial_console: console.as_ref().and_then(|factory| factory.serial_console.clone()),
            caps,
            wal: wal.clone(),
            registry: registry.clone(),
            state: StateStore::spawn(PathBuf::from(STATE_PATH), fs.clone()),
            loaded,
            load,
            clock: clock.clone(),
            env: env.clone(),
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
            code_fingerprint,
            code_path: code_path.or_else(|| Some("/fake/bundle".to_string())),
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
            env,
            console,
            flash: None,
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

    /// 指定 daemon 啟動快照的指紋與 bundle root，供健康狀態與一般 RPC 對齊測試使用。
    pub fn ready_shell_with_code_identity(
        code_fingerprint: Option<&str>,
        code_path: Option<&str>,
    ) -> ServiceRig {
        ServiceRig::build_with_code_identity(
            RigSpec {
                yaml: format!("{TEMPLATES}targets:\n{}", target_line("COM0", BY_ID_A)),
                devices: vec![(BY_ID_A.into(), REAL_A.into())],
                explicit_target_count: 1,
                allowlist: None,
                wait_ready: vec!["COM0".into()],
            },
            None,
            code_fingerprint.map(str::to_string),
            code_path.map(str::to_string),
            None,
            LoadedState::default(),
            None,
        )
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

    /// 設定環境變數（憑證與 console unlock 測試使用）。
    pub fn set_env(&self, key: &str, value: &str) {
        self.env
            .vars
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(key.to_string(), value.to_string());
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

    /// ConsoleHub 各 socket ring 的排隊 bytes 與累計丟棄 bytes。
    pub fn console_hub_diagnostics(&self, selector: &str) -> Vec<(String, usize, u64)> {
        self.mgr.console_hub_diagnostics(selector)
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

// ---------------------------------------------------------------------------
// P8 Task 10：flash 端點 rig（`mcu_rpc.rs`；計畫 09 Task 10 的 `ServiceRig`＋fake 平台）
// ---------------------------------------------------------------------------

/// TI SBL 的 auto-baud sync 與 ACK（`tests/contract_blackbox/fakemcu.py`）。
pub const MCU_SYNC: &[u8] = b"\x55\x55";
pub const MCU_ACK: &[u8] = b"\x00\xcc";
/// flash rig 的端點 symlink（記憶體替身；對應 `RUN_DIR/dev/ttyMCU`）。
pub const FLASH_LINK: &str = "/fake/run/dev/ttyMCU";
/// 不在裝置快照中的 by-id（該 target 停 DETACHED）。
pub const BY_ID_C: &str = "/s/by-id/usb-C";

/// shell 模板 `t`＋passthrough 模板 `m`（`m` 非 command_capable，為 flash 偵測候選）。
pub const FLASH_TEMPLATES: &str = "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\n  m:\n    platform: passthrough\n    prompt_regex: '(?m)^MCU> '\n    ready_probe: ''\n";

/// flash rig 裝置的假 DUT 行為。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DevKind {
    /// shell 假 DUT（`install_shell_replies`）。
    Shell,
    /// MCU 線：已確認 TX 出現 [`MCU_SYNC`] 時回一次 [`MCU_ACK`]。
    Mcu,
    /// 不回任何 bytes。
    Silent,
}

/// `ServiceRig::with_flash` 的組裝選項。
pub struct FlashSetup {
    /// profiles.yaml 全文（通常為 [`FLASH_TEMPLATES`]＋`targets:`）。
    pub yaml: String,
    /// 裝置快照（by_id, 實體路徑, 行為；依列舉順序）。
    pub devices: Vec<(String, String, DevKind)>,
    /// `Platform.caps.flash_endpoint`（false＝Windows／無端點：`FlashEndpoint::start` 回 `Ok(None)`）。
    pub caps_flash: bool,
    /// coordinator 的 Clock（manager 仍用 rig 的 FakeClock）。
    pub coord_clock: Arc<dyn sw_pal::traits::Clock>,
    /// 等待 (COM, state) 成立後才回傳。
    pub wait_states: Vec<(String, String)>,
}

/// `FlashSetup.yaml` 的 `targets:` 一列。
pub fn flash_target_line(com: &str, profile: &str, by_id: &str) -> String {
    format!("  - {{com: {com}, profile: {profile}, device_by_id: {by_id}}}\n")
}

/// 端點 PTY 的 console 工廠替身：`create_pty` 的 master 為 `FakeConsoleIo`（測試端以 `FakeConsoleHandle` 扮演
/// flasher：`push_rx`＝flasher 寫入、`take_tx`＝寫給 flasher 的 bytes），slave 路徑沿用 `FakeConsoleFactory` 的
/// `/fake/pts/<n>`；`link`／`unlink` 記錄於記憶體表（`fail_link` 時 `link` 回錯）。其餘方法委派 `FakeConsoleFactory`。
pub struct RigFlashConsole {
    fake: sw_pal::fakes::console::FakeConsoleFactory,
    masters: Mutex<Vec<sw_pal::fakes::console::FakeConsoleHandle>>,
    links: Mutex<BTreeMap<PathBuf, String>>,
    fail_link: std::sync::atomic::AtomicBool,
    /// 每次 `link` 呼叫時的回呼（daemon 測試記錄啟動序用）。
    on_link: Mutex<Option<Box<dyn Fn(&Path) + Send + Sync>>>,
}

impl RigFlashConsole {
    pub fn new() -> Arc<RigFlashConsole> {
        Arc::new(RigFlashConsole {
            fake: sw_pal::fakes::console::FakeConsoleFactory::new(ConsoleKind::Pty),
            masters: Mutex::new(Vec::new()),
            links: Mutex::new(BTreeMap::new()),
            fail_link: std::sync::atomic::AtomicBool::new(false),
            on_link: Mutex::new(None),
        })
    }

    /// 之後的 `link` 一律回 `[Errno 20] Not a directory`（模擬端點目錄不可建立）。
    pub fn fail_link(&self) {
        self.fail_link.store(true, std::sync::atomic::Ordering::SeqCst);
    }

    pub fn set_on_link(&self, f: Box<dyn Fn(&Path) + Send + Sync>) {
        *self.on_link.lock().unwrap_or_else(|e| e.into_inner()) = Some(f);
    }

    /// 目前的 symlink 表（link 路徑 → slave 路徑）。
    pub fn links(&self) -> BTreeMap<PathBuf, String> {
        self.links.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// `create_pty` 次數。
    pub fn pty_count(&self) -> usize {
        self.masters.lock().unwrap_or_else(|e| e.into_inner()).len()
    }

    /// 最近一次 `create_pty` 的 master 對端（flasher 視角）。
    pub fn flasher(&self) -> sw_pal::fakes::console::FakeConsoleHandle {
        self.masters
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .last()
            .cloned()
            .expect("端點 PTY 尚未建立")
    }
}

impl ConsoleTransportFactory for RigFlashConsole {
    fn kind(&self) -> ConsoleKind {
        ConsoleKind::Pty
    }
    fn create_pty(&self) -> PalResult<sw_pal::PtyPair> {
        let mut pair = self.fake.pty_pair_from(sw_pal::fakes::console::FakeIo::unbounded());
        let (master, handle) = sw_pal::fakes::console::FakeConsoleIo::new();
        pair.master = Box::new(master);
        self.masters.lock().unwrap_or_else(|e| e.into_inner()).push(handle);
        Ok(pair)
    }
    fn bind_tcp_console(&self) -> PalResult<Box<dyn sw_pal::console_pty::ConsoleListener>> {
        self.fake.bind_tcp_console()
    }
    fn held_slave_paths(&self) -> Option<std::collections::HashSet<String>> {
        self.fake.held_slave_paths()
    }
    fn slave_path_exists(&self, path: &str) -> bool {
        self.fake.slave_path_exists(path)
    }
    fn new_mux(&self) -> PalResult<Box<dyn sw_pal::console_mux::ConsoleMux>> {
        self.fake.new_mux()
    }
    fn link(&self, slave_path: &str, link: &Path) -> PalResult<()> {
        if self.fail_link.load(std::sync::atomic::Ordering::SeqCst) {
            return Err(PalError::from_errno(20));
        }
        self.links
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(link.to_path_buf(), slave_path.to_string());
        if let Some(f) = self.on_link.lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
            f(link);
        }
        Ok(())
    }
    fn unlink(&self, link: &Path) -> PalResult<()> {
        self.links.lock().unwrap_or_else(|e| e.into_inner()).remove(link);
        Ok(())
    }
    fn tty_setraw(&self, _slave: &sw_pal::console_pty::PtySlaveRef) -> PalResult<()> {
        Ok(())
    }
    fn tcgetattr_speed(&self, _slave: &sw_pal::console_pty::PtySlaveRef) -> PalResult<sw_pal::console_pty::TermSpeed> {
        Err(PalError::plain(PalErrorKind::Io, "tcgetattr failed"))
    }
}

/// `with_flash` 的 flash 端點組件。
pub struct FlashParts {
    pub state: Arc<sw_daemon::service::FlashState>,
    pub coord: Arc<sw_core::flash_endpoint::FlashCoordinator>,
    pub console: Arc<RigFlashConsole>,
}

/// 不做任何事的喚醒器（`EndpointShutdown` 的初始 waker；端點 master 的 waker 由 `FlashEndpoint::start` 另登記）。
struct NoWake;

impl sw_pal::traits::IoWaker for NoWake {
    fn wake(&self) {}
}

impl ServiceRig {
    /// P8 Task 10：真 `SessionManager`＋真 `FlashCoordinator`＋真 `FlashEndpoint`（記憶體 PTY），以
    /// `Service::attach_flash` 掛上（與 daemon_main 同序：coordinator 先於 bootstrap attach 建立——RX observer 於 bridge
    /// 建立時取快照；端點於 bootstrap attach 之後才 start）。
    pub fn with_flash(setup: FlashSetup) -> ServiceRig {
        use sw_core::flash_endpoint::{EndpointShutdown, FlashCoordinator, FlashEndpoint, ENDPOINT_GRACE};
        use sw_core::mcu_patterns::McuPatternRegistry;

        let serial = FakeSerialBackend::new();
        let mut devices = BTreeMap::new();
        let mut snapshot = Vec::new();
        for (by_id, real, kind) in &setup.devices {
            let dev = serial.add_device(real);
            match kind {
                DevKind::Shell => install_shell_replies(&dev),
                DevKind::Mcu => dev.on_tx_reply(MCU_SYNC, MCU_ACK),
                DevKind::Silent => {}
            }
            devices.insert(real.clone(), dev);
            snapshot.push((
                by_id.clone(),
                DeviceInfo {
                    by_id: by_id.clone(),
                    real_path: real.clone(),
                },
            ));
        }
        let load = load_profiles(PROFILE_DIR, &MemProfileFs { yaml: setup.yaml })
            .expect("測試 YAML 應可載入");
        let explicit_target_count = load.profiles.len();
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
            console: Arc::new(RigConsole),
            caps: sw_pal::fakes::posix_caps(),
            windows_serial_console: None,
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
        let console = RigFlashConsole::new();
        let coord = FlashCoordinator::new(
            mgr.clone(),
            Arc::new(McuPatternRegistry::default_registry()),
            setup.coord_clock.clone(),
            excl.clone(),
            console.clone(),
            EndpointShutdown::new(Arc::new(NoWake)),
        );
        let procs = FakeProcScan::new(Vec::new());
        mgr.update_devices(&snapshot);
        mgr.bootstrap_attach();
        for (com, state) in &setup.wait_states {
            let m = mgr.clone();
            wait_until(&format!("{com} state={state}"), || {
                m.list_sessions().iter().any(|s| {
                    s.get("com").and_then(PyValue::as_str) == Some(com.as_str())
                        && s.get("state").and_then(PyValue::as_str) == Some(state.as_str())
                })
            });
        }
        let mut caps = sw_pal::fakes::posix_caps();
        caps.flash_endpoint = setup.caps_flash;
        let pal = sw_pal::fakes::platform().console(console.clone()).caps(caps).build();
        let endpoint = FlashEndpoint::start(&pal, Path::new(FLASH_LINK), coord.clone(), ENDPOINT_GRACE)
            .expect("FlashEndpoint::start");
        let svc = Arc::new(Service::new(ServiceDeps {
            manager: mgr.clone(),
            wal: wal.clone(),
            registry: registry.clone(),
            procs: procs.clone(),
            version: RIG_VERSION.to_string(),
            // 與 `ServiceRig::build` 同一預設程式碼身分（無指紋、bundle root＝`/fake/bundle`）。
            code_fingerprint: None,
            code_path: Some("/fake/bundle".to_string()),
            started_at: RIG_STARTED_AT.to_string(),
            explicit_target_count,
            test_device_allowlist: None,
            pid: RIG_PID,
        }));
        let state = sw_daemon::service::FlashState::new(coord.clone(), endpoint);
        assert!(svc.attach_flash(state.clone()), "attach_flash 第一次應成功");
        // P8 Task 14：flash sync-probe 與 RPC 分派共用 Service 的 admission 邊界（同 daemon_main 組裝）。
        assert!(coord.set_admission(svc.admission().clone()), "set_admission 第一次應成功");
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
            env: Arc::new(FakeEnv::default()),
            console: None,
            flash: Some(FlashParts {
                state,
                coord,
                console,
            }),
        };
        rig.settle();
        rig
    }

    fn flash_parts(&self) -> &FlashParts {
        self.flash.as_ref().expect("此 rig 未以 with_flash 建立")
    }

    /// flasher 對 `/dev/ttyMCU` 寫入（端點 master 變可讀）。
    pub fn flasher_write(&self, bytes: &[u8]) {
        self.flash_parts().console.flasher().push_rx(bytes);
    }

    /// 端點寫給 flasher 的 bytes（讀走）。
    pub fn flasher_take(&self) -> Vec<u8> {
        self.flash_parts().console.flasher().take_tx()
    }

    /// coordinator 送出的 flash 事件種類（依序，`"FlashSyncProbeStart"` 等）與 COM。
    pub fn flash_events(&self) -> Vec<(String, &'static str)> {
        self.flash_parts()
            .coord
            .flash_events_for_test()
            .into_iter()
            .map(|e| (e.com, e.kind))
            .collect()
    }

    /// 取出端點並 `stop`（join＋unlink）；無端點回 `None`。
    pub fn stop_flash_endpoint(&self) -> Option<sw_core::flash_endpoint::EndpointStopReceipt> {
        self.flash_parts().state.take_endpoint().map(|e| e.stop())
    }

    /// `rpc(method, {})` 的 RPC_WIRE 位元組（去 `version`；Python `json.dumps(…, separators=(",", ":"))` 插入序）。
    pub fn wire_without_version(&self, method: &str) -> String {
        let mut d = self.call(method, "{}");
        d.0.shift_remove("version");
        pyjson::dumps(&PyValue::Dict(d), &pyjson::RPC_WIRE)
    }
}
