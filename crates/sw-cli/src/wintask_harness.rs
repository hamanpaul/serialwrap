//! P8 Task 13：`windows-task` 實跑測試的正式停止路徑 harness（只在 `test-fakes` 建置；正式 release 不含本模組）。
//!
//! 計畫 Task 13 要求實跑測試以 P4 `graceful_stop` 停止受測 daemon。凍結案例 `s24-sup-wintask-runtime` 的入口
//! `crates/sw-pal/tests/wintask_runtime.rs` 位於 sw-pal，而 sw-pal 不得依賴任何 workspace 成員（arch §1.3；
//! `scripts/rust/check_dep_direction.py` 連 dev-dependency 一併檢查），因此改由獨立行程
//! `wintask_stop_harness`（`src/test_support/wintask_stop_harness.rs`）執行正式的 `serialwrap service stop`
//! （[`crate::run`] → `run_service` → `service_action_response` → `graceful_stop`），實跑測試只讀其輸出判定。
//!
//! 正式路徑本身不改；harness 只在平台邊界注入三層限制（擁有者裁決 2026-11-08「T13 的 Windows 排程工作實跑」：
//! 不得碰正式 `\serialwrap` 工作、48700 埠與使用者的 state／config）：
//! 1. [`ScopedEnv`]（`SysEnv`）：CLI 只看得到呼叫端指定的 `SERIALWRAP_*`（繼承的一律隱藏，config、埠與目錄都落在
//!    staging）；子行程只允許 `schtasks`（恰一個 `/TN`）與 `Get-ScheduledTask` 狀態查詢，固定名稱 `serialwrap`
//!    改寫為測試工作 `sw-p8-test-<pid>`，其他名稱或命令一律拒絕。
//! 2. 監管控制為包住 [`ScopedEnv`] 的正式 `WindowsTaskCtl`：PAL 產生的 argv 原樣執行，只有工作名稱被改寫。
//! 3. [`ScopedRpc`]（`RpcConnector`）：只允許 `tcp://127.0.0.1:<測試埠>`；正式 48700 埠、其他主機與 unix socket
//!    一律拒絕，並記錄實際送出的 RPC 方法。
//!
//! 本模組不含平台 cfg：memory_only 測試（`tests/wintask_scoped_stop.rs`）以 `FakeSysEnv`／`FakeRpcConnector` 驗證，
//! 實跑只在 Windows 由 sw-pal 的 `wintask_runtime.rs` 呼叫 harness 執行檔。

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use sw_pal::traits::{
    CmdOutput, CmdOutputRaw, EndpointSpec, InterruptScope, PalError, PalErrorKind, PalResult,
    RpcConnector, RpcStream, ServiceCtl, SockTimeout, SysEnv, WindowsTaskCtl,
};
use sw_proto::pyjson::{dumps_dict, loads, PyDict, PyInt, PyValue, CLI_STDOUT};

fn lock_log(log: &Arc<Mutex<HarnessLog>>) -> std::sync::MutexGuard<'_, HarnessLog> {
    log.lock().unwrap_or_else(|error| error.into_inner())
}

fn denied(message: impl Into<String>) -> PalError {
    PalError::plain(PalErrorKind::PermissionDenied, message)
}

fn canonical_path(path: &str) -> String {
    path.replace('\\', "/")
        .trim_end_matches('/')
        .to_ascii_lowercase()
}

fn task_query_argv(task: &str) -> Vec<String> {
    vec![
        "powershell.exe".into(),
        "-NoProfile".into(),
        "-NonInteractive".into(),
        "-Command".into(),
        format!("try {{ (Get-ScheduledTask -TaskName '{task}' -ErrorAction Stop).State.ToString() }} catch {{ if ($_.CategoryInfo.Category -eq 'ObjectNotFound') {{ 'NotFound' }} else {{ exit 3 }} }}"),
    ]
}

/// 測試排程工作名稱前綴（擁有者裁決：`sw-p8-test-<pid>`）。
pub const TASK_PREFIX: &str = "sw-p8-test-";
/// 正式排程工作名稱：harness 絕不操作。
pub const PRODUCTION_TASK: &str = "serialwrap";
/// 正式 daemon 的 TCP 埠：harness 絕不連線。
pub const PRODUCTION_PORT: u16 = 48700;
/// 呼叫端必須提供的 staging 變數：CLI 的 config 與狀態目錄解析不得落回使用者預設路徑。
pub const REQUIRED_VARS: [&str; 4] = [
    "SERIALWRAP_CONFIG",
    "SERIALWRAP_CONFIG_DIR",
    "SERIALWRAP_STATE_DIR",
    "SERIALWRAP_RUN_DIR",
];
/// 由 harness 自行決定、呼叫端不得指定的變數（endpoint 一律為 `tcp://127.0.0.1:<測試埠>`）。
pub const RESERVED_VARS: [&str; 2] = ["SERIALWRAP_TCP_PORT", "SERIALWRAP_SOCKET"];

/// 名稱守門：只允許 `sw-p8-test-<十進位數字>`。
pub fn check_task_name(name: &str) -> Result<(), String> {
    let Some(pid) = name.strip_prefix(TASK_PREFIX) else {
        return Err(format!("排程工作名稱必須以 {TASK_PREFIX} 開頭"));
    };
    if pid.is_empty() || !pid.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err("排程工作名稱後綴必須是十進位 PID".into());
    }
    Ok(())
}

/// harness 的一次停止要求。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScopedStopRequest {
    /// 測試排程工作名稱（`sw-p8-test-<pid>`）。
    pub task: String,
    /// 測試 daemon 的 TCP 埠（≠ 48700）。
    pub port: u16,
    /// CLI 可見的 `SERIALWRAP_*`（staging 目錄與 config）；不含 [`RESERVED_VARS`]。
    pub vars: Vec<(String, String)>,
}

/// 解析 harness 命令列：`--task <name> --port <port> [--var K=V]...`（不含程式名）。
pub fn parse_harness_args(args: &[String]) -> Result<ScopedStopRequest, String> {
    let mut task = None;
    let mut port = None;
    let mut vars = Vec::new();
    let mut index = 0;
    while index < args.len() {
        let key = args[index].as_str();
        index += 1;
        let value = args.get(index).ok_or_else(|| format!("{key} 缺少值"))?;
        index += 1;
        match key {
            "--task" if task.is_none() => task = Some(value.clone()),
            "--port" if port.is_none() => {
                let parsed = value
                    .parse::<u16>()
                    .map_err(|_| "--port 必須是 1..=65535 的整數".to_string())?;
                if parsed == 0 || parsed == PRODUCTION_PORT {
                    return Err(format!("--port 不得為 0 或正式埠 {PRODUCTION_PORT}"));
                }
                port = Some(parsed);
            }
            "--var" => {
                let (name, value) = value
                    .split_once('=')
                    .ok_or_else(|| "--var 格式必須是 KEY=VALUE".to_string())?;
                if name.is_empty() || value.is_empty() {
                    return Err("--var 的 KEY 與 VALUE 不得為空".into());
                }
                vars.push((name.to_string(), value.to_string()));
            }
            _ => return Err(format!("未知、重複或順序錯誤的參數：{key}")),
        }
    }
    let task = task.ok_or_else(|| "缺少 --task".to_string())?;
    check_task_name(&task)?;
    let port = port.ok_or_else(|| "缺少 --port".to_string())?;
    let mut seen = Vec::new();
    for (name, _) in &vars {
        if !name.to_ascii_uppercase().starts_with("SERIALWRAP_") {
            return Err(format!("只允許 SERIALWRAP_* 變數：{name}"));
        }
        if RESERVED_VARS
            .iter()
            .any(|reserved| name.eq_ignore_ascii_case(reserved))
        {
            return Err(format!("不可指定由 harness 保留的變數：{name}"));
        }
        if seen
            .iter()
            .any(|prior: &String| prior.eq_ignore_ascii_case(name))
        {
            return Err(format!("重複變數：{name}"));
        }
        seen.push(name.clone());
    }
    Ok(ScopedStopRequest { task, port, vars })
}

/// harness 觀察到的平台邊界事件。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HarnessLog {
    /// 實際執行的子行程（改寫後 argv、rc；無法執行為 -1）。
    pub ran: Vec<(Vec<String>, i32)>,
    /// 實際送出的 RPC 方法（依序）。
    pub rpc: Vec<String>,
    /// 被拒絕的子行程或連線（說明文字）。
    pub refused: Vec<String>,
}

/// harness 的結果：正式 `serialwrap service stop` 的 exit code、stdout、stderr 與平台邊界紀錄。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ScopedStopReport {
    pub rc: i32,
    pub stdout: String,
    pub stderr: String,
    pub log: HarnessLog,
}

impl ScopedStopReport {
    /// 一行 JSON（`CLI_STDOUT`：緊湊、鍵排序）：`{"ran":[{"argv":[…],"rc":0}],"rc":0,"refused":[…],"rpc":[…],"stderr":"…","stdout":"…"}`。
    pub fn to_json(&self) -> String {
        let mut root = PyDict::new();
        let ran = self
            .log
            .ran
            .iter()
            .map(|(argv, rc)| {
                let mut item = PyDict::new();
                item.insert(
                    "argv",
                    PyValue::List(argv.iter().cloned().map(PyValue::Str).collect()),
                );
                item.insert("rc", PyValue::Int(PyInt::Small(i128::from(*rc))));
                PyValue::Dict(item)
            })
            .collect();
        root.insert("ran", PyValue::List(ran));
        root.insert("rc", PyValue::Int(PyInt::Small(i128::from(self.rc))));
        root.insert(
            "refused",
            PyValue::List(self.log.refused.iter().cloned().map(PyValue::Str).collect()),
        );
        root.insert(
            "rpc",
            PyValue::List(self.log.rpc.iter().cloned().map(PyValue::Str).collect()),
        );
        root.insert("stderr", PyValue::Str(self.stderr.clone()));
        root.insert("stdout", PyValue::Str(self.stdout.clone()));
        dumps_dict(&root, &CLI_STDOUT)
    }
}

/// 限定 CLI 可見環境與可執行子行程的 `SysEnv`（見模組說明第 1 點）。
#[allow(dead_code)]
pub struct ScopedEnv {
    inner: Arc<dyn SysEnv>,
    task: String,
    vars: Vec<(String, String)>,
    log: Arc<Mutex<HarnessLog>>,
}

impl ScopedEnv {
    pub fn new(
        inner: Arc<dyn SysEnv>,
        task: &str,
        vars: Vec<(String, String)>,
        log: Arc<Mutex<HarnessLog>>,
    ) -> Result<ScopedEnv, String> {
        Self::build(inner, task, vars, None, log)
    }

    fn for_request(
        inner: Arc<dyn SysEnv>,
        task: &str,
        vars: Vec<(String, String)>,
        port: u16,
        log: Arc<Mutex<HarnessLog>>,
    ) -> Result<ScopedEnv, String> {
        Self::build(inner, task, vars, Some(port), log)
    }

    fn build(
        inner: Arc<dyn SysEnv>,
        task: &str,
        mut vars: Vec<(String, String)>,
        port: Option<u16>,
        log: Arc<Mutex<HarnessLog>>,
    ) -> Result<ScopedEnv, String> {
        check_task_name(task)?;
        let mut seen = Vec::new();
        for (key, value) in &vars {
            if !key.to_ascii_uppercase().starts_with("SERIALWRAP_") {
                return Err(format!("只允許 SERIALWRAP_* 變數：{key}"));
            }
            if RESERVED_VARS
                .iter()
                .any(|reserved| key.eq_ignore_ascii_case(reserved))
            {
                return Err(format!("不可指定由 harness 保留的變數：{key}"));
            }
            if value.is_empty() {
                return Err(format!("變數值不得為空：{key}"));
            }
            if seen
                .iter()
                .any(|prior: &String| prior.eq_ignore_ascii_case(key))
            {
                return Err(format!("重複變數：{key}"));
            }
            seen.push(key.clone());
        }
        if let Some(port) = port {
            if port == 0 || port == PRODUCTION_PORT {
                return Err(format!("測試 RPC 埠不得為 0 或正式埠 {PRODUCTION_PORT}"));
            }
            vars.push(("SERIALWRAP_TCP_PORT".into(), port.to_string()));
        }
        Ok(ScopedEnv {
            inner,
            task: task.to_string(),
            vars,
            log,
        })
    }

    /// 允許的子行程改寫為測試工作名稱；其餘回 `Err(原因)`。
    pub fn scope_argv(&self, argv: &[String]) -> Result<Vec<String>, String> {
        if argv.len() == 5
            && (argv[0].eq_ignore_ascii_case("schtasks")
                || argv[0].eq_ignore_ascii_case("schtasks.exe"))
            && argv[1].eq_ignore_ascii_case("/change")
            && argv[2].eq_ignore_ascii_case("/tn")
            && matches!(
                argv[4].to_ascii_uppercase().as_str(),
                "/DISABLE" | "/ENABLE"
            )
        {
            return self.rewrite_task_arg(argv, 3);
        }
        if argv.len() == 4
            && (argv[0].eq_ignore_ascii_case("schtasks")
                || argv[0].eq_ignore_ascii_case("schtasks.exe"))
            && matches!(argv[1].to_ascii_lowercase().as_str(), "/end" | "/run")
            && argv[2].eq_ignore_ascii_case("/tn")
        {
            return self.rewrite_task_arg(argv, 3);
        }
        if argv == task_query_argv(PRODUCTION_TASK) || argv == task_query_argv(&self.task) {
            let mut scoped = argv.to_vec();
            scoped[4] = task_query_argv(&self.task)[4].clone();
            return Ok(scoped);
        }
        Err(format!("不允許的監管子行程：{argv:?}"))
    }

    fn rewrite_task_arg(&self, argv: &[String], task_index: usize) -> Result<Vec<String>, String> {
        let name = &argv[task_index];
        if !name.eq_ignore_ascii_case(PRODUCTION_TASK) && !name.eq_ignore_ascii_case(&self.task) {
            return Err(format!("排程工作名稱不在測試範圍：{name}"));
        }
        let mut scoped = argv.to_vec();
        scoped[task_index] = self.task.clone();
        Ok(scoped)
    }
}

impl SysEnv for ScopedEnv {
    fn var(&self, k: &str) -> Option<String> {
        if k.to_ascii_uppercase().starts_with("SERIALWRAP_") {
            return self
                .vars
                .iter()
                .find(|(key, _)| key.eq_ignore_ascii_case(k))
                .map(|(_, value)| value.clone());
        }
        self.inner.var(k)
    }
    fn vars(&self) -> Vec<(String, String)> {
        let mut vars = self
            .inner
            .vars()
            .into_iter()
            .filter(|(key, _)| !key.to_ascii_uppercase().starts_with("SERIALWRAP_"))
            .collect::<Vec<_>>();
        vars.extend(self.vars.clone());
        vars
    }
    fn system_config_dir(&self) -> String {
        self.inner.system_config_dir()
    }
    fn current_user_home(&self) -> Option<String> {
        self.inner.current_user_home()
    }
    fn user_home(&self, user: &str) -> Option<String> {
        self.inner.user_home(user)
    }
    fn current_exe(&self) -> PalResult<PathBuf> {
        self.inner.current_exe()
    }
    fn realpath(&self, p: &Path) -> PalResult<PathBuf> {
        self.inner.realpath(p)
    }
    fn terminal_columns(&self) -> Option<u16> {
        self.inner.terminal_columns()
    }
    fn stdout_is_terminal(&self) -> bool {
        self.inner.stdout_is_terminal()
    }
    fn errno_name(&self, code: i32) -> Option<&'static str> {
        self.inner.errno_name(code)
    }
    fn pid(&self) -> u32 {
        self.inner.pid()
    }
    fn path_flavor(&self) -> sw_pal::PalPathFlavor {
        self.inner.path_flavor()
    }
    fn run(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput> {
        let scoped = self.scope_argv(argv).map_err(|error| {
            lock_log(&self.log).refused.push(error.clone());
            denied(error)
        })?;
        let result = self.inner.run(&scoped, timeout);
        lock_log(&self.log)
            .ran
            .push((scoped, result.as_ref().map_or(-1, |output| output.rc)));
        result
    }
    fn run_merged(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput> {
        let scoped = self.scope_argv(argv).map_err(|error| {
            lock_log(&self.log).refused.push(error.clone());
            denied(error)
        })?;
        let result = self.inner.run_merged(&scoped, timeout);
        lock_log(&self.log)
            .ran
            .push((scoped, result.as_ref().map_or(-1, |output| output.rc)));
        result
    }
    fn interrupt_scope(&self) -> PalResult<Box<dyn InterruptScope>> {
        self.inner.interrupt_scope()
    }
    fn run_raw(
        &self,
        argv: &[String],
        env: Option<&[(OsString, OsString)]>,
        timeout: Option<Duration>,
    ) -> PalResult<CmdOutputRaw> {
        let scoped = self.scope_argv(argv).map_err(|error| {
            lock_log(&self.log).refused.push(error.clone());
            denied(error)
        })?;
        let result = self.inner.run_raw(&scoped, env, timeout);
        lock_log(&self.log)
            .ran
            .push((scoped, result.as_ref().map_or(-1, |output| output.rc)));
        result
    }
    fn which(&self, name: &str) -> Option<PathBuf> {
        self.inner.which(name)
    }
    fn which_all(&self, name: &str) -> Vec<PathBuf> {
        self.inner.which_all(name)
    }
    fn has_systemd(&self) -> bool {
        self.inner.has_systemd()
    }
    fn is_wsl(&self) -> bool {
        self.inner.is_wsl()
    }
    fn user_in_group(&self, g: &str) -> bool {
        self.inner.user_in_group(g)
    }
    fn user_identity(&self) -> String {
        self.inner.user_identity()
    }
    fn local_appdata(&self) -> Option<PathBuf> {
        self.inner.local_appdata()
    }
}

/// 只允許 `tcp://127.0.0.1:<測試埠>` 的 `RpcConnector`（見模組說明第 3 點）。
#[allow(dead_code)]
pub struct ScopedRpc {
    inner: Arc<dyn RpcConnector>,
    port: u16,
    log: Arc<Mutex<HarnessLog>>,
}

impl ScopedRpc {
    pub fn new(inner: Arc<dyn RpcConnector>, port: u16, log: Arc<Mutex<HarnessLog>>) -> ScopedRpc {
        ScopedRpc { inner, port, log }
    }
}

impl RpcConnector for ScopedRpc {
    fn af_unix_available(&self) -> bool {
        false
    }
    fn connect(&self, ep: &EndpointSpec, t: SockTimeout) -> PalResult<Box<dyn RpcStream>> {
        match ep {
            EndpointSpec::Tcp { host, port } if host == "127.0.0.1" && *port == self.port => {
                let stream = self.inner.connect(ep, t)?;
                Ok(Box::new(ScopedStream {
                    inner: stream,
                    log: Arc::clone(&self.log),
                }))
            }
            _ => {
                let error = format!("RPC endpoint 不在測試範圍：{ep:?}");
                lock_log(&self.log).refused.push(error.clone());
                Err(denied(error))
            }
        }
    }
    fn resolve_tcp(&self, host: &str, port: u16) -> PalResult<()> {
        if host == "127.0.0.1" && port == self.port {
            self.inner.resolve_tcp(host, port)
        } else {
            let error = format!("RPC endpoint 不在測試範圍：{host}:{port}");
            lock_log(&self.log).refused.push(error.clone());
            Err(denied(error))
        }
    }
}

struct ScopedStream {
    inner: Box<dyn RpcStream>,
    log: Arc<Mutex<HarnessLog>>,
}

impl RpcStream for ScopedStream {
    fn send_all(&mut self, data: &[u8]) -> PalResult<()> {
        self.inner.send_all(data)?;
        if let Ok(text) = std::str::from_utf8(data) {
            if let Ok(value) = loads(text.trim_end_matches(['\r', '\n'])) {
                if let Some(PyValue::Str(method)) = value.get("method") {
                    lock_log(&self.log).rpc.push(method.clone());
                }
            }
        }
        Ok(())
    }

    fn recv(&mut self, buf: &mut [u8]) -> PalResult<usize> {
        self.inner.recv(buf)
    }
}

/// 以限定後的平台執行正式 `serialwrap service stop`，回傳結果與平台邊界紀錄。
///
/// `base` 為呼叫端的平台（實跑為 `sw_pal::native_client()`，memory_only 測試為替身）；`env`、`rpc`、`service`
/// 三項被替換，其餘原樣沿用。要求不合法（非測試工作名稱、正式埠、缺 staging 變數、指定保留變數）→ `Err`，
/// 不執行任何命令。
pub fn scoped_service_stop(
    base: sw_pal::Platform,
    req: &ScopedStopRequest,
) -> Result<ScopedStopReport, String> {
    check_task_name(&req.task)?;
    if req.port == 0 || req.port == PRODUCTION_PORT {
        return Err(format!("測試 RPC 埠不得為 0 或正式埠 {PRODUCTION_PORT}"));
    }
    let vars = req.vars.clone();
    let mut seen = Vec::new();
    for (key, value) in &vars {
        if !key.to_ascii_uppercase().starts_with("SERIALWRAP_") {
            return Err(format!("只允許 SERIALWRAP_* 變數：{key}"));
        }
        if RESERVED_VARS
            .iter()
            .any(|reserved| key.eq_ignore_ascii_case(reserved))
        {
            return Err(format!("不可指定由 harness 保留的變數：{key}"));
        }
        if value.is_empty() {
            return Err(format!("變數值不得為空：{key}"));
        }
        if seen
            .iter()
            .any(|prior: &String| prior.eq_ignore_ascii_case(key))
        {
            return Err(format!("重複變數：{key}"));
        }
        seen.push(key.clone());
    }
    for required in REQUIRED_VARS {
        if !vars
            .iter()
            .any(|(key, _)| key.eq_ignore_ascii_case(required))
        {
            return Err(format!("缺少 staging 變數：{required}"));
        }
    }
    let get = |name: &str| {
        vars.iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    };
    let state = canonical_path(get("SERIALWRAP_STATE_DIR").unwrap());
    let run = canonical_path(get("SERIALWRAP_RUN_DIR").unwrap());
    let config_dir = canonical_path(get("SERIALWRAP_CONFIG_DIR").unwrap());
    let root_state = state
        .strip_suffix("/state")
        .ok_or_else(|| r"STATE_DIR 必須是 staging\state".to_string())?;
    let root_run = run
        .strip_suffix("/run")
        .ok_or_else(|| r"RUN_DIR 必須是 staging\run".to_string())?;
    let root_config = config_dir
        .strip_suffix("/config")
        .ok_or_else(|| r"CONFIG_DIR 必須是 staging\config".to_string())?;
    if root_state.is_empty() || root_state != root_run || root_state != root_config {
        return Err("STATE_DIR、RUN_DIR、CONFIG_DIR 必須是同一 staging 根目錄的子目錄".into());
    }
    let config = canonical_path(get("SERIALWRAP_CONFIG").unwrap());
    if config != format!("{config_dir}/cli-config.yaml") {
        return Err(r"SERIALWRAP_CONFIG 必須是 staging config\cli-config.yaml".into());
    }
    for (key, value) in &vars {
        let value = canonical_path(value);
        if !value.starts_with(&format!("{root_state}/"))
            || value.split('/').any(|part| part == "..")
        {
            return Err(format!("{key} 必須位於 staging 根目錄內"));
        }
    }
    let log = Arc::new(Mutex::new(HarnessLog::default()));
    let env: Arc<dyn SysEnv> = Arc::new(ScopedEnv::for_request(
        Arc::clone(&base.env),
        &req.task,
        vars,
        req.port,
        Arc::clone(&log),
    )?);
    let mut platform = base;
    platform.env = Arc::clone(&env);
    platform.rpc = Arc::new(ScopedRpc::new(
        Arc::clone(&platform.rpc),
        req.port,
        Arc::clone(&log),
    ));
    platform.service = Arc::new(WindowsTaskCtl::new(
        Arc::clone(&env),
        Arc::clone(&platform.fs),
        Arc::clone(&platform.clock),
    )) as Arc<dyn ServiceCtl>;

    let argv = ["serialwrap", "service", "stop"]
        .into_iter()
        .map(std::ffi::OsString::from)
        .collect();
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let rc = crate::run(argv, &platform, &mut stdout, &mut stderr);
    let stdout = String::from_utf8_lossy(&stdout).into_owned();
    let stderr = String::from_utf8_lossy(&stderr).into_owned();
    let log = lock_log(&log).clone();
    Ok(ScopedStopReport {
        rc,
        stdout,
        stderr,
        log,
    })
}
