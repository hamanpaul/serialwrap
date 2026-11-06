//! 組裝點替身（P4-T1）：`platform()` 建構器與 `posix_caps`／`windows_caps`。
//!
//! 未設定的元件以 `Unconfigured` 填入：任何方法呼叫即 panic `fake component not configured: <name>`，
//! 避免測試意外走到真實 OS（計畫 Task 1 替身規則）。
#![allow(unused_variables)]

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use crate::device_source::windows_model::RegistryView;
use crate::fs_ops::{FileLockGuard, FsOps, LinkKind};
use crate::proc_scan::{DaemonProc, HolderScanStatus, ProcIdentity, ProcScan, ScanFailure};
use crate::process_tunnel::{ProcessTunnel, TunnelProc};
use crate::service_ctl::{
    AutostartHoldRecord, CmdRecord, HoldState, ServiceAction, ServiceCtl, ServiceResult,
    SupervisionMode, SupervisorDefinition, TaskQuery,
};
use crate::traits::{
    ChildHandle, Clock, CmdOutput, CmdOutputRaw, EndpointSpec, InterruptScope, PalError, PalResult,
    ProcessCtl, RpcConnector, RpcStream, SockTimeout, SysEnv,
};
use crate::{BackendKind, Capabilities, ConsoleKind, PalPathFlavor, Platform, SkillPlatform};

/// POSIX 能力包（替身預設；對應 `native()` 的 POSIX 分支）。
pub fn posix_caps() -> Capabilities {
    Capabilities {
        path_flavor: PalPathFlavor::Posix,
        af_unix: true,
        native_backend: BackendKind::Posix,
        default_skill: SkillPlatform::Linux,
        text_newline: "\n",
        exec_replace: true,
        on_demand_autostart: false,
        console_kind: ConsoleKind::Pty,
    }
}

/// Windows 能力包（`console_kind=TcpTelnet`、`native_backend=Win`、`on_demand_autostart=true`，SC-18）。
pub fn windows_caps() -> Capabilities {
    Capabilities {
        path_flavor: PalPathFlavor::Windows,
        af_unix: false,
        native_backend: BackendKind::Win,
        default_skill: SkillPlatform::Windows,
        text_newline: "\r\n",
        exec_replace: false,
        on_demand_autostart: true,
        console_kind: ConsoleKind::TcpTelnet,
    }
}

/// 組裝點替身建構器：未設定的元件一律為 `Unconfigured`。
pub struct FakePlatformBuilder {
    env: Option<Arc<dyn SysEnv>>,
    fs: Option<Arc<dyn FsOps>>,
    clock: Option<Arc<dyn Clock>>,
    rpc: Option<Arc<dyn RpcConnector>>,
    process: Option<Arc<dyn ProcessCtl>>,
    procs: Option<Arc<dyn ProcScan>>,
    tunnel: Option<Arc<dyn ProcessTunnel>>,
    service: Option<Arc<dyn ServiceCtl>>,
    registry: Option<Arc<dyn RegistryView + Sync>>,
    caps: Capabilities,
}

/// 建立組裝點替身建構器（預設能力包為 `posix_caps()`）。
pub fn platform() -> FakePlatformBuilder {
    FakePlatformBuilder {
        env: None,
        fs: None,
        clock: None,
        rpc: None,
        process: None,
        procs: None,
        tunnel: None,
        service: None,
        registry: None,
        caps: posix_caps(),
    }
}

impl FakePlatformBuilder {
    pub fn env(mut self, v: Arc<dyn SysEnv>) -> Self {
        self.env = Some(v);
        self
    }
    pub fn procs(mut self, v: Arc<dyn ProcScan>) -> Self {
        self.procs = Some(v);
        self
    }
    pub fn process(mut self, v: Arc<dyn ProcessCtl>) -> Self {
        self.process = Some(v);
        self
    }
    pub fn tunnel(mut self, v: Arc<dyn ProcessTunnel>) -> Self {
        self.tunnel = Some(v);
        self
    }
    pub fn service(mut self, v: Arc<dyn ServiceCtl>) -> Self {
        self.service = Some(v);
        self
    }
    pub fn rpc(mut self, v: Arc<dyn RpcConnector>) -> Self {
        self.rpc = Some(v);
        self
    }
    pub fn registry(mut self, v: Arc<dyn RegistryView + Sync>) -> Self {
        self.registry = Some(v);
        self
    }
    pub fn fs(mut self, v: Arc<dyn FsOps>) -> Self {
        self.fs = Some(v);
        self
    }
    pub fn clock(mut self, v: Arc<dyn Clock>) -> Self {
        self.clock = Some(v);
        self
    }
    pub fn caps(mut self, v: Capabilities) -> Self {
        self.caps = v;
        self
    }

    pub fn build(self) -> Platform {
        Platform {
            env: self.env.unwrap_or_else(|| Arc::new(Unconfigured("env"))),
            fs: self.fs.unwrap_or_else(|| Arc::new(Unconfigured("fs"))),
            clock: self
                .clock
                .unwrap_or_else(|| Arc::new(Unconfigured("clock"))),
            rpc: self.rpc.unwrap_or_else(|| Arc::new(Unconfigured("rpc"))),
            process: self
                .process
                .unwrap_or_else(|| Arc::new(Unconfigured("process"))),
            procs: self
                .procs
                .unwrap_or_else(|| Arc::new(Unconfigured("procs"))),
            tunnel: self
                .tunnel
                .unwrap_or_else(|| Arc::new(Unconfigured("tunnel"))),
            service: self
                .service
                .unwrap_or_else(|| Arc::new(Unconfigured("service"))),
            registry: self
                .registry
                .unwrap_or_else(|| Arc::new(Unconfigured("registry"))),
            caps: self.caps,
        }
    }
}

/// 未設定元件的占位：任何呼叫即 panic，訊息含元件名稱。
pub struct Unconfigured(pub &'static str);

fn not_configured(name: &str) -> ! {
    panic!("fake component not configured: {name}")
}

impl SysEnv for Unconfigured {
    fn var(&self, _k: &str) -> Option<String> {
        not_configured(self.0)
    }
    fn vars(&self) -> Vec<(String, String)> {
        not_configured(self.0)
    }
    fn system_config_dir(&self) -> String {
        not_configured(self.0)
    }
    fn current_user_home(&self) -> Option<String> {
        not_configured(self.0)
    }
    fn user_home(&self, _user: &str) -> Option<String> {
        not_configured(self.0)
    }
    fn current_exe(&self) -> PalResult<PathBuf> {
        not_configured(self.0)
    }
    fn realpath(&self, _p: &Path) -> PalResult<PathBuf> {
        not_configured(self.0)
    }
    fn terminal_columns(&self) -> Option<u16> {
        not_configured(self.0)
    }
    fn stdout_is_terminal(&self) -> bool {
        not_configured(self.0)
    }
    fn errno_name(&self, _code: i32) -> Option<&'static str> {
        not_configured(self.0)
    }
    fn pid(&self) -> u32 {
        not_configured(self.0)
    }
    fn run(&self, _argv: &[String], _timeout: Option<Duration>) -> PalResult<CmdOutput> {
        not_configured(self.0)
    }
    fn run_merged(&self, _argv: &[String], _timeout: Option<Duration>) -> PalResult<CmdOutput> {
        not_configured(self.0)
    }
    fn interrupt_scope(&self) -> PalResult<Box<dyn InterruptScope>> {
        not_configured(self.0)
    }
    fn run_raw(
        &self,
        _argv: &[String],
        _env: Option<&[(OsString, OsString)]>,
        _timeout: Option<Duration>,
    ) -> PalResult<CmdOutputRaw> {
        not_configured(self.0)
    }
    fn which(&self, _name: &str) -> Option<PathBuf> {
        not_configured(self.0)
    }
    fn which_all(&self, _name: &str) -> Vec<PathBuf> {
        not_configured(self.0)
    }
    fn has_systemd(&self) -> bool {
        not_configured(self.0)
    }
    fn is_wsl(&self) -> bool {
        not_configured(self.0)
    }
    fn user_in_group(&self, _g: &str) -> bool {
        not_configured(self.0)
    }
    fn user_identity(&self) -> String {
        not_configured(self.0)
    }
    fn local_appdata(&self) -> Option<PathBuf> {
        not_configured(self.0)
    }
}

impl crate::traits::FsRead for Unconfigured {
    fn read(&self, _p: &Path) -> PalResult<Vec<u8>> {
        not_configured(self.0)
    }
    fn exists(&self, _p: &Path) -> bool {
        not_configured(self.0)
    }
    fn is_dir(&self, _p: &Path) -> bool {
        not_configured(self.0)
    }
    fn is_file(&self, _p: &Path) -> bool {
        not_configured(self.0)
    }
    fn list_dir(&self, _p: &Path) -> PalResult<Vec<String>> {
        not_configured(self.0)
    }
}

impl FsOps for Unconfigured {
    fn atomic_write(&self, _p: &Path, _data: &[u8], _mode: Option<u32>) -> PalResult<()> {
        not_configured(self.0)
    }
    fn write_file(&self, _p: &Path, _data: &[u8]) -> PalResult<()> {
        not_configured(self.0)
    }
    fn mkdirs(&self, _p: &Path) -> PalResult<()> {
        not_configured(self.0)
    }
    fn replace(&self, _from: &Path, _to: &Path) -> PalResult<()> {
        not_configured(self.0)
    }
    fn remove_file(&self, _p: &Path) -> PalResult<()> {
        not_configured(self.0)
    }
    fn remove_dir(&self, _p: &Path) -> PalResult<()> {
        not_configured(self.0)
    }
    fn copy_preserving(&self, _from: &Path, _to: &Path) -> PalResult<()> {
        not_configured(self.0)
    }
    fn sync_file(&self, _p: &Path) -> PalResult<()> {
        not_configured(self.0)
    }
    fn copy_tree(&self, _from: &Path, _to: &Path) -> PalResult<()> {
        not_configured(self.0)
    }
    fn remove_tree(&self, _p: &Path) -> PalResult<()> {
        not_configured(self.0)
    }
    fn read_link(&self, _p: &Path) -> PalResult<Option<PathBuf>> {
        not_configured(self.0)
    }
    fn set_mode(&self, _p: &Path, _mode: u32) -> PalResult<()> {
        not_configured(self.0)
    }
    fn sync_dir(&self, _p: &Path) -> PalResult<()> {
        not_configured(self.0)
    }
    fn symlink_file(&self, _link: &Path, _target: &Path) -> PalResult<()> {
        not_configured(self.0)
    }
    fn symlink_or_copy_dir(&self, _link: &Path, _target: &Path) -> PalResult<LinkKind> {
        not_configured(self.0)
    }
    fn file_lock_exclusive(&self, _p: &Path, _blocking: bool) -> PalResult<FileLockGuard> {
        not_configured(self.0)
    }
}

impl Clock for Unconfigured {
    fn mono_ns(&self) -> u64 {
        not_configured(self.0)
    }
    fn sleep(&self, _d: Duration) {
        not_configured(self.0)
    }
}

impl RpcConnector for Unconfigured {
    fn af_unix_available(&self) -> bool {
        not_configured(self.0)
    }
    fn connect(&self, _ep: &EndpointSpec, _t: SockTimeout) -> PalResult<Box<dyn RpcStream>> {
        not_configured(self.0)
    }
    fn resolve_tcp(&self, _host: &str, _port: u16) -> PalResult<()> {
        not_configured(self.0)
    }
}

impl ProcessCtl for Unconfigured {
    fn spawn_elevated(
        &self,
        _exe: &std::path::Path,
        _args: &[String],
    ) -> PalResult<crate::traits::ElevatedOutcome> {
        not_configured(self.0)
    }

    fn exec_or_wait(&self, _argv: &[OsString], _env_add: &[(String, String)]) -> PalError {
        not_configured(self.0)
    }
    fn spawn_daemon(
        &self,
        _argv: &[String],
        _env: &[(OsString, OsString)],
        _detached: bool,
    ) -> PalResult<ChildHandle> {
        not_configured(self.0)
    }
    fn poll(&self, _c: &ChildHandle) -> Option<i64> {
        not_configured(self.0)
    }
    fn call_foreground(&self, _argv: &[String], _env: &[(OsString, OsString)]) -> PalResult<i64> {
        not_configured(self.0)
    }
}

impl ProcScan for Unconfigured {
    fn serialwrapd_instances(&self) -> Result<Vec<DaemonProc>, ScanFailure> {
        not_configured(self.0)
    }
    fn serialwrapd_tty_holders(&self, _paths: &[PathBuf]) -> (Vec<u32>, HolderScanStatus) {
        not_configured(self.0)
    }
    fn pid_alive(&self, _pid: u32) -> bool {
        not_configured(self.0)
    }
    fn identity_alive(&self, _id: &ProcIdentity) -> bool {
        not_configured(self.0)
    }
}

impl ProcessTunnel for Unconfigured {
    fn spawn_detached(&self, _argv: &[String], _log: &Path) -> PalResult<TunnelProc> {
        not_configured(self.0)
    }
    fn alive(&self, _p: &TunnelProc) -> bool {
        not_configured(self.0)
    }
    fn terminate(&self, _p: &TunnelProc, _grace: Duration) -> PalResult<()> {
        not_configured(self.0)
    }
    fn stderr_tail(&self, _log: &Path, _max: usize) -> String {
        not_configured(self.0)
    }
    fn supports_control_master(&self) -> bool {
        not_configured(self.0)
    }
}

impl ServiceCtl for Unconfigured {
    fn supported_modes(&self) -> Vec<SupervisionMode> {
        not_configured(self.0)
    }
    fn action(
        &self,
        _mode: &SupervisionMode,
        _action: ServiceAction,
        _with_sudo: bool,
    ) -> ServiceResult {
        not_configured(self.0)
    }
    fn action_named(
        &self,
        _mode: &SupervisionMode,
        _name: &str,
        _action: ServiceAction,
        _with_sudo: bool,
    ) -> ServiceResult {
        not_configured(self.0)
    }
    fn install(&self, _def: &SupervisorDefinition, _with_sudo: bool) -> Vec<CmdRecord> {
        not_configured(self.0)
    }
    fn install_plan(&self, _def: &SupervisorDefinition) -> Vec<Vec<String>> {
        not_configured(self.0)
    }
    fn uninstall(&self, _mode: &SupervisionMode, _with_sudo: bool) -> Vec<CmdRecord> {
        not_configured(self.0)
    }
    fn uninstall_named(
        &self,
        _mode: &SupervisionMode,
        _name: &str,
        _with_sudo: bool,
    ) -> Vec<CmdRecord> {
        not_configured(self.0)
    }
    fn set_enabled(
        &self,
        _mode: &SupervisionMode,
        _enabled: bool,
        _with_sudo: bool,
    ) -> Vec<CmdRecord> {
        not_configured(self.0)
    }
    fn query(&self, _mode: &SupervisionMode) -> TaskQuery {
        not_configured(self.0)
    }
    fn query_named(&self, _mode: &SupervisionMode, _name: &str) -> TaskQuery {
        not_configured(self.0)
    }
    fn hard_stop(&self, _mode: &SupervisionMode, _with_sudo: bool) -> ServiceResult {
        not_configured(self.0)
    }
    fn hold_autostart(
        &self,
        _mode: &SupervisionMode,
        _with_sudo: bool,
    ) -> Result<AutostartHoldRecord, ServiceResult> {
        not_configured(self.0)
    }
    fn release_autostart(
        &self,
        _hold: &AutostartHoldRecord,
        _restore_enabled: Option<bool>,
        _with_sudo: bool,
    ) -> Result<Vec<CmdRecord>, ServiceResult> {
        not_configured(self.0)
    }
    fn autostart_hold_state(&self, _mode: &SupervisionMode) -> HoldState {
        not_configured(self.0)
    }
    fn hold_autostart_named(
        &self,
        _mode: &SupervisionMode,
        _name: &str,
        _with_sudo: bool,
    ) -> Result<AutostartHoldRecord, ServiceResult> {
        not_configured(self.0)
    }
}

impl RegistryView for Unconfigured {
    fn enum_values(&self, _key: &str) -> PalResult<Vec<(String, String)>> {
        not_configured(self.0)
    }
    fn enum_subkeys(&self, _key: &str) -> PalResult<Vec<String>> {
        not_configured(self.0)
    }
    fn query_string(&self, _key: &str, _name: &str) -> PalResult<Option<String>> {
        not_configured(self.0)
    }
}
