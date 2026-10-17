//! `windows-service`（sc.exe；OD-12 限制，本 Phase 只產生安裝指令計畫）。
use std::sync::Arc;

use crate::service_ctl::{
    blocked_windows_stop, cmd_record, no_systemd, quote_windows_arg, service_error, service_result,
    AutostartHoldKind, AutostartHoldRecord, CmdRecord, HoldState, ServiceAction, ServiceCtl,
    SupervisionMode, SupervisorDefinition, TaskQuery,
};
use crate::traits::{Clock, CmdOutput, SysEnv};

#[derive(Debug)]
enum StartTypeQueryError {
    NotFound,
    Failed(String),
}

impl StartTypeQueryError {
    fn detail(&self) -> &str {
        match self {
            Self::NotFound => "Windows 服務不存在（rc=1060）",
            Self::Failed(detail) => detail,
        }
    }
}

/// Windows Service Control Manager 控制器。
pub struct WindowsServiceCtl {
    env: Arc<dyn SysEnv>,
    _clock: Arc<dyn Clock>,
}

impl WindowsServiceCtl {
    pub fn new(env: Arc<dyn SysEnv>, clock: Arc<dyn Clock>) -> Self {
        Self { env, _clock: clock }
    }

    fn read_start_type(
        &self,
        name: &str,
        ran: &mut Vec<CmdRecord>,
    ) -> Result<Option<(u32, String)>, StartTypeQueryError> {
        let argv = vec!["sc.exe".into(), "qc".into(), name.into()];
        let label = format!("{argv:?}");
        let output = match self.env.run(&argv, None) {
            Ok(output) => {
                ran.push(CmdRecord {
                    argv,
                    rc: output.rc,
                });
                output
            }
            Err(error) => {
                ran.push(CmdRecord { argv, rc: -1 });
                return Err(StartTypeQueryError::Failed(format!(
                    "{label} 執行失敗：{}",
                    error.py_message
                )));
            }
        };
        if output.rc == 1060 {
            return Err(StartTypeQueryError::NotFound);
        }
        if output.rc != 0 {
            return Err(StartTypeQueryError::Failed(format!(
                "sc.exe qc 回傳 rc={}：{}",
                output.rc,
                output.stderr.trim()
            )));
        }
        Ok(parse_service_field(&output.stdout, "START_TYPE"))
    }

    fn config_command(&self, name: &str, value: &str) -> Vec<String> {
        vec![
            "sc.exe".into(),
            "config".into(),
            name.into(),
            "start=".into(),
            value.into(),
        ]
    }

    fn run_recorded(
        &self,
        argv: Vec<String>,
        ran: &mut Vec<CmdRecord>,
    ) -> Result<CmdOutput, String> {
        let label = format!("{argv:?}");
        match self.env.run(&argv, None) {
            Ok(output) => {
                ran.push(CmdRecord {
                    argv,
                    rc: output.rc,
                });
                if output.rc == 0 {
                    Ok(output)
                } else {
                    Err(format!(
                        "{label} 回傳 rc={}：{}",
                        output.rc,
                        output.stderr.trim()
                    ))
                }
            }
            Err(error) => {
                ran.push(CmdRecord { argv, rc: -1 });
                Err(format!("{label} 執行失敗：{}", error.py_message))
            }
        }
    }

    fn hold_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        with_sudo: bool,
    ) -> Result<AutostartHoldRecord, crate::service_ctl::ServiceResult> {
        if matches!(mode, SupervisionMode::OnDemand) {
            return Ok(AutostartHoldRecord {
                mode: mode.clone(),
                kind: AutostartHoldKind::NoSupervisor,
                name: None,
                dropin_path: None,
                prior_enabled: None,
                prior_restart: None,
                ran: Vec::new(),
            });
        }
        if !matches!(mode, SupervisionMode::WindowsService) {
            return Err(no_systemd(mode, ServiceAction::Stop));
        }
        if !with_sudo {
            return Err(admin_error(mode, name, "hold"));
        }

        let mut ran = Vec::new();
        let prior_start_type = self
            .read_start_type(name, &mut ran)
            .map_err(|error| hold_failure(mode, error.detail()))?
            .ok_or_else(|| hold_failure(mode, "sc.exe qc 未回傳 START_TYPE 欄位"))?;
        let prior_enabled = Some(prior_start_type.0 == 2);

        let disable = self.config_command(name, "disabled");
        if let Err(error) = self.run_recorded(disable, &mut ran) {
            let rollback = self.restore_service(name, prior_enabled, &mut ran);
            return Err(hold_failure(mode, append_rollback(error, rollback)));
        }
        let verify = match self.read_start_type(name, &mut ran) {
            Ok(verify) => verify,
            Err(error) => {
                let rollback = self.restore_service(name, prior_enabled, &mut ran);
                return Err(hold_failure(
                    mode,
                    append_rollback(error.detail().to_string(), rollback),
                ));
            }
        };
        if !verify
            .as_ref()
            .is_some_and(|(code, state)| *code == 4 || state.eq_ignore_ascii_case("DISABLED"))
        {
            let detail = format!("服務停用驗證不符：START_TYPE={verify:?}");
            let rollback = self.restore_service(name, prior_enabled, &mut ran);
            return Err(hold_failure(mode, append_rollback(detail, rollback)));
        }

        Ok(AutostartHoldRecord {
            mode: mode.clone(),
            kind: AutostartHoldKind::ServiceDisabled,
            name: Some(name.to_string()),
            dropin_path: None,
            prior_enabled,
            prior_restart: None,
            ran,
        })
    }

    fn restore_service(
        &self,
        name: &str,
        prior_enabled: Option<bool>,
        ran: &mut Vec<CmdRecord>,
    ) -> Option<String> {
        if prior_enabled == Some(true) {
            self.run_recorded(self.config_command(name, "auto"), ran)
                .err()
        } else {
            None
        }
    }
}

impl ServiceCtl for WindowsServiceCtl {
    fn supported_modes(&self) -> Vec<SupervisionMode> {
        vec![SupervisionMode::OnDemand, SupervisionMode::WindowsService]
    }

    fn action(
        &self,
        mode: &SupervisionMode,
        action: ServiceAction,
        with_sudo: bool,
    ) -> crate::service_ctl::ServiceResult {
        self.action_named(mode, "serialwrap", action, with_sudo)
    }

    fn action_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        action: ServiceAction,
        with_sudo: bool,
    ) -> crate::service_ctl::ServiceResult {
        if !matches!(mode, SupervisionMode::WindowsService) {
            return no_systemd(mode, action);
        }
        if matches!(action, ServiceAction::Stop | ServiceAction::Restart) {
            return blocked_windows_stop(mode, action);
        }
        if action == ServiceAction::Start && !with_sudo {
            return crate::service_ctl::needs_sudo_windows_service(mode, action.as_str(), name);
        }
        let argv = match action {
            ServiceAction::Start => vec!["sc.exe".into(), "start".into(), name.into()],
            ServiceAction::Status => vec!["sc.exe".into(), "query".into(), name.into()],
            ServiceAction::Stop | ServiceAction::Restart => unreachable!(),
        };
        service_result(mode, action, self.env.run(&argv, None))
    }

    fn install(&self, _def: &SupervisorDefinition, _with_sudo: bool) -> Vec<CmdRecord> {
        Vec::new()
    }

    fn install_plan(&self, def: &SupervisorDefinition) -> Vec<Vec<String>> {
        if !matches!(def.mode, SupervisionMode::WindowsService) {
            return Vec::new();
        }
        let Some((executable, args)) = def.exec.split_first() else {
            return Vec::new();
        };
        let mut bin_path = quote_windows_arg(executable, true);
        let joined_args = crate::service_ctl::shell_join_windows(args);
        if !joined_args.is_empty() {
            bin_path.push(' ');
            bin_path.push_str(&joined_args);
        }
        let mut plan = vec![
            vec![
                "sc.exe".into(),
                "create".into(),
                def.name.clone(),
                "binPath=".into(),
                bin_path,
                "start=".into(),
                if def.register_held { "disabled" } else { "auto" }.into(),
                "obj=".into(),
                "NT AUTHORITY\\LocalService".into(),
            ],
            vec![
                "sc.exe".into(),
                "failure".into(),
                def.name.clone(),
                "reset=".into(),
                "86400".into(),
                "actions=".into(),
                "restart/60000/restart/60000/restart/60000".into(),
            ],
        ];
        if !def.register_held {
            plan.push(vec!["sc.exe".into(), "start".into(), def.name.clone()]);
        }
        plan
    }

    fn uninstall(&self, mode: &SupervisionMode, with_sudo: bool) -> Vec<CmdRecord> {
        self.uninstall_named(mode, "serialwrap", with_sudo)
    }

    fn uninstall_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        _with_sudo: bool,
    ) -> Vec<CmdRecord> {
        if !matches!(mode, SupervisionMode::WindowsService) {
            return Vec::new();
        }
        vec![cmd_record(
            self.env.as_ref(),
            vec!["sc.exe".into(), "delete".into(), name.into()],
        )]
    }

    fn set_enabled(
        &self,
        mode: &SupervisionMode,
        enabled: bool,
        _with_sudo: bool,
    ) -> Vec<CmdRecord> {
        if !matches!(mode, SupervisionMode::WindowsService) {
            return Vec::new();
        }
        let value = if enabled { "auto" } else { "disabled" };
        vec![cmd_record(
            self.env.as_ref(),
            self.config_command("serialwrap", value),
        )]
    }

    fn query(&self, mode: &SupervisionMode) -> TaskQuery {
        self.query_named(mode, "serialwrap")
    }

    fn query_named(&self, mode: &SupervisionMode, name: &str) -> TaskQuery {
        if !matches!(mode, SupervisionMode::WindowsService) {
            return TaskQuery::QueryFailed(format!(
                "不支援的 windows-service 模式：{}",
                mode.as_str()
            ));
        }
        let argv = vec!["sc.exe".into(), "query".into(), name.into()];
        let output = match self.env.run(&argv, None) {
            Ok(output) => output,
            Err(error) => return TaskQuery::QueryFailed(error.py_message),
        };
        if output.rc == 1060 {
            return TaskQuery::NotFound;
        }
        if output.rc != 0 {
            return TaskQuery::QueryFailed(format!(
                "rc={} stderr={}",
                output.rc,
                output.stderr.trim()
            ));
        }
        match parse_service_field(&output.stdout, "STATE") {
            Some((1, _)) => TaskQuery::NotRunning("STOPPED".into()),
            Some((_, _)) => TaskQuery::Running,
            None => TaskQuery::QueryFailed("sc.exe query 未回傳 STATE 欄位".into()),
        }
    }

    fn hard_stop(
        &self,
        mode: &SupervisionMode,
        with_sudo: bool,
    ) -> crate::service_ctl::ServiceResult {
        self.hard_stop_named(mode, "serialwrap", with_sudo)
    }

    fn hold_autostart(
        &self,
        mode: &SupervisionMode,
        with_sudo: bool,
    ) -> Result<AutostartHoldRecord, crate::service_ctl::ServiceResult> {
        self.hold_named(mode, "serialwrap", with_sudo)
    }

    fn release_autostart(
        &self,
        hold: &AutostartHoldRecord,
        restore_enabled: Option<bool>,
        with_sudo: bool,
    ) -> Result<Vec<CmdRecord>, crate::service_ctl::ServiceResult> {
        if hold.kind == AutostartHoldKind::NoSupervisor {
            return Ok(Vec::new());
        }
        if hold.kind != AutostartHoldKind::ServiceDisabled
            || !matches!(hold.mode, SupervisionMode::WindowsService)
        {
            return Err(service_error(
                &hold.mode,
                "release_autostart",
                "INTERNAL_ERROR",
                "hold 紀錄與 windows-service 模式不符",
                Some("autostart_release"),
            ));
        }
        if restore_enabled != Some(true) {
            return Ok(Vec::new());
        }
        let name = hold.name.as_deref().unwrap_or("serialwrap");
        if !with_sudo {
            return Err(admin_error(&hold.mode, name, "release"));
        }

        let mut ran = Vec::new();
        self.run_recorded(self.config_command(name, "auto"), &mut ran)
            .map_err(|error| release_failure(&hold.mode, error))?;
        let start_type = self
            .read_start_type(name, &mut ran)
            .map_err(|error| release_failure(&hold.mode, error.detail()))?;
        if !start_type.as_ref().is_some_and(|(code, _)| *code == 2) {
            return Err(release_failure(
                &hold.mode,
                format!("服務自動啟動還原驗證不符：START_TYPE={start_type:?}"),
            ));
        }
        Ok(ran)
    }

    fn autostart_hold_state(&self, mode: &SupervisionMode) -> HoldState {
        if matches!(mode, SupervisionMode::OnDemand) {
            return HoldState::NotHeld;
        }
        if !matches!(mode, SupervisionMode::WindowsService) {
            return HoldState::QueryFailed(format!(
                "不支援的 windows-service 模式：{}",
                mode.as_str()
            ));
        }
        match self.read_start_type("serialwrap", &mut Vec::new()) {
            Ok(Some((4, _))) => HoldState::Held,
            Ok(Some(_)) => HoldState::NotHeld,
            Ok(None) => HoldState::QueryFailed("sc.exe qc 未回傳 START_TYPE 欄位".into()),
            Err(StartTypeQueryError::NotFound) => HoldState::NotHeld,
            Err(error) => HoldState::QueryFailed(error.detail().to_string()),
        }
    }

    fn hold_autostart_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        with_sudo: bool,
    ) -> Result<AutostartHoldRecord, crate::service_ctl::ServiceResult> {
        self.hold_named(mode, name, with_sudo)
    }
}

impl WindowsServiceCtl {
    fn hard_stop_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        with_sudo: bool,
    ) -> crate::service_ctl::ServiceResult {
        if !matches!(mode, SupervisionMode::WindowsService) {
            return no_systemd(mode, ServiceAction::Stop);
        }
        if !with_sudo {
            return crate::service_ctl::needs_sudo_windows_service(mode, "stop", name);
        }
        let argv = vec!["sc.exe".into(), "stop".into(), name.into()];
        service_result(mode, ServiceAction::Stop, self.env.run(&argv, None))
    }
}

fn parse_service_field(output: &str, field: &str) -> Option<(u32, String)> {
    for line in output.lines() {
        let Some((key, value)) = line.split_once(':') else {
            continue;
        };
        if key.trim() != field {
            continue;
        }
        let mut words = value.split_whitespace();
        let code = words.next()?.parse::<u32>().ok()?;
        let state = words.collect::<Vec<_>>().join(" ");
        return Some((code, state));
    }
    None
}

fn hold_failure(
    mode: &SupervisionMode,
    detail: impl Into<String>,
) -> crate::service_ctl::ServiceResult {
    service_error(
        mode,
        "hold_autostart",
        "INTERNAL_ERROR",
        detail,
        Some("autostart_hold"),
    )
}

fn release_failure(
    mode: &SupervisionMode,
    detail: impl Into<String>,
) -> crate::service_ctl::ServiceResult {
    service_error(
        mode,
        "release_autostart",
        "INTERNAL_ERROR",
        detail,
        Some("autostart_release"),
    )
}

fn admin_error(
    mode: &SupervisionMode,
    name: &str,
    action: &str,
) -> crate::service_ctl::ServiceResult {
    let (verb, command, phase) = match action {
        "hold" => (
            "hold_autostart",
            format!("sc.exe config {name} start= disabled"),
            Some("autostart_hold"),
        ),
        "release" => (
            "release_autostart",
            format!("sc.exe config {name} start= auto"),
            Some("autostart_release"),
        ),
        _ => (action, format!("sc.exe {action} {name}"), None),
    };
    service_error(
        mode,
        verb,
        "NEEDS_SUDO",
        format!(
            "需系統管理員權限：請在系統管理員 PowerShell 執行 `{command}`（或加 --with-sudo 讓本指令代跑）"
        ),
        phase,
    )
}

fn append_rollback(mut error: String, rollback: Option<String>) -> String {
    if let Some(rollback) = rollback {
        error.push_str("；還原失敗：");
        error.push_str(&rollback);
    }
    error
}
