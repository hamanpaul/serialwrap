//! `windows-task`（Task XML／schtasks；SC-24）。
use std::sync::Arc;
use std::time::Duration;

use crate::fs_ops::FsOps;
use crate::service_ctl::{
    blocked_windows_stop, cmd_record, encode_utf16le_bom, error_summary, no_systemd,
    render_task_xml, service_error, service_result, shell_join_windows, AutostartHoldKind,
    AutostartHoldRecord, CmdRecord, HoldState, ServiceAction, ServiceCtl, ServiceResult,
    SupervisionMode, SupervisorDefinition, TaskQuery, TaskXmlSpec,
};
use crate::traits::{Clock, CmdOutput, SysEnv};

const QUERY_TIMEOUT: Duration = Duration::from_secs(5);

/// Windows 工作排程器控制器。命令可以在 POSIX 測試替身中驗證。
pub struct WindowsTaskCtl {
    env: Arc<dyn SysEnv>,
    fs: Arc<dyn FsOps>,
    _clock: Arc<dyn Clock>,
}

impl WindowsTaskCtl {
    pub fn new(env: Arc<dyn SysEnv>, fs: Arc<dyn FsOps>, clock: Arc<dyn Clock>) -> Self {
        Self {
            env,
            fs,
            _clock: clock,
        }
    }

    fn run_recorded(
        &self,
        argv: Vec<String>,
        timeout: Option<Duration>,
        ran: &mut Vec<CmdRecord>,
    ) -> Result<CmdOutput, String> {
        let label = format!("{argv:?}");
        match self.env.run(&argv, timeout) {
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
                Err(format!("{label} 執行失敗：{}", error_summary(&error)))
            }
        }
    }

    fn run_xml_recorded(
        &self,
        name: &str,
        ran: &mut Vec<CmdRecord>,
    ) -> Result<(i32, String, String), String> {
        let argv = xml_query_argv(name);
        let label = format!("{argv:?}");
        match self.env.run_raw(&argv, None, Some(QUERY_TIMEOUT)) {
            Ok(output) => {
                ran.push(CmdRecord {
                    argv,
                    rc: output.rc,
                });
                Ok((
                    output.rc,
                    decode_xml(&output.stdout),
                    String::from_utf8_lossy(&output.stderr).into_owned(),
                ))
            }
            Err(error) => {
                ran.push(CmdRecord { argv, rc: -1 });
                Err(format!("{label} 執行失敗：{}", error_summary(&error)))
            }
        }
    }

    fn query_task(&self, name: &str) -> TaskQuery {
        self.query_task_recorded(name, &mut Vec::new())
    }

    fn query_task_recorded(&self, name: &str, ran: &mut Vec<CmdRecord>) -> TaskQuery {
        let argv = power_shell_query_argv(name);
        let output = match self.env.run(&argv, Some(QUERY_TIMEOUT)) {
            Ok(output) => {
                ran.push(CmdRecord {
                    argv,
                    rc: output.rc,
                });
                output
            }
            Err(error) => {
                ran.push(CmdRecord { argv, rc: -1 });
                return TaskQuery::QueryFailed(error_summary(&error));
            }
        };
        if output.rc != 0 {
            return TaskQuery::QueryFailed(format!(
                "rc={} stderr={}",
                output.rc,
                output.stderr.trim()
            ));
        }
        match output.stdout.trim() {
            "Running" | "Queued" => TaskQuery::Running,
            "Ready" | "Disabled" | "Unknown" => {
                TaskQuery::NotRunning(output.stdout.trim().to_string())
            }
            "NotFound" => TaskQuery::NotFound,
            _ => {
                TaskQuery::QueryFailed(format!("rc={} stderr={}", output.rc, output.stderr.trim()))
            }
        }
    }

    fn hold_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
    ) -> Result<AutostartHoldRecord, ServiceResult> {
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
        if !matches!(mode, SupervisionMode::WindowsTask) {
            return Err(no_systemd(mode, ServiceAction::Stop));
        }

        let mut ran = Vec::new();
        let prior_enabled = match self.run_xml_recorded(name, &mut ran) {
            Ok((0, xml, _)) => task_enabled(&xml),
            _ => None,
        };
        let disable = vec![
            "schtasks".into(),
            "/Change".into(),
            "/TN".into(),
            name.to_string(),
            "/DISABLE".into(),
        ];
        if let Err(error) = self.run_recorded(disable, None, &mut ran) {
            return Err(hold_failure(mode, error));
        }

        match self.query_task_recorded(name, &mut ran) {
            TaskQuery::NotRunning(state) if state == "Disabled" => {}
            TaskQuery::Running => match self.run_xml_recorded(name, &mut ran) {
                Ok((0, xml, _)) if task_enabled(&xml) == Some(false) => {}
                Ok((rc, xml, stderr)) => {
                    let error = format!(
                        "schtasks 工作仍可拉起（rc={rc}, Enabled={:?}, stderr={})",
                        task_enabled(&xml),
                        stderr.trim()
                    );
                    let rollback = self.restore_task_enabled(name, prior_enabled, &mut ran);
                    return Err(hold_failure(mode, append_rollback(error, rollback)));
                }
                Err(error) => {
                    let rollback = self.restore_task_enabled(name, prior_enabled, &mut ran);
                    return Err(hold_failure(mode, append_rollback(error, rollback)));
                }
            },
            other => {
                let error = format!("停用後無法確認工作狀態：{other:?}");
                let rollback = self.restore_task_enabled(name, prior_enabled, &mut ran);
                return Err(hold_failure(mode, append_rollback(error, rollback)));
            }
        }

        Ok(AutostartHoldRecord {
            mode: mode.clone(),
            kind: AutostartHoldKind::TaskDisabled,
            name: Some(name.to_string()),
            dropin_path: None,
            prior_enabled,
            prior_restart: None,
            ran,
        })
    }

    fn restore_task_enabled(
        &self,
        name: &str,
        prior_enabled: Option<bool>,
        ran: &mut Vec<CmdRecord>,
    ) -> Option<String> {
        if prior_enabled == Some(true) {
            self.run_recorded(
                vec![
                    "schtasks".into(),
                    "/Change".into(),
                    "/TN".into(),
                    name.into(),
                    "/ENABLE".into(),
                ],
                None,
                ran,
            )
            .err()
        } else {
            None
        }
    }
}

impl ServiceCtl for WindowsTaskCtl {
    fn supported_modes(&self) -> Vec<SupervisionMode> {
        vec![SupervisionMode::OnDemand, SupervisionMode::WindowsTask]
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
        _with_sudo: bool,
    ) -> ServiceResult {
        if !matches!(mode, SupervisionMode::WindowsTask) {
            return no_systemd(mode, action);
        }
        if matches!(action, ServiceAction::Stop | ServiceAction::Restart) {
            return blocked_windows_stop(mode, action);
        }
        let argv = match action {
            ServiceAction::Start => {
                vec!["schtasks".into(), "/Run".into(), "/TN".into(), name.into()]
            }
            ServiceAction::Status => vec![
                "schtasks".into(),
                "/Query".into(),
                "/TN".into(),
                name.into(),
                "/FO".into(),
                "LIST".into(),
                "/V".into(),
            ],
            ServiceAction::Stop | ServiceAction::Restart => unreachable!(),
        };
        service_result(mode, action, self.env.run(&argv, None))
    }

    fn install(&self, def: &SupervisorDefinition, _with_sudo: bool) -> Vec<CmdRecord> {
        if !matches!(def.mode, SupervisionMode::WindowsTask) {
            return Vec::new();
        }
        let Some(definition_path) = def.definition_path.as_deref() else {
            return vec![install_failure_record(def)];
        };
        let Some((command, arguments)) = def.exec.split_first() else {
            return vec![install_failure_record(def)];
        };
        let working_dir = def
            .working_dir
            .as_deref()
            .map(|path| path.to_string_lossy().into_owned())
            .unwrap_or_default();
        let xml = render_task_xml(&TaskXmlSpec {
            name: def.name.clone(),
            user_id: self.env.user_identity(),
            command: command.clone(),
            arguments: shell_join_windows(arguments),
            working_dir,
            enabled: !def.register_held,
        });
        if let Some(parent) = definition_path.parent() {
            if self.fs.mkdirs(parent).is_err() {
                return vec![install_failure_record(def)];
            }
        }
        if self
            .fs
            .atomic_write(definition_path, &encode_utf16le_bom(&xml), None)
            .is_err()
        {
            return vec![install_failure_record(def)];
        }
        self.install_plan(def)
            .into_iter()
            .map(|argv| cmd_record(self.env.as_ref(), argv))
            .collect()
    }

    fn install_plan(&self, def: &SupervisorDefinition) -> Vec<Vec<String>> {
        if !matches!(def.mode, SupervisionMode::WindowsTask) {
            return Vec::new();
        }
        let Some(path) = def.definition_path.as_ref() else {
            return Vec::new();
        };
        vec![vec![
            "schtasks".into(),
            "/Create".into(),
            "/XML".into(),
            path.display().to_string(),
            "/TN".into(),
            def.name.clone(),
            "/F".into(),
        ]]
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
        if !matches!(mode, SupervisionMode::WindowsTask) {
            return Vec::new();
        }
        vec![cmd_record(
            self.env.as_ref(),
            vec![
                "schtasks".into(),
                "/Delete".into(),
                "/TN".into(),
                name.into(),
                "/F".into(),
            ],
        )]
    }

    fn set_enabled(
        &self,
        mode: &SupervisionMode,
        enabled: bool,
        _with_sudo: bool,
    ) -> Vec<CmdRecord> {
        if !matches!(mode, SupervisionMode::WindowsTask) {
            return Vec::new();
        }
        let switch = if enabled { "/ENABLE" } else { "/DISABLE" };
        vec![cmd_record(
            self.env.as_ref(),
            vec![
                "schtasks".into(),
                "/Change".into(),
                "/TN".into(),
                "serialwrap".into(),
                switch.into(),
            ],
        )]
    }

    fn query(&self, mode: &SupervisionMode) -> TaskQuery {
        self.query_named(mode, "serialwrap")
    }

    fn query_named(&self, mode: &SupervisionMode, name: &str) -> TaskQuery {
        if !matches!(mode, SupervisionMode::WindowsTask) {
            return TaskQuery::QueryFailed(format!(
                "不支援的 windows-task 模式：{}",
                mode.as_str()
            ));
        }
        self.query_task(name)
    }

    fn hard_stop(&self, mode: &SupervisionMode, _with_sudo: bool) -> ServiceResult {
        if !matches!(mode, SupervisionMode::WindowsTask) {
            return no_systemd(mode, ServiceAction::Stop);
        }
        let argv = vec![
            "schtasks".into(),
            "/End".into(),
            "/TN".into(),
            "serialwrap".into(),
        ];
        service_result(mode, ServiceAction::Stop, self.env.run(&argv, None))
    }

    fn hold_autostart(
        &self,
        mode: &SupervisionMode,
        _with_sudo: bool,
    ) -> Result<AutostartHoldRecord, ServiceResult> {
        self.hold_named(mode, "serialwrap")
    }

    fn release_autostart(
        &self,
        hold: &AutostartHoldRecord,
        restore_enabled: Option<bool>,
        _with_sudo: bool,
    ) -> Result<Vec<CmdRecord>, ServiceResult> {
        if hold.kind == AutostartHoldKind::NoSupervisor {
            return Ok(Vec::new());
        }
        if hold.kind != AutostartHoldKind::TaskDisabled
            || !matches!(hold.mode, SupervisionMode::WindowsTask)
        {
            return Err(service_error(
                &hold.mode,
                "release_autostart",
                "INTERNAL_ERROR",
                "hold 紀錄與 windows-task 模式不符",
                Some("autostart_release"),
            ));
        }
        if restore_enabled == Some(true) {
            let argv = vec![
                "schtasks".into(),
                "/Change".into(),
                "/TN".into(),
                hold.name.as_deref().unwrap_or("serialwrap").into(),
                "/ENABLE".into(),
            ];
            let mut ran = Vec::new();
            self.run_recorded(argv, None, &mut ran).map_err(|error| {
                service_error(
                    &hold.mode,
                    "release_autostart",
                    "INTERNAL_ERROR",
                    error,
                    Some("autostart_release"),
                )
            })?;
            Ok(ran)
        } else {
            Ok(Vec::new())
        }
    }

    fn autostart_hold_state(&self, mode: &SupervisionMode) -> HoldState {
        if matches!(mode, SupervisionMode::OnDemand) {
            return HoldState::NotHeld;
        }
        if !matches!(mode, SupervisionMode::WindowsTask) {
            return HoldState::QueryFailed(format!(
                "不支援的 windows-task 模式：{}",
                mode.as_str()
            ));
        }
        match self.query_task("serialwrap") {
            TaskQuery::NotFound => HoldState::NotHeld,
            TaskQuery::QueryFailed(error) => HoldState::QueryFailed(error),
            _ => match self.run_xml_recorded("serialwrap", &mut Vec::new()) {
                Ok((0, xml, _)) => match task_enabled(&xml) {
                    Some(false) => HoldState::Held,
                    Some(true) => HoldState::NotHeld,
                    None => HoldState::QueryFailed("Task XML 缺少 Settings/Enabled".into()),
                },
                Ok((rc, _, stderr)) => {
                    HoldState::QueryFailed(format!("rc={rc} stderr={}", stderr.trim()))
                }
                Err(error) => HoldState::QueryFailed(error),
            },
        }
    }

    fn hold_autostart_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        _with_sudo: bool,
    ) -> Result<AutostartHoldRecord, ServiceResult> {
        self.hold_named(mode, name)
    }
}

fn create_task_argv(
    name: &str,
    definition_path: Option<&std::path::Path>,
) -> Vec<String> {
    vec![
        "schtasks".into(),
        "/Create".into(),
        "/XML".into(),
        definition_path
            .map(|path| path.display().to_string())
            .unwrap_or_default(),
        "/TN".into(),
        name.into(),
        "/F".into(),
    ]
}

fn install_failure_record(def: &SupervisorDefinition) -> CmdRecord {
    CmdRecord {
        argv: create_task_argv(&def.name, def.definition_path.as_deref()),
        rc: -1,
    }
}

fn power_shell_query_argv(name: &str) -> Vec<String> {
    let escaped = name.replace('\'', "''");
    vec![
        "powershell.exe".into(),
        "-NoProfile".into(),
        "-NonInteractive".into(),
        "-Command".into(),
        format!(
            "try {{ (Get-ScheduledTask -TaskName '{escaped}' -ErrorAction Stop).State.ToString() }} catch {{ if ($_.CategoryInfo.Category -eq 'ObjectNotFound') {{ 'NotFound' }} else {{ exit 3 }} }}"
        ),
    ]
}

fn xml_query_argv(name: &str) -> Vec<String> {
    vec![
        "schtasks".into(),
        "/Query".into(),
        "/TN".into(),
        name.into(),
        "/XML".into(),
    ]
}

fn decode_xml(bytes: &[u8]) -> String {
    if bytes.starts_with(&[0xff, 0xfe]) {
        let units = bytes[2..]
            .chunks_exact(2)
            .map(|chunk| u16::from_le_bytes([chunk[0], chunk[1]]))
            .collect::<Vec<_>>();
        String::from_utf16_lossy(&units)
    } else if bytes.starts_with(&[0xfe, 0xff]) {
        let units = bytes[2..]
            .chunks_exact(2)
            .map(|chunk| u16::from_be_bytes([chunk[0], chunk[1]]))
            .collect::<Vec<_>>();
        String::from_utf16_lossy(&units)
    } else {
        String::from_utf8_lossy(bytes).into_owned()
    }
}

fn task_enabled(xml: &str) -> Option<bool> {
    let settings = xml.split_once("<Settings>")?.1.split_once("</Settings>")?.0;
    match xml_tag(settings, "Enabled")?.trim() {
        "true" => Some(true),
        "false" => Some(false),
        _ => None,
    }
}

fn xml_tag(xml: &str, tag: &str) -> Option<String> {
    let opening = format!("<{tag}>");
    let closing = format!("</{tag}>");
    let body = xml.split_once(&opening)?.1.split_once(&closing)?.0;
    Some(body.to_string())
}

fn hold_failure(mode: &SupervisionMode, detail: impl Into<String>) -> ServiceResult {
    service_error(
        mode,
        "hold_autostart",
        "INTERNAL_ERROR",
        detail,
        Some("autostart_hold"),
    )
}

fn append_rollback(mut error: String, rollback: Option<String>) -> String {
    if let Some(rollback) = rollback {
        error.push_str("；還原失敗：");
        error.push_str(&rollback);
    }
    error
}
