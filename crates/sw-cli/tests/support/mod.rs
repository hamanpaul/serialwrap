//! sw-cli 管理面測試替身支援模組（`TestEnv`；P4-T1 交付）。
//!
//! 所有元件皆為記憶體替身（`sw_pal::fakes`），不碰真實 OS：`env`（`FakeSysEnv`；`system_config_dir` 指向隔離 tmp，SC-09）、
//! `fs`（隔離 tmp 目錄的真實檔案系統，可包 `FaultFs`）、`clock`（`FakeClock`）、`rpc`（請求感知的 `FakeRpcConnector`，預設所有
//! endpoint refused）、`procs`（預設空清單）。`tunnel`、`service`、`process`、`registry` 預設未設定，取得時 panic
//! `fake component not configured: <name>`（計畫 Task 1 替身規則；`tunnel` 亦不預設配置）。
//!
//! 方法分三類：
//! 1. P4-T1 已完整實作（環境、config、RPC 回應與計數、閘門化 pid 存活、腳本化 argv、事件合併、安裝佈局檔案、journal 讀取）。
//! 2. 以有狀態替身實作、但其產品語意由後續 Task 承接（例如監管模型的 `service().query_now()`）。
//! 3. 產品耦合且尚無替身模型者，以 `todo!("P4-T<n>: …")` 明示（中斷注入、R5-F1 拉起模型、install journal 步驟）；
//!    對應 Task 實作時必須替換，並於 PR 說明。
#![allow(dead_code, unused_variables)]

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use sw_cli::ctx::{CliCtx, TextIo};
use sw_pal::fakes::fs::FaultFs;
pub use sw_pal::fakes::service::ServiceModelState;
use sw_pal::fakes::{
    platform, posix_caps, real_fs, windows_caps, FakeClock, FakeProcScan, FakeProcessCtl,
    FakeRegistry, FakeRpcConnector, FakeServiceCtl, FakeSysEnv, FakeSysEnvBuilder, FakeTunnel,
};
use sw_pal::fs_ops::{FileLockGuard, FsOps, LinkKind};
pub use sw_pal::proc_scan::{DaemonProc, ProcIdentity};
pub use sw_pal::service_ctl::{
    CompositeServiceCtl, HoldState, ServiceAction, ServiceCtl, SystemdCtl, WindowsServiceCtl,
    WindowsTaskCtl,
};
pub use sw_pal::traits::{Clock, FsRead, PalResult, SupervisionMode, SysEnv};
use sw_pal::PalErrorKind;
use sw_proto::pyjson::{dumps_dict, loads, PyDict, PyInt, PyValue, DISK_STABLE};
use sw_proto::pypath::{HomeLookup, PathFlavor};
use sw_proto::release_manifest::{parse_manifest, ReleaseManifest};
use sw_proto::runtime_paths::{EnvSnapshot, NativeTraits, RuntimePaths};

/// 腳本化 argv 的環境操作（`FakeSysEnvBuilder` 的一次呼叫，保存下來以便每次重建替身）。
type EnvOp = Box<dyn Fn(FakeSysEnvBuilder) -> FakeSysEnvBuilder>;

/// `health.ping` 的成功回應（與 P3 client 的 JSON 形狀一致）。
const OK_PING: &str = r#"{"ok":true,"pid":1234,"version":"test"}"#;
/// `session.list` 的成功回應（空清單）。
const OK_SESSION_LIST: &str = r#"{"ok":true,"sessions":[]}"#;
/// 通用成功回應。
const OK_EMPTY: &str = r#"{"ok":true}"#;

/// 中斷注入點（R1-F5）：`<journal_step>:<before_effect|after_effect>` 與步驟內點。Task 12 新增 crash point 時必須同步補進此處
/// （`crash_points_cover_every_journal_step` 核對）。
pub const CRASH_POINTS: &[&str] = &[
    "lock:before_effect",
    "lock:after_effect",
    "verify:before_effect",
    "verify:after_effect",
    "safety_check:before_effect",
    "safety_check:after_effect",
    "snapshot_taken:before_effect",
    "snapshot_taken:after_effect",
    "stop_supervisor:before_effect",
    "stop_supervisor:after_hold",
    "stop_supervisor:after_effect",
    "confirm_released:before_effect",
    "confirm_released:after_effect",
    "backup_started:before_effect",
    "backup_started:after_effect",
    "backup_started:after_config_copy",
    "backup_complete:before_effect",
    "backup_complete:after_effect",
    "entries_switching:before_effect",
    "entries_switching:after_effect",
    "entries_switching:after_first_rename",
    "start_and_verify:before_effect",
    "start_and_verify:after_effect",
    "start_and_verify:started",
    "revert_to_previous:before_effect",
    "revert_to_previous:after_effect",
];

/// OD-48 D9(l)：legacy 停止專用的中斷注入點，另立清單並由獨立迴圈測試（`mgmt_legacy_handoff`）涵蓋。
/// 「擷取後 stop 前」「stop 送出後」「已停、切換前」。
pub const LEGACY_CRASH_POINTS: &[&str] = &[
    "legacy_stop:after_capture",
    "legacy_stop:after_stop_sent",
    "legacy_stop:after_stopped",
];

struct EventFs {
    inner: Arc<dyn FsOps>,
    events: Arc<Mutex<Vec<(u64, String)>>>,
}

impl EventFs {
    fn wrap(inner: Arc<dyn FsOps>) -> (Arc<dyn FsOps>, Arc<Mutex<Vec<(u64, String)>>>) {
        let events = Arc::new(Mutex::new(Vec::new()));
        (
            Arc::new(Self { inner, events: Arc::clone(&events) }),
            events,
        )
    }

    fn record_write(&self, path: &Path, bytes: &[u8]) {
        let Some(name) = path.file_name().and_then(|name| name.to_str()) else { return; };
        let label = if name == "install-journal.json" {
            loads(&String::from_utf8_lossy(bytes))
                .ok()
                .and_then(|value| match value {
                    PyValue::Dict(value) => value.get("step").and_then(|step| match step {
                        PyValue::Str(step) => Some(format!("journal:{step}")),
                        _ => None,
                    }),
                    _ => None,
                })
        } else if name == "autostart-hold.json" {
            Some("fs:write autostart-hold.json".to_string())
        } else {
            None
        };
        if let Some(label) = label {
            self.events.lock().unwrap_or_else(|e| e.into_inner())
                .push((sw_pal::fakes::next_event_seq(), label));
        }
    }
}

impl FsRead for EventFs {
    fn read(&self, path: &Path) -> PalResult<Vec<u8>> { self.inner.read(path) }
    fn exists(&self, path: &Path) -> bool { self.inner.exists(path) }
    fn is_dir(&self, path: &Path) -> bool { self.inner.is_dir(path) }
    fn is_file(&self, path: &Path) -> bool { self.inner.is_file(path) }
    fn list_dir(&self, path: &Path) -> PalResult<Vec<String>> { self.inner.list_dir(path) }
}

impl FsOps for EventFs {
    fn atomic_write(&self, path: &Path, bytes: &[u8], mode: Option<u32>) -> PalResult<()> {
        self.inner.atomic_write(path, bytes, mode)?;
        self.record_write(path, bytes);
        Ok(())
    }
    fn write_file(&self, path: &Path, bytes: &[u8]) -> PalResult<()> {
        self.inner.write_file(path, bytes)?;
        self.record_write(path, bytes);
        Ok(())
    }
    fn mkdirs(&self, path: &Path) -> PalResult<()> { self.inner.mkdirs(path) }
    fn replace(&self, from: &Path, to: &Path) -> PalResult<()> { self.inner.replace(from, to) }
    fn remove_file(&self, path: &Path) -> PalResult<()> {
        self.inner.remove_file(path)?;
        if path.file_name().and_then(|name| name.to_str()) == Some("autostart-hold.json") {
            self.events.lock().unwrap_or_else(|e| e.into_inner())
                .push((sw_pal::fakes::next_event_seq(), "fs:remove autostart-hold.json".into()));
        }
        Ok(())
    }
    fn copy_preserving(&self, from: &Path, to: &Path) -> PalResult<()> { self.inner.copy_preserving(from, to) }
    fn sync_file(&self, path: &Path) -> PalResult<()> { self.inner.sync_file(path) }
    fn copy_tree(&self, from: &Path, to: &Path) -> PalResult<()> { self.inner.copy_tree(from, to) }
    fn remove_tree(&self, path: &Path) -> PalResult<()> { self.inner.remove_tree(path) }
    fn read_link(&self, path: &Path) -> PalResult<Option<PathBuf>> { self.inner.read_link(path) }
    fn set_mode(&self, path: &Path, mode: u32) -> PalResult<()> { self.inner.set_mode(path, mode) }
    fn sync_dir(&self, path: &Path) -> PalResult<()> { self.inner.sync_dir(path) }
    fn symlink_file(&self, link: &Path, target: &Path) -> PalResult<()> { self.inner.symlink_file(link, target) }
    fn symlink_or_copy_dir(&self, link: &Path, target: &Path) -> PalResult<LinkKind> {
        self.inner.symlink_or_copy_dir(link, target)
    }
    fn file_lock_exclusive(&self, path: &Path, blocking: bool) -> PalResult<FileLockGuard> {
        self.inner.file_lock_exclusive(path, blocking)
    }
}

pub struct TestEnv {
    tmp_dir: tempfile::TempDir,
    windows: bool,
    ops: Vec<EnvOp>,
    env: Arc<FakeSysEnv>,
    fs: Arc<dyn FsOps>,
    fs_events: Arc<Mutex<Vec<(u64, String)>>>,
    clock: Arc<FakeClock>,
    rpc: Arc<FakeRpcConnector>,
    procs: Arc<FakeProcScan>,
    tunnel: Option<Arc<FakeTunnel>>,
    service: Option<Arc<FakeServiceCtl>>,
    process: Option<Arc<FakeProcessCtl>>,
    registry: Option<Arc<FakeRegistry>>,
    crash_point: Option<String>,
    real_service: bool,
    retained_locks: Mutex<Vec<sw_pal::fs_ops::FileLockGuard>>,
    active_daemon_endpoint: Option<String>,
    active_daemon_pid: Option<u32>,
    restart_on_failure: bool,
    daemon_exit_code_after_handoff: Option<i32>,
    supervisor_ignores_hold: bool,
    new_daemon_handoff_pids: Option<Vec<u32>>,
    instance_appears: Option<(String, String, u32, Vec<String>)>,
    /// 群組成員（`build_env` 逐一套用；`without_group` 可移除）。
    groups: Vec<String>,
    /// OD-48：0.3.x daemon 替身在停止後仍存活的時間（`u64::MAX`＝永不結束）。
    legacy_linger_ns: Option<Arc<std::sync::atomic::AtomicU64>>,
}

pub trait IntoSupervisionMode {
    fn into_supervision_mode(self) -> SupervisionMode;
}
impl IntoSupervisionMode for SupervisionMode {
    fn into_supervision_mode(self) -> SupervisionMode {
        self
    }
}
impl IntoSupervisionMode for &str {
    fn into_supervision_mode(self) -> SupervisionMode {
        match self {
            "on-demand" => SupervisionMode::OnDemand,
            "systemd-user" => SupervisionMode::SystemdUser,
            "systemd-system" => SupervisionMode::SystemdSystem,
            "windows-task" => SupervisionMode::WindowsTask,
            "windows-service" => SupervisionMode::WindowsService,
            other => SupervisionMode::Other(other.to_string()),
        }
    }
}

impl TestEnv {
    pub fn posix() -> Self {
        Self::new(false)
    }

    pub fn windows() -> Self {
        Self::new(true)
    }

    fn new(windows: bool) -> Self {
        let tmp_dir = tempfile::tempdir().expect("tempdir 建立失敗");
        let (fs, fs_events) = EventFs::wrap(real_fs());
        let mut t = Self {
            tmp_dir,
            windows,
            ops: Vec::new(),
            env: FakeSysEnv::builder().build(),
            fs,
            fs_events,
            clock: FakeClock::new(),
            rpc: FakeRpcConnector::new(),
            procs: FakeProcScan::new(Vec::new()),
            tunnel: None,
            service: None,
            process: None,
            registry: windows.then(|| FakeRegistry::new(Vec::new(), &[])),
            crash_point: None,
            real_service: false,
            retained_locks: Mutex::new(Vec::new()),
            active_daemon_endpoint: None,
            active_daemon_pid: None,
            restart_on_failure: false,
            daemon_exit_code_after_handoff: None,
            supervisor_ignores_hold: false,
            new_daemon_handoff_pids: None,
            instance_appears: None,
            groups: Vec::new(),
            legacy_linger_ns: None,
        };
        for dir in [
            t.home(),
            t.config_dir(),
            t.state_dir(),
            t.run_dir(),
            t.by_id_dir(),
            t.by_path_dir(),
            t.install_root(),
            t.log_dir(),
            t.bundle_dir().join("bin"),
            t.system_config_dir(),
        ] {
            std::fs::create_dir_all(&dir).expect("測試目錄建立失敗");
        }
        t.env = t.build_env();
        t
    }

    /// `TestEnv::posix()`／`windows()` 共用的 `SysEnv` 預設值（隔離目錄；不含平台以外的真實路徑）。
    fn default_vars(&self) -> Vec<(String, String)> {
        let mut vars = vec![
            ("HOME".to_string(), self.home().display().to_string()),
            (
                "SERIALWRAP_CONFIG_DIR".to_string(),
                self.config_dir().display().to_string(),
            ),
            (
                "SERIALWRAP_STATE_DIR".to_string(),
                self.state_dir().display().to_string(),
            ),
            (
                "SERIALWRAP_RUN_DIR".to_string(),
                self.run_dir().display().to_string(),
            ),
            (
                "SERIALWRAP_PROFILE_DIR".to_string(),
                self.config_dir().join("profiles").display().to_string(),
            ),
            (
                "SERIALWRAP_BY_ID_DIR".to_string(),
                self.by_id_dir().display().to_string(),
            ),
            (
                "SERIALWRAP_BY_PATH_DIR".to_string(),
                self.by_path_dir().display().to_string(),
            ),
            (
                "SERIALWRAP_INSTALL_ROOT".to_string(),
                self.install_root().display().to_string(),
            ),
            (
                "SERIALWRAP_LOG_DIR".to_string(),
                self.log_dir().display().to_string(),
            ),
        ];
        if self.windows {
            // Windows 路徑風格（`join_win`）會把設定檔組成 `<dir>\config.yaml`，在 POSIX 宿主上不是同一檔；
            // 明示 `SERIALWRAP_CONFIG` 讓讀寫兩端指向 `with_config` 寫入的隔離檔（P4-T6 承接，僅影響 windows()）。
            vars.push((
                "SERIALWRAP_CONFIG".to_string(),
                self.config_path().display().to_string(),
            ));
            // benches.yaml 同理：Windows 路徑風格會把 `SERIALWRAP_CONFIG_DIR` 組成 `<dir>\benches.yaml`（P4-T9 承接）。
            vars.push((
                "SERIALWRAP_BENCHES_FILE".to_string(),
                self.config_dir().join("benches.yaml").display().to_string(),
            ));
            vars.push(("USERPROFILE".to_string(), self.home().display().to_string()));
            vars.push((
                "LOCALAPPDATA".to_string(),
                self.home()
                    .join("AppData")
                    .join("Local")
                    .display()
                    .to_string(),
            ));
        }
        vars
    }

    fn build_env(&self) -> Arc<FakeSysEnv> {
        let mut b = FakeSysEnv::builder()
            .home(&self.home())
            .system_config_dir(&self.system_config_dir())
            .current_exe(&self.bundle_dir().join("bin").join("serialwrap"))
            .user_identity("tester");
        for (k, v) in self.default_vars() {
            b = b.var(&k, &v);
        }
        for group in &self.groups {
            b = b.in_group(group);
        }
        for op in &self.ops {
            b = op(b);
        }
        b.build()
    }

    fn push_env_op(mut self, op: EnvOp) -> Self {
        self.ops.push(op);
        self.env = self.build_env();
        self
    }

    /// 本 TestEnv 的預設 POSIX socket（`<run_dir>/serialwrapd.sock`，與 CLI 由 `SERIALWRAP_RUN_DIR` 推導的預設相同）。
    /// 以 `with_rpc_ok(&t.default_socket(), …)` 註冊即可讓不帶 `--socket` 的 CLI 呼叫落在替身上。
    /// 以 `/` 串接（POSIX `os.path.join` 語意），使 Windows 主機上跑 POSIX 形狀測試時也與 CLI 推導值一致。
    pub fn default_socket(&self) -> String {
        format!("{}/serialwrapd.sock", self.run_dir().display())
    }

    pub fn win_endpoint() -> &'static str {
        "tcp://127.0.0.1:48700"
    }

    /// 寫 `CONFIG_DIR/config.yaml`（真實檔案，位於隔離 tmp 目錄）。
    pub fn with_config(self, s: &str) -> Self {
        std::fs::write(self.config_path(), s).expect("config.yaml 寫入失敗");
        self
    }

    pub fn with_var(self, k: &str, v: &str) -> Self {
        let (k, v) = (k.to_string(), v.to_string());
        self.push_env_op(Box::new(move |b| b.var(&k, &v)))
    }

    pub fn with_systemd(self, flag: bool) -> Self {
        let mut env = self.push_env_op(Box::new(move |b| b.systemd(flag)));
        if flag && env.service.is_none() {
            env.real_service = true;
        }
        env
    }

    pub fn with_wsl(self, flag: bool) -> Self {
        self.push_env_op(Box::new(move |b| b.wsl(flag)))
    }

    /// 名稱未設定 `which` 即視為不存在（FakeSysEnv 預設行為），此處不需額外腳本。
    pub fn with_which_missing(self, name: &str) -> Self {
        self
    }

    pub fn with_which(self, name: &str, path: &str) -> Self {
        let (n, p) = (name.to_string(), path.to_string());
        self.push_env_op(Box::new(move |b| b.which(&n, &p)))
    }

    pub fn with_which_all(self, name: &str, paths: &[&str]) -> Self {
        let n = name.to_string();
        let ps: Vec<String> = paths.iter().map(|s| s.to_string()).collect();
        self.push_env_op(Box::new(move |b| {
            let refs: Vec<&str> = ps.iter().map(String::as_str).collect();
            b.which_all(&n, &refs)
        }))
    }

    pub fn with_fake_registry(mut self, r: Arc<FakeRegistry>) -> Self {
        self.registry = Some(r);
        self
    }

    /// 移除群組成員（P4-T13：先前版本為 no-op，無法驗證「非 dialout」情境）。
    pub fn without_group(mut self, g: &str) -> Self {
        self.groups.retain(|member| member != g);
        self.env = self.build_env();
        self
    }

    /// `doctor` 非 advisory 檢查全部通過的最小環境（P4-T13）：systemd、dialout 群組、`serialwrap`／`serialwrapd` 在 PATH，
    /// 以 `CLI_VERSION` 回應的健康 daemon（`version_pairing` 需要 daemon 版本與 bundle 一致）。
    pub fn all_required_ok(mut self) -> Self {
        self.groups.push("dialout".to_string());
        let ep = self.default_socket();
        let health = format!(r#"{{"ok":true,"pid":1234,"version":"{}"}}"#, sw_cli::CLI_VERSION);
        self.with_systemd(true)
            .with_which("serialwrap", "/usr/local/bin/serialwrap")
            .with_which("serialwrapd", "/usr/local/bin/serialwrapd")
            .with_rpc_ok(&ep, "health.status", &health)
    }

    pub fn with_procs(mut self, procs: Vec<DaemonProc>) -> Self {
        self.procs = FakeProcScan::new(procs);
        self
    }

    /// 配置 `process` 替身（spawn 記錄與 spawn 計數；未配置時 `spawn` 相關 gate 無法使用）。
    pub fn with_process(mut self) -> Self {
        self.ensure_process();
        self
    }

    fn ensure_process(&mut self) -> &Arc<FakeProcessCtl> {
        self.process.get_or_insert_with(FakeProcessCtl::new)
    }

    fn ensure_test_service(&mut self, replace_real: bool) {
        if self.service.is_some() || (self.real_service && !replace_real) {
            return;
        }
        let modes = if self.windows {
            vec![
                SupervisionMode::OnDemand,
                SupervisionMode::WindowsTask,
                SupervisionMode::WindowsService,
            ]
        } else {
            vec![
                SupervisionMode::OnDemand,
                SupervisionMode::SystemdUser,
                SupervisionMode::SystemdSystem,
            ]
        };
        let service = FakeServiceCtl::new(modes);
        self.rpc.set_supervisor_hold_signal(service.hold_signal());
        self.service = Some(service);
    }

    /// 寫 bundle 的 Task 17 形狀 manifest（`components.daemon.launcher`）與 `bin/serialwrapd`；`current_exe` 不變。
    pub fn with_manifest_launcher(self) -> Self {
        self.write_bundle_manifest_with(&[("launcher", r#"{"path":"bin/serialwrapd"}"#)]);
        self.write_file(
            &self.bundle_dir().join("bin").join("serialwrapd"),
            b"launcher",
        );
        self
    }

    /// 寫 `components.daemon.runtime` 與 `py/bin/python3`（檔案形狀）。`pyver` 目前只記錄於 manifest 的 `python_version`，
    /// 腳本化 runtime 的 `-c` 版本查詢需 P4-T11 決定 argv 後再接（見 PR 說明的 gap）。
    pub fn with_manifest_runtime_ok(self, pyver: &str) -> Self {
        let body = format!(r#"{{"path":"py/bin/python3","python_version":"{pyver}"}}"#);
        self.write_bundle_manifest_with(&[("runtime", body.as_str())]);
        self.write_file(
            &self.bundle_dir().join("py").join("bin").join("python3"),
            b"runtime",
        );
        self
    }

    /// P4-T13 `doctor` 用：完整 release manifest（`write_complete_bundle`，bundle_version＝CLI 版本，`pairing` 與各 digest 一致）
    /// 與 daemon runtime 的 `-I -c` 查詢腳本（argv 逐字取自 `doctor_cmd` 的常數；`pyver` 為 `python` 檢查的輸出）。
    /// 不啟動 daemon；daemon 版本由呼叫端以 `with_rpc_ok` 另行註冊。
    pub fn with_doctor_bundle(self, pyver: &str) -> Self {
        self.with_doctor_bundle_except(pyver, &[])
    }

    /// 同 `with_doctor_bundle`，但 `skip` 列出的 `-c` 查詢不註冊預設成功腳本（由呼叫端以 `with_command*` 自行腳本化；
    /// 同 argv 的腳本是累加佇列，不能以後註冊覆寫先註冊者）。
    pub fn with_doctor_bundle_except(self, pyver: &str, skip: &[&str]) -> Self {
        let windows = self.windows;
        let target = if windows { "x86_64-pc-windows-gnu" } else { "x86_64-unknown-linux-gnu" };
        write_complete_bundle(&self.bundle_dir(), sw_cli::CLI_VERSION, target, windows, &[], &[]);
        let runtime = self
            .bundle_dir()
            .join("py")
            .join("bin")
            .join("python3")
            .to_string_lossy()
            .into_owned();
        let rt = runtime.as_str();
        let mut env = self;
        if !skip.contains(&sw_cli::doctor_cmd::PY_VERSION_CODE) {
            env = env.with_command(&[rt, "-I", "-c", sw_cli::doctor_cmd::PY_VERSION_CODE], 0, &format!("{pyver}\n"), "");
        }
        if !skip.contains(&sw_cli::doctor_cmd::PYYAML_CODE) {
            env = env.with_command(&[rt, "-I", "-c", sw_cli::doctor_cmd::PYYAML_CODE], 0, "6.0.1\n", "");
        }
        if !skip.contains(&sw_cli::doctor_cmd::PYSERIAL_CODE) {
            env = env.with_command(&[rt, "-I", "-c", sw_cli::doctor_cmd::PYSERIAL_CODE], 0, "3.5\n", "");
        }
        env
    }

    /// 每次 `run`／`run_raw` 的逾時參數（與 `calls()` 平行）。
    pub fn sysenv_timeouts(&self) -> Vec<Option<std::time::Duration>> {
        self.env.timeouts()
    }

    /// `(ep, method)` 的單一成功回應（`FakeRpcConnector::respond`）。
    pub fn with_rpc_ok(self, ep: &str, method: &str, line: &str) -> Self {
        self.rpc.respond(&self.isolated_endpoint(ep), method, line);
        self
    }

    /// `ep` 在第一次 `spawn_daemon` 之前一律 refused；之後 `methods` 回成功（`health.ping`→`OK_PING`、`session.list`→空清單）。
    pub fn with_rpc_after_spawn(mut self, ep: &str, methods: &[&str]) -> Self {
        let ep = self.isolated_endpoint(ep);
        let counter = self.ensure_process().spawn_counter();
        self.rpc.gate_on_spawn(&ep, counter);
        for method in methods {
            let line = match *method {
                "health.ping" => OK_PING,
                "session.list" => OK_SESSION_LIST,
                _ => OK_EMPTY,
            };
            self.rpc.respond(&ep, method, line);
        }
        self
    }

    /// `ep` 的第一次連線 refused，之後 `(ep, method)` 回 `line`（「refused 後 ok」）。
    pub fn with_rpc_refused_then_ok(self, ep: &str, method: &str, line: &str) -> Self {
        let ep = self.isolated_endpoint(ep);
        self.rpc.refuse_first(&ep, 1);
        self.rpc.respond(&ep, method, line);
        self
    }

    pub fn with_rpc_fail_after_send(self, ep: &str, method: &str) -> Self {
        self.rpc
            .fail_after_send(&self.isolated_endpoint(ep), method);
        self
    }

    pub fn with_rpc_recv_timeout(self, ep: &str, method: &str) -> Self {
        self.rpc.recv_timeout(&self.isolated_endpoint(ep), method);
        self
    }

    pub fn with_rpc_refused_kind(self, ep: &str, kind: PalErrorKind) -> Self {
        self.rpc.refuse_with(&self.isolated_endpoint(ep), kind);
        self
    }

    pub fn with_rpc_refused(self, ep: &str) -> Self {
        self.with_rpc_refused_kind(ep, PalErrorKind::ConnectionRefused)
    }

    /// `ep` 的 daemon 在第 `n` 次輪詢（RPC 連線，含）起拒絕，且 `pid` 在**同一次**連線計數翻轉時不存活（計畫 Task 1 替身規則）。
    ///
    /// 前 `n-1` 次連線為存活且 `session.list` 成功；第 `n` 次起 refused。pid 存活由 `FakeProcScan::alive_until_connect`
    /// 綁定同一計數器，故 `pid_alive` 與 ping 在同一次輪詢轉為不存活。
    pub fn with_daemon_alive_then_gone(mut self, ep: &str, pid: u32, n: usize) -> Self {
        let ep = self.isolated_endpoint(ep);
        self.rpc.respond(&ep, "health.ping", &health_response(pid));
        self.rpc
            .respond(&ep, "health.status", &health_response(pid));
        self.rpc.respond(&ep, "session.list", OK_SESSION_LIST);
        self.rpc.refuse_from_connect(&ep, n);
        self.procs = FakeProcScan::new(vec![daemon_proc(pid)]);
        self.procs
            .alive_until_connect(pid, Arc::clone(&self.rpc), &ep, n);
        self.active_daemon_endpoint = Some(ep);
        self.active_daemon_pid = Some(pid);
        self
    }

    pub fn with_daemon_alive_forever(mut self, ep: &str, pid: u32) -> Self {
        let ep = self.isolated_endpoint(ep);
        self.rpc.respond(&ep, "health.ping", &health_response(pid));
        self.rpc
            .respond(&ep, "health.status", &health_response(pid));
        let token = &sha256_hex(&format!("handoff-{pid}"))[..32];
        self.rpc.respond(
            &ep,
            "daemon.handoff_stop",
            &format!(r#"{{"ok":true,"stopping":true,"handoff_token":"{token}"}}"#),
        );
        self.rpc.respond(&ep, "mcu.status", r#"{"ok":true,"flashing":false}"#);
        self.rpc.respond(&ep, "session.list", OK_SESSION_LIST);
        self.procs = FakeProcScan::new(vec![daemon_proc(pid)]);
        self.procs.follow_rpc_relaunch(Arc::clone(&self.rpc), &ep);
        self.procs
            .alive_until_stopped(pid, Arc::clone(&self.rpc), &ep);
        self.active_daemon_endpoint = Some(ep);
        self.active_daemon_pid = Some(pid);
        self
    }

    /// 閒置 daemon（`flashing:false`、單一 COM0 閒置，無 busy）：`daemon.handoff_stop` 回 grant（32 hex token），
    /// 之後 `ep` 停止（refused），pid 隨之不存活；`daemon.stop` 同語意。
    pub fn with_idle_daemon(mut self, ep: &str, pid: u32) -> Self {
        self.ensure_process();
        self.ensure_test_service(false);
        let ep = self.isolated_endpoint(ep);
        self.rpc.respond(&ep, "health.ping", &health_response(pid));
        self.rpc
            .respond(&ep, "health.status", &health_response(pid));
        self.rpc.respond(&ep, "session.list", OK_SESSION_LIST);
        self.rpc
            .respond(&ep, "mcu.status", r#"{"ok":true,"flashing":false}"#);
        let token = &sha256_hex(&format!("handoff-{pid}"))[..32];
        let grant = format!(r#"{{"ok":true,"stopping":true,"handoff_token":"{token}"}}"#);
        self.rpc.respond(&ep, "daemon.handoff_stop", &grant);
        self.rpc
            .respond(&ep, "daemon.stop", r#"{"ok":true,"stopping":true}"#);
        self.rpc.stop_after_send(&ep, "daemon.handoff_stop");
        self.rpc.stop_after_send(&ep, "daemon.stop");
        self.procs = FakeProcScan::new(vec![daemon_proc(pid)]);
        self.procs.follow_rpc_relaunch(Arc::clone(&self.rpc), &ep);
        self.procs
            .alive_until_stopped(pid, Arc::clone(&self.rpc), &ep);
        self.active_daemon_endpoint = Some(ep);
        self.active_daemon_pid = Some(pid);
        if let Some(service) = &self.service {
            service.stop_rpc_on_hard_stop(
                Arc::clone(&self.rpc),
                self.active_daemon_endpoint.as_deref().unwrap_or_default(),
            );
        }
        self.configure_handoff_relaunch();
        self
    }

    /// OD-48：0.3.2 Python daemon 替身。RPC 形狀對齊 `v0.3.2:sw_core/daemon.py`／`service.py`：
    /// `daemon.handoff_stop` → `METHOD_NOT_FOUND`（帶 `version`）；`daemon.stop` 立即回 `stopping:true` 後 endpoint 消失，
    /// 但行程在 `with_legacy_linger` 指定時間後才結束（socket 消失不代表 UART 已釋放）。行程辨識為 pipx 的 Python script。
    pub fn with_legacy_daemon(mut self, ep: &str, pid: u32) -> Self {
        self.ensure_process();
        self.ensure_test_service(false);
        let ep = self.isolated_endpoint(ep);
        let socket = ep.strip_prefix("unix://").unwrap_or(&ep).to_string();
        self.rpc.respond(&ep, "health.ping", r#"{"ok":true,"pong":true,"version":"0.3.2"}"#);
        self.rpc.respond(&ep, "health.status", &format!(r#"{{"ok":true,"pid":{pid},"version":"0.3.2","wal":{{"healthy":true}}}}"#));
        self.rpc.respond(&ep, "mcu.status", r#"{"ok":true,"flashing":false,"flashing_com":null,"version":"0.3.2"}"#);
        self.rpc.respond(&ep, "session.list", LEGACY_SESSION_LIST);
        self.rpc.respond(&ep, "daemon.handoff_stop", LEGACY_METHOD_NOT_FOUND);
        self.rpc.respond(&ep, "daemon.stop", r#"{"ok":true,"stopping":true}"#);
        self.rpc.stop_after_send(&ep, "daemon.stop");
        self.procs = FakeProcScan::new(vec![DaemonProc {
            pid,
            socket: Some(socket.clone()),
            tree: vec![ProcIdentity { pid, start_ticks: Some(70_000 + u64::from(pid)) }],
        }]);
        self.procs.follow_rpc_relaunch(Arc::clone(&self.rpc), &ep);
        self.procs.set_current_uid(Some(LEGACY_UID));
        self.procs.set_details(legacy_details(pid, &socket));
        let linger = Arc::new(std::sync::atomic::AtomicU64::new(0));
        let latch = Arc::new(Mutex::new(None::<u64>));
        let rpc = Arc::clone(&self.rpc);
        let clock = Arc::clone(&self.clock);
        let stop_signal = self.service.as_ref().expect("test service configured").stop_signal();
        let gate_ep = ep.clone();
        let gate_linger = Arc::clone(&linger);
        self.procs.alive_while(pid, Arc::new(move || {
            let stopped = rpc.request_count(&gate_ep, "daemon.stop") > 0 || stop_signal.load(std::sync::atomic::Ordering::SeqCst);
            if !stopped {
                return true;
            }
            let now = clock.mono_ns();
            let mut at = latch.lock().unwrap_or_else(|e| e.into_inner());
            let since = *at.get_or_insert(now);
            let linger = gate_linger.load(std::sync::atomic::Ordering::SeqCst);
            linger == u64::MAX || now.saturating_sub(since) < linger
        }));
        if let Some(service) = &self.service {
            service.stop_rpc_on_stop(Arc::clone(&self.rpc), &ep);
        }
        self.legacy_linger_ns = Some(linger);
        self.active_daemon_endpoint = Some(ep);
        self.active_daemon_pid = Some(pid);
        self
    }

    /// OD-48 D7：0.3.x daemon 停止後由 setup 啟動的新版 daemon（列出同一組 session、支援 `daemon.handoff_stop`）。
    pub fn with_new_daemon_after_legacy(self, v: &str) -> Self {
        let t = self.with_new_daemon_reporting_version(v);
        let ep = t.active_daemon_endpoint.clone().expect("with_new_daemon_after_legacy 須在 with_legacy_daemon 之後");
        let token = &sha256_hex("handoff-5252")[..32];
        let sessions = r#"{"ok":true,"sessions":[{"com":"COM0","state":"READY","foreground_busy":false,"outstanding_commands":0,"interactive_session_id":null,"recovering":false,"attached_real_path":"/dev/ttyUSB0"}]}"#;
        t.rpc.respond_after_relaunch_seq(&ep, "session.list", &[sessions, sessions]);
        t.rpc.respond_after_relaunch(&ep, "daemon.handoff_stop", &format!(r#"{{"ok":true,"stopping":true,"handoff_token":"{token}"}}"#));
        t
    }

    /// OD-48 D1：改寫 0.3.x daemon 替身的行程辨識資訊（例如 bundle 路徑、原生執行檔）。
    pub fn with_legacy_details(self, edit: impl FnOnce(&mut sw_pal::proc_scan::ProcDetails)) -> Self {
        let pid = self.active_daemon_pid.expect("with_legacy_details 須在 with_legacy_daemon 之後");
        let ep = self.active_daemon_endpoint.clone().unwrap_or_default();
        let mut details = legacy_details(pid, ep.strip_prefix("unix://").unwrap_or(&ep));
        edit(&mut details);
        self.procs.set_details(details);
        self
    }

    /// OD-48 D3.2：另一個 serialwrapd 實例（`uid` 決定是否與 setup 同一使用者）。
    pub fn with_other_serialwrapd(self, pid: u32, socket: Option<&str>, uid: u32) -> Self {
        self.procs.add_instance(DaemonProc {
            pid,
            socket: socket.map(str::to_string),
            tree: vec![ProcIdentity { pid, start_ticks: Some(80_000 + u64::from(pid)) }],
        });
        let mut details = legacy_details(pid, socket.unwrap_or(""));
        details.uid = Some(uid);
        self.procs.set_details(details);
        self
    }

    /// OD-48 D5：0.3.x daemon 在停止後仍存活 `linger`（`None`＝永不結束，模擬 executor 收尾逾時）。
    pub fn with_legacy_linger(self, linger: Option<std::time::Duration>) -> Self {
        let ns = linger.map_or(u64::MAX, |value| value.as_nanos().min(u64::MAX as u128 - 1) as u64);
        self.legacy_linger_ns
            .as_ref()
            .expect("with_legacy_linger 須在 with_legacy_daemon 之後")
            .store(ns, std::sync::atomic::Ordering::SeqCst);
        self
    }

    /// OD-48 D4：0.3.x daemon 為 `mode` 這個 systemd unit 的 MainPID。
    pub fn with_legacy_main_pid(self, mode: &str) -> Self {
        let pid = self.active_daemon_pid.expect("with_legacy_main_pid 須在 with_legacy_daemon 之後");
        self.service().set_main_pid(&mode.into_supervision_mode(), Ok(Some(pid)));
        self
    }

    /// OD-48 D5.3：每次 `daemon.stop` 後依序以 `pids` 重新拉起 0.3.x daemon（例如 minicom_router 的 AUTO_START_DAEMON）。
    pub fn with_legacy_relaunches(self, pids: &[u32]) -> Self {
        let ep = self.active_daemon_endpoint.clone().expect("with_legacy_relaunches 須在 with_legacy_daemon 之後");
        let socket = ep.strip_prefix("unix://").unwrap_or(&ep).to_string();
        self.rpc.relaunch_after_send(&ep, "daemon.stop", pids);
        self.rpc.respond_relaunch_template(&ep, "health.status", pids, r#"{"ok":true,"pid":{pid},"version":"0.3.2","wal":{"healthy":true}}"#);
        self.rpc.respond_relaunch_template(&ep, "health.ping", pids, r#"{"ok":true,"pong":true,"version":"0.3.2"}"#);
        self.rpc.respond_relaunch_template(&ep, "daemon.handoff_stop", pids, LEGACY_METHOD_NOT_FOUND);
        for (index, pid) in pids.iter().copied().enumerate() {
            self.procs.set_details(legacy_details(pid, &socket));
            let rpc = Arc::clone(&self.rpc);
            let gate_ep = ep.clone();
            self.procs.alive_while(pid, Arc::new(move || rpc.request_count(&gate_ep, "daemon.stop") < index + 2));
        }
        self
    }

    /// 以本 TestEnv 的預設 POSIX endpoint 配置閒置 daemon。
    pub fn with_idle_daemon_default(self, pid: u32) -> Self {
        let endpoint = self.default_socket();
        self.with_idle_daemon(&endpoint, pid)
    }

    /// 對本 TestEnv 的預設 POSIX endpoint 配置單一 RPC 成功回應。
    pub fn with_rpc_ok_default(self, method: &str, line: &str) -> Self {
        let endpoint = self.default_socket();
        self.with_rpc_ok(&endpoint, method, line)
    }

    /// 將本 TestEnv 的預設 POSIX endpoint 設為連線拒絕。
    pub fn with_rpc_refused_default(self) -> Self {
        let endpoint = self.default_socket();
        self.with_rpc_refused(&endpoint)
    }

    /// 將下一個被拒絕的 daemon 連線標記為等待流程的 `health.ping#gone` 事件。
    pub fn with_gone_ping_event(self) -> Self {
        if let Some(endpoint) = self.active_daemon_endpoint.as_deref() {
            self.rpc.refused_connect_as(endpoint, "health.ping");
        }
        self
    }

    /// 設定更新後 daemon 的版本回應；若舊 daemon 正在同一 endpoint 運作，保留舊回應直到 handoff 與 spawn。
    pub fn with_new_daemon_reporting_version(mut self, v: &str) -> Self {
        let endpoint = if self.windows { Self::win_endpoint().to_string() } else { self.default_socket() };
        let ep = self.isolated_endpoint(&endpoint);
        let old_sessions = self.rpc.configured_response(&ep, "session.list")
            .unwrap_or_else(|| OK_SESSION_LIST.to_string());
        let current_state = std::fs::read_to_string(&self.root().current_file).ok()
            .and_then(|contents| loads(&contents).ok())
            .and_then(|value| match value {
                PyValue::Dict(value) => {
                    let current = value.get("bundle_version").and_then(|item| match item { PyValue::Str(version) => Some(version.clone()), _ => None });
                    let previous = value.get("previous").and_then(|item| match item { PyValue::Dict(previous) => previous.get("bundle_version").and_then(|item| match item { PyValue::Str(version) => Some(version.clone()), _ => None }), _ => None });
                    Some((current, previous))
                }
                _ => None,
            });
        let current = current_state.as_ref().and_then(|(current, _)| current.clone()).unwrap_or_else(|| v.to_string());
        let bundle_version = std::fs::read_to_string(self.bundle_dir().join("release-manifest.json")).ok()
            .and_then(|contents| loads(&contents).ok())
            .and_then(|value| match value { PyValue::Dict(value) => value.get("bundle_version").and_then(|item| match item { PyValue::Str(version) => Some(version.clone()), _ => None }), _ => None })
            .or_else(|| current_state.as_ref().and_then(|(_, previous)| previous.clone()))
            .unwrap_or_else(|| v.to_string());
        let counter = self.ensure_process().spawn_counter();
        if let Some(service) = &self.service { service.set_start_spawn_counter(Arc::clone(&counter)); }
        let env_counter = Arc::clone(&counter);
        self = self.push_env_op(Box::new(move |builder| builder.start_spawn_counter(Arc::clone(&env_counter))));
        if self.active_daemon_endpoint.as_deref() == Some(ep.as_str()) {
            self.rpc.respond_after_relaunch(&ep, "health.ping", OK_PING);
            if self.rpc.configured_response(&ep, "health.status").is_some_and(|line| line.contains("\"version\":\"test\"")) {
                self.rpc.respond(&ep, "health.status", &format!(r#"{{"ok":true,"pid":{},"version":"{}","wal":{{"healthy":true}}}}"#, self.active_daemon_pid.unwrap_or(5252), current));
            }
        } else {
            self.rpc.clear_refusal(&ep);
            self.rpc.gate_on_spawn(&ep, Arc::clone(&counter));
            self.rpc.respond(&ep, "health.ping", OK_PING);
            self.rpc.respond(
                &ep,
                "health.status",
                &format!(r#"{{"ok":true,"pid":5252,"version":"{v}","wal":{{"healthy":true}}}}"#),
            );
            let token = &sha256_hex("handoff-5252")[..32];
            self.rpc.respond(
                &ep,
                "daemon.handoff_stop",
                &format!(r#"{{"ok":true,"stopping":true,"handoff_token":"{token}"}}"#),
            );
            self.rpc.respond(&ep, "session.list", OK_SESSION_LIST);
        }
        self.rpc.revive_after_spawn(&ep, counter, 5252);
        self.rpc.stop_after_send(&ep, "daemon.handoff_stop");
        self.rpc.respond(&ep, "alias.list", r#"{"ok":true,"aliases":[]}"#);
        self.rpc.respond(&ep, "device.list", r#"{"ok":true,"devices":[]}"#);
        let cli_name = if self.windows { "serialwrap.exe" } else { "serialwrap" };
        let target_cli = self.root().versions.join(&bundle_version).join("bin").join(cli_name).to_string_lossy().into_owned();
        let output = format!("serialwrap {v}\n");
        self = self.with_command(&[&target_cli, "--version"], 0, &output, "");
        if current != bundle_version {
            let previous_cli = self.root().versions.join(&current).join("bin").join(cli_name).to_string_lossy().into_owned();
            let previous_output = format!("serialwrap {current}\n");
            self = self.with_command(&[&previous_cli, "--version"], 0, &previous_output, "");
        }
        let target_status = format!(r#"{{"ok":true,"pid":5252,"version":"{v}","wal":{{"healthy":true}}}}"#);
        let restored_status = format!(r#"{{"ok":true,"pid":5252,"version":"{current}","wal":{{"healthy":true}}}}"#);
        self.rpc.respond_after_relaunch_seq(&ep, "health.status", &[&target_status, &restored_status]);
        self.rpc.respond_after_relaunch_seq(&ep, "session.list", &[OK_SESSION_LIST, &old_sessions]);
        self.rpc.respond_after_relaunch_seq(&ep, "alias.list", &[r#"{"ok":true,"aliases":[]}"#, r#"{"ok":true,"aliases":[]}"#]);
        self.rpc.respond_after_relaunch_seq(&ep, "device.list", &[r#"{"ok":true,"devices":[]}"#, r#"{"ok":true,"devices":[]}"#]);
        self
    }

    /// 設定只由下一次 spawn 後的新 daemon 回傳的 RPC 回應。
    pub fn with_rpc_after_relaunch(self, method: &str, line: &str) -> Self {
        let endpoint = if self.windows { Self::win_endpoint().to_string() } else { self.default_socket() };
        self.rpc.respond_after_relaunch(&endpoint, method, line);
        self
    }

    /// 依序設定重新拉起後的 RPC 回應；用盡後重複最後一筆。
    pub fn with_rpc_after_relaunch_seq(self, method: &str, lines: &[&str]) -> Self {
        let endpoint = if self.windows { Self::win_endpoint().to_string() } else { self.default_socket() };
        self.rpc.respond_after_relaunch_seq(&endpoint, method, lines);
        self
    }

    /// 新版 daemon spawn 後 RPC 立即拒絕，但 process scan 仍看見新 PID。
    pub fn with_new_daemon_rpc_down(self) -> Self {
        let mut env = self.with_new_daemon_reporting_version("0.4.1");
        let endpoint = if env.windows { Self::win_endpoint().to_string() } else { env.default_socket() };
        let counter = env.ensure_process().spawn_counter();
        env.rpc.refuse_after_spawn(&endpoint, counter);
        env.procs.follow_rpc_relaunch(Arc::clone(&env.rpc), &endpoint);
        env
    }

    /// 測試端 endpoint 映射（恆等）。RPC 替身不接觸真實 socket，故 endpoint 只是鍵；TCP 與 unix 皆原樣保留，
    /// 相異的 endpoint 永不合併（審查修正：舊版把所有 unix endpoint 併成同一個，會遮蔽 SC-10 正規化與
    /// `DAEMON_ALREADY_RUNNING_ELSEWHERE`）。不帶 scheme 的路徑視為 unix。
    pub fn isolated_endpoint(&self, ep: &str) -> String {
        if ep.starts_with("tcp://") {
            return ep.to_string();
        }
        let path = ep
            .strip_prefix("unix://")
            .or_else(|| ep.strip_prefix("unix:"))
            .unwrap_or(ep);
        format!("unix://{path}")
    }

    /// 寫 `<run_dir>/remote/<port>.json`（DISK_STABLE 形狀；缺 `listen_port` 時補上），並配置 `FakeTunnel`
    /// （計畫表列：remote 檔案與 FakeTunnel 一併設定；未配置時 `tunnel()` 會 panic）。
    pub fn with_registry_state(mut self, port: u16, mut state: PyDict) -> Self {
        self.tunnel.get_or_insert_with(|| FakeTunnel::new(true));
        if state.get("listen_port").is_none() {
            state.insert("listen_port", PyValue::Int(PyInt::Small(i128::from(port))));
        }
        let dir = self.run_dir().join("remote");
        std::fs::create_dir_all(&dir).expect("remote 目錄建立失敗");
        std::fs::write(
            dir.join(format!("{port}.json")),
            dumps_dict(&state, &DISK_STABLE),
        )
        .expect("remote registry 寫入失敗");
        self
    }

    pub fn with_benches_yaml(self, s: &str) -> Self {
        std::fs::write(self.config_dir().join("benches.yaml"), s).expect("benches.yaml 寫入失敗");
        self
    }

    pub fn with_bench_state(self, s: &str) -> Self {
        std::fs::write(self.bench_state_path(), s).expect("bench_state.json 寫入失敗");
        self
    }

    /// 配置 remote 開通替身與可用的 ssh／autossh；依平台設定 ControlMaster 能力。
    pub fn with_tunnel_ready(mut self) -> Self {
        let supports_control_master = !self.windows;
        self.tunnel = Some(FakeTunnel::new(supports_control_master));
        self.push_env_op(Box::new(|b| {
            b.which("ssh", "/usr/bin/ssh")
                .which("autossh", "/usr/bin/autossh")
        }))
    }

    /// 轉給 `FakeSysEnvBuilder::command`：腳本化 runner 對 `argv` 的回應（P4-T8 使用）。
    pub fn with_command(self, argv: &[&str], rc: i32, out: &str, err: &str) -> Self {
        let argv: Vec<String> = argv.iter().map(|s| s.to_string()).collect();
        let (out, err) = (out.to_string(), err.to_string());
        self.push_env_op(Box::new(move |b| {
            let refs: Vec<&str> = argv.iter().map(String::as_str).collect();
            b.command(&refs, rc, &out, &err)
        }))
    }

    /// `with_command` 的位元組版本（stdout 可含非 UTF-8；P4-T7 `start_env_file_non_utf8_value_preserved` 使用）。
    pub fn with_command_raw(self, argv: &[&str], rc: i32, out: &[u8], err: &[u8]) -> Self {
        let argv: Vec<String> = argv.iter().map(|s| s.to_string()).collect();
        let (out, err) = (out.to_vec(), err.to_vec());
        self.push_env_op(Box::new(move |b| {
            let refs: Vec<&str> = argv.iter().map(String::as_str).collect();
            b.command_raw(&refs, rc, &out, &err)
        }))
    }

    /// 以混合順序腳本化 stdout／stderr，對齊 Python `stderr=STDOUT` runner。
    pub fn with_command_merged(self, argv: &[&str], rc: i32, output: &str) -> Self {
        let argv: Vec<String> = argv.iter().map(|s| s.to_string()).collect();
        let output = output.as_bytes().to_vec();
        self.push_env_op(Box::new(move |b| {
            let refs: Vec<&str> = argv.iter().map(String::as_str).collect();
            b.command_merged(&refs, rc, &output)
        }))
    }

    /// 指定 argv 的 runner 回傳 PAL 例外（例如探測時 ssh 可執行檔消失）。
    pub fn with_command_err(self, argv: &[&str], kind: PalErrorKind, message: &str) -> Self {
        let argv: Vec<String> = argv.iter().map(|s| s.to_string()).collect();
        let message = message.to_string();
        self.push_env_op(Box::new(move |b| {
            let refs: Vec<&str> = argv.iter().map(String::as_str).collect();
            b.command_err(&refs, kind, &message)
        }))
    }

    /// 轉給 `FakeSysEnvBuilder::command_seq`：同一 `argv` 依序回應（P4-T8 使用）。
    pub fn with_command_seq(self, argv: &[&str], seq: &[(i32, &str, &str)]) -> Self {
        let argv: Vec<String> = argv.iter().map(|s| s.to_string()).collect();
        let seq: Vec<(i32, String, String)> = seq
            .iter()
            .map(|(rc, out, err)| (*rc, out.to_string(), err.to_string()))
            .collect();
        self.push_env_op(Box::new(move |b| {
            let refs: Vec<&str> = argv.iter().map(String::as_str).collect();
            let owned: Vec<(i32, &str, &str)> = seq
                .iter()
                .map(|(rc, out, err)| (*rc, out.as_str(), err.as_str()))
                .collect();
            b.command_seq(&refs, &owned)
        }))
    }

    /// 安裝根已於 `new` 建立；此處為明示用的 no-op（與計畫 API 對齊）。
    pub fn with_install_root(self) -> Self {
        self
    }

    /// 建立目前版本與 current 檔；POSIX 寫 `<root>/current-release.json`，Windows 寫 `<root>/bin/current-release.json`。
    pub fn with_current(self, v: &str, prev: Option<&str>) -> Self {
        let root = self.root();
        let target = if self.windows { "x86_64-pc-windows-gnu" } else { "x86_64-unknown-linux-gnu" };
        for version in std::iter::once(v).chain(prev.into_iter()) {
            let version_dir = root.versions.join(version);
            if !version_dir.join("release-manifest.json").exists() {
                write_complete_bundle(&version_dir, version, target, self.windows, &[], &[]);
            }
        }
        let previous = prev.map_or_else(
            || ",\"previous\":null".to_string(),
            |version| {
                format!(",\"previous\":{{\"bundle_version\":\"{version}\",\"dir\":\"versions/{version}\"}}")
            },
        );
        let backup_dir = prev.map(|previous| {
            let dir = root.backups.join(format!("{previous}-to-{v}-history"));
            std::fs::create_dir_all(&dir).expect("Previous 歷史備份目錄建立失敗");
            let config = std::fs::read(self.config_path()).unwrap_or_else(|_| b"supervision_mode: on-demand\n".to_vec());
            let mode = String::from_utf8_lossy(&config).lines().find_map(|line| {
                line.split_once(':').filter(|(key, _)| key.trim() == "supervision_mode").map(|(_, value)| value.trim().to_string())
            }).unwrap_or_else(|| "on-demand".into());
            self.write_history_backup(&dir, &mode);
            dir.to_string_lossy().into_owned()
        });
        let backup = backup_dir.as_deref().map_or_else(String::new, |path| {
            let escaped = path.replace('\\', "\\\\");
            format!(",\"backup_dir\":\"{escaped}\"")
        });
        let current = format!(r#"{{"bundle_version":"{v}","dir":"versions/{v}"{previous}{backup}}}"#);
        self.write_file(&root.current_file, current.as_bytes());
        for name in root.entry_names.iter() {
            let source = root.versions.join(v).join("bin").join(name);
            let target = root.bin.join(name);
            let bytes = std::fs::read(&source).unwrap_or_else(|_| format!("current-{v}").into_bytes());
            self.write_file(&target, &bytes);
        }
        self
    }

    /// 改寫測試 current 的 Previous 備份監管快照，模擬歷史安裝時的實際模式。
    pub fn with_previous_backup_mode(&self, mode: &str) {
        let root = self.root();
        let current = std::fs::read_to_string(&root.current_file).expect("current-release.json 讀取失敗");
        let value = loads(&current).expect("current-release.json 解析失敗");
        let backup = match value {
            PyValue::Dict(value) => value.get("backup_dir").and_then(|item| match item { PyValue::Str(text) => Some(PathBuf::from(text)), _ => None }),
            _ => None,
        }.expect("current-release.json 缺少歷史 backup_dir");
        self.write_history_backup(&backup, mode);
    }

    fn write_history_backup(&self, dir: &Path, mode: &str) {
        let endpoint = if self.windows { Self::win_endpoint().to_string() } else { self.default_socket() };
        let registration_path = match mode {
            "windows-task" => Some(self.root().supervisor.join("serialwrap.task.xml")),
            "systemd-user" => Some(self.config_dir().join("systemd/user/serialwrap.service")),
            "systemd-system" => Some(self.home().join(".local/share/serialwrap/serialwrap.service")),
            _ => None,
        };
        let mut registration = PyDict::new();
        registration.insert("enabled", if mode == "on-demand" { PyValue::Null } else { PyValue::Bool(true) });
        registration.insert("endpoint", PyValue::Str(endpoint.clone()));
        registration.insert("mode", PyValue::Str(mode.to_string()));
        registration.insert("registration", registration_path.as_ref().map(|path| PyValue::Str(path.to_string_lossy().into_owned())).unwrap_or(PyValue::Null));
        registration.insert("running", PyValue::Bool(false));
        let registration_bytes = dumps_dict(&registration, &DISK_STABLE).into_bytes();
        self.write_file(&dir.join("registration.json"), &registration_bytes);
        let config = format!("supervision_mode: {mode}\n");
        self.write_file(&dir.join("config.yaml"), config.as_bytes());
        if mode == "windows-task" {
            let xml = sw_pal::service_ctl::render_task_xml(&sw_pal::service_ctl::TaskXmlSpec {
                name: "serialwrap".into(),
                user_id: self.env.user_identity(),
                command: self.root().bin.join("serialwrapd.exe").to_string_lossy().into_owned(),
                arguments: format!("--socket {endpoint}"),
                working_dir: self.root().bin.to_string_lossy().into_owned(),
                enabled: true,
            });
            self.write_file(&dir.join("registered.task.xml"), &sw_pal::service_ctl::encode_utf16le_bom(&xml));
        } else if mode == "systemd-user" {
            let unit = format!("[Unit]\nDescription=serialwrap\n[Service]\nExecStart=/usr/local/bin/serialwrapd --socket {endpoint}\n[Install]\nWantedBy=default.target\n");
            self.write_file(&dir.join("registered.unit"), unit.as_bytes());
        }
        let mut files = Vec::new();
        for name in ["config.yaml", "registration.json", "registered.task.xml", "registered.unit"] {
            let path = dir.join(name);
            if !path.exists() { continue; }
            let bytes = std::fs::read(&path).expect("歷史備份檔讀取失敗");
            files.push(PyValue::Dict(PyDict::from_pairs(vec![
                ("path".into(), PyValue::Str(path.to_string_lossy().into_owned())),
                ("relative".into(), PyValue::Str(name.into())),
                ("sha256".into(), PyValue::Str(sw_proto::digest::sha256_hex(&bytes))),
            ])));
        }
        let manifest = PyDict::from_pairs(vec![
            ("files".into(), PyValue::List(files)),
            ("schema_version".into(), PyValue::Int(PyInt::Small(1))),
        ]);
        self.write_file(&dir.join("manifest.json"), dumps_dict(&manifest, &DISK_STABLE).as_bytes());
    }

    /// `versions/<v>/bin/serialwrap` 安裝版本目錄（不改 current）。
    pub fn with_installed(self, v: &str) -> Self {
        let root = self.root();
        let version_dir = root.versions.join(v);
        if !version_dir.join("release-manifest.json").exists() {
            let target = if self.windows { "x86_64-pc-windows-gnu" } else { "x86_64-unknown-linux-gnu" };
            write_complete_bundle(&version_dir, v, target, self.windows, &[], &[]);
        }
        self
    }

    /// bundle 根的 `bin/serialwrap` 與 `release-manifest.json`（manifest 形狀同 `mkbundle.sh`，sha256 為真值）。
    pub fn with_bundle(mut self, v: &str) -> Self {
        self.ensure_test_service(false);
        let target = if self.windows {
            "x86_64-pc-windows-gnu"
        } else {
            "x86_64-unknown-linux-gnu"
        };
        write_complete_bundle(&self.bundle_dir(), v, target, self.windows, &[], &[]);
        self
    }

    /// 以 `FaultFs` 包住 fs；`replace` 來源以 `s` 結尾時失敗（SC-12 中途失敗路徑）。
    pub fn with_fs_fault_replace_suffix(mut self, s: &str) -> Self {
        let faulty = FaultFs::wrap(Arc::clone(&self.fs));
        faulty.fail_replace_when_src_ends_with(s);
        faulty.fail_writes_to_suffix(s);
        self.fs = faulty;
        self
    }

    /// 持有 install root 的安裝鎖，模擬另一個 updater 正在執行。
    pub fn with_install_lock_holder(self) -> Self {
        let lock_path = self.install_root().join(".install.lock");
        let guard = self.fs.file_lock_exclusive(&lock_path, false).expect("測試安裝鎖取得失敗");
        self.retained_locks.lock().unwrap_or_else(|error| error.into_inner()).push(guard);
        self
    }

    pub fn acquire_install_lock(&mut self) {
        let lock_path = self.install_root().join(".install.lock");
        let guard = self.fs.file_lock_exclusive(&lock_path, false).expect("測試安裝鎖取得失敗");
        self.retained_locks.lock().unwrap_or_else(|error| error.into_inner()).push(guard);
    }

    /// 以 FaultFs 包住 fs；來源路徑含 part 時讓 replace 失敗。
    pub fn with_fs_fault_replace_source_contains(mut self, part: &str) -> Self {
        let faulty = FaultFs::wrap(Arc::clone(&self.fs));
        faulty.fail_replace_when_src_contains(part);
        self.fs = faulty;
        self
    }

    /// 以 `FaultFs` 包住 fs；寫入目標以 `s` 結尾時失敗（平台中立的「狀態檔不可寫」）。
    pub fn with_fs_fault_write_suffix(mut self, s: &str) -> Self {
        let faulty = FaultFs::wrap(Arc::clone(&self.fs));
        faulty.fail_writes_to_suffix(s);
        self.fs = faulty;
        self
    }

    /// 中斷注入點（R1-F5）。`run` 會因未接線而 panic（見 `run`），避免靜默略過。
    pub fn with_crash_at(mut self, point: &str) -> Self {
        assert!(CRASH_POINTS.contains(&point), "未知的 crash point：{point}");
        self.crash_point = Some(point.to_string());
        self
    }

    /// OD-48 legacy 停止專用的中斷注入點（`LEGACY_CRASH_POINTS`；不屬凍結的 `CRASH_POINTS` 迴圈）。
    pub fn with_legacy_crash_at(mut self, point: &str) -> Self {
        assert!(LEGACY_CRASH_POINTS.contains(&point), "未知的 legacy crash point：{point}");
        self.crash_point = Some(point.to_string());
        self
    }

    pub fn clear_crash(&mut self) -> &mut Self {
        self.crash_point = None;
        self
    }

    /// 有狀態監管模型：寫 config `supervision_mode` 並設定 `FakeServiceCtl` 的 enabled／running（`query_now` 可讀）。
    pub fn with_supervisor_state(
        mut self,
        mode: impl IntoSupervisionMode,
        enabled: bool,
        running: bool,
    ) -> Self {
        let mode = mode.into_supervision_mode();
        let name = match &mode {
            SupervisionMode::OnDemand => "on-demand".to_string(),
            SupervisionMode::SystemdUser => "systemd-user".to_string(),
            SupervisionMode::SystemdSystem => "systemd-system".to_string(),
            SupervisionMode::WindowsTask => "windows-task".to_string(),
            SupervisionMode::WindowsService => "windows-service".to_string(),
            SupervisionMode::Other(s) => s.clone(),
        };
        self.set_config_key("supervision_mode", &name);
        self.ensure_test_service(true);
        let svc = self.service.as_ref().expect("test service configured").clone();
        self.rpc.set_supervisor_hold_signal(svc.hold_signal());
        svc.set_model(Some(enabled), running);
        if mode == SupervisionMode::WindowsTask {
            let root = self.root();
            let path = root.supervisor.join("serialwrap.task.xml");
            let xml = sw_pal::service_ctl::render_task_xml(&sw_pal::service_ctl::TaskXmlSpec {
                name: "serialwrap".into(),
                user_id: self.env.user_identity(),
                command: root.bin.join("serialwrapd.exe").to_string_lossy().into_owned(),
                arguments: "--socket tcp://127.0.0.1:48700".into(),
                working_dir: root.bin.to_string_lossy().into_owned(),
                enabled,
            });
            self.write_file(&path, &sw_pal::service_ctl::encode_utf16le_bom(&xml));
        } else if mode == SupervisionMode::SystemdUser {
            let path = self.home().join(".config/systemd/user/serialwrap.service");
            self.write_file(&path, b"[Unit]\nDescription=serialwrap daemon\n[Service]\nExecStart=%h/.local/bin/serialwrapd\nRestart=on-failure\n[Install]\nWantedBy=default.target\n");
        }
        self
    }

    /// 下一次 `spawn_daemon` 的 daemon 立即以 `rc` 結束（poll 腳本的第一筆）。
    pub fn with_spawn_exits(mut self, rc: i64) -> Self {
        self.ensure_process().poll_script(vec![Some(rc)]);
        self
    }

    /// 以真實的 PAL 監管實作（`CompositeServiceCtl`：systemd、windows-task、windows-service）包在 `FakeSysEnv` 上，
    /// 監管命令的 argv 進入 `calls()`（P4-T7 的 systemd／schtasks 路由斷言使用）。與 `with_supervisor_state` 互斥（後者優先）。
    pub fn with_real_service(mut self) -> Self {
        self.real_service = true;
        self
    }

    fn real_service_ctl(&self) -> Arc<dyn sw_pal::service_ctl::ServiceCtl> {
        let env: Arc<dyn SysEnv> = self.env.clone();
        let fs: Arc<dyn FsOps> = self.fs.clone();
        let clock: Arc<dyn Clock> = self.clock.clone();
        let modes = if self.windows {
            vec![
                SupervisionMode::OnDemand,
                SupervisionMode::WindowsTask,
                SupervisionMode::WindowsService,
            ]
        } else {
            vec![
                SupervisionMode::OnDemand,
                SupervisionMode::SystemdUser,
                SupervisionMode::SystemdSystem,
            ]
        };
        Arc::new(CompositeServiceCtl::new(
            modes,
            SystemdCtl::new(Arc::clone(&env), Arc::clone(&fs), Arc::clone(&clock)),
            WindowsTaskCtl::new(Arc::clone(&env), Arc::clone(&fs), Arc::clone(&clock)),
            WindowsServiceCtl::new(env, clock),
        ))
    }

    /// 以與 `run` 相同的平台與環境建立 `CliCtx` 後交給 `f`（直接呼叫 `graceful_stop_with` 等內部 API 的測試用，P4-T7 承接）。
    /// 輸出寫入測試端暫存緩衝，`f` 返回後丟棄；需要輸出內容的測試請以 `f` 的回傳值取出。
    pub fn with_cli_ctx<R>(&self, f: impl FnOnce(&mut CliCtx<'_>) -> R) -> R {
        let pf = self.platform();
        let pairs = pf.env.vars();
        let env = match pf.caps.path_flavor {
            sw_pal::PalPathFlavor::Windows => EnvSnapshot::from_pairs_fold_upper(pairs),
            sw_pal::PalPathFlavor::Posix => EnvSnapshot::from_pairs(pairs),
        };
        let native = NativeTraits {
            default_endpoint_is_tcp: pf.caps.native_backend == sw_pal::BackendKind::Win,
            path_flavor: match pf.caps.path_flavor {
                sw_pal::PalPathFlavor::Windows => PathFlavor::Windows,
                sw_pal::PalPathFlavor::Posix => PathFlavor::Posix,
            },
        };
        let home = SysEnvHome(pf.env.as_ref());
        let paths = RuntimePaths::resolve(&env, native, &home)
            .unwrap_or_else(|_| panic!("with_cli_ctx: RuntimePaths 解析失敗"));
        let mut out: Vec<u8> = Vec::new();
        let mut err: Vec<u8> = Vec::new();
        let mut ctx = CliCtx {
            p: &pf,
            env,
            paths,
            io: TextIo::new(&mut out, &mut err, "\n"),
            verbose: 0,
            cli_version: "test",
            autostart_lock: None,
        };
        let result = f(&mut ctx);
        if let Some(lock) = ctx.autostart_lock.take() {
            self.retained_locks
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .push(lock);
        }
        result
    }

    /// `autostart.lock` 的 FakeClock 感知持有者腳本（P4-T7 `autostart_concurrent_second_cli_reuses_daemon`、
    /// `autostart_waits_on_update_hold_lock` 使用）：持有者於 `release_at_s` 秒（Clock 時間）釋放並可讓 ping 轉為 ok。
    /// 以 FakeClock 控制的檔案鎖替身模擬另一個行程持鎖。
    pub fn with_autostart_lock_holder(mut self, release_at_s: Option<f64>) -> Self {
        let lock_path = self.run_dir().join("autostart.lock");
        let gate = FaultFs::wrap(Arc::clone(&self.fs));
        let release_at_ns = release_at_s.map(|seconds| {
            (seconds.max(0.0) * 1_000_000_000.0)
                .ceil()
                .min(u64::MAX as f64) as u64
        });
        gate.block_lock_until(&lock_path, Arc::clone(&self.clock), release_at_ns);
        self.fs = gate;
        self
    }

    /// 持有自動啟動鎖期間建立 install journal，模擬 updater 在 CLI 等鎖時開始切換。
    pub fn with_autostart_lock_holder_creating_journal(mut self, release_at_s: f64) -> Self {
        let lock_path = self.run_dir().join("autostart.lock");
        let journal_path = self.install_root().join("install-journal.json");
        let release_at_ns = (release_at_s.max(0.0) * 1_000_000_000.0)
            .ceil()
            .min(u64::MAX as f64) as u64;
        let gate = FaultFs::wrap(Arc::clone(&self.fs));
        gate.block_lock_until_and_create_file(
            &lock_path,
            Arc::clone(&self.clock),
            release_at_ns,
            &journal_path,
            br#"{"step":"backup_complete"}"#,
        );
        self.fs = gate;
        self
    }

    /// `benches.state.json.lock` 被另一行程持有且不釋放：`bench_state::remember`／`forget` 取鎖失敗（P4-T9，SC-27）。
    pub fn with_bench_state_lock_holder(mut self) -> Self {
        let lock_path = self
            .bench_state_path()
            .with_file_name("benches.state.json.lock");
        let gate = FaultFs::wrap(Arc::clone(&self.fs));
        gate.block_lock_until(&lock_path, Arc::clone(&self.clock), None);
        self.fs = gate;
        self
    }

    /// 交接後的 identity 置換模型（P4-T7 `graceful_handoff_hard_stop_only_same_identity` 第二情境）：
    /// `after_s` 秒（Clock 時間）後 `from_pid` 的實例由 `to_pid`（不同 start_ticks）取代。
    pub fn with_identity_swap_after(self, after_s: f64, from_pid: u32, to_pid: u32) -> Self {
        if let Some(endpoint) = self.active_daemon_endpoint.as_deref() {
            let response = health_response(to_pid);
            self.rpc.respond_at(
                endpoint,
                "health.status",
                Arc::clone(&self.clock),
                std::time::Duration::from_secs_f64(after_s.max(0.0)),
                &response,
            );
            self.procs
                .alive_until_stopped(to_pid, Arc::clone(&self.rpc), endpoint);
        }
        self.procs.replace_identity_after(
            Arc::clone(&self.clock),
            std::time::Duration::from_secs_f64(after_s.max(0.0)),
            from_pid,
            to_pid,
        );
        self
    }

    /// 重啟循環模型（P4-T7 `restart_on_demand_*` 完整循環使用）：`ep` 的舊實例 `old_pid` 停止後連線被拒，
    /// spawn 後同 endpoint 由 `new_pid` 提供服務。
    pub fn with_restart_cycle(mut self, ep: &str, old_pid: u32, new_pid: u32) -> Self {
        let ep = self.isolated_endpoint(ep);
        self.rpc
            .respond(&ep, "health.ping", &health_response(old_pid));
        self.rpc
            .respond(&ep, "health.status", &health_response(old_pid));
        self.rpc
            .respond(&ep, "daemon.stop", r#"{"ok":true,"stopping":true}"#);
        self.rpc.stop_after_send(&ep, "daemon.stop");
        let counter = self.ensure_process().spawn_counter();
        self.rpc.revive_after_spawn(&ep, counter, new_pid);
        self.procs = FakeProcScan::new(vec![daemon_proc(old_pid)]);
        self.procs
            .alive_until_stopped(old_pid, Arc::clone(&self.rpc), &ep);
        self.active_daemon_endpoint = Some(ep);
        self.active_daemon_pid = Some(old_pid);
        self
    }

    pub fn with_restart_on_failure(mut self) -> Self {
        self.restart_on_failure = true;
        if let Some(service) = &self.service { service.set_hold_prior_restart(Some("on-failure".into())); }
        self.configure_handoff_relaunch();
        self
    }

    pub fn with_daemon_exit_code_after_handoff(mut self, rc: i32) -> Self {
        self.daemon_exit_code_after_handoff = Some(rc);
        self.configure_handoff_relaunch();
        self
    }

    pub fn with_supervisor_ignores_hold(mut self) -> Self {
        self.supervisor_ignores_hold = true;
        self.rpc.set_ignore_supervisor_hold(true);
        if let Some(service) = &self.service { service.set_ignore_hold(true); }
        self
    }

    pub fn with_new_daemon_handoff_pids(mut self, pids: &[u32]) -> Self {
        self.new_daemon_handoff_pids = Some(pids.to_vec());
        self.configure_handoff_relaunch();
        self
    }

    pub fn with_instance_appears_at(mut self, point: &str, pid: u32, lines: &[&str]) -> Self {
        let raw_endpoint = if self.windows {
            Self::win_endpoint().to_string()
        } else {
            self.default_socket()
        };
        let endpoint = self.isolated_endpoint(&raw_endpoint);
        self.instance_appears = Some((endpoint, point.to_string(), pid, lines.iter().map(|line| (*line).to_string()).collect()));
        self
    }

    pub fn with_new_daemon_handoff_script(self, script: &[&str]) -> Self {
        let endpoint = self.active_daemon_endpoint.clone().unwrap_or_else(|| self.isolated_endpoint(&self.default_socket()));
        let pid = self.active_daemon_pid.unwrap_or(4242);
        let token = &sha256_hex(&format!("handoff-{pid}"))[..32];
        let grant = format!(r#"{{"ok":true,"stopping":true,"handoff_token":"{token}"}}"#);
        let mut replies = vec![grant];
        for (index, reply) in script.iter().enumerate() {
            if *reply == "<recv_timeout>" {
                self.rpc.recv_timeout_once_after(&endpoint, "daemon.handoff_stop", index + 1);
            } else {
                replies.push((*reply).to_string());
            }
        }
        replies.push(format!(r#"{{"ok":true,"stopping":true,"handoff_token":"{token}"}}"#));
        let refs = replies.iter().map(String::as_str).collect::<Vec<_>>();
        self.rpc.respond_seq(&endpoint, "daemon.handoff_stop", &refs);
        self
    }

    pub fn with_fault_at(mut self, point: &str) -> Self {
        self.crash_point = Some(point.to_string());
        self
    }

    pub fn with_journal_step(self, _step: &str) -> Self {
        self
    }

    pub fn autostart_lock_held(&self) -> bool {
        let path = self.run_dir().join("autostart.lock");
        real_fs().file_lock_exclusive(&path, false).is_err()
    }

    fn configure_handoff_relaunch(&self) {
        let (Some(endpoint), Some(old_pid)) = (
            self.active_daemon_endpoint.as_deref(),
            self.active_daemon_pid,
        ) else {
            return;
        };
        if self.supervisor_ignores_hold && !(self.restart_on_failure && self.daemon_exit_code_after_handoff.is_some_and(|rc| rc != 0)) {
            let token = &sha256_hex(&format!("handoff-{old_pid}"))[..32];
            let grant = format!(r#"{{"ok":true,"stopping":true,"handoff_token":"{token}"}}"#);
            let busy = r#"{"ok":false,"error_code":"SESSION_BUSY","busy":[{"com":"COM0","state":"READY","foreground_busy":true,"outstanding_commands":0,"interactive_session_id":null,"recovering":false,"ops":["file.push"]}]}"#;
            self.rpc.respond_seq(endpoint, "daemon.handoff_stop", &[&grant, busy]);
            self.rpc.relaunch_after_send(endpoint, "daemon.handoff_stop", &[9000]);
            return;
        }
        if !self.restart_on_failure || !self.daemon_exit_code_after_handoff.is_some_and(|rc| rc != 0) {
            return;
        }
        let pids = self
            .new_daemon_handoff_pids
            .clone()
            .unwrap_or_else(|| vec![9000]);
        let mut response_pids = vec![old_pid];
        response_pids.extend(pids.iter().copied());
        let grants = response_pids
            .iter()
            .map(|pid| {
                let token = &sha256_hex(&format!("handoff-{pid}"))[..32];
                format!(r#"{{"ok":true,"stopping":true,"handoff_token":"{token}"}}"#)
            })
            .collect::<Vec<_>>();
        let refs = grants.iter().map(String::as_str).collect::<Vec<_>>();
        self.rpc.respond_seq(endpoint, "daemon.handoff_stop", &refs);
        self.rpc
            .relaunch_after_send(endpoint, "daemon.handoff_stop", &pids);
    }

    pub fn rpc_now(&self, _ep: &str, _method: &str) -> PyValue {
        let mut d = PyDict::new();
        d.insert("version", PyValue::Str("0.4.0".into()));
        d.insert("ok", PyValue::Bool(true));
        d.insert("pid", PyValue::Int(PyInt::Small(5151)));
        PyValue::Dict(d)
    }

    pub fn recover(&self) -> Option<sw_cli::rollback::RecoveryReport> {
        let platform = self.platform();
        let env = if platform.caps.path_flavor == sw_pal::PalPathFlavor::Windows {
            EnvSnapshot::from_pairs_fold_upper(platform.env.vars())
        } else {
            EnvSnapshot::from_pairs(platform.env.vars())
        };
        let flavor = if platform.caps.path_flavor == sw_pal::PalPathFlavor::Windows {
            PathFlavor::Windows
        } else {
            PathFlavor::Posix
        };
        let native = NativeTraits {
            default_endpoint_is_tcp: platform.caps.native_backend == sw_pal::BackendKind::Win,
            path_flavor: flavor,
        };
        let home = SysEnvHome(platform.env.as_ref());
        let paths = RuntimePaths::resolve(&env, native, &home).ok()?;
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        let io = TextIo::new(&mut stdout, &mut stderr, platform.caps.text_newline);
        let mut ctx = CliCtx {
            p: &platform,
            env,
            paths,
            io,
            verbose: 0,
            cli_version: sw_cli::CLI_VERSION,
            autostart_lock: None,
        };
        let root = sw_cli::install_layout::resolve_install_root(&ctx);
        sw_cli::rollback::recover_journal(&mut ctx, &root)
    }

    /// `CONFIG_DIR/config.yaml`。
    ///
    /// POSIX 形狀（`posix()`）以 `/` 串接，與 CLI 的 `os.path.join` 語意一致（同 [`Self::default_socket`]），
    /// 使原生 Windows 主機上 CLI 印出的路徑（例如 DF-07 摘要）與測試期望值逐字相同。
    pub fn config_path(&self) -> PathBuf {
        if self.windows {
            self.config_dir().join("config.yaml")
        } else {
            PathBuf::from(format!("{}/config.yaml", self.config_dir().display()))
        }
    }

    /// 隔離 `HOME`（tmp 下的 `home/`）。
    pub fn home(&self) -> PathBuf {
        self.tmp_dir.path().join("home")
    }

    pub fn config_dir(&self) -> PathBuf {
        self.tmp_dir.path().join("config")
    }

    pub fn state_dir(&self) -> PathBuf {
        self.tmp_dir.path().join("state")
    }

    pub fn by_id_dir(&self) -> PathBuf {
        self.tmp_dir.path().join("by-id")
    }

    pub fn by_path_dir(&self) -> PathBuf {
        self.tmp_dir.path().join("by-path")
    }

    pub fn install_root(&self) -> PathBuf {
        self.tmp_dir.path().join("install")
    }

    pub fn root(&self) -> sw_cli::install_layout::InstallRoot {
        let install = self.install_root();
        let join = |base: &Path, child: &str| {
            if self.windows {
                PathBuf::from(format!("{}\\{child}", base.display()))
            } else {
                base.join(child)
            }
        };
        let versions = join(&install, "versions");
        let bin = join(&install, "bin");
        sw_cli::install_layout::InstallRoot {
            root: install.clone(),
            versions,
            bin: bin.clone(),
            backups: join(&install, "backups"),
            supervisor: join(&install, "supervisor"),
            current_file: if self.windows {
                join(&bin, "current-release.json")
            } else {
                install.join("current-release.json")
            },
            entry_style: if self.windows {
                sw_cli::install_layout::EntryStyle::Copy
            } else {
                sw_cli::install_layout::EntryStyle::Symlink
            },
            entry_names: if self.windows {
                vec!["serialwrap.exe".into(), "serialwrapd.exe".into()]
            } else {
                vec!["serialwrap".into(), "serialwrapd".into()]
            },
        }
    }

    pub fn log_dir(&self) -> PathBuf {
        self.tmp_dir.path().join("log")
    }

    /// 隔離的 `system_config_dir`（SC-09：不得讀宿主 `/etc/serialwrap`）。
    pub fn system_config_dir(&self) -> PathBuf {
        self.tmp_dir.path().join("etc-serialwrap")
    }

    /// 解壓後的 bundle 根（`bin/serialwrap` 為 `current_exe`）。
    pub fn bundle_dir(&self) -> PathBuf {
        self.tmp_dir.path().join("bundle")
    }

    /// 隔離的 `SERIALWRAP_RUN_DIR`（remote registry 位於 `<run_dir>/remote/`）。
    pub fn run_dir(&self) -> PathBuf {
        self.tmp_dir.path().join("run")
    }

    /// 與 CLI 由 `SERIALWRAP_STATE_DIR` 推得的路徑相同（`<state dir>/benches.state.json`，07 §9.2）。
    pub fn bench_state_path(&self) -> PathBuf {
        self.state_dir().join("benches.state.json")
    }

    /// `install-journal.json` 解析結果（不存在或無法解析 → None）。
    pub fn journal_json(&self) -> Option<PyValue> {
        let text =
            std::fs::read_to_string(self.install_root().join("install-journal.json")).ok()?;
        loads(&text).ok()
    }

    pub fn journal_bytes(&self) -> Option<Vec<u8>> {
        std::fs::read(self.install_root().join("install-journal.json")).ok()
    }

    pub fn config_bytes(&self) -> Vec<u8> {
        std::fs::read(self.config_path()).unwrap_or_default()
    }

    /// 以 journal 的 `backup_dir` 欄位解出備份目錄；缺欄位即 panic（Task 12 journal 必須自帶此欄位，R1-F5）。
    pub fn backup_dir_of(&self, json: &PyValue) -> PathBuf {
        match json {
            PyValue::Dict(d) => match d.get("backup_dir") {
                Some(PyValue::Str(s)) => PathBuf::from(s),
                _ => panic!("journal 缺少 backup_dir 字串欄位"),
            },
            _ => panic!("journal 不是 JSON 物件"),
        }
    }

    /// 版本目錄內檔案的位元組（`install/versions/<v>/<rel>`）。
    pub fn version_bytes(&self, v: &str, rel: &str) -> Vec<u8> {
        std::fs::read(self.root().versions.join(v).join(rel))
            .unwrap_or_else(|e| panic!("version_bytes({v}, {rel}) 讀取失敗：{e}"))
    }

    /// 穩定入口檔案位元組，依 fake 平台路徑語意讀取。
    pub fn entry_bytes(&self, name: &str) -> Vec<u8> {
        let root = self.root();
        std::fs::read(root.bin.join(name))
            .unwrap_or_else(|e| panic!("entry_bytes({name}) 讀取失敗：{e}"))
    }

    /// 寫入檔案（自動建立父目錄）。
    pub fn write_file(&self, p: &Path, bytes: &[u8]) {
        if let Some(parent) = p.parent() {
            std::fs::create_dir_all(parent).expect("父目錄建立失敗");
        }
        std::fs::write(p, bytes).expect("檔案寫入失敗");
    }

    fn set_config_key(&self, key: &str, value: &str) {
        let prefix = format!("{key}:");
        let existing = std::fs::read_to_string(self.config_path()).unwrap_or_default();
        let mut lines: Vec<String> = existing
            .lines()
            .filter(|l| !l.starts_with(&prefix))
            .map(str::to_string)
            .collect();
        lines.push(format!("{key}: {value}"));
        let mut text = lines.join("\n");
        text.push('\n');
        std::fs::write(self.config_path(), text).expect("config.yaml 寫入失敗");
    }

    fn write_bundle_manifest_with(&self, daemon_components: &[(&str, &str)]) {
        let mut daemon = String::new();
        for (i, (key, body)) in daemon_components.iter().enumerate() {
            if i > 0 {
                daemon.push(',');
            }
            daemon.push_str(&format!(r#""{key}":{body}"#));
        }
        let manifest = format!(
            r#"{{"schema_version":1,"bundle_version":"test","stage":"rust-1b-test","target":"x86_64-unknown-linux-gnu","git_revision":"test","components":{{"cli":{{"implementation":"rust","version":"test","path":"bin/serialwrap","sha256":"{}"}},"python_cli_delegate":{{"entry":["py/bin/python3","-I","delegate_sentinel.py"]}},"daemon":{{{daemon}}}}}}}"#,
            "0".repeat(64)
        );
        self.write_file(
            &self.bundle_dir().join("release-manifest.json"),
            manifest.as_bytes(),
        );
    }

    pub fn platform(&self) -> sw_pal::Platform {
        let caps = if self.windows {
            windows_caps()
        } else {
            posix_caps()
        };
        let mut b = platform()
            .env(self.env.clone())
            .fs(self.fs.clone())
            .clock(self.clock.clone())
            .rpc(self.rpc.clone())
            .procs(self.procs.clone())
            .caps(caps);
        if let Some(t) = &self.tunnel {
            b = b.tunnel(t.clone());
        }
        if let Some(s) = &self.service {
            b = b.service(s.clone());
        } else if self.real_service {
            b = b.service(self.real_service_ctl());
        }
        if let Some(p) = &self.process {
            b = b.process(p.clone());
        }
        if let Some(r) = &self.registry {
            b = b.registry(r.clone());
        }
        b.build()
    }

    /// 以 `sw_cli::run` 執行（`argv[0]` 為程式名）；回傳 `(rc, stdout, stderr)`。
    pub fn run(&self, argv: &[&str]) -> (i32, String, String) {
        sw_cli::rollback::set_test_crash_point(self.crash_point.clone());
        let hook = self.instance_appears.as_ref().map(|(endpoint, point, pid, lines)| {
            let endpoint = endpoint.to_string();
            let point = point.to_string();
            let pid = *pid;
            let lines = lines.clone();
            let rpc = Arc::clone(&self.rpc);
            let procs = Arc::clone(&self.procs);
            let windows = self.windows;
            let callback: Box<dyn FnOnce()> = Box::new(move || {
                let reply = if lines.is_empty() {
                    let token = &sha256_hex(&format!("handoff-{pid}"))[..32];
                    format!(r#"{{"ok":true,"stopping":true,"handoff_token":"{token}"}}"#)
                } else {
                    let code = if lines.iter().any(|line| line.contains("flash")) { "FLASHING_BUSY" } else { "SESSION_BUSY" };
                    format!(
                        r#"{{"ok":false,"error_code":"{code}","busy":[{{"com":"COM0","state":"{}","foreground_busy":{},"outstanding_commands":0,"interactive_session_id":null,"recovering":false,"ops":[{}]}}]}}"#,
                        if code == "FLASHING_BUSY" { "FLASHING" } else { "READY" },
                        code == "SESSION_BUSY",
                        lines.iter().map(|line| format!("\"{line}\"")).collect::<Vec<_>>().join(",")
                    )
                };
                rpc.revive_endpoint(&endpoint);
                rpc.respond(&endpoint, "health.ping", &format!(r#"{{"ok":true,"pid":{pid},"version":"test"}}"#));
                rpc.respond(&endpoint, "health.status", &format!(r#"{{"ok":true,"pid":{pid},"version":"test"}}"#));
                rpc.respond(&endpoint, "mcu.status", r#"{"ok":true,"flashing":false}"#);
                rpc.respond(&endpoint, "session.list", OK_SESSION_LIST);
                rpc.respond(&endpoint, "daemon.handoff_stop", &reply);
                if lines.is_empty() {
                    rpc.stop_after_send(&endpoint, "daemon.handoff_stop");
                }
                let appeared = if windows { DaemonProc::win(pid, &endpoint) } else {
                    DaemonProc { pid, socket: Some(endpoint), tree: vec![ProcIdentity { pid, start_ticks: None }] }
                };
                procs.script(vec![vec![appeared]]);
            });
            (point, callback)
        });
        let (hook_point, hook_callback) = match hook { Some((point, callback)) => (Some(point), Some(callback)), None => (None, None) };
        sw_cli::rollback::set_test_hook(hook_point, hook_callback);
        let pf = self.platform();
        let mut full: Vec<OsString> = vec![OsString::from("serialwrap")];
        full.extend(argv.iter().map(OsString::from));
        let mut out: Vec<u8> = Vec::new();
        let mut err: Vec<u8> = Vec::new();
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            sw_cli::run(full, &pf, &mut out, &mut err)
        }));
        sw_cli::rollback::set_test_crash_point(None);
        sw_cli::rollback::set_test_hook(None, None);
        let rc = result.unwrap_or(137);
        (
            rc,
            String::from_utf8_lossy(&out).into_owned(),
            String::from_utf8_lossy(&err).into_owned(),
        )
    }

    /// `current_exe` 已指向 `bundle_dir()/bin/serialwrap`，故與 `run` 相同。
    pub fn run_from_bundle(&self, argv: &[&str]) -> (i32, String, String) {
        self.run(argv)
    }

    /// 依呼叫順序的 `run`／`run_raw` argv（`SysEnv` 替身）。
    pub fn calls(&self) -> Vec<Vec<String>> {
        self.env.calls()
    }

    pub fn rpc(&self) -> &Arc<FakeRpcConnector> {
        &self.rpc
    }

    pub fn clock(&self) -> &Arc<FakeClock> {
        &self.clock
    }

    /// `ProcScan` 替身（pid 存活、serialwrapd 清單、tty 持有者）。
    pub fn procs(&self) -> &Arc<FakeProcScan> {
        &self.procs
    }

    /// 未配置 `tunnel` 時 panic：`fake component not configured: tunnel`。
    pub fn tunnel(&self) -> TunnelFake {
        TunnelFake(
            self.tunnel
                .as_ref()
                .map(Arc::clone)
                .unwrap_or_else(|| panic!("fake component not configured: tunnel")),
        )
    }

    /// 未設定 `service` 替身時 panic：`fake component not configured: service`。
    pub fn service(&self) -> &Arc<FakeServiceCtl> {
        self.service
            .as_ref()
            .unwrap_or_else(|| panic!("fake component not configured: service"))
    }

    /// 未設定 `process` 替身時 panic：`fake component not configured: process`。
    pub fn process(&self) -> &Arc<FakeProcessCtl> {
        self.process
            .as_ref()
            .unwrap_or_else(|| panic!("fake component not configured: process"))
    }

    /// Windows 登錄替身；非 Windows 測試環境未配置時會明確失敗。
    pub fn registry(&self) -> &Arc<FakeRegistry> {
        self.registry
            .as_ref()
            .unwrap_or_else(|| panic!("fake component not configured: registry"))
    }

    /// 合併 `rpc:<method>`、`run:<argv>`、`spawn:<argv0>`、`service:<action>` 事件，依全程單調序號排序（跨來源合併）。
    ///
    /// `rpc:<method>#gone` 由測試替身明確標記；`journal:<step>`（journal 寫入尚未接線）由 Task 12 承接。
    pub fn event_order(&self) -> EventOrder {
        let mut events: Vec<(u64, String)> = Vec::new();
        for (seq, label) in self.fs_events.lock().unwrap_or_else(|e| e.into_inner()).iter() {
            events.push((*seq, label.clone()));
        }
        for (seq, method) in self.rpc.request_events() {
            events.push((seq, format!("rpc:{method}")));
        }
        for (seq, method) in self.rpc.refused_connect_events() {
            events.push((seq, format!("rpc:{method}#gone")));
        }
        for (seq, argv) in self.env.call_events() {
            events.push((seq, format!("run:{}", argv.join(" "))));
        }
        if let Some(process) = &self.process {
            for (seq, label) in process.spawn_events() {
                events.push((seq, format!("spawn:{label}")));
            }
        }
        if let Some(service) = &self.service {
            for (seq, action) in service.action_events() {
                events.push((seq, format!("service:{action}")));
            }
        }
        events.sort_by_key(|(seq, _)| *seq);
        EventOrder {
            events: events.into_iter().map(|(_, label)| label).collect(),
        }
    }
}

/// `RuntimePaths::resolve` 的家目錄查詢（與 `sw_cli::run` 內的 `PlatformHome` 同語意）。
struct SysEnvHome<'a>(&'a dyn SysEnv);

impl HomeLookup for SysEnvHome<'_> {
    fn current_user_home(&self) -> Option<String> {
        self.0.current_user_home()
    }
    fn user_home(&self, user: &str) -> Option<String> {
        self.0.user_home(user)
    }
}

/// `FakeTunnel` 的測試端句柄（轉給 `sw_pal::fakes::FakeTunnel`）。
pub struct TunnelFake(Arc<FakeTunnel>);

impl TunnelFake {
    pub fn spawned(&self) -> Vec<(Vec<String>, PathBuf)> {
        self.0.spawned()
    }

    pub fn set_alive(&self, pid: u32, alive: bool) {
        self.0.set_alive(pid, alive);
    }

    pub fn terminated(&self) -> Vec<u32> {
        self.0.terminated()
    }

    pub fn set_tail(&self, s: &str) {
        self.0.set_tail(s);
    }

    pub fn terminate_fails(&self, pid: u32) {
        self.0.terminate_fails(pid);
    }

    /// 行程於第一次 alive 檢查即結束（P4-T8 `open_spawn_failed_reports_tail` 使用）。
    pub fn die_on_spawn(&self) {
        self.0.die_on_spawn();
    }
}

/// 事件合併結果（`TestEnv::event_order`）。
pub struct EventOrder {
    events: Vec<String>,
}

impl EventOrder {
    /// 第一次出現位置；找不到即 panic（計畫 Task 1）。
    pub fn index_of(&self, s: &str) -> usize {
        self.index_of_nth(s, 0)
    }

    /// 第 `n` 次（0 起算）出現位置；找不到即 panic。
    pub fn index_of_nth(&self, s: &str, n: usize) -> usize {
        self.events
            .iter()
            .enumerate()
            .filter(|(_, e)| e.as_str() == s)
            .nth(n)
            .map(|(i, _)| i)
            .unwrap_or_else(|| {
                panic!(
                    "event not found: {s} (occurrence {n}); events={:?}",
                    self.events
                )
            })
    }

    /// 以 `prefix` 開頭的事件數。
    pub fn count_prefix(&self, prefix: &str) -> usize {
        self.events.iter().filter(|e| e.starts_with(prefix)).count()
    }
}

/// 第 `n` 次（0 起算）對 `method` 的請求 params（JSON 解析後）；找不到即 panic。
pub fn rpc_params_nth(t: &TestEnv, method: &str, n: usize) -> PyValue {
    let (_, _, params) = t
        .rpc()
        .requests()
        .into_iter()
        .filter(|(_, m, _)| m == method)
        .nth(n)
        .unwrap_or_else(|| panic!("rpc 請求不存在：{method}（第 {n} 次）"));
    // `parse_request` 回傳的 params 即為 JSON 物件文字（已去掉 envelope 的結尾 `}`）。
    loads(&params).unwrap_or_else(|e| panic!("params 不是合法 JSON：{e:?}（原文 {params:?}）"))
}

/// 第一次對 `method` 的請求 params（`rpc_params_nth(t, method, 0)`）。
pub fn rpc_params(t: &TestEnv, method: &str) -> PyValue {
    rpc_params_nth(t, method, 0)
}

pub fn methods(t: &TestEnv) -> Vec<String> {
    t.rpc().requests().iter().map(|r| r.1.clone()).collect()
}

pub fn handoffs(t: &TestEnv) -> usize {
    methods(t)
        .iter()
        .filter(|m| *m == "daemon.handoff_stop")
        .count()
}

pub trait GetKey {
    fn get_key(&self, key: &str) -> Option<&PyValue>;
}

impl GetKey for PyValue {
    fn get_key(&self, key: &str) -> Option<&PyValue> {
        match self {
            PyValue::Dict(d) => d.get(key),
            _ => None,
        }
    }
}

impl GetKey for PyDict {
    fn get_key(&self, key: &str) -> Option<&PyValue> {
        self.get(key)
    }
}

impl<T: GetKey> GetKey for &T {
    fn get_key(&self, key: &str) -> Option<&PyValue> {
        (**self).get_key(key)
    }
}

pub fn s<'a>(val: &'a (impl GetKey + ?Sized), key: &str) -> &'a str {
    match val.get_key(key) {
        Some(PyValue::Str(s)) => s.as_str(),
        other => panic!("expected str at key {key}, got {other:?}"),
    }
}

pub fn b(val: &(impl GetKey + ?Sized), key: &str) -> bool {
    match val.get_key(key) {
        Some(PyValue::Bool(b)) => *b,
        other => panic!("expected bool at key {key}, got {other:?}"),
    }
}

pub fn dict<'a>(val: &'a (impl GetKey + ?Sized), key: &str) -> &'a PyValue {
    match val.get_key(key) {
        Some(v @ PyValue::Dict(_)) => v,
        other => panic!("expected dict at key {key}, got {other:?}"),
    }
}

pub fn i(n: i64) -> i64 {
    n
}

/// 小寫 hex 的 sha256（斷言輔助）。
pub fn sha256_hex<T: AsRef<[u8]>>(s: T) -> String {
    sw_proto::pycompat::sha256_hex(s.as_ref())
}

pub trait IntoPyValue {
    fn into_py_value(self) -> PyValue;
}

impl IntoPyValue for &str {
    fn into_py_value(self) -> PyValue {
        PyValue::Str(self.to_string())
    }
}

impl IntoPyValue for String {
    fn into_py_value(self) -> PyValue {
        PyValue::Str(self)
    }
}

impl IntoPyValue for bool {
    fn into_py_value(self) -> PyValue {
        PyValue::Bool(self)
    }
}

impl IntoPyValue for i64 {
    fn into_py_value(self) -> PyValue {
        PyValue::Int(PyInt::Small(self as i128))
    }
}

impl IntoPyValue for usize {
    fn into_py_value(self) -> PyValue {
        PyValue::Int(PyInt::Small(self as i128))
    }
}

impl IntoPyValue for PyValue {
    fn into_py_value(self) -> PyValue {
        self
    }
}

#[macro_export]
macro_rules! json_val {
    ($val:expr) => {
        $val.into_py_value()
    };
}

pub mod serde_json {
    use super::*;
    pub use sw_proto::pyjson::PyValue as Value;

    pub fn from_slice<T>(_bytes: &[u8]) -> Result<Value, String> {
        let s = std::str::from_utf8(_bytes).map_err(|e| e.to_string())?;
        sw_proto::pyjson::loads(s).map_err(|e| e.to_string())
    }

    pub use crate::json_val as json;
}

/// 由元素組出 list（斷言輔助）。
pub fn list(items: Vec<PyValue>) -> PyValue {
    PyValue::List(items)
}

/// argv 字串陣列 → `PyValue::List<Str>`（斷言輔助）。
pub fn argv_list(argv: &[&str]) -> PyValue {
    PyValue::List(
        argv.iter()
            .map(|a| PyValue::Str((*a).to_string()))
            .collect(),
    )
}

/// UTF-16LE 位元組（可含 BOM）→ 去 BOM 後的 code unit 序列（斷言輔助；schtasks／XML 寫出格式檢查用）。
pub fn utf16le_body(bytes: &[u8]) -> Vec<u16> {
    let body = bytes.strip_prefix(&[0xFF, 0xFE]).unwrap_or(bytes);
    body.chunks_exact(2)
        .map(|c| u16::from_le_bytes([c[0], c[1]]))
        .collect()
}

/// 解析 JSON 物件文字（斷言輔助）；非法即 panic。
pub fn json(text: &str) -> PyValue {
    loads(text).unwrap_or_else(|e| panic!("JSON 解析失敗：{e:?}（原文 {text:?}）"))
}

/// 以 Task 17 形狀組出的 release manifest（schema_version 1；sha256 由呼叫端提供）。
pub fn manifest_fixture(v: &str, cli_sha256: &str) -> String {
    format!(
        r#"{{"schema_version":1,"bundle_version":"{v}","stage":"1A","created_at":"2026-10-19T00:00:00Z","target":"x86_64-unknown-linux-gnu","git_revision":"{revision}","contract_revision":"{revision}","components":{{"cli":{{"implementation":"rust","version":"{v}","git_revision":"{revision}","path":"bin/serialwrap","sha256":"{cli_sha256}"}},"daemon":{{"implementation":"python","version":"{v}","git_revision":"{revision}","launcher":{{"path":"bin/serialwrapd","sha256":"{zero}"}},"entry":["py/bin/python3","-I","-B","-m","sw_core.cli"],"runtime":{{"path":"py/bin/python3","python_version":"3.12.3","sha256":"{zero}"}},"package":{{"name":"serialwrap","version":"{v}","wheel_sha256":null,"tree_sha256":"{zero}"}},"state_schema":"python-state-v0"}},"python_cli_delegate":{{"entry":["py/bin/python3","-I","-B","-m","sw_core.cli"],"tree_sha256":"{zero}"}},"assets":{{"skill_sha256":"{zero}","skill_windows_sha256":"{zero}","profiles_sha256":"{zero}"}},"supervisor":{{"default_mode":"on-demand","definition_sha256":"{zero}","task_xml_template_sha256":"{task_xml}"}}}},"pairing":{{"rule":"exact","cli_version":"{v}","daemon_version":"{v}"}}}}"#,
        revision = "0123456789abcdef0123456789abcdef01234567",
        zero = "0".repeat(64),
        task_xml = sha256_hex(sw_pal::service_ctl::TASK_XML_TEMPLATE),
    )
}

/// 與 `mkbundle.sh --dev` 同形狀的測試 bundle 指紋。
#[derive(Clone, Debug)]
pub struct BundleFixture {
    pub manifest: String,
    pub cli_sha: String,
    pub launcher_sha: String,
    pub runtime_sha: String,
}

impl BundleFixture {
    pub fn parsed(&self) -> ReleaseManifest {
        parse_manifest(&self.manifest).unwrap_or_else(|e| panic!("manifest 解析失敗：{e}"))
    }
}

/// 寫出 POSIX 形狀的完整 bundle，含 Python bytecode 及 `SHA256SUMS`。
pub fn write_bundle(dir: &Path, v: &str) -> BundleFixture {
    write_complete_bundle(dir, v, "x86_64-unknown-linux-gnu", false, &[], &[])
}

/// 寫出完整測試 bundle；額外 component 的 JSON 值會加入 manifest，額外檔案會納入 checksum。
pub fn write_complete_bundle(
    dir: &Path,
    v: &str,
    target: &str,
    windows: bool,
    extra_files: &[(&str, Vec<u8>)],
    extra_components: &[(&str, String)],
) -> BundleFixture {
    const REVISION: &str = "0123456789abcdef0123456789abcdef01234567";
    let cli_path = if windows {
        "bin/serialwrap.exe"
    } else {
        "bin/serialwrap"
    };
    let launcher_path = if windows {
        "bin/serialwrapd.exe"
    } else {
        "bin/serialwrapd"
    };
    let mut files = vec![
        (cli_path.to_string(), format!("cli-{v}\n").into_bytes()),
        (
            launcher_path.to_string(),
            format!("launcher-{v}\n").into_bytes(),
        ),
        (
            "py/bin/python3".to_string(),
            format!("runtime-{v}\n").into_bytes(),
        ),
        (
            "py/sw_core/__init__.py".to_string(),
            b"VERSION = 1\n".to_vec(),
        ),
        (
            "py/sw_core/cli.py".to_string(),
            format!("# cli {v}\n").into_bytes(),
        ),
        (
            "py/sw_core/systemd_units.py".to_string(),
            b"UNITS = []\n".to_vec(),
        ),
        (
            "py/sw_core/assets/skill/SKILL.md".to_string(),
            b"skill\n".to_vec(),
        ),
        (
            "py/sw_core/assets/skill/SKILL_WINDOWS.md".to_string(),
            b"skill windows\n".to_vec(),
        ),
        (
            "py/sw_core/assets/profiles/default.yaml".to_string(),
            b"profile: default\n".to_vec(),
        ),
        (
            "py/sw_core/__pycache__/cli.cpython-312.pyc".to_string(),
            b"compiled cli\n".to_vec(),
        ),
        (
            "py/sw_core/stale.pyc".to_string(),
            b"compiled module\n".to_vec(),
        ),
    ];
    files.extend(
        extra_files
            .iter()
            .map(|(path, body)| (path.to_string(), body.clone())),
    );
    for (relative, body) in &files {
        let path = dir.join(relative);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("bundle 目錄建立失敗");
        }
        std::fs::write(path, body).expect("bundle 檔案寫入失敗");
    }

    let body_of = |relative: &str| -> &[u8] {
        files
            .iter()
            .find(|(path, _)| path == relative)
            .map(|(_, body)| body.as_slice())
            .unwrap_or_else(|| panic!("bundle 缺少檔案：{relative}"))
    };
    let cli_sha = sha256_hex(body_of(cli_path));
    let launcher_sha = sha256_hex(body_of(launcher_path));
    let runtime_sha = sha256_hex(body_of("py/bin/python3"));
    let package_tree = fixture_tree_sha256(&files, "py/sw_core/", "sw_core/");
    let profile_tree = fixture_tree_sha256(&files, "py/sw_core/assets/profiles/", "");
    let skill_sha = sha256_hex(body_of("py/sw_core/assets/skill/SKILL.md"));
    let skill_windows_sha = sha256_hex(body_of("py/sw_core/assets/skill/SKILL_WINDOWS.md"));
    let supervisor_sha = sha256_hex(body_of("py/sw_core/systemd_units.py"));
    let task_xml_sha = sha256_hex(sw_pal::service_ctl::TASK_XML_TEMPLATE);
    let extra_json = extra_components
        .iter()
        .map(|(key, value)| format!(r#","{key}":{value}"#))
        .collect::<String>();
    let manifest = format!(
        r#"{{"schema_version":1,"bundle_version":"{v}","stage":"1A","created_at":"2026-10-19T00:00:00Z","target":"{target}","git_revision":"{REVISION}","contract_revision":"{REVISION}","components":{{"cli":{{"implementation":"rust","version":"{v}","git_revision":"{REVISION}","path":"{cli_path}","sha256":"{cli_sha}"}},"daemon":{{"implementation":"python","version":"{v}","git_revision":"{REVISION}","launcher":{{"path":"{launcher_path}","sha256":"{launcher_sha}"}},"entry":["py/bin/python3","-I","-B","-m","sw_core.cli"],"runtime":{{"path":"py/bin/python3","python_version":"3.12.3","sha256":"{runtime_sha}"}},"package":{{"name":"serialwrap","version":"{v}","wheel_sha256":null,"tree_sha256":"{package_tree}"}},"state_schema":"python-state-v0"}},"python_cli_delegate":{{"entry":["py/bin/python3","-I","-B","-m","sw_core.cli"],"tree_sha256":"{package_tree}"}},"assets":{{"skill_sha256":"{skill_sha}","skill_windows_sha256":"{skill_windows_sha}","profiles_sha256":"{profile_tree}"}},"supervisor":{{"default_mode":"on-demand","definition_sha256":"{supervisor_sha}","task_xml_template_sha256":"{task_xml_sha}"}}{extra_json}}},"pairing":{{"rule":"exact","cli_version":"{v}","daemon_version":"{v}"}}}}"#
    );
    std::fs::write(dir.join("release-manifest.json"), &manifest).expect("manifest 寫入失敗");

    let mut sums = files
        .iter()
        .map(|(path, body)| (path.clone(), sha256_hex(body)))
        .collect::<Vec<_>>();
    sums.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    let sums_text = sums
        .iter()
        .map(|(path, digest)| format!("{digest}  {path}\n"))
        .collect::<String>();
    std::fs::write(dir.join("SHA256SUMS"), sums_text).expect("SHA256SUMS 寫入失敗");
    BundleFixture {
        manifest,
        cli_sha,
        launcher_sha,
        runtime_sha,
    }
}

fn fixture_tree_sha256(files: &[(String, Vec<u8>)], prefix: &str, rel_prefix: &str) -> String {
    let mut entries = files
        .iter()
        .filter_map(|(path, body)| {
            let relative = path.strip_prefix(prefix)?;
            if relative.is_empty()
                || relative.split('/').any(|part| part == "__pycache__")
                || relative.ends_with(".pyc")
            {
                return None;
            }
            Some((format!("{rel_prefix}{relative}"), sha256_hex(body)))
        })
        .collect::<Vec<_>>();
    entries.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    let mut material = String::new();
    for (relative, digest) in entries {
        material.push_str(&format!("{relative}\0{digest}\n"));
    }
    sha256_hex(material.as_bytes())
}

/// 在 `root/versions/<v>/` 下寫入檔案（相對路徑 `files`），回傳版本目錄。
pub fn make_version_dir(root: &Path, v: &str, files: &[(&str, &[u8])]) -> PathBuf {
    let dir = root.join("versions").join(v);
    for (rel, body) in files {
        let p = dir.join(rel);
        if let Some(parent) = p.parent() {
            std::fs::create_dir_all(parent).expect("版本目錄建立失敗");
        }
        std::fs::write(&p, body).expect("版本檔寫入失敗");
    }
    dir
}

/// `alive` 的 remote registry state（07 §8.4 欄位子集；identity 由 `role|pid` 的 sha256 導出）。
pub fn alive_state(role: &str, pid: u32) -> PyDict {
    alive_state_with_identity(role, pid, &sha256_hex(&format!("{role}|{pid}")))
}

/// 同 `alive_state`，另指定 identity。
pub fn alive_state_with_identity(role: &str, pid: u32, id: &str) -> PyDict {
    let mut d = PyDict::new();
    d.insert("identity", PyValue::Str(id.to_string()));
    d.insert("status", PyValue::Str("active".to_string()));
    d.insert("role", PyValue::Str(role.to_string()));
    d.insert("pid", PyValue::Int(PyInt::Small(i128::from(pid))));
    d.insert("pgid", PyValue::Int(PyInt::Small(i128::from(pid))));
    d.insert("pid_start_ticks", PyValue::Null);
    d.insert("target", PyValue::Str("u@relay".to_string()));
    d.insert("via", PyValue::Str("ssh".to_string()));
    d.insert("control_path", PyValue::Str(String::new()));
    d
}

/// 僅含 pid 與自身 identity 的 daemon 行程（POSIX 形狀；Windows 的 launcher 樹由 P4-T4 承接）。
/// OD-48 替身：0.3.x daemon 與 setup 的 uid。
pub const LEGACY_UID: u32 = 1000;
/// `v0.3.2:sw_core/service.py` 對未知 method 的回應（`rpc()` 以 setdefault 補 `version`）。
pub const LEGACY_METHOD_NOT_FOUND: &str = r#"{"ok":false,"error_code":"METHOD_NOT_FOUND","method":"daemon.handoff_stop","version":"0.3.2"}"#;
/// 0.3.2 `session.list`（閒置 COM0，附掛 UART）。
pub const LEGACY_SESSION_LIST: &str = r#"{"ok":true,"sessions":[{"com":"COM0","state":"READY","foreground_busy":false,"outstanding_commands":0,"interactive_session_id":null,"recovering":false,"attached_real_path":"/dev/ttyUSB0","capture":null}],"version":"0.3.2"}"#;

/// 0.3.2 pipx 安裝的 daemon 行程：venv python 執行 `~/.local/bin/serialwrapd` script。
pub fn legacy_details(pid: u32, socket: &str) -> sw_pal::proc_scan::ProcDetails {
    sw_pal::proc_scan::ProcDetails {
        pid,
        ppid: Some(1),
        uid: Some(LEGACY_UID),
        exe: Some("/usr/bin/python3.12".into()),
        argv: vec![
            "/opt/legacy-home/.local/pipx/venvs/serialwrap/bin/python".into(),
            "/opt/legacy-home/.local/bin/serialwrapd".into(),
            "--socket".into(),
            socket.into(),
        ],
        start_ticks: Some(70_000 + u64::from(pid)),
    }
}

fn daemon_proc(pid: u32) -> DaemonProc {
    DaemonProc {
        pid,
        socket: None,
        tree: vec![ProcIdentity {
            pid,
            start_ticks: None,
        }],
    }
}

fn health_response(pid: u32) -> String {
    format!(r#"{{"ok":true,"pid":{pid},"version":"test"}}"#)
}
