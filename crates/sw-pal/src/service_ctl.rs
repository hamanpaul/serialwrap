//! 監管服務控制（P4-T3）：systemd、`windows-task`（Task XML／schtasks）、`windows-service`（sc.exe）與交接前拉起抑制。
//!
//! 型別、簽章與建構子由 P4-T1 交付；命令產生、查詢與交接前 hold 由 P4-T3 實作。
//! 命令產生寫成不帶 `cfg` 的共用程式碼（arch §4.8）；平台差異僅在 `posix`／`windows` 的 host 模式清單。
//! `SystemdCtl`／`WindowsTaskCtl`／`WindowsServiceCtl` 分別位於 `systemd`、`windows_task`、`windows_service` 子模組並由此再匯出。
use std::path::PathBuf;
use std::sync::Arc;

use crate::fs_ops::FsOps;
use crate::traits::{Clock, CmdOutput, PalError, PalErrorKind, PalResult, SysEnv};

#[cfg(unix)]
pub mod posix;
pub mod systemd;
#[cfg(windows)]
pub mod windows;
pub mod windows_service;
pub mod windows_task;

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
        match s {
            "on-demand" => Self::OnDemand,
            "systemd-user" => Self::SystemdUser,
            "systemd-system" => Self::SystemdSystem,
            "windows-task" => Self::WindowsTask,
            "windows-service" => Self::WindowsService,
            other => Self::Other(other.to_string()),
        }
    }
    pub fn as_str(&self) -> &str {
        match self {
            Self::OnDemand => "on-demand",
            Self::SystemdUser => "systemd-user",
            Self::SystemdSystem => "systemd-system",
            Self::WindowsTask => "windows-task",
            Self::WindowsService => "windows-service",
            Self::Other(s) => s,
        }
    }
    /// 前述 4 個監管模式為 true。
    pub fn is_supervised(&self) -> bool {
        !matches!(self, Self::OnDemand | Self::Other(_))
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
        match s {
            "start" => Some(Self::Start),
            "stop" => Some(Self::Stop),
            "restart" => Some(Self::Restart),
            "status" => Some(Self::Status),
            _ => None,
        }
    }
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Start => "start",
            Self::Stop => "stop",
            Self::Restart => "restart",
            Self::Status => "status",
        }
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
    /// 具名 hold 的監管項名稱；release 必須還原同一項目。
    pub name: Option<String>,
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

/// 輸出 Task Scheduler XML（CRLF、`ExecutionTimeLimit=PT0S`）。
pub fn render_task_xml(spec: &TaskXmlSpec) -> String {
    let enabled = if spec.enabled { "true" } else { "false" };
    format!(
        concat!(
            "<?xml version=\"1.0\" encoding=\"UTF-16\"?>\r\n",
            "<Task version=\"1.2\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">\r\n",
            "  <RegistrationInfo>\r\n",
            "    <Description>serialwrap UART broker daemon (windows-task)</Description>\r\n",
            "    <URI>\\{}</URI>\r\n",
            "  </RegistrationInfo>\r\n",
            "  <Triggers>\r\n",
            "    <LogonTrigger>\r\n",
            "      <Enabled>true</Enabled>\r\n",
            "      <UserId>{}</UserId>\r\n",
            "    </LogonTrigger>\r\n",
            "  </Triggers>\r\n",
            "  <Principals>\r\n",
            "    <Principal id=\"Author\">\r\n",
            "      <UserId>{}</UserId>\r\n",
            "      <LogonType>InteractiveToken</LogonType>\r\n",
            "      <RunLevel>LeastPrivilege</RunLevel>\r\n",
            "    </Principal>\r\n",
            "  </Principals>\r\n",
            "  <Settings>\r\n",
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\r\n",
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\r\n",
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\r\n",
            "    <AllowHardTerminate>true</AllowHardTerminate>\r\n",
            "    <StartWhenAvailable>true</StartWhenAvailable>\r\n",
            "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\r\n",
            "    <IdleSettings>\r\n",
            "      <StopOnIdleEnd>false</StopOnIdleEnd>\r\n",
            "      <RestartOnIdle>false</RestartOnIdle>\r\n",
            "    </IdleSettings>\r\n",
            "    <AllowStartOnDemand>true</AllowStartOnDemand>\r\n",
            "    <Enabled>{}</Enabled>\r\n",
            "    <Hidden>false</Hidden>\r\n",
            "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\r\n",
            "    <WakeToRun>false</WakeToRun>\r\n",
            "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\r\n",
            "    <Priority>7</Priority>\r\n",
            "    <RestartOnFailure>\r\n",
            "      <Interval>PT1M</Interval>\r\n",
            "      <Count>999</Count>\r\n",
            "    </RestartOnFailure>\r\n",
            "  </Settings>\r\n",
            "  <Actions Context=\"Author\">\r\n",
            "    <Exec>\r\n",
            "      <Command>{}</Command>\r\n",
            "      <Arguments>{}</Arguments>\r\n",
            "      <WorkingDirectory>{}</WorkingDirectory>\r\n",
            "    </Exec>\r\n",
            "  </Actions>\r\n",
            "</Task>\r\n"
        ),
        xml_escape(&spec.name),
        xml_escape(&spec.user_id),
        xml_escape(&spec.user_id),
        enabled,
        xml_escape(&spec.command),
        xml_escape(&spec.arguments),
        xml_escape(&spec.working_dir),
    )
}

/// UTF-16LE 並加 BOM（`FF FE`）。
pub fn encode_utf16le_bom(s: &str) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(2 + s.len() * 2);
    bytes.extend_from_slice(&[0xff, 0xfe]);
    for unit in s.encode_utf16() {
        bytes.extend_from_slice(&unit.to_le_bytes());
    }
    bytes
}

/// `subprocess.list2cmdline` 規則的反向（純函式，不呼叫 SHELL32）。
pub fn shell_join_windows(args: &[String]) -> String {
    args.iter()
        .map(|arg| quote_windows_arg(arg, false))
        .collect::<Vec<_>>()
        .join(" ")
}

/// MSVCRT／`CommandLineToArgvW` 解析規則（純函式自寫，不呼叫 SHELL32）。
pub fn split_windows_cmdline(s: &str) -> Vec<String> {
    let chars: Vec<char> = s.chars().collect();
    let mut args = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        while i < chars.len() && is_windows_space(chars[i]) {
            i += 1;
        }
        if i == chars.len() {
            break;
        }

        let mut arg = String::new();
        let mut quoted = false;
        while i < chars.len() {
            if is_windows_space(chars[i]) && !quoted {
                break;
            }

            if chars[i] == '\\' {
                let start = i;
                while i < chars.len() && chars[i] == '\\' {
                    i += 1;
                }
                let slashes = i - start;
                if i < chars.len() && chars[i] == '"' {
                    arg.push_str(&"\\".repeat(slashes / 2));
                    if slashes % 2 == 1 {
                        arg.push('"');
                        i += 1;
                    } else {
                        i += 1;
                        if quoted && i < chars.len() && chars[i] == '"' {
                            arg.push('"');
                            i += 1;
                        } else {
                            quoted = !quoted;
                        }
                    }
                } else {
                    arg.push_str(&"\\".repeat(slashes));
                }
                continue;
            }

            if chars[i] == '"' {
                i += 1;
                if quoted && i < chars.len() && chars[i] == '"' {
                    arg.push('"');
                    i += 1;
                } else {
                    quoted = !quoted;
                }
                continue;
            }

            arg.push(chars[i]);
            i += 1;
        }
        args.push(arg);
    }
    args
}

pub(crate) fn quote_windows_arg(arg: &str, force_quotes: bool) -> String {
    let needs_quotes = force_quotes || arg.is_empty() || arg.chars().any(is_windows_space);
    let mut out = String::with_capacity(arg.len() + 2);
    if needs_quotes {
        out.push('"');
    }
    let mut slashes = 0usize;
    for c in arg.chars() {
        if c == '\\' {
            slashes += 1;
        } else if c == '"' {
            out.push_str(&"\\".repeat(slashes * 2 + 1));
            out.push('"');
            slashes = 0;
        } else {
            out.push_str(&"\\".repeat(slashes));
            out.push(c);
            slashes = 0;
        }
    }
    out.push_str(&"\\".repeat(slashes * if needs_quotes { 2 } else { 1 }));
    if needs_quotes {
        out.push('"');
    }
    out
}

fn is_windows_space(c: char) -> bool {
    c == ' ' || c == '\t'
}

fn xml_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        out.push_str(match c {
            '&' => "&amp;",
            '<' => "&lt;",
            '>' => "&gt;",
            '"' => "&quot;",
            '\'' => "&apos;",
            _ => {
                out.push(c);
                continue;
            }
        });
    }
    out
}

pub(crate) fn no_systemd(mode: &SupervisionMode, action: ServiceAction) -> ServiceResult {
    ServiceResult {
        ok: false,
        mode: mode.as_str().to_string(),
        action: action.as_str().to_string(),
        rc: None,
        stdout: None,
        stderr: None,
        error_code: Some("NO_SYSTEMD"),
        hint: Some(
            "目前為 on-demand 監管模式（無 systemd 服務）。使用 `serialwrap daemon start/stop`。"
                .into(),
        ),
        phase: None,
    }
}

pub(crate) fn needs_sudo_systemd(
    mode: &SupervisionMode,
    action: ServiceAction,
    name: &str,
) -> ServiceResult {
    ServiceResult {
        ok: false,
        mode: mode.as_str().to_string(),
        action: action.as_str().to_string(),
        rc: None,
        stdout: None,
        stderr: None,
        error_code: Some("NEEDS_SUDO"),
        hint: Some(format!(
            "需 root：請執行 `sudo systemctl {} {name}`（或加 --with-sudo 讓本指令代跑）",
            action.as_str()
        )),
        phase: None,
    }
}

pub(crate) fn needs_sudo_windows_service(
    mode: &SupervisionMode,
    action: &str,
    name: &str,
) -> ServiceResult {
    ServiceResult {
        ok: false,
        mode: mode.as_str().to_string(),
        action: action.to_string(),
        rc: None,
        stdout: None,
        stderr: None,
        error_code: Some("NEEDS_SUDO"),
        hint: Some(format!(
            "需系統管理員權限：請在系統管理員 PowerShell 執行 `sc.exe {action} {name}`（或加 --with-sudo 讓本指令代跑）"
        )),
        phase: None,
    }
}

pub(crate) fn blocked_windows_stop(mode: &SupervisionMode, action: ServiceAction) -> ServiceResult {
    ServiceResult {
        ok: false,
        mode: mode.as_str().to_string(),
        action: action.as_str().to_string(),
        rc: None,
        stdout: None,
        stderr: None,
        error_code: Some("INTERNAL_ERROR"),
        hint: Some("windows-task 的 stop／restart 必須經 graceful_stop 組合".into()),
        phase: None,
    }
}

pub(crate) fn service_result(
    mode: &SupervisionMode,
    action: ServiceAction,
    output: PalResult<CmdOutput>,
) -> ServiceResult {
    match output {
        Ok(output) => ServiceResult {
            ok: output.rc == 0,
            mode: mode.as_str().to_string(),
            action: action.as_str().to_string(),
            rc: Some(output.rc),
            stdout: Some(output.stdout),
            stderr: Some(output.stderr),
            error_code: None,
            hint: None,
            phase: None,
        },
        Err(error) => ServiceResult {
            ok: false,
            mode: mode.as_str().to_string(),
            action: action.as_str().to_string(),
            rc: None,
            stdout: None,
            stderr: Some(error.py_message.clone()),
            error_code: Some("INTERNAL_ERROR"),
            hint: Some(error.py_message),
            phase: None,
        },
    }
}

pub(crate) fn service_error(
    mode: &SupervisionMode,
    action: &str,
    error_code: &'static str,
    hint: impl Into<String>,
    phase: Option<&'static str>,
) -> ServiceResult {
    ServiceResult {
        ok: false,
        mode: mode.as_str().to_string(),
        action: action.to_string(),
        rc: None,
        stdout: None,
        stderr: None,
        error_code: Some(error_code),
        hint: Some(hint.into()),
        phase,
    }
}

pub(crate) fn error_summary(error: &PalError) -> String {
    if error.py_message.is_empty() {
        error.strerror.clone()
    } else {
        error.py_message.clone()
    }
}

pub(crate) fn cmd_record(env: &dyn SysEnv, argv: Vec<String>) -> CmdRecord {
    let rc = env.run(&argv, None).map(|output| output.rc).unwrap_or(-1);
    CmdRecord { argv, rc }
}

pub(crate) fn parse_key_values(output: &str) -> std::collections::BTreeMap<String, String> {
    output
        .lines()
        .filter_map(|line| line.split_once('='))
        .map(|(key, value)| (key.trim().to_string(), value.trim().to_string()))
        .collect()
}

pub(crate) fn is_not_found(error: &PalError) -> bool {
    error.kind == PalErrorKind::NotFound
        || matches!(error.os_code, Some(2 | 3))
        || matches!(error.winerror, Some(2 | 3))
        || error.py_message.contains("No such file or directory")
        || error.py_message.contains("os error 2")
}

/// 監管控制介面（arch §4.8）。
pub trait ServiceCtl: Send + Sync {
    fn supported_modes(&self) -> Vec<SupervisionMode>;
    /// 固定名稱 `serialwrap`。
    fn action(
        &self,
        mode: &SupervisionMode,
        action: ServiceAction,
        with_sudo: bool,
    ) -> ServiceResult;
    fn action_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        action: ServiceAction,
        with_sudo: bool,
    ) -> ServiceResult;
    fn install(&self, def: &SupervisorDefinition, with_sudo: bool) -> Vec<CmdRecord>;
    /// 不執行，供 `pending_sudo`。
    fn install_plan(&self, def: &SupervisorDefinition) -> Vec<Vec<String>>;
    fn uninstall(&self, mode: &SupervisionMode, with_sudo: bool) -> Vec<CmdRecord>;
    /// 具名版本（與 `action_named` 同語意；cap 測試需要，Task 1 Produces 未列，骨架補上）。
    fn uninstall_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        with_sudo: bool,
    ) -> Vec<CmdRecord>;
    fn set_enabled(&self, mode: &SupervisionMode, enabled: bool, with_sudo: bool)
        -> Vec<CmdRecord>;
    /// 固定名稱 `serialwrap`。
    fn query(&self, mode: &SupervisionMode) -> TaskQuery;
    /// 具名版本（與 `query` 同語意；骨架補上）。
    fn query_named(&self, mode: &SupervisionMode, name: &str) -> TaskQuery;
    /// windows-task `/End`；systemd `stop`。
    fn hard_stop(&self, mode: &SupervisionMode, with_sudo: bool) -> ServiceResult;
    /// R5-F1：交接前抑制監管拉起（持久、跨 CLI 中斷有效，只由 `release_autostart` 撤銷）。
    fn hold_autostart(
        &self,
        mode: &SupervisionMode,
        with_sudo: bool,
    ) -> Result<AutostartHoldRecord, ServiceResult>;
    /// 冪等：hold 不存在也成功。
    fn release_autostart(
        &self,
        hold: &AutostartHoldRecord,
        restore_enabled: Option<bool>,
        with_sudo: bool,
    ) -> Result<Vec<CmdRecord>, ServiceResult>;
    fn autostart_hold_state(&self, mode: &SupervisionMode) -> HoldState;
    /// 同 `hold_autostart`，unit／task 名可指定（cap 測試用）。
    fn hold_autostart_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        with_sudo: bool,
    ) -> Result<AutostartHoldRecord, ServiceResult>;
}

/// 組合式監管控制（arch §4.8）：依 `modes` 分派到 systemd／windows-task／windows-service 實作。
///
/// 依目前平台支援清單，把呼叫分派至對應的原生監管控制器。
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

    fn backend(&self, mode: &SupervisionMode) -> Option<&dyn ServiceCtl> {
        if !self.modes.contains(mode) {
            return None;
        }
        match mode {
            SupervisionMode::OnDemand
            | SupervisionMode::SystemdUser
            | SupervisionMode::SystemdSystem => Some(&self.systemd),
            SupervisionMode::WindowsTask => Some(&self.task),
            SupervisionMode::WindowsService => Some(&self.service),
            SupervisionMode::Other(_) => None,
        }
    }
}

impl ServiceCtl for CompositeServiceCtl {
    fn supported_modes(&self) -> Vec<SupervisionMode> {
        self.modes.clone()
    }

    fn action(
        &self,
        mode: &SupervisionMode,
        action: ServiceAction,
        with_sudo: bool,
    ) -> ServiceResult {
        self.action_named(mode, "serialwrap", action, with_sudo)
    }

    fn action_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        action: ServiceAction,
        with_sudo: bool,
    ) -> ServiceResult {
        self.backend(mode)
            .map(|backend| backend.action_named(mode, name, action, with_sudo))
            .unwrap_or_else(|| no_systemd(mode, action))
    }

    fn install(&self, def: &SupervisorDefinition, with_sudo: bool) -> Vec<CmdRecord> {
        self.backend(&def.mode)
            .map(|backend| backend.install(def, with_sudo))
            .unwrap_or_default()
    }

    fn install_plan(&self, def: &SupervisorDefinition) -> Vec<Vec<String>> {
        self.backend(&def.mode)
            .map(|backend| backend.install_plan(def))
            .unwrap_or_default()
    }

    fn uninstall(&self, mode: &SupervisionMode, with_sudo: bool) -> Vec<CmdRecord> {
        self.uninstall_named(mode, "serialwrap", with_sudo)
    }

    fn uninstall_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        with_sudo: bool,
    ) -> Vec<CmdRecord> {
        self.backend(mode)
            .map(|backend| backend.uninstall_named(mode, name, with_sudo))
            .unwrap_or_default()
    }

    fn set_enabled(
        &self,
        mode: &SupervisionMode,
        enabled: bool,
        with_sudo: bool,
    ) -> Vec<CmdRecord> {
        self.backend(mode)
            .map(|backend| backend.set_enabled(mode, enabled, with_sudo))
            .unwrap_or_default()
    }

    fn query(&self, mode: &SupervisionMode) -> TaskQuery {
        self.query_named(mode, "serialwrap")
    }

    fn query_named(&self, mode: &SupervisionMode, name: &str) -> TaskQuery {
        self.backend(mode)
            .map(|backend| backend.query_named(mode, name))
            .unwrap_or_else(|| TaskQuery::QueryFailed(format!("不支援監管模式：{}", mode.as_str())))
    }

    fn hard_stop(&self, mode: &SupervisionMode, with_sudo: bool) -> ServiceResult {
        self.backend(mode)
            .map(|backend| backend.hard_stop(mode, with_sudo))
            .unwrap_or_else(|| no_systemd(mode, ServiceAction::Stop))
    }

    fn hold_autostart(
        &self,
        mode: &SupervisionMode,
        with_sudo: bool,
    ) -> Result<AutostartHoldRecord, ServiceResult> {
        self.hold_autostart_named(mode, "serialwrap", with_sudo)
    }

    fn release_autostart(
        &self,
        hold: &AutostartHoldRecord,
        restore_enabled: Option<bool>,
        with_sudo: bool,
    ) -> Result<Vec<CmdRecord>, ServiceResult> {
        self.backend(&hold.mode)
            .ok_or_else(|| {
                service_error(
                    &hold.mode,
                    "release_autostart",
                    "NO_SYSTEMD",
                    "不支援此監管模式",
                    Some("autostart_release"),
                )
            })?
            .release_autostart(hold, restore_enabled, with_sudo)
    }

    fn autostart_hold_state(&self, mode: &SupervisionMode) -> HoldState {
        self.backend(mode)
            .map(|backend| backend.autostart_hold_state(mode))
            .unwrap_or_else(|| HoldState::QueryFailed(format!("不支援監管模式：{}", mode.as_str())))
    }

    fn hold_autostart_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        with_sudo: bool,
    ) -> Result<AutostartHoldRecord, ServiceResult> {
        self.backend(mode)
            .ok_or_else(|| {
                service_error(
                    mode,
                    "hold_autostart",
                    "NO_SYSTEMD",
                    "不支援此監管模式",
                    Some("autostart_hold"),
                )
            })?
            .hold_autostart_named(mode, name, with_sudo)
    }
}
