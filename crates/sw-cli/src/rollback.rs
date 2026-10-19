//! `setup --rollback`、update 與 `recover_journal`（§12.1；P4-T12 承接）。

use std::path::{Path, PathBuf};

use crate::argparse::Namespace;
use crate::ctx::CliCtx;
use crate::graceful_stop::DaemonIdentity;
pub use crate::graceful_stop::StopFailure;
use crate::install_layout::{CurrentRelease, InstallRoot, LayoutError};
use sw_pal::service_ctl::{AutostartHoldKind, SupervisionMode};
use sw_proto::pyjson::{PyDict, PyValue};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StepName {
    Lock,
    Verify,
    SafetyCheck,
    StopSupervisor,
    ConfirmReleased,
    BackupAndSwitch,
    StartAndVerify,
    RevertToPrevious,
}

impl StepName {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Lock => "lock",
            Self::Verify => "verify",
            Self::SafetyCheck => "safety_check",
            Self::StopSupervisor => "stop_supervisor",
            Self::ConfirmReleased => "confirm_released",
            Self::BackupAndSwitch => "backup_and_switch",
            Self::StartAndVerify => "start_and_verify",
            Self::RevertToPrevious => "revert_to_previous",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct StepRecord {
    pub step: StepName,
    pub ok: bool,
    pub detail: PyDict,
}

#[derive(Clone, Debug, PartialEq)]
pub struct UpdateReport {
    pub ok: bool,
    pub op: &'static str,
    pub from_version: Option<String>,
    pub to_version: String,
    pub steps: Vec<StepRecord>,
    pub backup_dir: Option<PathBuf>,
    pub reverted: bool,
    pub error_code: Option<&'static str>,
    pub phase: Option<&'static str>,
    pub reason: Option<String>,
    pub message: Option<String>,
    pub revert_pending: bool,
    pub revert_error: Option<PyDict>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SupervisorSnapshot {
    pub mode: SupervisionMode,
    pub endpoint: String,
    pub enabled: Option<bool>,
    pub running: bool,
    pub registration: Option<PathBuf>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BackupSet {
    pub dir: PathBuf,
    pub supervisor: SupervisorSnapshot,
}

pub fn backup_state(
    _ctx: &CliCtx,
    _root: &InstallRoot,
    _from: Option<&str>,
    _to: &str,
    _sup: SupervisorSnapshot,
) -> Result<BackupSet, LayoutError> {
    unimplemented!("not implemented: P4-T12")
}

pub fn restore_backup(
    _ctx: &mut CliCtx,
    _set: &BackupSet,
    _include_state: bool,
) -> Result<Vec<PathBuf>, LayoutError> {
    unimplemented!("not implemented: P4-T12")
}

#[derive(Clone, Debug, PartialEq)]
pub struct DaemonSnapshot {
    pub id: DaemonIdentity,
    pub attached_paths: Vec<PathBuf>,
    pub sessions: Vec<PyDict>,
}

pub fn safety_check(
    _ctx: &CliCtx,
    _endpoint: &str,
) -> Result<Option<DaemonSnapshot>, (&'static str, PyDict)> {
    unimplemented!("not implemented: P4-T12")
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HandoffGrant {
    pub token: String,
}

#[derive(Clone, Debug, PartialEq)]
pub enum HandoffError {
    Busy {
        code: String,
        ops: Vec<String>,
    },
    Unconfirmed {
        code: String,
        probe: &'static str,
        outcome: &'static str,
    },
}

pub fn request_handoff_stop(
    _ctx: &CliCtx,
    _endpoint: &str,
    _reason: &str,
) -> Result<HandoffGrant, HandoffError> {
    unimplemented!("not implemented: P4-T12")
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JournalStep {
    Lock,
    Verify,
    SafetyCheck,
    SnapshotTaken,
    StopSupervisor,
    ConfirmReleased,
    BackupStarted,
    BackupComplete,
    EntriesSwitching,
    StartAndVerify,
    RevertToPrevious,
}

impl JournalStep {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Lock => "lock",
            Self::Verify => "verify",
            Self::SafetyCheck => "safety_check",
            Self::SnapshotTaken => "snapshot_taken",
            Self::StopSupervisor => "stop_supervisor",
            Self::ConfirmReleased => "confirm_released",
            Self::BackupStarted => "backup_started",
            Self::BackupComplete => "backup_complete",
            Self::EntriesSwitching => "entries_switching",
            Self::StartAndVerify => "start_and_verify",
            Self::RevertToPrevious => "revert_to_previous",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EntrySnapshot {
    Symlink { name: String, target: String },
    Copy { name: String, sha256: String, size: u64 },
    Absent { name: String },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AutostartPlan {
    pub kind: AutostartHoldKind,
    pub dropin_path: Option<String>,
    pub prior_enabled: Option<bool>,
    pub prior_restart: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct OriginalSnapshot {
    pub current: Option<CurrentRelease>,
    pub entries: Vec<EntrySnapshot>,
    pub config_sha256: Option<String>,
    pub supervisor: SupervisorSnapshot,
    pub daemon: Option<DaemonSnapshot>,
    pub daemon_start: &'static str,
    pub autostart: AutostartPlan,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Journal {
    pub op: String,
    pub from: Option<String>,
    pub to: String,
    pub step: JournalStep,
    pub started_at: String,
    pub original: Option<OriginalSnapshot>,
    pub snapshot_dir: Option<String>,
    pub snapshot_sha256: Option<Vec<(String, String)>>,
    pub backup_dir: Option<String>,
    pub backup_manifest_sha256: Option<String>,
    pub target_autostart: Option<AutostartPlan>,
}

pub fn write_journal(_ctx: &CliCtx, _root: &InstallRoot, _j: &Journal) -> Result<(), LayoutError> {
    unimplemented!("not implemented: P4-T12")
}

pub fn persist_original_snapshot(
    _ctx: &CliCtx,
    _root: &InstallRoot,
    _sup: &SupervisorSnapshot,
    _daemon: &Option<DaemonSnapshot>,
) -> Result<(OriginalSnapshot, PathBuf), LayoutError> {
    unimplemented!("not implemented: P4-T12")
}

#[derive(Clone, Debug, PartialEq)]
pub struct RecoveryReport {
    pub ok: bool,
    pub from_step: &'static str,
    pub backup_used: bool,
    pub journal_kept: bool,
    pub restored: PyDict,
    pub error_code: Option<&'static str>,
    pub reason: Option<String>,
    pub message: Option<String>,
    pub detail: PyDict,
}

pub fn recover_journal(_ctx: &mut CliCtx, _root: &InstallRoot) -> Option<RecoveryReport> {
    unimplemented!("not implemented: P4-T12")
}

#[derive(Clone, Debug, PartialEq)]
pub enum RevertStop {
    Absent,
    Stopped { token: String, graceful: bool },
    Deferred(HandoffError),
}

pub fn stop_for_revert(
    _ctx: &mut CliCtx,
    _mode: &SupervisionMode,
    _endpoint: &str,
    _with_sudo: bool,
    _phase: &'static str,
) -> Result<RevertStop, StopFailure> {
    unimplemented!("not implemented: P4-T12")
}

pub fn run_install(_ctx: &mut CliCtx, _bundle_dir: &Path, _ns: &Namespace) -> UpdateReport {
    unimplemented!("not implemented: P4-T12")
}

pub fn run_update(
    _ctx: &mut CliCtx,
    _bundle_dir: &Path,
    _target_mode: &SupervisionMode,
    _with_sudo: bool,
    _force_requested: bool,
) -> UpdateReport {
    unimplemented!("not implemented: P4-T12")
}

pub fn run_rollback(
    _ctx: &mut CliCtx,
    _with_sudo: bool,
    _force_requested: bool,
) -> UpdateReport {
    unimplemented!("not implemented: P4-T12")
}

pub(crate) fn recover_autostart_hold(_ctx: &mut CliCtx) -> Result<Option<PyDict>, PyDict> {
    unimplemented!("not implemented: P4-T12")
}

pub fn run_supervision_switch(
    _ctx: &mut CliCtx,
    _target: &SupervisionMode,
    _with_sudo: bool,
    _force_requested: bool,
) -> i32 {
    unimplemented!("not implemented: P4-T12")
}
