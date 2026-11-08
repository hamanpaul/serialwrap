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
    CmdOutput, CmdOutputRaw, EndpointSpec, InterruptScope, PalResult, RpcConnector, RpcStream,
    SockTimeout, SysEnv,
};

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
    todo!("P8-T13: check_task_name")
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
    todo!("P8-T13: parse_harness_args")
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
        todo!("P8-T13: ScopedStopReport::to_json")
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
        todo!("P8-T13: ScopedEnv::new")
    }

    /// 允許的子行程改寫為測試工作名稱；其餘回 `Err(原因)`。
    pub fn scope_argv(&self, argv: &[String]) -> Result<Vec<String>, String> {
        todo!("P8-T13: ScopedEnv::scope_argv")
    }
}

impl SysEnv for ScopedEnv {
    fn var(&self, k: &str) -> Option<String> {
        todo!("P8-T13: ScopedEnv::var")
    }
    fn vars(&self) -> Vec<(String, String)> {
        todo!("P8-T13: ScopedEnv::vars")
    }
    fn system_config_dir(&self) -> String {
        todo!("P8-T13: ScopedEnv::system_config_dir")
    }
    fn current_user_home(&self) -> Option<String> {
        todo!("P8-T13: ScopedEnv::current_user_home")
    }
    fn user_home(&self, user: &str) -> Option<String> {
        todo!("P8-T13: ScopedEnv::user_home")
    }
    fn current_exe(&self) -> PalResult<PathBuf> {
        todo!("P8-T13: ScopedEnv::current_exe")
    }
    fn realpath(&self, p: &Path) -> PalResult<PathBuf> {
        todo!("P8-T13: ScopedEnv::realpath")
    }
    fn terminal_columns(&self) -> Option<u16> {
        todo!("P8-T13: ScopedEnv::terminal_columns")
    }
    fn stdout_is_terminal(&self) -> bool {
        todo!("P8-T13: ScopedEnv::stdout_is_terminal")
    }
    fn errno_name(&self, code: i32) -> Option<&'static str> {
        todo!("P8-T13: ScopedEnv::errno_name")
    }
    fn pid(&self) -> u32 {
        todo!("P8-T13: ScopedEnv::pid")
    }
    fn path_flavor(&self) -> sw_pal::PalPathFlavor {
        todo!("P8-T13: ScopedEnv::path_flavor")
    }
    fn run(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput> {
        todo!("P8-T13: ScopedEnv::run")
    }
    fn run_merged(&self, argv: &[String], timeout: Option<Duration>) -> PalResult<CmdOutput> {
        todo!("P8-T13: ScopedEnv::run_merged")
    }
    fn interrupt_scope(&self) -> PalResult<Box<dyn InterruptScope>> {
        todo!("P8-T13: ScopedEnv::interrupt_scope")
    }
    fn run_raw(
        &self,
        argv: &[String],
        env: Option<&[(OsString, OsString)]>,
        timeout: Option<Duration>,
    ) -> PalResult<CmdOutputRaw> {
        todo!("P8-T13: ScopedEnv::run_raw")
    }
    fn which(&self, name: &str) -> Option<PathBuf> {
        todo!("P8-T13: ScopedEnv::which")
    }
    fn which_all(&self, name: &str) -> Vec<PathBuf> {
        todo!("P8-T13: ScopedEnv::which_all")
    }
    fn has_systemd(&self) -> bool {
        todo!("P8-T13: ScopedEnv::has_systemd")
    }
    fn is_wsl(&self) -> bool {
        todo!("P8-T13: ScopedEnv::is_wsl")
    }
    fn user_in_group(&self, g: &str) -> bool {
        todo!("P8-T13: ScopedEnv::user_in_group")
    }
    fn user_identity(&self) -> String {
        todo!("P8-T13: ScopedEnv::user_identity")
    }
    fn local_appdata(&self) -> Option<PathBuf> {
        todo!("P8-T13: ScopedEnv::local_appdata")
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
        todo!("P8-T13: ScopedRpc::af_unix_available")
    }
    fn connect(&self, ep: &EndpointSpec, t: SockTimeout) -> PalResult<Box<dyn RpcStream>> {
        todo!("P8-T13: ScopedRpc::connect")
    }
    fn resolve_tcp(&self, host: &str, port: u16) -> PalResult<()> {
        todo!("P8-T13: ScopedRpc::resolve_tcp")
    }
}

/// 以限定後的平台執行正式 `serialwrap service stop`，回傳結果與平台邊界紀錄。
///
/// `base` 為呼叫端的平台（實跑為 `sw_pal::native_client()`，memory_only 測試為替身）；`env`、`rpc`、`service`
/// 三項被替換，其餘原樣沿用。要求不合法（非測試工作名稱、正式埠、缺 staging 變數、指定保留變數）→ `Err`，
/// 不執行任何命令。
pub fn scoped_service_stop(base: sw_pal::Platform, req: &ScopedStopRequest) -> Result<ScopedStopReport, String> {
    todo!("P8-T13: scoped_service_stop")
}
