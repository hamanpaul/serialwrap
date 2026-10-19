//! `setup` 子命令（P4-T10／T11／T12 承接）。

use std::path::{Path, PathBuf};

use crate::argparse::Namespace;
use crate::ctx::CliCtx;
pub use crate::graceful_stop::DaemonIdentity;
use sw_pal::traits::SupervisionMode;
use sw_proto::pyjson::PyDict;
use sw_proto::runtime_paths::EnvSnapshot;

pub struct UserDirs {
    pub config: PathBuf,
    pub data: PathBuf,
    pub agents_skill_link: PathBuf,
    pub legacy_agents_skill_link: PathBuf,
    pub bin: PathBuf,
}

pub fn user_dirs(env: &EnvSnapshot, home: &Path) -> UserDirs {
    todo!("not implemented: P4-T10: user_dirs")
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LinkKind {
    Symlink,
    Copy,
}

pub struct MaterializeReport {
    pub profiles: PathBuf,
    pub skill_link: PathBuf,
    pub skill_link_kind: LinkKind,
    pub bin: Option<PathBuf>,
}

#[derive(Debug)]
pub enum SetupError {
    Io(String),
}

pub fn materialize_assets(
    ctx: &CliCtx,
    dirs: &UserDirs,
    force: bool,
) -> Result<MaterializeReport, SetupError> {
    todo!("not implemented: P4-T10: materialize_assets")
}

pub fn detect_legacy_install(ctx: &CliCtx, home: &Path) -> Option<PyDict> {
    todo!("not implemented: P4-T10: detect_legacy_install")
}

pub struct WslInfo {
    pub wsl: bool,
    pub already: bool,
    pub enabled_now: bool,
    pub needs_restart: bool,
    pub rc: Option<i32>,
    pub hint: Option<String>,
    pub pending: Vec<Vec<String>>,
}

pub fn ensure_wsl_systemd(
    ctx: &CliCtx,
    home: &Path,
    target: &SupervisionMode,
    with_sudo: bool,
) -> WslInfo {
    todo!("not implemented: P4-T10: ensure_wsl_systemd")
}

pub fn render_user_unit(exec_start: &str) -> String {
    todo!("not implemented: P4-T10: render_user_unit")
}

pub fn render_system_unit(exec_start: &str, run_user: &str) -> String {
    todo!("not implemented: P4-T10: render_system_unit")
}

pub struct ReconcileRequest {
    pub old_mode: SupervisionMode,
    pub target_mode: SupervisionMode,
    pub home: PathBuf,
    pub daemon_running: bool,
    pub daemon_id: DaemonIdentity,
    pub old_endpoint: String,
    pub any_flashing: bool,
    pub with_sudo: bool,
    pub force: bool,
    pub socket_path: Option<String>,
    pub config_path: PathBuf,
    pub old_stop: OldStop,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OldStop {
    Reconcile,
    HandedOff {
        token: String,
        ran: Vec<Vec<String>>,
    },
}

#[derive(Debug)]
pub enum ReconcileError {
    FlashingBusy(String),
    WaitOldExitTimeout { partial: PyDict },
    Config(String),
}

pub fn reconcile(ctx: &mut CliCtx, req: &ReconcileRequest) -> Result<PyDict, ReconcileError> {
    todo!("not implemented: P4-T10: reconcile")
}

pub fn reconcile_stops_old(ctx: &CliCtx, req: &ReconcileRequest) -> bool {
    todo!("not implemented: P4-T10: reconcile_stops_old")
}

pub fn resolve_target_mode(
    ns: &Namespace,
    ctx: &CliCtx,
) -> Result<SupervisionMode, PyDict> {
    todo!("not implemented: P4-T10: resolve_target_mode")
}

pub fn run_setup(ns: &Namespace, ctx: &mut CliCtx) -> i32 {
    todo!("not implemented: P4-T10: run_setup")
}
