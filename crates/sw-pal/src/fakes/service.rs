//! 假 `ServiceCtl`（feature `test-fakes`）：記錄每次動作，回應與查詢結果可腳本化。
//!
//! 記錄格式：`(mode, action, with_sudo)`；`action` 為 `start|stop|restart|status`、`install`、`uninstall`、
//! `set_enabled:<true|false>`、`hard_stop`、`hold_autostart`、`release_autostart:<true|false|keep>`。
#![allow(unused_variables)]

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use crate::service_ctl::{
    AutostartHoldKind, AutostartHoldRecord, CmdRecord, HoldState, ServiceAction, ServiceCtl,
    ServiceResult, SupervisionMode, SupervisorDefinition, TaskQuery,
};

fn mode_name(mode: &SupervisionMode) -> String {
    match mode {
        SupervisionMode::OnDemand => "on-demand".into(),
        SupervisionMode::SystemdUser => "systemd-user".into(),
        SupervisionMode::SystemdSystem => "systemd-system".into(),
        SupervisionMode::WindowsTask => "windows-task".into(),
        SupervisionMode::WindowsService => "windows-service".into(),
        SupervisionMode::Other(s) => s.clone(),
    }
}

fn action_name(action: ServiceAction) -> &'static str {
    match action {
        ServiceAction::Start => "start",
        ServiceAction::Stop => "stop",
        ServiceAction::Restart => "restart",
        ServiceAction::Status => "status",
    }
}

fn kind_for(mode: &SupervisionMode) -> AutostartHoldKind {
    match mode {
        SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem => AutostartHoldKind::SystemdDropIn,
        SupervisionMode::WindowsTask => AutostartHoldKind::TaskDisabled,
        SupervisionMode::WindowsService => AutostartHoldKind::ServiceDisabled,
        _ => AutostartHoldKind::NoSupervisor,
    }
}

#[derive(Debug, Default)]
pub struct FakeServiceCtl {
    modes: Vec<SupervisionMode>,
    actions: Mutex<Vec<(u64, String, String, bool)>>,
    responses: Mutex<VecDeque<ServiceResult>>,
    query_seq: Mutex<VecDeque<TaskQuery>>,
    last_query: Mutex<Option<TaskQuery>>,
    held: Mutex<bool>,
    model: Mutex<Option<ServiceModelState>>,
}

/// 有狀態監管模型的快照（`FakeServiceCtl::set_model`／`query_now`）：`enabled` 為 `None` 表示未註冊。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ServiceModelState {
    pub enabled: Option<bool>,
    pub running: bool,
}

impl FakeServiceCtl {
    pub fn new(modes: Vec<SupervisionMode>) -> Arc<Self> {
        Arc::new(Self {
            modes,
            ..Self::default()
        })
    }

    /// 依呼叫順序回傳記錄 `(mode, action, with_sudo)`。
    pub fn actions(&self) -> Vec<(String, String, bool)> {
        self.actions
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .map(|(_, mode, action, sudo)| (mode.clone(), action.clone(), *sudo))
            .collect()
    }

    /// 同 `actions`，回傳 `(事件序號, "<mode>", action)` 的 action 事件（事件合併用）。
    pub fn action_events(&self) -> Vec<(u64, String)> {
        self.actions
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .map(|(seq, _, action, _)| (*seq, action.clone()))
            .collect()
    }

    /// 下一次 `action`／`action_named` 的回應（依序；用盡後回預設成功）。
    pub fn respond(&self, r: ServiceResult) {
        self.responses
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push_back(r);
    }

    /// 設定有狀態模型（`TestEnv::with_supervisor_state` 使用）：之後的 start／stop／restart／hard_stop／set_enabled
    /// 會更新 `enabled`／`running`，未腳本化的 `query` 由此狀態推導（`enabled==Some(false)`→`Disabled`、running→`Running`、其餘→`Ready`）。
    pub fn set_model(&self, enabled: Option<bool>, running: bool) {
        *self.model.lock().unwrap_or_else(|e| e.into_inner()) = Some(ServiceModelState { enabled, running });
    }

    /// 目前有狀態模型的快照。
    pub fn query_now(&self) -> ServiceModelState {
        self.model
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .unwrap_or(ServiceModelState {
                enabled: None,
                running: false,
            })
    }

    /// 目前 autostart hold 狀態。
    pub fn hold_state_now(&self) -> HoldState {
        self.autostart_hold_state(&SupervisionMode::OnDemand)
    }

    /// 記錄被監管拉起的 pid 清單。
    pub fn relaunches(&self) -> Vec<u32> {
        Vec::new()
    }

    /// 依序回傳 `query`／`query_named` 的結果；用盡後重複最後一筆（不得以 `NotFound` 讓等待迴圈假綠）。
    /// 從未腳本化時：有模型則由模型推導，否則回 `NotFound`。
    pub fn query_script(&self, seq: Vec<TaskQuery>) {
        *self.query_seq.lock().unwrap_or_else(|e| e.into_inner()) = seq.into();
    }

    fn log(&self, mode: &SupervisionMode, action: &str, with_sudo: bool) {
        self.actions
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push((super::next_event_seq(), mode_name(mode), action.to_string(), with_sudo));
        let mut model = self.model.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(m) = model.as_mut() {
            match action {
                "start" | "restart" => m.running = true,
                "stop" | "hard_stop" => m.running = false,
                "set_enabled:true" => m.enabled = Some(true),
                "set_enabled:false" => m.enabled = Some(false),
                _ => {}
            }
        }
    }

    fn default_result(mode: &SupervisionMode, action: &str) -> ServiceResult {
        ServiceResult {
            ok: true,
            mode: mode_name(mode),
            action: action.to_string(),
            rc: Some(0),
            stdout: None,
            stderr: None,
            error_code: None,
            hint: None,
            phase: None,
        }
    }

    fn next_result(&self, mode: &SupervisionMode, action: &str) -> ServiceResult {
        self.responses
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .pop_front()
            .unwrap_or_else(|| Self::default_result(mode, action))
    }
}

impl ServiceCtl for FakeServiceCtl {
    fn supported_modes(&self) -> Vec<SupervisionMode> {
        self.modes.clone()
    }

    fn action(&self, mode: &SupervisionMode, action: ServiceAction, with_sudo: bool) -> ServiceResult {
        self.log(mode, action_name(action), with_sudo);
        self.next_result(mode, action_name(action))
    }

    fn action_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        action: ServiceAction,
        with_sudo: bool,
    ) -> ServiceResult {
        self.log(mode, action_name(action), with_sudo);
        self.next_result(mode, action_name(action))
    }

    fn install(&self, def: &SupervisorDefinition, with_sudo: bool) -> Vec<CmdRecord> {
        self.log(&def.mode, "install", with_sudo);
        Vec::new()
    }

    fn install_plan(&self, _def: &SupervisorDefinition) -> Vec<Vec<String>> {
        Vec::new()
    }

    fn uninstall(&self, mode: &SupervisionMode, with_sudo: bool) -> Vec<CmdRecord> {
        self.log(mode, "uninstall", with_sudo);
        Vec::new()
    }

    fn uninstall_named(&self, mode: &SupervisionMode, _name: &str, with_sudo: bool) -> Vec<CmdRecord> {
        self.log(mode, "uninstall", with_sudo);
        Vec::new()
    }

    fn set_enabled(&self, mode: &SupervisionMode, enabled: bool, with_sudo: bool) -> Vec<CmdRecord> {
        self.log(mode, &format!("set_enabled:{enabled}"), with_sudo);
        Vec::new()
    }

    fn query(&self, _mode: &SupervisionMode) -> TaskQuery {
        let next = self.query_seq.lock().unwrap_or_else(|e| e.into_inner()).pop_front();
        let mut last = self.last_query.lock().unwrap_or_else(|e| e.into_inner());
        match next {
            Some(q) => {
                *last = Some(q.clone());
                q
            }
            None => {
                if let Some(m) = *self.model.lock().unwrap_or_else(|e| e.into_inner()) {
                    return match (m.enabled, m.running) {
                        (Some(false), _) => TaskQuery::NotRunning("Disabled".into()),
                        (_, true) => TaskQuery::Running,
                        _ => TaskQuery::NotRunning("Ready".into()),
                    };
                }
                last.clone().unwrap_or(TaskQuery::NotFound)
            }
        }
    }

    fn query_named(&self, mode: &SupervisionMode, _name: &str) -> TaskQuery {
        self.query(mode)
    }

    fn hard_stop(&self, mode: &SupervisionMode, with_sudo: bool) -> ServiceResult {
        self.log(mode, "hard_stop", with_sudo);
        self.next_result(mode, "hard_stop")
    }

    fn hold_autostart(
        &self,
        mode: &SupervisionMode,
        with_sudo: bool,
    ) -> Result<AutostartHoldRecord, ServiceResult> {
        self.log(mode, "hold_autostart", with_sudo);
        // 腳本化失敗：respond() 的下一筆 ok=false 即為 hold 失敗（R5-F1 失敗路徑注入）。
        let scripted = self.next_result(mode, "hold_autostart");
        if !scripted.ok {
            return Err(scripted);
        }
        *self.held.lock().unwrap_or_else(|e| e.into_inner()) = true;
        Ok(AutostartHoldRecord {
            mode: mode.clone(),
            kind: kind_for(mode),
            name: None,
            dropin_path: None,
            prior_enabled: None,
            prior_restart: None,
            ran: Vec::new(),
        })
    }

    fn release_autostart(
        &self,
        hold: &AutostartHoldRecord,
        restore_enabled: Option<bool>,
        with_sudo: bool,
    ) -> Result<Vec<CmdRecord>, ServiceResult> {
        let tag = match restore_enabled {
            Some(true) => "true",
            Some(false) => "false",
            None => "keep",
        };
        self.log(&hold.mode, &format!("release_autostart:{tag}"), with_sudo);
        let scripted = self.next_result(&hold.mode, "release_autostart");
        if !scripted.ok {
            return Err(scripted);
        }
        *self.held.lock().unwrap_or_else(|e| e.into_inner()) = false;
        Ok(Vec::new())
    }

    fn autostart_hold_state(&self, _mode: &SupervisionMode) -> HoldState {
        if *self.held.lock().unwrap_or_else(|e| e.into_inner()) {
            HoldState::Held
        } else {
            HoldState::NotHeld
        }
    }

    fn hold_autostart_named(
        &self,
        mode: &SupervisionMode,
        name: &str,
        with_sudo: bool,
    ) -> Result<AutostartHoldRecord, ServiceResult> {
        let mut hold = self.hold_autostart(mode, with_sudo)?;
        hold.name = Some(name.to_string());
        Ok(hold)
    }
}
