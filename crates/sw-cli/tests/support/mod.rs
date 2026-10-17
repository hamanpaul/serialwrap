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
    platform, posix_caps, real_fs, windows_caps, FakeClock, FakeProcScan, FakeProcessCtl, FakeRegistry,
    FakeRpcConnector, FakeServiceCtl, FakeSysEnv, FakeSysEnvBuilder, FakeTunnel,
};
use sw_pal::fs_ops::FsOps;
use sw_pal::proc_scan::{DaemonProc, ProcIdentity};
use sw_pal::traits::SupervisionMode;
use sw_pal::PalErrorKind;
use sw_proto::pyjson::{dumps_dict, loads, PyDict, PyInt, PyValue, DISK_STABLE};

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
            ("SERIALWRAP_CONFIG_DIR".to_string(), self.config_dir().display().to_string()),
            ("SERIALWRAP_STATE_DIR".to_string(), self.state_dir().display().to_string()),
            ("SERIALWRAP_RUN_DIR".to_string(), self.run_dir().display().to_string()),
            ("SERIALWRAP_PROFILE_DIR".to_string(), self.config_dir().join("profiles").display().to_string()),
            ("SERIALWRAP_BY_ID_DIR".to_string(), self.by_id_dir().display().to_string()),
            ("SERIALWRAP_BY_PATH_DIR".to_string(), self.by_path_dir().display().to_string()),
            ("SERIALWRAP_INSTALL_ROOT".to_string(), self.install_root().display().to_string()),
            ("SERIALWRAP_LOG_DIR".to_string(), self.log_dir().display().to_string()),
        ];
        if self.windows {
            vars.push(("USERPROFILE".to_string(), self.home().display().to_string()));
            vars.push((
                "LOCALAPPDATA".to_string(),
                self.home().join("AppData").join("Local").display().to_string(),
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
    pub fn default_socket(&self) -> String {
        self.run_dir().join("serialwrapd.sock").display().to_string()
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

    /// 預設即不屬於任何群組（FakeSysEnv 預設行為）。
    pub fn without_group(self, g: &str) -> Self {
        self
    }

    /// `doctor` 非 advisory 檢查全部通過的最小環境（P4-T9 承接細節；T1 只設定 systemd 與 dialout 群組）。
    pub fn all_required_ok(self) -> Self {
        self.with_systemd(true).push_env_op(Box::new(|b| b.in_group("dialout")))
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
        self.write_file(&self.bundle_dir().join("bin").join("serialwrapd"), b"launcher");
        self
    }

    /// 寫 `components.daemon.runtime` 與 `py/bin/python3`（檔案形狀）。`pyver` 目前只記錄於 manifest 的 `python_version`，
    /// 腳本化 runtime 的 `-c` 版本查詢需 P4-T11 決定 argv 後再接（見 PR 說明的 gap）。
    pub fn with_manifest_runtime_ok(self, pyver: &str) -> Self {
        let body = format!(r#"{{"path":"py/bin/python3","python_version":"{pyver}"}}"#);
        self.write_bundle_manifest_with(&[("runtime", body.as_str())]);
        self.write_file(&self.bundle_dir().join("py").join("bin").join("python3"), b"runtime");
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
        self.rpc.fail_after_send(&self.isolated_endpoint(ep), method);
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
        self.procs.alive_until_connect(pid, Arc::clone(&self.rpc), &ep, n);
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
        self.rpc.respond(&ep, "mcu.status", r#"{"ok":true,"flashing":false}"#);
        let token = &sha256_hex(&format!("handoff-{pid}"))[..32];
        let grant = format!(r#"{{"ok":true,"stopping":true,"handoff_token":"{token}"}}"#);
        self.rpc.respond(&ep, "daemon.handoff_stop", &grant);
        self.rpc.respond(&ep, "daemon.stop", r#"{"ok":true,"stopping":true}"#);
        self.rpc.stop_after_send(&ep, "daemon.handoff_stop");
        self.rpc.stop_after_send(&ep, "daemon.stop");
        self.procs = FakeProcScan::new(vec![daemon_proc(pid)]);
        self.procs.alive_until_stopped(pid, Arc::clone(&self.rpc), &ep);
        self
    }

    /// `health.ping` 與 `health.status.version`（spawn 之後才可連線，對應「新 spawn 的 daemon」）。
    pub fn with_new_daemon_reporting_version(mut self, v: &str) -> Self {
        let ep = self.isolated_endpoint(&self.default_socket());
        let counter = self.ensure_process().spawn_counter();
        self.rpc.gate_on_spawn(&ep, counter);
        self.rpc.respond(&ep, "health.ping", OK_PING);
        self.rpc.respond(&ep, "health.status", &format!(r#"{{"ok":true,"version":"{v}"}}"#));
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
        std::fs::write(dir.join(format!("{port}.json")), dumps_dict(&state, &DISK_STABLE))
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

    /// `FakeTunnel` 支援 ControlMaster，且 spawn 之後的行程存活（remote 開通路徑）。
    pub fn with_tunnel_ready(mut self) -> Self {
        self.tunnel = Some(FakeTunnel::new(true));
        self
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

    /// `install/current-release.json` 與 `versions/<v>/bin/serialwrap`；`prev` 給定時一併建立 `versions/<prev>`。
    pub fn with_current(self, v: &str, prev: Option<&str>) -> Self {
        let install = self.install_root();
        let body = format!("current-{v}");
        make_version_dir(&install, v, &[("bin/serialwrap", body.as_bytes())]);
        if let Some(p) = prev {
            let prev_body = format!("previous-{p}");
            make_version_dir(&install, p, &[("bin/serialwrap", prev_body.as_bytes())]);
        }
        let current = format!(r#"{{"bundle_version":"{v}","dir":"versions/{v}"}}"#);
        self.write_file(&install.join("current-release.json"), current.as_bytes());
        self
    }

    /// `versions/<v>/bin/serialwrap` 安裝版本目錄（不改 current）。
    pub fn with_installed(self, v: &str) -> Self {
        let body = format!("installed-{v}");
        make_version_dir(&self.install_root(), v, &[("bin/serialwrap", body.as_bytes())]);
        self
    }

    /// bundle 根的 `bin/serialwrap` 與 `release-manifest.json`（manifest 形狀同 `mkbundle.sh`，sha256 為真值）。
    pub fn with_bundle(self, v: &str) -> Self {
        let cli = format!("bundle-{v}");
        self.write_file(&self.bundle_dir().join("bin").join("serialwrap"), cli.as_bytes());
        let cli_sha = sha256_hex(&cli);
        let manifest = manifest_fixture(v, &cli_sha);
        self.write_file(&self.bundle_dir().join("release-manifest.json"), manifest.as_bytes());
        self
    }

    /// 以 `FaultFs` 包住 fs；`replace` 來源以 `s` 結尾時失敗（SC-12 中途失敗路徑）。
    pub fn with_fs_fault_replace_suffix(mut self, s: &str) -> Self {
        let faulty = FaultFs::wrap(Arc::clone(&self.fs));
        faulty.fail_replace_when_src_ends_with(s);
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
    pub fn with_supervisor_state(mut self, mode: SupervisionMode, enabled: bool, running: bool) -> Self {
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
    pub fn with_spawn_exits(mut self, rc: i32) -> Self {
        self.ensure_process().poll_script(vec![Some(rc)]);
        self
    }

    pub fn with_restart_on_failure(self) -> Self {
        todo!("P4-T12: R5-F1 監管拉起模型（Restart=on-failure／RestartOnFailure）尚未實作")
    }

    pub fn with_daemon_exit_code_after_handoff(self, rc: i32) -> Self {
        todo!("P4-T12: 交接 grant 之後的 daemon 結束碼模型尚未實作（P8 停機未收斂）")
    }

    pub fn with_supervisor_ignores_hold(self) -> Self {
        todo!("P4-T12: 監管模型忽略 hold 的開關尚未實作（重新交接防線測試用）")
    }

    pub fn with_instance_appears_at(self, point: &str, pid: u32, lines: &[&str]) -> Self {
        todo!("P4-T12: crash point {point} 同 endpoint 生出新實例的模型尚未實作")
    }

    pub fn with_journal_step(self, step: &str) -> Self {
        todo!("P4-T12: install-journal 步驟寫入（JournalStep 格式）尚由 Task 12 定義")
    }

    /// `CONFIG_DIR/config.yaml`。
    pub fn config_path(&self) -> PathBuf {
        self.config_dir().join("config.yaml")
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

    pub fn bench_state_path(&self) -> PathBuf {
        self.tmp_dir.path().join("bench_state.json")
    }

    /// `install-journal.json` 解析結果（不存在或無法解析 → None）。
    pub fn journal_json(&self) -> Option<PyValue> {
        let text = std::fs::read_to_string(self.install_root().join("install-journal.json")).ok()?;
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
        self.write_file(&self.bundle_dir().join("release-manifest.json"), manifest.as_bytes());
    }

    fn platform(&self) -> sw_pal::Platform {
        let caps = if self.windows { windows_caps() } else { posix_caps() };
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
            .unwrap_or_else(|| panic!("event not found: {s} (occurrence {n}); events={:?}", self.events))
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

pub fn s(v: &str) -> String {
    v.to_string()
}

pub fn i(n: i64) -> i64 {
    n
}

pub fn b(v: bool) -> bool {
    v
}

/// 小寫 hex 的 sha256（斷言輔助）。
pub fn sha256_hex(s: &str) -> String {
    sw_proto::pycompat::sha256_hex(s.as_bytes())
}

/// 由 (key, value) 組出 Python 語意的 dict（斷言輔助）。
pub fn dict(pairs: &[(&str, PyValue)]) -> PyValue {
    let mut d = PyDict::new();
    for (k, v) in pairs {
        d.insert(k, v.clone());
    }
    PyValue::Dict(d)
}

/// 由元素組出 list（斷言輔助）。
pub fn list(items: Vec<PyValue>) -> PyValue {
    PyValue::List(items)
}

/// argv 字串陣列 → `PyValue::List<Str>`（斷言輔助）。
pub fn argv_list(argv: &[&str]) -> PyValue {
    PyValue::List(argv.iter().map(|a| PyValue::Str((*a).to_string())).collect())
}

/// UTF-16LE 位元組（可含 BOM）→ 去 BOM 後的 code unit 序列（斷言輔助；schtasks／XML 寫出格式檢查用）。
pub fn utf16le_body(bytes: &[u8]) -> Vec<u16> {
    let body = bytes.strip_prefix(&[0xFF, 0xFE]).unwrap_or(bytes);
    body.chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect()
}

/// 解析 JSON 物件文字（斷言輔助）；非法即 panic。
pub fn json(text: &str) -> PyValue {
    loads(text).unwrap_or_else(|e| panic!("JSON 解析失敗：{e:?}（原文 {text:?}）"))
}

/// 以 Task 17 形狀組出的 release manifest（schema_version 1；sha256 由呼叫端提供）。
pub fn manifest_fixture(v: &str, cli_sha256: &str) -> String {
    format!(
        r#"{{"schema_version":1,"bundle_version":"{v}","stage":"rust-1b-test","target":"x86_64-unknown-linux-gnu","git_revision":"test","components":{{"cli":{{"implementation":"rust","version":"{v}","path":"bin/serialwrap","sha256":"{cli_sha256}"}},"python_cli_delegate":{{"entry":["py/bin/python3","-I","delegate_sentinel.py"]}}}}}}"#
    )
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
