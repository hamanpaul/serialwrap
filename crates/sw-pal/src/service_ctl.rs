//! 監管服務控制（P4-T3）：systemd、`windows-task`（Task XML／schtasks）、`windows-service`（sc.exe）與交接前拉起抑制。
//!
//! 型別、簽章與建構子由 P4-T1 交付；所有行為方法的預設主體為 `unimplemented!("P4-T3")`，由 P4-T3 替換。
//! 命令產生寫成不帶 `cfg` 的共用程式碼（arch §4.8）；平台差異僅在 `posix`／`windows` 的 host 模式清單。
//! `SystemdCtl`／`WindowsTaskCtl`／`WindowsServiceCtl` 分別位於 `systemd`、`windows_task`、`windows_service` 子模組並由此再匯出。
#![allow(unused_variables)]

use std::path::PathBuf;
use std::sync::Arc;

use crate::fs_ops::FsOps;
use crate::traits::{Clock, SysEnv};

#[cfg(unix)]
pub mod posix;
pub mod systemd;
pub mod windows_service;
pub mod windows_task;
#[cfg(windows)]
pub mod windows;

pub use systemd::SystemdCtl;
pub use windows_service::WindowsServiceCtl;
pub use windows_task::WindowsTaskCtl;

/// 監管模式（單一事實來源：`config.yaml` 的 `supervision_mode`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SupervisionMode {
    OnDemand,
    SystemdUser,
    SystemdSystem,
    WindowsTask,
    WindowsService,
    Other(String),
}

impl SupervisionMode {
    /// `on-demand|systemd-user|systemd-system|windows-task|windows-service`，其他 → `Other(s)`。
    pub fn parse(s: &str) -> Self {
        unimplemented!("P4-T3")
    }
    pub fn as_str(&self) -> &str {
        unimplemented!("P4-T3")
    }
    /// 前述 4 個監管模式為 true。
    pub fn is_supervised(&self) -> bool {
        unimplemented!("P4-T3")
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServiceAction {
    Start,
    Stop,
    Restart,
    Status,
}

impl ServiceAction {
    pub fn parse(s: &str) -> Option<Self> {
        unimplemented!("P4-T3")
    }
    pub fn as_str(&self) -> &'static str {
        unimplemented!("P4-T3")
    }
}

/// 監管動作結果；成功形狀 `{ok, mode, action, rc, stdout, stderr}`，失敗附 `error_code`／`hint`／`phase`。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceResult {
    pub ok: bool,
    pub mode: String,
    pub action: String,
    pub rc: Option<i32>,
    pub stdout: Option<String>,
    pub stderr: Option<String>,
    pub error_code: Option<&'static str>,
    pub hint: Option<String>,
    pub phase: Option<&'static str>,
}

/// 監管項定義。`register_held`：true＝以 hold 狀態註冊（Task 12 update hold 期間的重新註冊）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SupervisorDefinition {
    pub mode: SupervisionMode,
    pub name: String,
    pub exec: Vec<String>,
    pub working_dir: Option<PathBuf>,
    pub run_user: Option<String>,
    pub env: Vec<(String, String)>,
    pub definition_path: Option<PathBuf>,
    pub register_held: bool,
}

/// 一筆已執行（或僅產生）的命令紀錄。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CmdRecord {
    pub argv: Vec<String>,
    pub rc: i32,
}

/// `query` 結果；`QueryFailed` 永不視為已停止。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TaskQuery {
    Running,
    NotRunning(String),
    NotFound,
    QueryFailed(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AutostartHoldKind {
    SystemdDropIn,
    TaskDisabled,
    ServiceDisabled,
    /// on-demand：PAL 無監管項；sw-cli 以 autostart.lock＋install-journal 閘門承擔（Task 7 SC-18）。
    NoSupervisor,
}

/// 交接前抑制拉起的紀錄（持久化於 journal，由 `release_autostart` 撤銷）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AutostartHoldRecord {
    pub mode: SupervisionMode,
    pub kind: AutostartHoldKind,
    pub dropin_path: Option<PathBuf>,
    pub prior_enabled: Option<bool>,
    /// systemd `Restart=` 原值，例如 `"on-failure"`。
    pub prior_restart: Option<String>,
    pub ran: Vec<CmdRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HoldState {
    Held,
    NotHeld,
    QueryFailed(String),
}

/// `render_task_xml` 的輸入（golden：`crates/sw-pal/tests/golden/serialwrap.task.xml`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TaskXmlSpec {
    pub name: String,
    pub user_id: String,
    pub command: String,
    pub arguments: String,
    pub working_dir: String,
    /// `Settings/Enabled`；update hold 期間的重新註冊為 false。
    pub enabled: bool,
}

/// 輸出 Task Scheduler XML（CRLF、`ExecutionTimeLimit=PT0S`）。紅燈骨架。
pub fn render_task_xml(spec: &TaskXmlSpec) -> String {
    unimplemented!("P4-T3")
}

/// UTF-16LE 並加 BOM（`FF FE`）。紅燈骨架。
pub fn encode_utf16le_bom(s: &str) -> Vec<u8> {
    unimplemented!("P4-T3")
}

/// `subprocess.list2cmdline` 規則的反向（純函式，不呼叫 SHELL32）。紅燈骨架。
pub fn shell_join_windows(args: &[String]) -> String {
    unimplemented!("P4-T3")
}

/// MSVCRT／`CommandLineToArgvW` 解析規則（純函式自寫，不呼叫 SHELL32）。紅燈骨架。
pub fn split_windows_cmdline(s: &str) -> Vec<String> {
    unimplemented!("P4-T3")
}

/// 監管控制介面（arch §4.8）。行為方法的預設主體為紅燈占位，由 `SystemdCtl`／`WindowsTaskCtl`／`WindowsServiceCtl` 沿用直到實作。
pub trait ServiceCtl: Send + Sync {
    fn supported_modes(&self) -> Vec<SupervisionMode> {
        unimplemented!("P4-T3")
    }
    /// 固定名稱 `serialwrap`。
    fn action(&self, mode: &SupervisionMode, action: ServiceAction, with_sudo: bool) -> ServiceResult {
        unimplemented!("P4-T3")
    }
    fn action_named(&self, mode: &SupervisionMode, name: &str, action: ServiceAction, with_sudo: bool) -> ServiceResult {
        unimplemented!("P4-T3")
    }
    fn install(&self, def: &SupervisorDefinition, with_sudo: bool) -> Vec<CmdRecord> {
        unimplemented!("P4-T3")
    }
    /// 不執行，供 `pending_sudo`。
    fn install_plan(&self, def: &SupervisorDefinition) -> Vec<Vec<String>> {
        unimplemented!("P4-T3")
    }
    fn uninstall(&self, mode: &SupervisionMode, with_sudo: bool) -> Vec<CmdRecord> {
        unimplemented!("P4-T3")
    }
    /// 具名版本（與 `action_named` 同語意；cap 測試需要，Task 1 Produces 未列，骨架補上）。
    fn uninstall_named(&self, mode: &SupervisionMode, name: &str, with_sudo: bool) -> Vec<CmdRecord> {
        unimplemented!("P4-T3")
    }
    fn set_enabled(&self, mode: &SupervisionMode, enabled: bool, with_sudo: bool) -> Vec<CmdRecord> {
        unimplemented!("P4-T3")
    }
    /// 固定名稱 `serialwrap`。
    fn query(&self, mode: &SupervisionMode) -> TaskQuery {
        unimplemented!("P4-T3")
    }
    /// 具名版本（與 `query` 同語意；骨架補上）。
    fn query_named(&self, mode: &SupervisionMode, name: &str) -> TaskQuery {
        unimplemented!("P4-T3")
    }
    /// windows-task `/End`；systemd `stop`。
    fn hard_stop(&self, mode: &SupervisionMode, with_sudo: bool) -> ServiceResult {
        unimplemented!("P4-T3")
    }
    /// R5-F1：交接前抑制監管拉起（持久、跨 CLI 中斷有效，只由 `release_autostart` 撤銷）。
    fn hold_autostart(&self, mode: &SupervisionMode, with_sudo: bool) -> Result<AutostartHoldRecord, ServiceResult> {
        unimplemented!("P4-T3")
    }
    /// 冪等：hold 不存在也成功。
    fn release_autostart(
        &self,
        hold: &AutostartHoldRecord,
        restore_enabled: Option<bool>,
        with_sudo: bool,
    ) -> Result<Vec<CmdRecord>, ServiceResult> {
        unimplemented!("P4-T3")
    }
    fn autostart_hold_state(&self, mode: &SupervisionMode) -> HoldState {
        unimplemented!("P4-T3")
    }
    /// 同 `hold_autostart`，unit／task 名可指定（cap 測試用）。
    fn hold_autostart_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        with_sudo: bool,
    ) -> Result<AutostartHoldRecord, ServiceResult> {
        unimplemented!("P4-T3")
    }
}

/// 組合式監管控制（arch §4.8）：依 `modes` 分派到 systemd／windows-task／windows-service 實作。
///
/// P4-T1 只交付建構子與 `supported_modes`；分派行為由 P4-T3 實作（預設主體 `unimplemented!("P4-T3")`）。
#[allow(dead_code)]
pub struct CompositeServiceCtl {
    modes: Vec<SupervisionMode>,
    systemd: SystemdCtl,
    task: WindowsTaskCtl,
    service: WindowsServiceCtl,
}

impl CompositeServiceCtl {
    pub fn new(
        modes: Vec<SupervisionMode>,
        systemd: SystemdCtl,
        task: WindowsTaskCtl,
        service: WindowsServiceCtl,
    ) -> Self {
        Self {
            modes,
            systemd,
            task,
            service,
        }
    }

    /// 目前平台的組裝（`lib.rs` 的 `native()` 呼叫）：POSIX `[OnDemand, SystemdUser, SystemdSystem]`；
    /// Windows `[OnDemand, WindowsTask, WindowsService]`。
    pub fn native(env: Arc<dyn SysEnv>, fs: Arc<dyn FsOps>, clock: Arc<dyn Clock>) -> Self {
        #[cfg(unix)]
        let modes = posix::host_modes();
        #[cfg(windows)]
        let modes = windows::host_modes();
        Self::new(
            modes,
            SystemdCtl::new(Arc::clone(&env), Arc::clone(&fs), Arc::clone(&clock)),
            WindowsTaskCtl::new(Arc::clone(&env), Arc::clone(&fs), Arc::clone(&clock)),
            WindowsServiceCtl::new(env, clock),
        )
    }
}

impl ServiceCtl for CompositeServiceCtl {
    fn supported_modes(&self) -> Vec<SupervisionMode> {
        self.modes.clone()
    }
}
