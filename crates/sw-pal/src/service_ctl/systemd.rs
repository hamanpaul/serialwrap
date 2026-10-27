//! systemd `--user`／system 控制與持久化交接 hold（07 §5.2、§5.4）。
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use crate::fs_ops::FsOps;
use crate::service_ctl::{
    cmd_record, error_summary, is_not_found, needs_sudo_systemd, no_systemd, parse_key_values,
    service_error, service_result, AutostartHoldKind, AutostartHoldRecord, CmdRecord, HoldState,
    ServiceAction, ServiceCtl, ServiceResult, SupervisionMode, SupervisorDefinition, TaskQuery,
};
use crate::traits::{Clock, CmdOutput, SysEnv};

const HOLD_DROPIN_NAME: &str = "90-serialwrap-update-hold.conf";
const HOLD_DROPIN: &[u8] = b"# serialwrap update hold (R5-F1): removed by serialwrap setup\n[Unit]\nRefuseManualStart=yes\n\n[Service]\nRestart=no\n";

/// systemd 控制器。命令經 `SysEnv` 執行，hold 檔案經 `FsOps` 原子寫入。
pub struct SystemdCtl {
    env: Arc<dyn SysEnv>,
    fs: Arc<dyn FsOps>,
    _clock: Arc<dyn Clock>,
}

impl SystemdCtl {
    pub fn new(env: Arc<dyn SysEnv>, fs: Arc<dyn FsOps>, clock: Arc<dyn Clock>) -> Self {
        Self {
            env,
            fs,
            _clock: clock,
        }
    }

    fn systemctl_argv(mode: &SupervisionMode, with_sudo: bool, words: &[String]) -> Vec<String> {
        let mut argv = Vec::new();
        if matches!(mode, SupervisionMode::SystemdSystem) && with_sudo {
            argv.push("sudo".to_string());
        }
        argv.push("systemctl".to_string());
        if matches!(mode, SupervisionMode::SystemdUser) {
            argv.push("--user".to_string());
        }
        argv.extend(words.iter().cloned());
        argv
    }

    fn home(&self) -> Option<PathBuf> {
        self.env
            .var("HOME")
            .filter(|home| !home.is_empty())
            .or_else(|| self.env.current_user_home().filter(|home| !home.is_empty()))
            .map(PathBuf::from)
    }

    fn hold_path(&self, mode: &SupervisionMode, name: &str) -> Option<PathBuf> {
        match mode {
            SupervisionMode::SystemdUser => {
                let config_home = self
                    .env
                    .var("XDG_CONFIG_HOME")
                    .filter(|path| !path.is_empty())
                    .map(PathBuf::from)
                    .or_else(|| self.home().map(|home| home.join(".config")))?;
                Some(
                    config_home
                        .join("systemd/user")
                        .join(format!("{name}.service.d"))
                        .join(HOLD_DROPIN_NAME),
                )
            }
            // 系統層 drop-in 路徑會原樣進入 `sudo install`／`sudo rm` 的 argv，必為 POSIX 路徑；
            // 以 `/` 組字串而非 `Path::join`，避免共用程式在 Windows 測試時混入 `\\`。
            SupervisionMode::SystemdSystem => Some(PathBuf::from(format!(
                "/etc/systemd/system/{name}.service.d/{HOLD_DROPIN_NAME}"
            ))),
            _ => None,
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

    fn run_enabled_state(
        &self,
        mode: &SupervisionMode,
        name: &str,
        ran: &mut Vec<CmdRecord>,
    ) -> Result<Option<bool>, String> {
        let argv = Self::systemctl_argv(mode, false, &strings(["is-enabled", name]));
        let label = format!("{argv:?}");
        match self.env.run(&argv, None) {
            Ok(output) => {
                let enabled = match output.stdout.trim() {
                    "enabled" => Some(true),
                    "disabled" => Some(false),
                    _ => None,
                };
                ran.push(CmdRecord {
                    argv,
                    rc: output.rc,
                });
                // `is-enabled` 對 static、indirect 等狀態可回傳非零；契約只把
                // enabled／disabled 轉成布林值，其餘狀態保留為未知。
                Ok(enabled)
            }
            Err(error) => {
                ran.push(CmdRecord { argv, rc: -1 });
                Err(format!("{label} 執行失敗：{}", error_summary(&error)))
            }
        }
    }

    fn rollback_hold(
        &self,
        mode: &SupervisionMode,
        name: &str,
        path: &Path,
        prior_enabled: Option<bool>,
        with_sudo: bool,
        ran: &mut Vec<CmdRecord>,
    ) -> Vec<String> {
        let mut failures = Vec::new();
        if matches!(mode, SupervisionMode::SystemdSystem) {
            let remove = vec![
                "rm".to_string(),
                "-f".to_string(),
                path.display().to_string(),
            ];
            let argv = prepend_sudo(remove);
            if let Err(error) = self.run_recorded(argv, None, ran) {
                failures.push(error);
            }
        } else if self.fs.exists(path) {
            if let Err(error) = self.fs.remove_file(path) {
                failures.push(format!("drop-in 移除失敗：{}", error_summary(&error)));
            }
        }

        let reload = Self::systemctl_argv(mode, with_sudo, &strings(["daemon-reload"]));
        if let Err(error) = self.run_recorded(reload, None, ran) {
            failures.push(error);
        }
        if prior_enabled == Some(true) {
            let enable = Self::systemctl_argv(mode, with_sudo, &strings(["enable", name]));
            if let Err(error) = self.run_recorded(enable, None, ran) {
                failures.push(error);
            }
        }
        failures
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

    fn release_failure(mode: &SupervisionMode, detail: impl Into<String>) -> ServiceResult {
        service_error(
            mode,
            "release_autostart",
            "INTERNAL_ERROR",
            detail,
            Some("autostart_release"),
        )
    }

    fn hold_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        with_sudo: bool,
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
        if !matches!(
            mode,
            SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem
        ) {
            return Err(no_systemd(mode, ServiceAction::Stop));
        }
        if matches!(mode, SupervisionMode::SystemdSystem) && !with_sudo {
            return Err(service_error(
                mode,
                "hold_autostart",
                "NEEDS_SUDO",
                "需 root：更新／回滾須先停用 systemd 自動拉起，請加 --with-sudo",
                None,
            ));
        }

        let path = self
            .hold_path(mode, name)
            .ok_or_else(|| Self::hold_failure(mode, "無法解析 systemd hold drop-in 路徑"))?;
        let mut ran = Vec::new();
        let already_held = match self.fs.read(&path) {
            Ok(_) => true,
            Err(error) if is_not_found(&error) => false,
            Err(error) => {
                return Err(Self::hold_failure(
                    mode,
                    format!("讀取既有 hold drop-in 失敗：{}", error_summary(&error)),
                ));
            }
        };
        let (prior_restart, prior_enabled) = if already_held {
            (None, None)
        } else {
            let restart_argv = Self::systemctl_argv(
                mode,
                false,
                &strings(["show", name, "-p", "Restart", "--value"]),
            );
            let restart = self
                .run_recorded(restart_argv, None, &mut ran)
                .map_err(|error| Self::hold_failure(mode, error))?;
            let prior_restart = match restart.stdout.trim() {
                "" => None,
                value => Some(value.to_string()),
            };
            let prior_enabled = self
                .run_enabled_state(mode, name, &mut ran)
                .map_err(|error| Self::hold_failure(mode, error))?;
            (prior_restart, prior_enabled)
        };

        let write_path = if matches!(mode, SupervisionMode::SystemdSystem) {
            let home = self
                .home()
                .ok_or_else(|| Self::hold_failure(mode, "無法取得使用者家目錄"))?;
            home.join(".local/share/serialwrap/update-hold.conf")
        } else {
            path.clone()
        };
        let parent = write_path
            .parent()
            .ok_or_else(|| Self::hold_failure(mode, "hold drop-in 沒有父目錄"))?;
        self.fs.mkdirs(parent).map_err(|error| {
            Self::hold_failure(
                mode,
                format!("建立 hold 目錄失敗：{}", error_summary(&error)),
            )
        })?;
        self.fs
            .atomic_write(&write_path, HOLD_DROPIN, Some(0o644))
            .map_err(|error| {
                Self::hold_failure(
                    mode,
                    format!("寫入 hold drop-in 失敗：{}", error_summary(&error)),
                )
            })?;

        if matches!(mode, SupervisionMode::SystemdSystem) {
            let install = vec![
                "sudo".to_string(),
                "install".to_string(),
                "-D".to_string(),
                "-m".to_string(),
                "0644".to_string(),
                write_path.display().to_string(),
                path.display().to_string(),
            ];
            if let Err(error) = self.run_recorded(install, None, &mut ran) {
                let rollback =
                    self.rollback_hold(mode, name, &path, prior_enabled, with_sudo, &mut ran);
                return Err(Self::hold_failure(mode, append_rollback(error, rollback)));
            }
        }

        let reload = Self::systemctl_argv(mode, with_sudo, &strings(["daemon-reload"]));
        if let Err(error) = self.run_recorded(reload, None, &mut ran) {
            let rollback =
                self.rollback_hold(mode, name, &path, prior_enabled, with_sudo, &mut ran);
            return Err(Self::hold_failure(mode, append_rollback(error, rollback)));
        }
        let disable = Self::systemctl_argv(mode, with_sudo, &strings(["disable", name]));
        if let Err(error) = self.run_recorded(disable, None, &mut ran) {
            let rollback =
                self.rollback_hold(mode, name, &path, prior_enabled, with_sudo, &mut ran);
            return Err(Self::hold_failure(mode, append_rollback(error, rollback)));
        }

        let verify_argv = Self::systemctl_argv(
            mode,
            false,
            &strings([
                "show",
                name,
                "-p",
                "Restart",
                "-p",
                "RefuseManualStart",
                "-p",
                "UnitFileState",
            ]),
        );
        let verify = match self.run_recorded(verify_argv, None, &mut ran) {
            Ok(output) => parse_key_values(&output.stdout),
            Err(error) => {
                let rollback =
                    self.rollback_hold(mode, name, &path, prior_enabled, with_sudo, &mut ran);
                return Err(Self::hold_failure(mode, append_rollback(error, rollback)));
            }
        };
        let verified = verify.get("Restart").is_some_and(|value| value == "no")
            && verify
                .get("RefuseManualStart")
                .is_some_and(|value| value == "yes")
            && verify
                .get("UnitFileState")
                .is_some_and(|value| value == "disabled");
        if !verified {
            let error = format!("systemd hold 驗證不符：{verify:?}");
            let rollback =
                self.rollback_hold(mode, name, &path, prior_enabled, with_sudo, &mut ran);
            return Err(Self::hold_failure(mode, append_rollback(error, rollback)));
        }

        Ok(AutostartHoldRecord {
            mode: mode.clone(),
            kind: AutostartHoldKind::SystemdDropIn,
            name: Some(name.to_string()),
            dropin_path: Some(path),
            prior_enabled,
            prior_restart,
            ran,
        })
    }

    fn release_named(
        &self,
        hold: &AutostartHoldRecord,
        restore_enabled: Option<bool>,
        with_sudo: bool,
    ) -> Result<Vec<CmdRecord>, ServiceResult> {
        let mode = &hold.mode;
        if hold.kind == AutostartHoldKind::NoSupervisor {
            return Ok(Vec::new());
        }
        if hold.kind != AutostartHoldKind::SystemdDropIn
            || !matches!(
                mode,
                SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem
            )
        {
            return Err(Self::release_failure(mode, "hold 紀錄與 systemd 模式不符"));
        }
        if matches!(mode, SupervisionMode::SystemdSystem) && !with_sudo {
            return Err(service_error(
                mode,
                "release_autostart",
                "NEEDS_SUDO",
                "需 root：撤銷 systemd 自動拉起抑制，請加 --with-sudo",
                Some("autostart_release"),
            ));
        }
        let path = hold
            .dropin_path
            .as_deref()
            .ok_or_else(|| Self::release_failure(mode, "hold 紀錄缺少 drop-in 路徑"))?;
        let name = path
            .file_name()
            .and_then(|_| path.parent())
            .and_then(Path::file_name)
            .and_then(|value| value.to_str())
            .and_then(|directory| directory.strip_suffix(".service.d"))
            .unwrap_or("serialwrap");
        let mut ran = Vec::new();

        // drop-in 移除後，`<unit>.service.d` 若已空則 best-effort 一併移除（非空或失敗皆不影響 release）。
        let dropin_dir = path.parent();
        if matches!(mode, SupervisionMode::SystemdSystem) {
            let remove = prepend_sudo(vec!["rm".into(), "-f".into(), path.display().to_string()]);
            self.run_recorded(remove, None, &mut ran)
                .map_err(|error| Self::release_failure(mode, error))?;
            if let Some(dir) = dropin_dir {
                let rmdir = prepend_sudo(vec![
                    "rmdir".into(),
                    "--ignore-fail-on-non-empty".into(),
                    dir.display().to_string(),
                ]);
                let _ = self.run_recorded(rmdir, None, &mut ran);
            }
        } else {
            if self.fs.exists(path) {
                self.fs.remove_file(path).map_err(|error| {
                    Self::release_failure(
                        mode,
                        format!("移除 hold drop-in 失敗：{}", error_summary(&error)),
                    )
                })?;
            }
            if let Some(dir) = dropin_dir {
                let _ = self.fs.remove_dir(dir);
            }
        }

        let reload = Self::systemctl_argv(mode, with_sudo, &strings(["daemon-reload"]));
        self.run_recorded(reload, None, &mut ran)
            .map_err(|error| Self::release_failure(mode, error))?;
        if restore_enabled == Some(true) {
            let enable = Self::systemctl_argv(mode, with_sudo, &strings(["enable", name]));
            self.run_recorded(enable, None, &mut ran)
                .map_err(|error| Self::release_failure(mode, error))?;
        }

        let verify_argv = Self::systemctl_argv(
            mode,
            false,
            &strings(["show", name, "-p", "Restart", "-p", "RefuseManualStart"]),
        );
        let verify = self
            .run_recorded(verify_argv, None, &mut ran)
            .map_err(|error| Self::release_failure(mode, error))?;
        let values = parse_key_values(&verify.stdout);
        let restart_ok = match hold.prior_restart.as_deref() {
            Some(prior) => values.get("Restart").is_some_and(|value| value == prior),
            None => values.get("Restart").is_some_and(|value| value != "no"),
        };
        if !restart_ok
            || values
                .get("RefuseManualStart")
                .is_none_or(|value| value != "no")
        {
            return Err(Self::release_failure(
                mode,
                format!("systemd hold 撤銷驗證不符：{values:?}"),
            ));
        }
        Ok(ran)
    }
}

impl ServiceCtl for SystemdCtl {
    fn supported_modes(&self) -> Vec<SupervisionMode> {
        vec![
            SupervisionMode::OnDemand,
            SupervisionMode::SystemdUser,
            SupervisionMode::SystemdSystem,
        ]
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
        if !matches!(
            mode,
            SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem
        ) {
            return no_systemd(mode, action);
        }
        let privileged = matches!(
            action,
            ServiceAction::Start | ServiceAction::Stop | ServiceAction::Restart
        );
        if matches!(mode, SupervisionMode::SystemdSystem) && privileged && !with_sudo {
            return needs_sudo_systemd(mode, action, name);
        }
        let argv = Self::systemctl_argv(
            mode,
            matches!(mode, SupervisionMode::SystemdSystem) && privileged && with_sudo,
            &strings([action.as_str(), name]),
        );
        service_result(mode, action, self.env.run(&argv, None))
    }

    fn install(&self, def: &SupervisorDefinition, with_sudo: bool) -> Vec<CmdRecord> {
        let mode = &def.mode;
        if !matches!(
            mode,
            SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem
        ) || (matches!(mode, SupervisionMode::SystemdSystem) && !with_sudo)
        {
            return Vec::new();
        }
        let sudo = matches!(mode, SupervisionMode::SystemdSystem) && with_sudo;
        let mut commands = vec![Self::systemctl_argv(
            mode,
            sudo,
            &strings(["daemon-reload"]),
        )];
        if !def.register_held {
            commands.push(Self::systemctl_argv(
                mode,
                sudo,
                &strings(["enable", &def.name]),
            ));
            commands.push(Self::systemctl_argv(
                mode,
                sudo,
                &strings(["start", &def.name]),
            ));
        }
        commands
            .into_iter()
            .map(|argv| cmd_record(self.env.as_ref(), argv))
            .collect()
    }

    fn install_plan(&self, def: &SupervisorDefinition) -> Vec<Vec<String>> {
        if !matches!(def.mode, SupervisionMode::SystemdSystem) {
            return Vec::new();
        }
        let mut plan = vec![vec![
            "sudo".into(),
            "systemctl".into(),
            "daemon-reload".into(),
        ]];
        if !def.register_held {
            plan.push(vec![
                "sudo".into(),
                "systemctl".into(),
                "enable".into(),
                def.name.clone(),
            ]);
            plan.push(vec![
                "sudo".into(),
                "systemctl".into(),
                "start".into(),
                def.name.clone(),
            ]);
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
        with_sudo: bool,
    ) -> Vec<CmdRecord> {
        if !matches!(
            mode,
            SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem
        ) || (matches!(mode, SupervisionMode::SystemdSystem) && !with_sudo)
        {
            return Vec::new();
        }
        let argv = Self::systemctl_argv(mode, with_sudo, &strings(["disable", name]));
        vec![cmd_record(self.env.as_ref(), argv)]
    }

    fn set_enabled(
        &self,
        mode: &SupervisionMode,
        enabled: bool,
        with_sudo: bool,
    ) -> Vec<CmdRecord> {
        if !matches!(
            mode,
            SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem
        ) || (matches!(mode, SupervisionMode::SystemdSystem) && !with_sudo)
        {
            return Vec::new();
        }
        let action = if enabled { "enable" } else { "disable" };
        let argv = Self::systemctl_argv(mode, with_sudo, &strings([action, "serialwrap"]));
        vec![cmd_record(self.env.as_ref(), argv)]
    }

    fn query(&self, mode: &SupervisionMode) -> TaskQuery {
        self.query_named(mode, "serialwrap")
    }

    fn query_named(&self, mode: &SupervisionMode, name: &str) -> TaskQuery {
        if !matches!(
            mode,
            SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem
        ) {
            return TaskQuery::QueryFailed(format!("不支援的 systemd 模式：{}", mode.as_str()));
        }
        let argv = Self::systemctl_argv(mode, false, &strings(["is-active", name]));
        let output = match self.env.run(&argv, Some(Duration::from_secs(5))) {
            Ok(output) => output,
            Err(error) => return TaskQuery::QueryFailed(error_summary(&error)),
        };
        if output.rc == 4 {
            return TaskQuery::NotFound;
        }
        let state = output.stdout.trim();
        match state {
            "active" | "activating" | "deactivating" | "reloading" => TaskQuery::Running,
            "inactive" | "failed" => TaskQuery::NotRunning(state.to_string()),
            _ => {
                TaskQuery::QueryFailed(format!("rc={} stderr={}", output.rc, output.stderr.trim()))
            }
        }
    }

    fn hard_stop(&self, mode: &SupervisionMode, with_sudo: bool) -> ServiceResult {
        self.action(mode, ServiceAction::Stop, with_sudo)
    }

    fn hold_autostart(
        &self,
        mode: &SupervisionMode,
        with_sudo: bool,
    ) -> Result<AutostartHoldRecord, ServiceResult> {
        self.hold_named(mode, "serialwrap", with_sudo)
    }

    fn release_autostart(
        &self,
        hold: &AutostartHoldRecord,
        restore_enabled: Option<bool>,
        with_sudo: bool,
    ) -> Result<Vec<CmdRecord>, ServiceResult> {
        self.release_named(hold, restore_enabled, with_sudo)
    }

    fn autostart_hold_state(&self, mode: &SupervisionMode) -> HoldState {
        if matches!(mode, SupervisionMode::OnDemand) {
            return HoldState::NotHeld;
        }
        let Some(path) = self.hold_path(mode, "serialwrap") else {
            return HoldState::QueryFailed(format!("不支援的 systemd 模式：{}", mode.as_str()));
        };
        match self.fs.read(&path) {
            Ok(_) => HoldState::Held,
            Err(error) if is_not_found(&error) => HoldState::NotHeld,
            Err(error) => HoldState::QueryFailed(error_summary(&error)),
        }
    }

    fn hold_autostart_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        with_sudo: bool,
    ) -> Result<AutostartHoldRecord, ServiceResult> {
        self.hold_named(mode, name, with_sudo)
    }
}

fn strings<const N: usize>(values: [&str; N]) -> Vec<String> {
    values.into_iter().map(str::to_string).collect()
}

fn prepend_sudo(mut argv: Vec<String>) -> Vec<String> {
    argv.insert(0, "sudo".into());
    argv
}

fn append_rollback(mut error: String, rollback: Vec<String>) -> String {
    if !rollback.is_empty() {
        error.push_str("；回復失敗：");
        error.push_str(&rollback.join("；"));
    }
    error
}
