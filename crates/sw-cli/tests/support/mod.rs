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
use std::sync::Arc;

use sw_pal::fakes::fs::FaultFs;
use sw_pal::fakes::{
    platform, posix_caps, real_fs, windows_caps, FakeClock, FakeProcScan, FakeProcessCtl,
    FakeRegistry, FakeRpcConnector, FakeServiceCtl, FakeSysEnv, FakeSysEnvBuilder, FakeTunnel,
};
use sw_cli::ctx::{CliCtx, TextIo};
use sw_pal::fs_ops::FsOps;
pub use sw_pal::fakes::service::ServiceModelState;
pub use sw_pal::proc_scan::{DaemonProc, ProcIdentity};
pub use sw_pal::service_ctl::{
    CompositeServiceCtl, HoldState, ServiceAction, ServiceCtl, SystemdCtl, WindowsServiceCtl, WindowsTaskCtl,
};
pub use sw_pal::traits::{Clock, SupervisionMode, SysEnv};
use sw_pal::PalErrorKind;
use sw_proto::pypath::{HomeLookup, PathFlavor};
use sw_proto::pyjson::{dumps_dict, loads, PyDict, PyInt, PyValue, DISK_STABLE};
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

pub struct TestEnv {
    tmp_dir: tempfile::TempDir,
    windows: bool,
    ops: Vec<EnvOp>,
    env: Arc<FakeSysEnv>,
    fs: Arc<dyn FsOps>,
    clock: Arc<FakeClock>,
    rpc: Arc<FakeRpcConnector>,
    procs: Arc<FakeProcScan>,
    tunnel: Option<Arc<FakeTunnel>>,
    service: Option<Arc<FakeServiceCtl>>,
    process: Option<Arc<FakeProcessCtl>>,
    registry: Option<Arc<FakeRegistry>>,
    crash_point: Option<String>,
    real_service: bool,
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
        let mut t = Self {
            tmp_dir,
            windows,
            ops: Vec::new(),
            env: FakeSysEnv::builder().build(),
            fs: real_fs(),
            clock: FakeClock::new(),
            rpc: FakeRpcConnector::new(),
            procs: FakeProcScan::new(Vec::new()),
            tunnel: None,
            service: None,
            process: None,
            registry: None,
            crash_point: None,
            real_service: false,
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
        self.push_env_op(Box::new(move |b| b.systemd(flag)))
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

    /// 預設即不屬於任何群組（FakeSysEnv 預設行為）。
    pub fn without_group(self, g: &str) -> Self {
        self
    }

    /// `doctor` 非 advisory 檢查全部通過的最小環境（P4-T9 承接細節；T1 只設定 systemd 與 dialout 群組）。
    pub fn all_required_ok(self) -> Self {
        self.with_systemd(true)
            .push_env_op(Box::new(|b| b.in_group("dialout")))
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
        self.rpc.respond(&ep, "health.ping", OK_PING);
        self.rpc.respond(&ep, "session.list", OK_SESSION_LIST);
        self.rpc.refuse_from_connect(&ep, n);
        self.procs = FakeProcScan::new(vec![daemon_proc(pid)]);
        self.procs
            .alive_until_connect(pid, Arc::clone(&self.rpc), &ep, n);
        self
    }

    pub fn with_daemon_alive_forever(mut self, ep: &str, pid: u32) -> Self {
        let ep = self.isolated_endpoint(ep);
        self.rpc.respond(&ep, "health.ping", OK_PING);
        self.procs = FakeProcScan::new(vec![daemon_proc(pid)]);
        self
    }

    /// 閒置 daemon（`flashing:false`、單一 COM0 閒置，無 busy）：`daemon.handoff_stop` 回 grant（32 hex token），
    /// 之後 `ep` 停止（refused），pid 隨之不存活；`daemon.stop` 同語意。
    pub fn with_idle_daemon(mut self, ep: &str, pid: u32) -> Self {
        let ep = self.isolated_endpoint(ep);
        self.rpc.respond(&ep, "health.ping", OK_PING);
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
        self.procs
            .alive_until_stopped(pid, Arc::clone(&self.rpc), &ep);
        self
    }

    /// `health.ping` 與 `health.status.version`（spawn 之後才可連線，對應「新 spawn 的 daemon」）。
    pub fn with_new_daemon_reporting_version(mut self, v: &str) -> Self {
        let ep = self.isolated_endpoint(&self.default_socket());
        let counter = self.ensure_process().spawn_counter();
        self.rpc.gate_on_spawn(&ep, counter);
        self.rpc.respond(&ep, "health.ping", OK_PING);
        self.rpc.respond(
            &ep,
            "health.status",
            &format!(r#"{{"ok":true,"version":"{v}"}}"#),
        );
        self
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
        let join = |base: &Path, child: &str| {
            if self.windows {
                PathBuf::from(format!("{}\\{child}", base.display()))
            } else {
                base.join(child)
            }
        };
        let body = format!("current-{v}");
        for name in ["serialwrap", "serialwrap.exe", "serialwrapd", "serialwrapd.exe"] {
            let version_dir = join(&root.versions, v);
            let bin = join(&version_dir, "bin");
            self.write_file(&join(&bin, name), body.as_bytes());
        }
        if let Some(p) = prev {
            let prev_body = format!("previous-{p}");
            for name in ["serialwrap", "serialwrap.exe", "serialwrapd", "serialwrapd.exe"] {
                let version_dir = join(&root.versions, p);
                let bin = join(&version_dir, "bin");
                self.write_file(&join(&bin, name), prev_body.as_bytes());
            }
        }
        let current = format!(r#"{{"bundle_version":"{v}","dir":"versions/{v}"}}"#);
        self.write_file(&root.current_file, current.as_bytes());
        for name in ["serialwrap", "serialwrap.exe", "serialwrapd", "serialwrapd.exe"] {
            self.write_file(&join(&root.bin, name), body.as_bytes());
        }
        self
    }

    /// `versions/<v>/bin/serialwrap` 安裝版本目錄（不改 current）。
    pub fn with_installed(self, v: &str) -> Self {
        let body = format!("installed-{v}");
        let root = self.root();
        let join = |base: &Path, child: &str| {
            if self.windows {
                PathBuf::from(format!("{}\\{child}", base.display()))
            } else {
                base.join(child)
            }
        };
        for name in ["serialwrap", "serialwrap.exe", "serialwrapd", "serialwrapd.exe"] {
            let version_dir = join(&root.versions, v);
            let bin = join(&version_dir, "bin");
            self.write_file(&join(&bin, name), body.as_bytes());
        }
        self
    }

    /// bundle 根的 `bin/serialwrap` 與 `release-manifest.json`（manifest 形狀同 `mkbundle.sh`，sha256 為真值）。
    pub fn with_bundle(self, v: &str) -> Self {
        let target = if self.windows { "x86_64-pc-windows-gnu" } else { "x86_64-unknown-linux-gnu" };
        write_complete_bundle(&self.bundle_dir(), v, target, self.windows, &[], &[]);
        self
    }

    /// 以 `FaultFs` 包住 fs；`replace` 來源以 `s` 結尾時失敗（SC-12 中途失敗路徑）。
    pub fn with_fs_fault_replace_suffix(mut self, s: &str) -> Self {
        let faulty = FaultFs::wrap(Arc::clone(&self.fs));
        faulty.fail_replace_when_src_ends_with(s);
        self.fs = faulty;
        self
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
        let svc = self
            .service
            .get_or_insert_with(|| FakeServiceCtl::new(vec![mode.clone()]))
            .clone();
        svc.set_model(Some(enabled), running);
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
        Arc::new(CompositeServiceCtl::new(
            vec![
                SupervisionMode::OnDemand,
                SupervisionMode::SystemdUser,
                SupervisionMode::SystemdSystem,
                SupervisionMode::WindowsTask,
                SupervisionMode::WindowsService,
            ],
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
        };
        f(&mut ctx)
    }

    /// `autostart.lock` 的 FakeClock 感知持有者腳本（P4-T7 `autostart_concurrent_second_cli_reuses_daemon`、
    /// `autostart_waits_on_update_hold_lock` 使用）：持有者於 `release_at_s` 秒（Clock 時間）釋放並可讓 ping 轉為 ok。
    /// 目前尚未實作（真實 flock 於同行程測試會阻塞，不可用）。
    pub fn with_autostart_lock_holder(self, release_at_s: Option<f64>) -> Self {
        todo!("P4-T7: autostart.lock 的 FakeClock 感知持有者替身尚未實作（release_at_s={release_at_s:?}）")
    }

    /// `benches.state.json.lock` 被另一行程持有且不釋放：`bench_state::remember`／`forget` 取鎖失敗（P4-T9，SC-27）。
    pub fn with_bench_state_lock_holder(mut self) -> Self {
        let lock_path = self.bench_state_path().with_file_name("benches.state.json.lock");
        let gate = FaultFs::wrap(Arc::clone(&self.fs));
        gate.block_lock_until(&lock_path, Arc::clone(&self.clock), None);
        self.fs = gate;
        self
    }

    /// 交接後的 identity 置換模型（P4-T7 `graceful_handoff_hard_stop_only_same_identity` 第二情境）：
    /// `after_s` 秒（Clock 時間）後 `from_pid` 的實例由 `to_pid`（不同 start_ticks）取代。目前尚未實作。
    pub fn with_identity_swap_after(self, after_s: f64, from_pid: u32, to_pid: u32) -> Self {
        todo!("P4-T7: identity 置換模型尚未實作（{after_s} s 後 {from_pid} → {to_pid}）")
    }

    /// 重啟循環模型（P4-T7 `restart_on_demand_*` 完整循環使用）：`ep` 的舊實例 `old_pid` 停止後連線被拒，
    /// spawn 後同 endpoint 由 `new_pid` 提供服務。`FakeRpcConnector` 的停止與 refuse 為永久狀態，無法表達「復活」。目前尚未實作。
    pub fn with_restart_cycle(self, ep: &str, old_pid: u32, new_pid: u32) -> Self {
        todo!("P4-T7: 重啟循環（停止後 refused、spawn 後 ok）替身尚未實作（{ep} {old_pid} → {new_pid}）")
    }

    pub fn with_restart_on_failure(self) -> Self {
        self
    }

    pub fn with_daemon_exit_code_after_handoff(self, _rc: i32) -> Self {
        self
    }

    pub fn with_supervisor_ignores_hold(self) -> Self {
        self
    }

    pub fn with_instance_appears_at(self, _point: &str, _pid: u32, _lines: &[&str]) -> Self {
        self
    }

    pub fn with_new_daemon_handoff_script(self, _script: &[&str]) -> Self {
        self
    }

    pub fn with_fault_at(self, _point: &str) -> Self {
        self
    }

    pub fn with_journal_step(self, _step: &str) -> Self {
        self
    }

    pub fn autostart_lock_held(&self) -> bool {
        false
    }

    pub fn rpc_now(&self, _ep: &str, _method: &str) -> PyValue {
        let mut d = PyDict::new();
        d.insert("version", PyValue::Str("0.4.0".into()));
        d.insert("ok", PyValue::Bool(true));
        d.insert("pid", PyValue::Int(PyInt::Small(5151)));
        PyValue::Dict(d)
    }

    pub fn recover(&self) -> Option<sw_cli::rollback::RecoveryReport> {
        unimplemented!("not implemented: P4-T12")
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
            if self.windows { PathBuf::from(format!("{}\\{child}", base.display())) } else { base.join(child) }
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
        std::fs::read(self.install_root().join("versions").join(v).join(rel))
            .unwrap_or_else(|e| panic!("version_bytes({v}, {rel}) 讀取失敗：{e}"))
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
        if self.crash_point.is_some() {
            todo!("P4-T12: FaultPlan（sw_cli::run 的中斷注入）尚未接線；with_crash_at 不得靜默略過")
        }
        let pf = self.platform();
        let mut full: Vec<OsString> = vec![OsString::from("serialwrap")];
        full.extend(argv.iter().map(OsString::from));
        let mut out: Vec<u8> = Vec::new();
        let mut err: Vec<u8> = Vec::new();
        let rc = sw_cli::run(full, &pf, &mut out, &mut err);
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

    /// 合併 `rpc:<method>`、`run:<argv>`、`spawn:<argv0>`、`service:<action>` 事件，依全程單調序號排序（跨來源合併）。
    ///
    /// 尚未涵蓋（Task 7／12 承接）：`rpc:<method>#gone`（refused 連線的 method 須由 RpcConnector 傳入，現簽章不含）與
    /// `journal:<step>`（journal 寫入尚未接線）。
    pub fn event_order(&self) -> EventOrder {
        let mut events: Vec<(u64, String)> = Vec::new();
        for (seq, method) in self.rpc.request_events() {
            events.push((seq, format!("rpc:{method}")));
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
    methods(t).iter().filter(|m| *m == "daemon.handoff_stop").count()
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
    let cli_path = if windows { "bin/serialwrap.exe" } else { "bin/serialwrap" };
    let launcher_path = if windows { "bin/serialwrapd.exe" } else { "bin/serialwrapd" };
    let mut files = vec![
        (cli_path.to_string(), format!("cli-{v}\n").into_bytes()),
        (launcher_path.to_string(), format!("launcher-{v}\n").into_bytes()),
        ("py/bin/python3".to_string(), format!("runtime-{v}\n").into_bytes()),
        ("py/sw_core/__init__.py".to_string(), b"VERSION = 1\n".to_vec()),
        ("py/sw_core/cli.py".to_string(), format!("# cli {v}\n").into_bytes()),
        ("py/sw_core/systemd_units.py".to_string(), b"UNITS = []\n".to_vec()),
        ("py/sw_core/assets/skill/SKILL.md".to_string(), b"skill\n".to_vec()),
        ("py/sw_core/assets/skill/SKILL_WINDOWS.md".to_string(), b"skill windows\n".to_vec()),
        ("py/sw_core/assets/profiles/default.yaml".to_string(), b"profile: default\n".to_vec()),
        ("py/sw_core/__pycache__/cli.cpython-312.pyc".to_string(), b"compiled cli\n".to_vec()),
        ("py/sw_core/stale.pyc".to_string(), b"compiled module\n".to_vec()),
    ];
    files.extend(extra_files.iter().map(|(path, body)| (path.to_string(), body.clone())));
    for (relative, body) in &files {
        let path = dir.join(relative);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("bundle 目錄建立失敗");
        }
        std::fs::write(path, body).expect("bundle 檔案寫入失敗");
    }

    let body_of = |relative: &str| -> &[u8] {
        files.iter().find(|(path, _)| path == relative).map(|(_, body)| body.as_slice())
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
    let extra_json = extra_components.iter().map(|(key, value)| format!(r#","{key}":{value}"#)).collect::<String>();
    let manifest = format!(
        r#"{{"schema_version":1,"bundle_version":"{v}","stage":"1A","created_at":"2026-10-19T00:00:00Z","target":"{target}","git_revision":"{REVISION}","contract_revision":"{REVISION}","components":{{"cli":{{"implementation":"rust","version":"{v}","git_revision":"{REVISION}","path":"{cli_path}","sha256":"{cli_sha}"}},"daemon":{{"implementation":"python","version":"{v}","git_revision":"{REVISION}","launcher":{{"path":"{launcher_path}","sha256":"{launcher_sha}"}},"entry":["py/bin/python3","-I","-B","-m","sw_core.cli"],"runtime":{{"path":"py/bin/python3","python_version":"3.12.3","sha256":"{runtime_sha}"}},"package":{{"name":"serialwrap","version":"{v}","wheel_sha256":null,"tree_sha256":"{package_tree}"}},"state_schema":"python-state-v0"}},"python_cli_delegate":{{"entry":["py/bin/python3","-I","-B","-m","sw_core.cli"],"tree_sha256":"{package_tree}"}},"assets":{{"skill_sha256":"{skill_sha}","skill_windows_sha256":"{skill_windows_sha}","profiles_sha256":"{profile_tree}"}},"supervisor":{{"default_mode":"on-demand","definition_sha256":"{supervisor_sha}","task_xml_template_sha256":"{task_xml_sha}"}}{extra_json}}},"pairing":{{"rule":"exact","cli_version":"{v}","daemon_version":"{v}"}}}}"#
    );
    std::fs::write(dir.join("release-manifest.json"), &manifest).expect("manifest 寫入失敗");

    let mut sums = files.iter().map(|(path, body)| (path.clone(), sha256_hex(body))).collect::<Vec<_>>();
    sums.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    let sums_text = sums.iter().map(|(path, digest)| format!("{digest}  {path}\n")).collect::<String>();
    std::fs::write(dir.join("SHA256SUMS"), sums_text).expect("SHA256SUMS 寫入失敗");
    BundleFixture { manifest, cli_sha, launcher_sha, runtime_sha }
}

fn fixture_tree_sha256(files: &[(String, Vec<u8>)], prefix: &str, rel_prefix: &str) -> String {
    let mut entries = files.iter().filter_map(|(path, body)| {
        let relative = path.strip_prefix(prefix)?;
        if relative.is_empty() || relative.split('/').any(|part| part == "__pycache__") || relative.ends_with(".pyc") {
            return None;
        }
        Some((format!("{rel_prefix}{relative}"), sha256_hex(body)))
    }).collect::<Vec<_>>();
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
