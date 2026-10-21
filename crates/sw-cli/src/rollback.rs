//! `setup --rollback`、update 與 `recover_journal`（§12.1；P4-T12 承接）。

use std::path::{Path, PathBuf};
use std::cell::RefCell;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::argparse::Namespace;
use crate::ctx::CliCtx;
use crate::endpoint_resolve;
use crate::graceful_stop::{self, DaemonIdentity, HandoffLink, StopRpc};
pub use crate::graceful_stop::StopFailure;
use crate::install_layout::{self, CurrentRelease, InstallRoot, LayoutError};
use sw_pal::proc_scan::ProcIdentity;
use sw_pal::fs_ops::FileLockGuard;
use sw_pal::service_ctl::{AutostartHoldKind, AutostartHoldRecord, HoldState, ServiceAction, SupervisionMode, TaskQuery};
use sw_proto::digest::sha256_hex;
use sw_proto::error_codes::ErrorCode;
use sw_proto::pyjson::{self, DumpOpts, PyDict, PyInt, PyValue, Sep};
use sw_proto::yaml11::{py_str_of, py_truthy_of};

const RELEASED_SESSION_STATE: &[u8] = b"released";

// 中斷注入點（Task 12 R1-F5）：只由整合測試經 `set_test_crash_point`／`set_test_hook` 設定，
// 產品路徑不設定時 `crash_if` 只做一次 thread-local 讀取、無任何副作用。不以 `debug_assertions`
// 切換編譯（arch §2.1 cfg 邊界只允許 `test`／`feature = "test-fakes"`；sw-cli 整合測試無法啟用自身 feature）。
thread_local! {
    static TEST_CRASH_POINT: RefCell<Option<String>> = RefCell::new(None);
    static TEST_HOOK: RefCell<Option<(String, Box<dyn FnOnce()>)>> = RefCell::new(None);
}

#[doc(hidden)]
pub fn set_test_crash_point(point: Option<String>) {
    TEST_CRASH_POINT.with(|slot| *slot.borrow_mut() = point);
}

#[doc(hidden)]
pub fn set_test_hook(point: Option<String>, callback: Option<Box<dyn FnOnce()>>) {
    TEST_HOOK.with(|slot| *slot.borrow_mut() = point.zip(callback));
}

fn crash_if(point: &str) {
    let callback = TEST_HOOK.with(|slot| {
        let mut hook = slot.borrow_mut();
        if hook.as_ref().is_some_and(|(at, _)| at == point) { hook.take().map(|(_, callback)| callback) } else { None }
    });
    if let Some(callback) = callback { callback(); }
    let should_crash = TEST_CRASH_POINT.with(|slot| slot.borrow().as_deref() == Some(point));
    if should_crash {
        panic!("injected process interruption at {point}");
    }
}

fn as_str(value: &PyValue) -> Option<&str> {
    match value {
        PyValue::Str(value) => Some(value),
        _ => None,
    }
}

fn as_u32(value: &PyValue) -> Option<u32> {
    match value {
        PyValue::Int(PyInt::Small(number)) => u32::try_from(*number).ok(),
        PyValue::Int(PyInt::Big(number)) => number.parse::<u32>().ok(),
        _ => None,
    }
}

fn rpc(ctx: &CliCtx<'_>, endpoint: &str, method: &str, timeout: f64) -> Option<PyDict> {
    crate::client::call(
        ctx,
        endpoint,
        method,
        &PyValue::Dict(PyDict::new()),
        timeout,
        &PyInt::Small(0),
    )
    .ok()
    .map(|outcome| outcome.resp)
}

fn error_code(code: Option<&str>) -> &'static str {
    code.and_then(ErrorCode::parse)
        .unwrap_or(ErrorCode::InternalError)
        .as_str()
}

fn invalid_probe(method: &str) -> (&'static str, PyDict) {
    let mut detail = PyDict::new();
    detail.insert("probe", PyValue::Str(method.to_string()));
    detail.insert("health", PyValue::Str("unknown".into()));
    (ErrorCode::InvalidResponse.as_str(), detail)
}

fn checked_rpc(
    ctx: &CliCtx<'_>,
    endpoint: &str,
    method: &str,
    timeout: f64,
) -> Result<PyDict, (&'static str, PyDict)> {
    let params = PyValue::Dict(PyDict::new());
    let (outcome, failure) = crate::client::call_with_failure(
        ctx,
        endpoint,
        method,
        &params,
        timeout,
        &PyInt::Small(0),
    )
    .map_err(|_| {
        let mut detail = PyDict::new();
        detail.insert("probe", PyValue::Str(method.to_string()));
        detail.insert("health", PyValue::Str("unknown".into()));
        (ErrorCode::InternalError.as_str(), detail)
    })?;
    let response = outcome.resp;
    if let Some(failure) = failure {
        let mut detail = response.clone();
        detail.insert("probe", PyValue::Str(method.to_string()));
        detail.insert("health", PyValue::Str("unknown".into()));
        let code = if failure.pal_kind == Some(sw_pal::PalErrorKind::TimedOut) {
            ErrorCode::Timeout.as_str()
        } else {
            ErrorCode::SocketError.as_str()
        };
        return Err((code, detail));
    }
    if !response.get_truthy("ok") {
        let mut detail = response.clone();
        detail.insert("probe", PyValue::Str(method.to_string()));
        detail.insert("health", PyValue::Str("unknown".into()));
        let code = error_code(response.get("error_code").and_then(as_str));
        return Err((code, detail));
    }
    Ok(response)
}

fn busy_session(value: &PyValue) -> Result<Option<PyValue>, ()> {
    let PyValue::Dict(session) = value else { return Err(()); };
    // 明確可辨識的忙碌訊號優先拒絕；其餘欄位缺漏或型別錯誤仍 fail closed。
    if session.get("foreground_busy") == Some(&PyValue::Bool(true))
        || session.get("recovering") == Some(&PyValue::Bool(true))
        || session.get("state").and_then(as_str).is_some_and(|state| {
            state.eq_ignore_ascii_case("recovering") || state.eq_ignore_ascii_case("flashing")
        })
        || session.get("interactive_session_id").is_some_and(|value| !matches!(value, PyValue::Null))
        || session.get("outstanding_commands").and_then(|value| match value {
            PyValue::Int(PyInt::Small(number)) => u64::try_from(*number).ok(),
            PyValue::Int(PyInt::Big(number)) => number.parse::<u64>().ok(),
            _ => None,
        }).is_some_and(|value| value > 0)
    {
        return Ok(Some(value.clone()));
    }
    let Some(PyValue::Bool(foreground_busy)) = session.get("foreground_busy") else { return Err(()); };
    let Some(PyValue::Bool(recovering)) = session.get("recovering") else { return Err(()); };
    let Some(state) = session.get("state").and_then(as_str) else { return Err(()); };
    let Some(outstanding) = session.get("outstanding_commands").and_then(|value| match value {
        PyValue::Int(PyInt::Small(number)) => u64::try_from(*number).ok(),
        PyValue::Int(PyInt::Big(number)) => number.parse::<u64>().ok(),
        _ => None,
    }) else { return Err(()); };
    let Some(interactive) = session.get("interactive_session_id") else { return Err(()); };
    if !matches!(interactive, PyValue::Null | PyValue::Str(_)) { return Err(()); }
    let busy = *foreground_busy
        || *recovering
        || state.eq_ignore_ascii_case("recovering")
        || state.eq_ignore_ascii_case("flashing")
        || outstanding != 0
        || !matches!(interactive, PyValue::Null);
    Ok(busy.then(|| PyValue::Dict(session.clone())))
}

fn matching_daemons(
    _ctx: &CliCtx<'_>,
    endpoint: &str,
    daemons: &[sw_pal::proc_scan::DaemonProc],
) -> Vec<sw_pal::proc_scan::DaemonProc> {
    daemons.iter().filter(|daemon| daemon.socket.as_deref().map_or(true, |socket| socket == endpoint)).cloned().collect()
}

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
    pub detail: PyDict,
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
    ctx: &CliCtx,
    root: &InstallRoot,
    dir: &Path,
    sup: SupervisorSnapshot,
) -> Result<BackupSet, LayoutError> {
    let dir = dir.to_path_buf();
    let partial = PathBuf::from(format!("{}.partial", dir.display()));
    if ctx.p.fs.exists(&partial) {
        ctx.p.fs.remove_tree(&partial).map_err(|error| layout_io(&partial, error.py_message))?;
    }
    ctx.p.fs.mkdirs(&root.backups).map_err(|error| layout_io(&root.backups, error.py_message))?;
    ctx.p.fs.mkdirs(&partial).map_err(|error| layout_io(&partial, error.py_message))?;

    let mut files = Vec::<(String, PathBuf)>::new();
    copy_optional(ctx, &crate::endpoint_resolve::config_write_path(ctx), &partial.join("config.yaml"), &mut files)?;
    let state_dir = PathBuf::from(&ctx.paths.state_dir);
    for name in ["state.json", "benches.state.json"] {
        copy_optional(ctx, &state_dir.join(name), &partial.join(name), &mut files)?;
    }
    let config_dir = PathBuf::from(&ctx.paths.config_dir);
    let profiles = config_dir.join("profiles");
    if ctx.p.fs.is_dir(&profiles) {
        let target = partial.join("profiles");
        ctx.p.fs.copy_tree(&profiles, &target).map_err(|error| layout_io(&target, error.py_message))?;
        sync_tree(ctx, &target)?;
        collect_files(ctx, &partial, &target, &mut files)?;
    }
    if ctx.p.fs.is_dir(&root.supervisor) {
        let target = partial.join("supervisor");
        ctx.p.fs.copy_tree(&root.supervisor, &target).map_err(|error| layout_io(&target, error.py_message))?;
        sync_tree(ctx, &target)?;
        collect_files(ctx, &partial, &target, &mut files)?;
    }
    let entries = capture_entries(ctx, root)?;
    let entries_path = partial.join("entries.json");
    write_json(ctx, &entries_path, &entries_value(&entries))?;
    files.push(("entries.json".into(), entries_path.clone()));
    let registration = supervisor_value(&sup);
    let registration_path = partial.join("registration.json");
    write_json(ctx, &registration_path, &registration)?;
    files.push(("registration.json".into(), registration_path.clone()));
    if sup.mode == SupervisionMode::WindowsTask {
        let task_path = partial.join("registered.task.xml");
        if sup.enabled.is_some() || sup.running {
            export_task_registration(ctx, &task_path)?;
            files.push(("registered.task.xml".into(), task_path));
        }
    } else if matches!(sup.mode, SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem) {
        let registered = sup.registration.as_deref();
        let unit_path = partial.join("registered.unit");
        if let Some(source) = registered.filter(|path| ctx.p.fs.exists(path)) {
            copy_file(ctx, source, &unit_path)?;
            files.push(("registered.unit".into(), unit_path));
        } else if sup.enabled.is_some() || sup.running {
            return Err(layout_io(registered.unwrap_or(&unit_path), "監管狀態存在但找不到已註冊的 systemd unit"));
        }
    }
    crash_if("backup_started:after_config_copy");

    let mut manifest = PyDict::new();
    let file_values = files.iter().map(|(relative, path)| {
        let mut item = PyDict::new();
        item.insert("path", PyValue::Str(dir.join(relative).to_string_lossy().into_owned()));
        item.insert("relative", PyValue::Str(relative.clone()));
        item.insert("sha256", PyValue::Str(hash_path(ctx, path).unwrap_or_default()));
        PyValue::Dict(item)
    }).collect();
    manifest.insert("files", PyValue::List(file_values));
    manifest.insert("schema_version", PyValue::Int(PyInt::Small(1)));
    let manifest_path = partial.join("manifest.json");
    write_json(ctx, &manifest_path, &PyValue::Dict(manifest))?;
    ctx.p.fs.sync_dir(&partial).map_err(|error| layout_io(&partial, error.py_message))?;
    ctx.p.fs.replace(&partial, &dir).map_err(|error| layout_io(&dir, error.py_message))?;
    ctx.p.fs.sync_dir(&root.backups).map_err(|error| layout_io(&root.backups, error.py_message))?;
    Ok(BackupSet { dir, supervisor: sup })
}

pub fn restore_backup(
    ctx: &mut CliCtx,
    set: &BackupSet,
    include_state: bool,
) -> Result<Vec<PathBuf>, LayoutError> {
    let mut restored = Vec::new();
    let config = set.dir.join("config.yaml");
    if ctx.p.fs.exists(&config) {
        copy_replace(ctx, &config, &crate::endpoint_resolve::config_write_path(ctx))?;
        restored.push(crate::endpoint_resolve::config_write_path(ctx));
    }
    let profiles = set.dir.join("profiles");
    if ctx.p.fs.is_dir(&profiles) {
        let target = PathBuf::from(&ctx.paths.config_dir).join("profiles");
        if ctx.p.fs.exists(&target) { ctx.p.fs.remove_tree(&target).map_err(|error| layout_io(&target, error.py_message))?; }
        ctx.p.fs.copy_tree(&profiles, &target).map_err(|error| layout_io(&target, error.py_message))?;
        sync_tree(ctx, &target)?;
        restored.push(target);
    }
    restore_registration(ctx, &set.dir, &set.supervisor)?;
    for name in ["state.json", "benches.state.json"] {
        if include_state {
            let source = set.dir.join(name);
            if ctx.p.fs.exists(&source) {
                let target = PathBuf::from(&ctx.paths.state_dir).join(name);
                copy_replace(ctx, &source, &target)?;
                restored.push(target);
            }
        }
    }
    Ok(restored)
}

#[derive(Clone, Debug, PartialEq)]
pub struct DaemonSnapshot {
    pub id: DaemonIdentity,
    pub attached_paths: Vec<PathBuf>,
    pub sessions: Vec<PyDict>,
}

pub fn safety_check(
    ctx: &CliCtx<'_>,
    endpoint: &str,
) -> Result<Option<DaemonSnapshot>, (&'static str, PyDict)> {
    let ping = rpc(ctx, endpoint, "health.ping", 0.5);
    if !ping.as_ref().is_some_and(|response| response.get_truthy("ok")) {
        let daemons = ctx.p.procs.serialwrapd_instances().map_err(|_| {
            let mut detail = PyDict::new();
            detail.insert("holder_unknown", PyValue::Bool(true));
            (ErrorCode::DeviceStillHeld.as_str(), detail)
        })?;
        let matches = matching_daemons(ctx, endpoint, &daemons);
        if !matches.is_empty() {
            let mut detail = PyDict::new();
            detail.insert("pids", PyValue::List(matches.iter().map(|proc| PyValue::Int(PyInt::Small(i128::from(proc.pid)))).collect()));
            return Err((ErrorCode::DeviceStillHeld.as_str(), detail));
        }
        return Ok(None);
    }

    let mcu = checked_rpc(ctx, endpoint, "mcu.status", 0.5)?;
    match mcu.get("flashing") {
        Some(PyValue::Bool(true)) => {
            let mut detail = PyDict::new();
            detail.insert("message", PyValue::Str("flash 進行中，拒絕更新／回滾（--force 不適用，請待 flash 完成後重試）".into()));
            return Err((ErrorCode::FlashingBusy.as_str(), detail));
        }
        Some(PyValue::Bool(false)) => {}
        _ => return Err(invalid_probe("mcu.status")),
    }
    let session_response = checked_rpc(ctx, endpoint, "session.list", 1.0)?;
    let sessions = match session_response.get("sessions") {
        Some(PyValue::List(values)) => values,
        _ => return Err(invalid_probe("session.list")),
    };
    for session in sessions {
        match busy_session(session) {
            Ok(Some(busy)) => {
                let mut detail = PyDict::new();
                detail.insert("busy", PyValue::List(vec![busy]));
                return Err((ErrorCode::SessionBusy.as_str(), detail));
            }
            Ok(None) => {}
            Err(()) => return Err(invalid_probe("session.list")),
        }
    }

    let id = crate::commands::daemon_cmd::capture_daemon_identity_public(ctx, endpoint);
    if !id.known {
        let mut detail = PyDict::new();
        detail.insert("pids", PyValue::List(Vec::new()));
        detail.insert("holder_unknown", PyValue::Bool(true));
        return Err((ErrorCode::DeviceStillHeld.as_str(), detail));
    }
    let attached_paths = sessions.iter().filter_map(|session| match session {
        PyValue::Dict(session) => session.get("attached_real_path").and_then(as_str).map(PathBuf::from),
        _ => None,
    }).collect();
    Ok(Some(DaemonSnapshot {
        id,
        attached_paths,
        sessions: sessions.iter().filter_map(|session| match session { PyValue::Dict(dict) => Some(dict.clone()), _ => None }).collect(),
    }))
}

#[derive(Clone, Debug, PartialEq)]
pub enum HandoffError {
    Busy {
        code: String,
        ops: Vec<String>,
        busy: Vec<PyValue>,
    },
    Unconfirmed {
        code: String,
        probe: &'static str,
        outcome: &'static str,
        pids: Vec<u32>,
    },
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

pub fn write_journal(ctx: &CliCtx, root: &InstallRoot, j: &Journal) -> Result<(), LayoutError> {
    let path = root.root.join("install-journal.json");
    let bytes = json_bytes(&journal_value(j));
    ctx.p.fs.mkdirs(&root.root).map_err(|error| layout_io(&root.root, error.py_message))?;
    ctx.p.fs.atomic_write(&path, &bytes, None).map_err(|error| layout_io(&path, error.py_message))?;
    ctx.p.fs.sync_dir(&root.root).map_err(|error| layout_io(&root.root, error.py_message))
}

pub fn persist_original_snapshot(
    ctx: &CliCtx,
    root: &InstallRoot,
    sup: &SupervisorSnapshot,
    daemon: &Option<DaemonSnapshot>,
) -> Result<(OriginalSnapshot, PathBuf), LayoutError> {
    let dir = root.root.join("install-journal.snapshot");
    if ctx.p.fs.exists(&dir) { ctx.p.fs.remove_tree(&dir).map_err(|error| layout_io(&dir, error.py_message))?; }
    ctx.p.fs.mkdirs(&dir).map_err(|error| layout_io(&dir, error.py_message))?;
    let entries = capture_entries(ctx, root)?;
    let entries_path = dir.join("entries.json");
    write_json(ctx, &entries_path, &entries_value(&entries))?;
    let entry_dir = dir.join("entries");
    for entry in &entries {
        if let EntrySnapshot::Copy { name, .. } = entry {
            copy_file(ctx, &root.bin.join(name), &entry_dir.join(name))?;
        }
    }
    let config_path = crate::endpoint_resolve::config_write_path(ctx);
    let config_sha256 = if ctx.p.fs.exists(&config_path) {
        let bytes = ctx.p.fs.read(&config_path).map_err(|error| layout_io(&config_path, error.py_message))?;
        let path = dir.join("config.yaml");
        ctx.p.fs.atomic_write(&path, &bytes, None).map_err(|error| layout_io(&path, error.py_message))?;
        Some(sha256_hex(&bytes))
    } else { None };
    let current_bytes = if ctx.p.fs.exists(&root.current_file) {
        let bytes = ctx.p.fs.read(&root.current_file).map_err(|error| layout_io(&root.current_file, error.py_message))?;
        let path = dir.join("current-release.json");
        ctx.p.fs.atomic_write(&path, &bytes, None).map_err(|error| layout_io(&path, error.py_message))?;
        Some(bytes)
    } else { None };
    if ctx.p.fs.is_dir(&root.supervisor) {
        let target = dir.join("supervisor");
        ctx.p.fs.copy_tree(&root.supervisor, &target).map_err(|error| layout_io(&target, error.py_message))?;
        sync_tree(ctx, &target)?;
    }
    match sup.mode {
        SupervisionMode::WindowsTask if sup.enabled.is_some() || sup.running => {
            export_task_registration(ctx, &dir.join("registered.task.xml"))?;
        }
        SupervisionMode::WindowsTask => {}
        SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem => {
            if let Some(source) = sup.registration.as_ref().filter(|source| ctx.p.fs.exists(source)) {
                copy_file(ctx, source, &dir.join("registered.unit"))?;
            } else if sup.enabled.is_some() || sup.running {
                return Err(layout_io(sup.registration.as_deref().unwrap_or(&dir), "監管狀態存在但找不到已註冊的 systemd unit"));
            }
        }
        _ => {}
    }
    for name in ["state.json", "benches.state.json"] {
        let source = PathBuf::from(&ctx.paths.state_dir).join(name);
        let target = dir.join(name);
        if ctx.p.fs.exists(&source) { copy_file(ctx, &source, &target)?; }
    }
    let profiles = PathBuf::from(&ctx.paths.config_dir).join("profiles");
    if ctx.p.fs.is_dir(&profiles) {
        let target = dir.join("profiles");
        ctx.p.fs.copy_tree(&profiles, &target).map_err(|error| layout_io(&target, error.py_message))?;
        sync_tree(ctx, &target)?;
    }
    let daemon_start = if daemon.is_none() { "none" } else if sup.mode == SupervisionMode::OnDemand { "on_demand_internal" } else { "supervised" };
    let planned_hold = planned_switch_hold(ctx, &sup.mode, sup.enabled)
        .map_err(|message| layout_io(&dir, format!("無法記錄監管 hold 計畫：{message}")))?;
    let original = OriginalSnapshot {
        current: install_layout::read_current(root)?,
        entries,
        config_sha256,
        supervisor: sup.clone(),
        daemon: daemon.clone(),
        daemon_start,
        autostart: autostart_plan(&planned_hold, sup.enabled),
    };
    let original_path = dir.join("original.json");
    write_json(ctx, &original_path, &original_value(&original))?;
    let _ = current_bytes;
    ctx.p.fs.sync_dir(&dir).map_err(|error| layout_io(&dir, error.py_message))?;
    Ok((original, dir))
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

pub fn recover_journal(ctx: &mut CliCtx, root: &InstallRoot) -> Option<RecoveryReport> {
    let journal_path = root.root.join("install-journal.json");
    if !ctx.p.fs.exists(&journal_path) {
        return None;
    }
    let bytes = match ctx.p.fs.read(&journal_path) {
        Ok(bytes) => bytes,
        Err(error) => return Some(recovery_error("unknown", ErrorCode::InternalError.as_str(), "journal_corrupt", error.py_message)),
    };
    let parsed = match pyjson::loads(&String::from_utf8_lossy(&bytes)) {
        Ok(PyValue::Dict(value)) => value,
        Ok(_) => return Some(recovery_error("unknown", ErrorCode::InternalError.as_str(), "journal_corrupt", "install-journal.json 頂層必須為物件")),
        Err(error) => return Some(recovery_error("unknown", ErrorCode::InternalError.as_str(), "journal_corrupt", error.to_string())),
    };
    let from_step = parsed.get("step").and_then(as_str).unwrap_or("unknown").to_string();
    let journal = match parse_journal(&parsed) {
        Ok(journal) => journal,
        Err(message) => return Some(recovery_error(&from_step, ErrorCode::InternalError.as_str(), "journal_corrupt", message)),
    };
    if journal.step == JournalStep::Lock || journal.step == JournalStep::Verify || journal.step == JournalStep::SafetyCheck || journal.step == JournalStep::SnapshotTaken {
        return finish_recovery(ctx, root, &journal_path, &journal, false, PyDict::new());
    }

    let original = match journal.original.as_ref() {
        Some(original) => original,
        None => return Some(recovery_error(&from_step, ErrorCode::InternalError.as_str(), "journal_corrupt", "snapshot_taken 之後的 journal 缺少 original")),
    };
    if let Err(message) = configured_mode_checked(ctx) {
        return Some(recovery_error(&from_step, ErrorCode::InternalError.as_str(), "config_read_failed", message));
    }
    if let Err((reason, message)) = verify_snapshot(ctx, &journal) {
        return Some(recovery_error(&from_step, ErrorCode::InternalError.as_str(), reason, message));
    }

    let pre_switch = matches!(journal.step,
        JournalStep::SnapshotTaken | JournalStep::StopSupervisor | JournalStep::ConfirmReleased |
        JournalStep::BackupStarted | JournalStep::BackupComplete);
    let install_state_unchanged = if pre_switch {
        match original_install_state_matches(ctx, root, original) {
            Ok(matches) => matches,
            Err(message) => return Some(recovery_error(&from_step, ErrorCode::InternalError.as_str(), "snapshot_incomplete", message)),
        }
    } else {
        false
    };
    if pre_switch && !install_state_unchanged && journal.backup_manifest_sha256.is_none() {
        return Some(recovery_error(&from_step, ErrorCode::InternalError.as_str(), "backup_incomplete", "尚未切換的 journal 卻發現安裝入口、current 或 config 已變更；沒有完整備份可供還原"));
    }

    let use_backup = journal.step == JournalStep::EntriesSwitching
        || journal.step == JournalStep::StartAndVerify
        || journal.step == JournalStep::RevertToPrevious
        || (pre_switch && !install_state_unchanged);
    let backup = if use_backup {
        match verified_backup(ctx, &journal) {
            Ok(path) => Some(path),
            Err(message) => return Some(recovery_error(&from_step, ErrorCode::InternalError.as_str(), "backup_incomplete", message)),
        }
    } else {
        None
    };

    let endpoint = original.supervisor.endpoint.as_str();
    let daemon_present = match endpoint_presence(ctx, endpoint) {
        EndpointPresence::Present(_) => true,
        EndpointPresence::Absent => false,
        EndpointPresence::Unknown => return Some(recovery_error(
            &from_step,
            ErrorCode::DeviceStillHeld.as_str(),
            "daemon_presence_unknown",
            "無法確認 daemon 行程是否存在；保留 journal，避免在未知持有狀態下切換或啟動版本",
        )),
    };
    if pre_switch && install_state_unchanged && daemon_present {
        if journal.step != JournalStep::SnapshotTaken {
            if original.autostart.kind != AutostartHoldKind::NoSupervisor {
                let hold = hold_record_from_plan(original);
                if let Err(error) = ctx.p.service.release_autostart(&hold, original.supervisor.enabled, false) {
                    return Some(recovery_error(&from_step, ErrorCode::InternalError.as_str(), "autostart_release_failed", error.hint.unwrap_or_else(|| "原監管 hold 撤銷失敗".into())));
                }
            }
            ctx.autostart_lock.take();
        }
        let restored = match verify_restored(ctx, root, original, original.daemon.is_some(), false) {
            Ok(value) => value,
            Err(message) => return Some(recovery_error(&from_step, ErrorCode::InternalError.as_str(), "restore_verify_failed", message)),
        };
        return finish_recovery(ctx, root, &journal_path, &journal, false, restored);
    }
    let recovery_mode = match configured_mode_checked(ctx) {
        Ok(mode) => mode,
        Err(message) => return Some(recovery_error(&from_step, ErrorCode::InternalError.as_str(), "config_read_failed", message)),
    };
    let reusable_hold = (recovery_mode == original.supervisor.mode
        && original.autostart.kind != AutostartHoldKind::NoSupervisor
        && ctx.p.service.autostart_hold_state(&recovery_mode) == HoldState::Held)
        .then(|| hold_record_from_plan(original));
    let (recovery_handoff, recovery_target_hold) = match stop_for_revert_with_hold(ctx, &recovery_mode, endpoint, false, "install_recover", reusable_hold) {
        Ok(RevertStop::Stopped { token, hold, .. }) => {
            (Some(token), hold)
        }
        Ok(RevertStop::Absent { hold }) => (None, hold),
        Ok(RevertStop::Deferred(error)) => {
            return Some(recovery_deferred(&from_step, error, ctx, endpoint, "install_recover"));
        }
        Err(error) => return Some(recovery_stop_error(&from_step, &error, ctx, endpoint)),
    };

    let holders_status = match confirm_released(ctx, endpoint, original.daemon.as_ref()) {
        Ok(status) => status,
        Err(failure) => return Some(recovery_error_from_op(&from_step, "tty_holder_remains", failure)),
    };

    if journal.step == JournalStep::BackupStarted {
        preserve_partial_backup(ctx, &journal);
    }
    if let Err(message) = restore_original(ctx, root, &journal, backup.as_deref()) {
        return Some(recovery_error(&from_step, ErrorCode::InternalError.as_str(), "restore_failed", message));
    }
    let restored = match restore_supervisor_and_daemon(ctx, root, original, backup.as_deref(), recovery_handoff.as_deref(), recovery_target_hold.as_ref(), false) {
        Ok(value) => value,
        Err(message) => {
            if let Some(message) = message.strip_prefix("AUTOSTART_RELEASE_FAILED:") {
                return Some(recovery_error(&from_step, ErrorCode::InternalError.as_str(), "autostart_release_failed", message));
            }
            let code = if message.starts_with("DAEMON_EXITED:") { ErrorCode::DaemonExited.as_str() } else { ErrorCode::InternalError.as_str() };
            return Some(recovery_error(&from_step, code, "restore_verify_failed", message));
        }
    };
    let mut completed = finish_recovery(ctx, root, &journal_path, &journal, backup.is_some(), restored)?;
    if let Some(status) = holders_status {
        completed.detail.insert("holders_status", str_value(status));
    }
    Some(completed)
}

#[derive(Clone, Debug, PartialEq)]
pub enum RevertStop {
    Absent { hold: Option<AutostartHoldRecord> },
    Stopped { token: String, graceful: bool, hold: Option<AutostartHoldRecord> },
    Deferred(HandoffError),
}

pub fn stop_for_revert(
    ctx: &mut CliCtx,
    mode: &SupervisionMode,
    endpoint: &str,
    with_sudo: bool,
    phase: &'static str,
) -> Result<RevertStop, StopFailure> {
    stop_for_revert_with_hold(ctx, mode, endpoint, with_sudo, phase, None)
}

fn stop_for_revert_with_hold(
    ctx: &mut CliCtx<'_>,
    mode: &SupervisionMode,
    endpoint: &str,
    with_sudo: bool,
    phase: &'static str,
    existing_hold: Option<AutostartHoldRecord>,
) -> Result<RevertStop, StopFailure> {
    let enabled = mode_enabled(&ctx.p.service.query(mode));
    let hold = if *mode == SupervisionMode::OnDemand {
        acquire_switch_autostart_lock(ctx).map_err(|code| {
            let mut failure = service_result_failure(mode, "hold_autostart", "無法取得 autostart.lock；請稍後重試".into());
            failure.error_code = Some(code.as_str().into());
            StopFailure::Hold(failure)
        })?;
        None
    } else {
        if let Some(hold) = existing_hold {
            if hold.mode != *mode || ctx.p.service.autostart_hold_state(mode) != HoldState::Held {
                return Err(StopFailure::Hold(service_result_failure(mode, "hold_autostart", "既有自動啟動 hold 與目標監管模式不符".into())));
            }
            Some(hold)
        } else {
            let planned = planned_switch_hold(ctx, mode, enabled)
                .map_err(|message| StopFailure::Hold(service_result_failure(mode, "hold_autostart", message)))?;
            let actual = ctx.p.service.hold_autostart(mode, with_sudo).map_err(StopFailure::Hold)?;
            let hold = merge_hold_plan(&planned, actual);
            if ctx.p.service.autostart_hold_state(mode) != HoldState::Held {
                let _ = ctx.p.service.release_autostart(&hold, enabled, with_sudo);
                return Err(StopFailure::Hold(service_result_failure(mode, "hold_autostart", "無法確認自動啟動 hold 已生效".into())));
            }
            Some(hold)
        }
    };
    match endpoint_presence(ctx, endpoint) {
        EndpointPresence::Absent => return Ok(RevertStop::Absent { hold }),
        EndpointPresence::Present(_) => {}
        EndpointPresence::Unknown => {
            if let Some(hold) = hold.as_ref() {
                if let Err(error) = ctx.p.service.release_autostart(hold, enabled, with_sudo) {
                    return Err(StopFailure::Hold(error));
                }
            }
            ctx.autostart_lock.take();
            return Err(StopFailure::Handoff(HandoffError::Unconfirmed {
                code: ErrorCode::DeviceStillHeld.as_str().into(), probe: "proc_scan", outcome: "unknown",
                pids: Vec::new(),
            }));
        }
    }
    match graceful_stop::graceful_stop_with_existing_hold(
        ctx,
        mode,
        endpoint,
        with_sudo,
        true,
        phase,
        StopRpc::Handoff { reason: "revert" },
        hold,
    ) {
        Ok(report) => Ok(RevertStop::Stopped {
            token: report.handoff_token.unwrap_or_default(),
            graceful: report.graceful,
            hold: report.hold,
        }),
        Err(error) => {
            if !matches!(error.cause(), StopFailure::Timeout { .. }) {
                if let Some(hold) = error.hold() {
                    let _ = ctx.p.service.release_autostart(hold, enabled, with_sudo);
                }
                ctx.autostart_lock.take();
                if matches!(error.cause(), StopFailure::Handoff(_) | StopFailure::Relaunched { .. }) {
                    return Ok(RevertStop::Deferred(handoff_from_stop(&error)));
                }
            }
            Err(error)
        }
    }
}

pub fn run_install(ctx: &mut CliCtx, bundle_dir: &Path, ns: &Namespace) -> UpdateReport {
    let root = install_layout::resolve_install_root(ctx);
    match install_layout::install_lock(ctx, &root) {
        Ok(lock) => run_install_locked(ctx, bundle_dir, ns, lock),
        Err(detail) => lock_failure_report("install", detail),
    }
}

pub(crate) fn run_install_locked(ctx: &mut CliCtx, bundle_dir: &Path, ns: &Namespace, lock: FileLockGuard) -> UpdateReport {
    execute_install(ctx, bundle_dir, ns, lock)
}

pub fn run_update(
    ctx: &mut CliCtx,
    bundle_dir: &Path,
    target_mode: &SupervisionMode,
    with_sudo: bool,
    force_requested: bool,
) -> UpdateReport {
    let root = install_layout::resolve_install_root(ctx);
    match install_layout::install_lock(ctx, &root) {
        Ok(lock) => run_update_locked(ctx, bundle_dir, target_mode, with_sudo, force_requested, lock),
        Err(detail) => lock_failure_report("update", detail),
    }
}

pub(crate) fn run_update_locked(
    ctx: &mut CliCtx<'_>, bundle_dir: &Path, target_mode: &SupervisionMode,
    with_sudo: bool, force_requested: bool, lock: FileLockGuard,
) -> UpdateReport {
    execute_change(ctx, bundle_dir, target_mode, with_sudo, force_requested, "update", lock)
}

pub fn run_rollback(
    ctx: &mut CliCtx,
    with_sudo: bool,
    force_requested: bool,
) -> UpdateReport {
    let root = install_layout::resolve_install_root(ctx);
    match install_layout::install_lock(ctx, &root) {
        Ok(lock) => run_rollback_locked(ctx, with_sudo, force_requested, lock),
        Err(detail) => lock_failure_report("rollback", detail),
    }
}

pub(crate) fn run_rollback_locked(ctx: &mut CliCtx<'_>, with_sudo: bool, force_requested: bool, lock: FileLockGuard) -> UpdateReport {
    execute_rollback(ctx, with_sudo, force_requested, lock)
}

fn lock_failure_report(op: &'static str, detail: PyDict) -> UpdateReport {
    let mut report = report_from_dict(op, detail);
    let detail = report.detail.clone();
    add_step(&mut report, StepName::Lock, false, detail);
    report
}

#[derive(Clone, Debug)]
struct OpFailure {
    code: &'static str,
    phase: &'static str,
    reason: Option<&'static str>,
    message: Option<String>,
    detail: PyDict,
}

#[derive(Clone, Debug)]
struct HeldAutostart {
    record: AutostartHoldRecord,
    with_sudo: bool,
}

fn new_report(op: &'static str, from: Option<String>, to: String) -> UpdateReport {
    UpdateReport {
        ok: false,
        op,
        from_version: from,
        to_version: to,
        steps: Vec::new(),
        backup_dir: None,
        reverted: false,
        error_code: None,
        phase: None,
        reason: None,
        message: None,
        revert_pending: false,
        revert_error: None,
        detail: PyDict::new(),
    }
}

fn add_step(report: &mut UpdateReport, step: StepName, ok: bool, detail: PyDict) {
    report.steps.push(StepRecord { step, ok, detail });
}

fn fail_report(report: &mut UpdateReport, step: StepName, failure: OpFailure) {
    add_step(report, step, false, failure.detail.clone());
    report.error_code = Some(failure.code);
    report.phase = Some(failure.phase);
    report.reason = failure.reason.map(str::to_owned);
    report.message = failure.message;
    report.detail = failure.detail;
}

fn failure(code: ErrorCode, phase: &'static str, reason: Option<&'static str>, message: Option<String>) -> OpFailure {
    OpFailure { code: code.as_str(), phase, reason, message, detail: PyDict::new() }
}

fn report_value(report: &UpdateReport) -> PyValue {
    let mut value = PyDict::new();
    value.insert("backup_dir", report.backup_dir.as_ref().map(|path| str_value(path.to_string_lossy().into_owned())).unwrap_or(PyValue::Null));
    value.insert("detail", PyValue::Dict(report.detail.clone()));
    value.insert("from_version", opt_str(report.from_version.as_deref()));
    value.insert("ok", PyValue::Bool(report.ok));
    value.insert("op", str_value(report.op));
    value.insert("phase", opt_str(report.phase));
    value.insert("reason", opt_str(report.reason.as_deref()));
    value.insert("revert_error", report.revert_error.as_ref().map(|detail| PyValue::Dict(detail.clone())).unwrap_or(PyValue::Null));
    value.insert("revert_pending", PyValue::Bool(report.revert_pending));
    value.insert("reverted", PyValue::Bool(report.reverted));
    value.insert("steps", PyValue::List(report.steps.iter().map(|record| {
        PyValue::Dict(PyDict::from_pairs(vec![
            ("detail".into(), PyValue::Dict(record.detail.clone())),
            ("ok".into(), PyValue::Bool(record.ok)),
            ("step".into(), str_value(record.step.as_str())),
        ]))
    }).collect()));
    value.insert("to_version", str_value(report.to_version.clone()));
    PyValue::Dict(value)
}

pub(crate) fn recovery_value(report: &RecoveryReport) -> PyValue {
    PyValue::Dict(PyDict::from_pairs(vec![
        ("backup_used".into(), PyValue::Bool(report.backup_used)),
        ("detail".into(), PyValue::Dict(report.detail.clone())),
        ("error_code".into(), opt_str(report.error_code)),
        ("from_step".into(), str_value(report.from_step)),
        ("journal_kept".into(), PyValue::Bool(report.journal_kept)),
        ("message".into(), opt_str(report.message.as_deref())),
        ("ok".into(), PyValue::Bool(report.ok)),
        ("reason".into(), opt_str(report.reason.as_deref())),
        ("restored".into(), PyValue::Dict(report.restored.clone())),
    ]))
}

pub(crate) fn emit_update_report(
    ctx: &mut CliCtx<'_>,
    report: &UpdateReport,
    recovered_journal: Option<&RecoveryReport>,
    recovered_hold: Option<&PyDict>,
) -> i32 {
    let mut response = PyDict::new();
    response.insert("ok", PyValue::Bool(report.ok));
    if let Some(code) = report.error_code { response.insert("error_code", str_value(code)); }
    if let Some(phase) = report.phase { response.insert("phase", str_value(phase)); }
    if let Some(reason) = &report.reason { response.insert("reason", str_value(reason.clone())); }
    if let Some(message) = &report.message { response.insert("message", str_value(message.clone())); }
    if let Some(backup_dir) = &report.backup_dir { response.insert("backup_dir", str_value(backup_dir.to_string_lossy().into_owned())); }
    for (key, value) in &report.detail.0 { response.insert(key, value.clone()); }
    response.insert(report.op, report_value(report));
    if let Some(recovered) = recovered_journal { response.insert("recovered_journal", recovery_value(recovered)); }
    if let Some(recovered) = recovered_hold { response.insert("recovered_autostart_hold", PyValue::Dict(recovered.clone())); }
    let context = "setup";
    let _ = crate::output::print_json(&mut ctx.io, &response);
    if !report.ok {
        let _ = crate::output::mirror_err(&mut ctx.io, &response, Some(context));
        2
    } else {
        0
    }
}

fn execute_install(ctx: &mut CliCtx<'_>, bundle_dir: &Path, ns: &Namespace, lock: FileLockGuard) -> UpdateReport {
    let target = match crate::setup_cmd::resolve_target_mode(ns, ctx) {
        Ok(mode) => mode,
        Err(error) => return report_from_dict("install", error),
    };
    execute_change(ctx, bundle_dir, &target, matches!(ns.get("with_sudo"), crate::argparse::ArgValue::Bool(true)), matches!(ns.get("force"), crate::argparse::ArgValue::Bool(true)), "install", lock)
}

fn report_from_dict(op: &'static str, detail: PyDict) -> UpdateReport {
    let mut report = new_report(op, None, String::new());
    report.error_code = detail.get("error_code").and_then(as_str).and_then(ErrorCode::parse).map(ErrorCode::as_str);
    report.phase = detail.get("phase").and_then(as_str).and_then(static_phase);
    report.message = detail.get("message").and_then(as_str).map(str::to_owned);
    report.detail = detail;
    report
}

fn execute_rollback(ctx: &mut CliCtx<'_>, with_sudo: bool, force_requested: bool, lock: FileLockGuard) -> UpdateReport {
    let root = install_layout::resolve_install_root(ctx);
    let current = match install_layout::read_current(&root) {
        Ok(Some(current)) => current,
        Ok(None) => return rollback_unavailable(None, "未記錄 current"),
        Err(error) => return layout_failure("rollback", None, String::new(), StepName::Verify, "rollback_verify", error),
    };
    let previous = match current.previous.clone() {
        Some(previous) => previous,
        None => return rollback_unavailable(Some(current.bundle_version), "未記錄 previous"),
    };
    let target_dir = version_dir(&root, &previous.dir, &previous.bundle_version);
    let previous_backup_path = match current.backup_dir.as_deref() {
        Some(path) => PathBuf::from(path),
        None => return rollback_unavailable(Some(current.bundle_version), "Current 未記錄 Previous 完整備份"),
    };
    let previous_backup = match read_backup_set(ctx, &root, &previous_backup_path) {
        Ok(backup) => backup,
        Err(message) => {
            let mut report = new_report("rollback", Some(current.bundle_version), previous.bundle_version);
            fail_report(&mut report, StepName::Verify, OpFailure {
                code: ErrorCode::ChecksumVerifyFailed.as_str(),
                phase: "rollback_verify",
                reason: Some("backup_incomplete"),
                message: Some(message),
                detail: PyDict::new(),
            });
            return report;
        }
    };
    let target_manifest = match read_manifest_if_present(ctx, &target_dir) {
        Ok(Some((path, manifest))) => Some((path, manifest)),
        Ok(None) => None,
        Err(error) => return layout_failure("rollback", Some(current.bundle_version), previous.bundle_version, StepName::Verify, "rollback_verify", error),
    };
    let target_schema = target_manifest.as_ref().and_then(|(_, manifest)| manifest.state_schema());
    let current_dir = version_dir(&root, &current.dir, &current.bundle_version);
    let current_schema = read_manifest_if_present(ctx, &current_dir).ok().flatten().and_then(|(_, manifest)| manifest.state_schema().map(str::to_owned));
    if schemas_incompatible(current_schema.as_deref(), target_schema) {
        let mut report = new_report("rollback", Some(current.bundle_version), previous.bundle_version);
        fail_report(&mut report, StepName::Verify, schema_failure(report_phase("rollback_verify"), current_schema.as_deref(), target_schema));
        return report;
    }
    if !ctx.p.fs.is_dir(&target_dir) {
        return rollback_unavailable(Some(current.bundle_version), "Previous 版本目錄不存在");
    }
    if let Some((manifest_path, manifest)) = target_manifest {
        let dir = manifest_path.parent().unwrap_or(&target_dir);
        if let Err(diffs) = install_layout::verify_bundle(ctx, dir, &manifest) {
            let mut detail = PyDict::new();
            detail.insert("diffs", diffs_value(&diffs));
            let mut report = new_report("rollback", Some(current.bundle_version), previous.bundle_version);
            fail_report(&mut report, StepName::Verify, OpFailure { code: ErrorCode::ChecksumVerifyFailed.as_str(), phase: "rollback_verify", reason: None, message: None, detail });
            return report;
        }
    }
    let target_mode = previous_backup.supervisor.mode.clone();
    execute_change_with_restore(ctx, &target_dir, &target_mode, with_sudo, force_requested, "rollback", Some(&previous_backup), lock)
}

fn rollback_unavailable(from: Option<String>, reason: &str) -> UpdateReport {
    let mut report = new_report("rollback", from, String::new());
    let message = format!("沒有可回滾的 Previous 版本（{reason}）");
    fail_report(&mut report, StepName::Verify, OpFailure {
        code: ErrorCode::InvalidArgs.as_str(), phase: "rollback_verify", reason: Some("rollback_unavailable"), message: Some(message), detail: PyDict::new(),
    });
    report
}

fn report_phase(op: &str) -> &'static str {
    if op == "rollback_verify" { "rollback_verify" } else { "update_verify" }
}

fn execute_change(
    ctx: &mut CliCtx<'_>,
    bundle_dir: &Path,
    target_mode: &SupervisionMode,
    with_sudo: bool,
    force_requested: bool,
    op: &'static str,
    lock: FileLockGuard,
) -> UpdateReport {
    execute_change_with_restore(ctx, bundle_dir, target_mode, with_sudo, force_requested, op, None, lock)
}

fn execute_change_with_restore(
    ctx: &mut CliCtx<'_>,
    bundle_dir: &Path,
    target_mode: &SupervisionMode,
    with_sudo: bool,
    force_requested: bool,
    op: &'static str,
    restore_previous: Option<&BackupSet>,
    lock: FileLockGuard,
) -> UpdateReport {
    let root = install_layout::resolve_install_root(ctx);
    let current = match install_layout::read_current(&root) {
        Ok(current) => current,
        Err(error) => return layout_failure(op, None, String::new(), StepName::Verify, if op == "rollback" { "rollback_verify" } else { "update_verify" }, error),
    };
    let old_mode = match configured_mode_checked(ctx) {
        Ok(mode) => mode,
        Err(message) => {
            let from = current.as_ref().map(|current| current.bundle_version.clone());
            let mut report = new_report(op, from, String::new());
            fail_report(&mut report, StepName::Verify, OpFailure { code: ErrorCode::InternalError.as_str(), phase: if op == "rollback" { "rollback_verify" } else { "update_verify" }, reason: Some("config_read_failed"), message: Some(message), detail: PyDict::new() });
            return report;
        }
    };
    if op == "install" && current.is_some() {
        return execute_change(ctx, bundle_dir, target_mode, with_sudo, force_requested, "update", lock);
    }
    if op == "update" && current.is_none() {
        return execute_change(ctx, bundle_dir, target_mode, with_sudo, force_requested, "install", lock);
    }
    let from = current.as_ref().map(|current| current.bundle_version.clone());
    let (manifest_path, manifest) = match load_bundle_manifest(ctx, bundle_dir) {
        Ok(value) => value,
        Err(error) => return layout_failure(op, from, String::new(), StepName::Verify, if op == "rollback" { "rollback_verify" } else { "update_verify" }, error),
    };
    let mut report = new_report(op, from.clone(), manifest.bundle_version.clone());
    let verify_phase = if op == "rollback" { "rollback_verify" } else { "update_verify" };
    if let Some(current) = current.as_ref() {
        let current_dir = version_dir(&root, &current.dir, &current.bundle_version);
        let current_schema = read_manifest_if_present(ctx, &current_dir).ok().flatten().and_then(|(_, old)| old.state_schema().map(str::to_owned));
        if schemas_incompatible(current_schema.as_deref(), manifest.state_schema()) {
            fail_report(&mut report, StepName::Verify, schema_failure(verify_phase, current_schema.as_deref(), manifest.state_schema()));
            return report;
        }
    }
    let Some(bundle_dir) = manifest_path.parent() else {
        fail_report(&mut report, StepName::Verify, failure(ErrorCode::InvalidArgs, verify_phase, Some("manifest_invalid"), Some("release manifest 沒有父目錄".into())));
        return report;
    };
    if let Err(diffs) = install_layout::verify_bundle(ctx, bundle_dir, &manifest) {
        let mut detail = PyDict::new();
        detail.insert("diffs", diffs_value(&diffs));
        fail_report(&mut report, StepName::Verify, OpFailure { code: ErrorCode::ChecksumVerifyFailed.as_str(), phase: verify_phase, reason: None, message: None, detail });
        return report;
    }

    let mut journal = Journal {
        op: op.to_string(), from: from.clone(), to: manifest.bundle_version.clone(), step: JournalStep::Lock,
        started_at: SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs().to_string(),
        original: None, snapshot_dir: None, snapshot_sha256: None, backup_dir: None, backup_manifest_sha256: None, target_autostart: None,
    };
    if let Err(error) = write_journal(ctx, &root, &journal) {
        let failed = journal_failure(report, StepName::Lock, "lock", error);
        drop(lock);
        return failed;
    }
    crash_if("lock:before_effect");
    add_step(&mut report, StepName::Lock, true, PyDict::new());
    crash_if("lock:after_effect");

    journal.step = JournalStep::Verify;
    if let Err(error) = write_journal(ctx, &root, &journal) { return journal_failure(report, StepName::Verify, "verify", error); }
    crash_if("verify:before_effect");
    let version_dir = match install_layout::stage_version(ctx, &root, bundle_dir, &manifest) {
        Ok(dir) => dir,
        Err(error) => return layout_failure(op, from, manifest.bundle_version, StepName::Verify, verify_phase, error),
    };
    let mut verify_detail = PyDict::new();
    verify_detail.insert("manifest", str_value(manifest_path.display().to_string()));
    verify_detail.insert("version_dir", str_value(version_dir.display().to_string()));
    add_step(&mut report, StepName::Verify, true, verify_detail);
    crash_if("verify:after_effect");

    journal.step = JournalStep::SafetyCheck;
    if let Err(error) = write_journal(ctx, &root, &journal) { return journal_failure(report, StepName::SafetyCheck, "safety_check", error); }
    crash_if("safety_check:before_effect");
    let endpoint = crate::endpoint_resolve::local_default_endpoint(ctx);
    let daemon = match safety_check(ctx, &endpoint) {
        Ok(snapshot) => snapshot,
        Err((code, detail)) => {
            let mut detail = detail;
            if force_requested { detail.insert("force_ignored", PyValue::Bool(true)); }
            fail_report(&mut report, StepName::SafetyCheck, OpFailure { code, phase: "update_safety_check", reason: None, message: detail.get("message").and_then(as_str).map(str::to_owned), detail });
            clear_journal(ctx, &root, &journal);
            drop(lock);
            return report;
        }
    };
    let sup = match query_supervisor(ctx, &old_mode, &endpoint, daemon.is_some()) {
        Ok(snapshot) => snapshot,
        Err(error) => {
            fail_report(&mut report, StepName::SafetyCheck, error);
            clear_journal(ctx, &root, &journal);
            drop(lock);
            return report;
        }
    };
    if old_mode == SupervisionMode::SystemdSystem && !with_sudo {
        fail_report(&mut report, StepName::StopSupervisor, OpFailure {
            code: ErrorCode::NeedsSudo.as_str(),
            phase: "update_autostart_hold",
            reason: Some("autostart_hold_unverified"),
            message: Some("systemd-system 更新須加 --with-sudo 才能抑制自動啟動".into()),
            detail: PyDict::new(),
        });
        clear_journal(ctx, &root, &journal);
        drop(lock);
        return report;
    }
    let mut safety_detail = PyDict::new();
    safety_detail.insert("daemon_running", PyValue::Bool(daemon.is_some()));
    if force_requested { safety_detail.insert("force_ignored", PyValue::Bool(true)); }
    add_step(&mut report, StepName::SafetyCheck, true, safety_detail);
    crash_if("safety_check:after_effect");

    crash_if("snapshot_taken:before_effect");
    let (mut original, snapshot_dir) = match persist_original_snapshot(ctx, &root, &sup, &daemon) {
        Ok(value) => value,
        Err(error) => return journal_failure(report, StepName::SafetyCheck, "snapshot_taken", error),
    };
    original.supervisor.registration = Some(snapshot_dir.join(if old_mode == SupervisionMode::WindowsTask { "registered.task.xml" } else { "registered.unit" }));
    journal.original = Some(original.clone());
    journal.snapshot_dir = Some(snapshot_dir.to_string_lossy().into_owned());
    journal.snapshot_sha256 = Some(snapshot_hashes(ctx, &snapshot_dir).unwrap_or_default());
    journal.target_autostart = (target_mode != &old_mode).then(|| AutostartPlan {
        kind: hold_kind(target_mode),
        dropin_path: None,
        prior_enabled: None,
        prior_restart: None,
    });
    journal.step = JournalStep::SnapshotTaken;
    if let Err(error) = write_journal(ctx, &root, &journal) { return journal_failure(report, StepName::SafetyCheck, "snapshot_taken", error); }
    crash_if("snapshot_taken:after_effect");

    journal.step = JournalStep::StopSupervisor;
    if let Err(error) = write_journal(ctx, &root, &journal) { return journal_failure(report, StepName::StopSupervisor, "stop_supervisor", error); }
    crash_if("stop_supervisor:before_effect");
    let (mut held, initial_handoff_chain) = match stop_supervisor(ctx, &old_mode, &endpoint, with_sudo, daemon.as_ref(), original.supervisor.enabled, &mut journal, &root) {
        Ok((held, detail, chain)) => {
            add_step(&mut report, StepName::StopSupervisor, true, detail);
            (held, chain)
        }
        Err((failure, keep_journal)) => {
            fail_report(&mut report, StepName::StopSupervisor, failure);
            if !keep_journal { clear_journal(ctx, &root, &journal); }
            drop(lock);
            return report;
        }
    };
    crash_if("stop_supervisor:after_effect");

    journal.step = JournalStep::ConfirmReleased;
    if let Err(error) = write_journal(ctx, &root, &journal) { return journal_failure(report, StepName::ConfirmReleased, "confirm_released", error); }
    crash_if("confirm_released:before_effect");
    match confirm_released(ctx, &endpoint, daemon.as_ref()) {
        Ok(holders_status) => {
            let mut detail = PyDict::new();
            detail.insert("released", PyValue::Bool(true));
            if let Some(status) = holders_status {
                detail.insert("holders_status", str_value(status));
            }
            add_step(&mut report, StepName::ConfirmReleased, true, detail);
        }
        Err(failure) => {
            fail_report(&mut report, StepName::ConfirmReleased, failure);
            release_held(ctx, &mut held, original.supervisor.enabled);
            drop(lock);
            return report;
        }
    }
    crash_if("confirm_released:after_effect");

    let backup_dir = allocate_backup_dir(ctx, &root, from.as_deref(), &manifest.bundle_version);
    journal.step = JournalStep::BackupStarted;
    journal.backup_dir = Some(backup_dir.to_string_lossy().into_owned());
    if let Err(error) = write_journal(ctx, &root, &journal) { return journal_failure(report, StepName::BackupAndSwitch, "backup_started", error); }
    crash_if("backup_started:before_effect");
    let backup = match backup_state(ctx, &root, &backup_dir, sup.clone()) {
        Ok(backup) => backup,
        Err(error) => {
            let mut failed = layout_failure(op, from.clone(), manifest.bundle_version.clone(), StepName::BackupAndSwitch, "update_journal", error);
            failed.reason = Some("journal_write_failed".into());
            release_held(ctx, &mut held, original.supervisor.enabled);
            return failed;
        }
    };
    report.backup_dir = Some(backup.dir.clone());
    journal.backup_dir = Some(backup.dir.to_string_lossy().into_owned());
    crash_if("backup_started:after_effect");
    journal.step = JournalStep::BackupComplete;
    let manifest_hash = hash_path(ctx, &backup.dir.join("manifest.json")).unwrap_or_default();
    journal.backup_manifest_sha256 = Some(manifest_hash.clone());
    if let Err(error) = write_journal(ctx, &root, &journal) { return journal_failure(report, StepName::BackupAndSwitch, "backup_complete", error); }
    let mut backup_detail = PyDict::new();
    backup_detail.insert("backup_dir", str_value(backup.dir.display().to_string()));
    backup_detail.insert("manifest_sha256", str_value(manifest_hash));
    crash_if("backup_complete:before_effect");
    crash_if("backup_complete:after_effect");

    let mut backup_handoff_chain = initial_handoff_chain;
    if let Err(error) = graceful_stop::ensure_released_or_rehandoff(
        ctx,
        &old_mode,
        &endpoint,
        with_sudo,
        if op == "rollback" { "rollback" } else { "update" },
        "update_confirm_released",
        &mut backup_handoff_chain,
    ) {
        let failure = stop_failure_to_op(&error, "update_confirm_released", &backup_handoff_chain);
        fail_report(&mut report, StepName::ConfirmReleased, failure);
        release_held(ctx, &mut held, original.supervisor.enabled);
        clear_journal(ctx, &root, &journal);
        drop(lock);
        return report;
    }

    journal.step = JournalStep::EntriesSwitching;
    if let Err(error) = write_journal(ctx, &root, &journal) { return journal_failure(report, StepName::BackupAndSwitch, "entries_switching", error); }
    crash_if("entries_switching:before_effect");
    if let Some(previous_backup) = restore_previous {
        if let Err(error) = restore_backup(ctx, previous_backup, false) {
            let failure = layout_op_failure(error, "update_start_and_verify");
            return revert_after_failure(ctx, root, journal, original, backup, report, StepName::BackupAndSwitch, failure, &mut held, with_sudo, target_mode);
        }
    } else if op != "install" {
        if let Err(error) = install_supervisor_held(ctx, &root, target_mode, &endpoint, with_sudo) {
            let failure = service_failure_as_op(error, "update_start_and_verify", "supervisor_install_failed");
            return revert_after_failure(ctx, root, journal, original, backup, report, StepName::BackupAndSwitch, failure, &mut held, with_sudo, target_mode);
        }
    }
    if op != "install" && old_mode != *target_mode && old_mode.is_supervised() {
        let records = ctx.p.service.uninstall(&old_mode, with_sudo);
        if let Some(record) = records.iter().find(|record| record.rc != 0) {
            let mut detail = PyDict::new();
            detail.insert("ran", PyValue::List(records.iter().map(|item| PyValue::List(item.argv.iter().cloned().map(PyValue::Str).collect())).collect()));
            let failure = OpFailure {
                code: ErrorCode::InternalError.as_str(),
                phase: "update_start_and_verify",
                reason: Some("supervisor_remove_failed"),
                message: Some(format!("無法移除舊監管模式（exit {}）", record.rc)),
                detail,
            };
            return revert_after_failure(ctx, root, journal, original, backup, report, StepName::BackupAndSwitch, failure, &mut held, with_sudo, target_mode);
        }
    }
    let switch_report = match switch_entries_with_crash(ctx, &root, &version_dir) {
        Ok(report) => report,
        Err(error) => {
            let failure = layout_op_failure(error, "update_start_and_verify");
            return revert_after_failure(ctx, root, journal, original, backup, report, StepName::BackupAndSwitch, failure, &mut held, with_sudo, target_mode);
        }
    };
    let home = crate::setup_cmd::home_dir(ctx);
    let dirs = crate::setup_cmd::user_dirs(&ctx.env, &home);
    if let Err(error) = crate::setup_cmd::materialize_assets(ctx, &dirs, false) {
        let failure = OpFailure {
            code: ErrorCode::InternalError.as_str(),
            phase: "update_start_and_verify",
            reason: Some("asset_materialization_failed"),
            message: Some(format!("安裝資產物化失敗：{error:?}")),
            detail: PyDict::new(),
        };
        return revert_after_failure(ctx, root, journal, original, backup, report, StepName::BackupAndSwitch, failure, &mut held, with_sudo, target_mode);
    }
    if op == "install" {
        let home = crate::setup_cmd::home_dir(ctx);
        let wsl = crate::setup_cmd::ensure_wsl_systemd(ctx, &home, target_mode, with_sudo);
        let config = match endpoint_resolve::load_runtime_config(ctx) {
            Ok(config) => config,
            Err(error) => {
                let failure = OpFailure {
                    code: ErrorCode::InternalError.as_str(),
                    phase: "update_start_and_verify",
                    reason: Some("config_update_failed"),
                    message: Some(error.to_string()),
                    detail: PyDict::new(),
                };
                return revert_after_failure(ctx, root, journal, original, backup, report, StepName::BackupAndSwitch, failure, &mut held, with_sudo, target_mode);
            }
        };
        let explicit_endpoint: Option<String> = None;
        let explicit_socket: Option<String> = None;
        let socket_path = if *target_mode == SupervisionMode::SystemdSystem {
            "/run/serialwrap/serialwrapd.sock".to_string()
        } else if matches!(target_mode, SupervisionMode::WindowsTask | SupervisionMode::WindowsService)
            || endpoint_resolve::rpc_backend_is_win(ctx)
        {
            endpoint.clone()
        } else if old_mode == SupervisionMode::OnDemand
            && target_mode == &SupervisionMode::OnDemand
            && explicit_endpoint.is_none()
            && explicit_socket.is_none()
        {
            config.socket_path().and_then(|value| match value {
                sw_proto::yaml11::YValue::Str(socket)
                    if matches!(sw_proto::endpoint::parse_endpoint(socket), Ok(sw_proto::endpoint::Endpoint::Tcp { .. })) => Some(socket.clone()),
                _ => None,
            }).unwrap_or_else(|| endpoint.clone())
        } else {
            endpoint.clone()
        };
        let setup_req = crate::setup_cmd::ReconcileRequest {
            old_mode: old_mode.clone(),
            target_mode: target_mode.clone(),
            home: home.clone(),
            daemon_running: daemon.is_some(),
            daemon_id: daemon.as_ref().map(|snapshot| snapshot.id.clone()).unwrap_or_default(),
            old_endpoint: endpoint.clone(),
            any_flashing: false,
            with_sudo,
            force: force_requested,
            hold_target: true,
            socket_path: Some(socket_path),
            config_path: endpoint_resolve::config_write_path(ctx),
            old_stop: crate::setup_cmd::OldStop::Reconcile,
        };
        let setup_result = match crate::setup_cmd::reconcile(ctx, &setup_req) {
            Ok(result) => result,
            Err(crate::setup_cmd::ReconcileError::FlashingBusy(message)) => {
                let failure = OpFailure { code: ErrorCode::FlashingBusy.as_str(), phase: "update_start_and_verify", reason: Some("flashing_busy"), message: Some(message), detail: PyDict::new() };
                return revert_after_failure(ctx, root, journal, original, backup, report, StepName::BackupAndSwitch, failure, &mut held, with_sudo, target_mode);
            }
            Err(crate::setup_cmd::ReconcileError::WaitOldExitTimeout { partial }) => {
                let failure = OpFailure { code: ErrorCode::Timeout.as_str(), phase: "update_start_and_verify", reason: Some("reconcile_wait_old_exit"), message: Some("首次安裝等待舊 daemon 結束逾時".into()), detail: partial };
                return revert_after_failure(ctx, root, journal, original, backup, report, StepName::BackupAndSwitch, failure, &mut held, with_sudo, target_mode);
            }
            Err(crate::setup_cmd::ReconcileError::Config(message)) => {
                let failure = OpFailure { code: ErrorCode::InternalError.as_str(), phase: "update_start_and_verify", reason: Some("reconcile_failed"), message: Some(message), detail: PyDict::new() };
                return revert_after_failure(ctx, root, journal, original, backup, report, StepName::BackupAndSwitch, failure, &mut held, with_sudo, target_mode);
            }
        };
        let legacy = crate::setup_cmd::detect_legacy_install(ctx, &home).map_or(PyValue::Null, PyValue::Dict);
        report.detail.insert("setup", PyValue::Dict(setup_result.clone()));
        report.detail.insert("legacy", legacy);
        report.detail.insert("doctor_hint", str_value("serialwrap doctor 可驗證環境"));
        report.detail.insert("console_hint", str_value("human console 請用 serialwrap-minicom COMx（勿直接 minicom -D /dev/ttyUSBx，避免與 daemon two-reader 衝突）"));
        if let Some(pending) = setup_result.get("pending_sudo") {
            report.detail.insert("pending_sudo", pending.clone());
        }
        report.detail.insert("wsl_systemd_enabled", PyValue::Bool(wsl.enabled_now));
        report.detail.insert("needs_restart", PyValue::Bool(wsl.needs_restart));
        if let Some(hint) = wsl.hint {
            report.detail.insert("hint", str_value(hint));
        }
        if !wsl.pending.is_empty() {
            report.detail.insert("wsl_pending", PyValue::List(wsl.pending.into_iter().map(|argv| PyValue::List(argv.into_iter().map(PyValue::Str).collect())).collect()));
        }
    }
    let next_current = current_release(&manifest.bundle_version, &version_dir, current.as_ref(), &backup.dir);
    if let Err(error) = install_layout::write_current(ctx, &root, &next_current) {
        let failure = layout_op_failure(error, "update_start_and_verify");
        let _ = install_layout::revert_entries(ctx, &root, &switch_report);
        return revert_after_failure(ctx, root, journal, original, backup, report, StepName::BackupAndSwitch, failure, &mut held, with_sudo, target_mode);
    }
    if restore_previous.is_none() && op != "install" && old_mode != *target_mode {
    let mut config = match endpoint_resolve::load_runtime_config(ctx) {
        Ok(config) => config,
        Err(error) => {
            let failure = OpFailure {
                code: ErrorCode::InternalError.as_str(),
                phase: "update_start_and_verify",
                reason: Some("config_update_failed"),
                message: Some(error.to_string()),
                detail: PyDict::new(),
            };
            return revert_after_failure(ctx, root, journal, original, backup, report, StepName::BackupAndSwitch, failure, &mut held, with_sudo, target_mode);
        }
    };
    let effective_socket = if old_mode == SupervisionMode::OnDemand
        && target_mode == &SupervisionMode::OnDemand
        && ctx.p.caps.path_flavor != sw_pal::PalPathFlavor::Windows
    {
        config.socket_path().and_then(|value| match value {
            sw_proto::yaml11::YValue::Str(socket) if matches!(sw_proto::endpoint::parse_endpoint(socket), Ok(sw_proto::endpoint::Endpoint::Tcp { .. })) => Some(socket.clone()),
            _ => None,
        }).unwrap_or_else(|| endpoint.clone())
    } else {
        endpoint.clone()
    };
    config.set_mode(target_mode.as_str(), Some(&effective_socket));
    if let Err(error) = endpoint_resolve::save_runtime_config(ctx, &config) {
        let failure = OpFailure {
            code: ErrorCode::InternalError.as_str(),
            phase: "update_start_and_verify",
            reason: Some("config_update_failed"),
            message: Some(error.to_string()),
            detail: PyDict::new(),
        };
        return revert_after_failure(ctx, root, journal, original, backup, report, StepName::BackupAndSwitch, failure, &mut held, with_sudo, target_mode);
    }
    }
    crash_if("entries_switching:after_effect");
    let mut switch_detail = backup_detail;
    switch_detail.insert("version_dir", str_value(version_dir.display().to_string()));
    add_step(&mut report, StepName::BackupAndSwitch, true, switch_detail);
    crash_if("start_and_verify:before_effect");
    journal.step = JournalStep::StartAndVerify;
    if let Err(error) = write_journal(ctx, &root, &journal) { return journal_failure(report, StepName::StartAndVerify, "start_and_verify", error); }
    let mut start_handoff_chain = Vec::new();
    if let Err(error) = graceful_stop::ensure_released_or_rehandoff(
        ctx,
        target_mode,
        &endpoint,
        with_sudo,
        if op == "rollback" { "rollback" } else { "update" },
        "update_start_and_verify",
        &mut start_handoff_chain,
    ) {
        let failure = stop_failure_to_op(&error, "update_start_and_verify", &start_handoff_chain);
        return revert_after_failure(ctx, root, journal, original, backup, report, StepName::StartAndVerify, failure, &mut held, with_sudo, target_mode);
    }
    let desired_target_enabled = restore_previous.map(|set| set.supervisor.enabled).unwrap_or_else(|| {
        if target_mode == &original.supervisor.mode { original.supervisor.enabled } else { Some(true) }
    });
    let source_release_enabled = if target_mode == &original.supervisor.mode { desired_target_enabled } else { Some(false) };
    if let Err(failure) = release_held_checked(ctx, &mut held, source_release_enabled) {
        fail_report(&mut report, StepName::StartAndVerify, failure);
        drop(lock);
        return report;
    }
    if target_mode != &old_mode && target_mode.is_supervised() {
        let records = ctx.p.service.set_enabled(target_mode, desired_target_enabled.unwrap_or(true), with_sudo);
        if let Some(record) = records.iter().find(|record| record.rc != 0) {
            let mut detail = PyDict::new();
            detail.insert("ran", PyValue::List(records.iter().map(|item| PyValue::List(item.argv.iter().cloned().map(PyValue::Str).collect())).collect()));
            let failure = OpFailure {
                code: ErrorCode::InternalError.as_str(),
                phase: "update_autostart_release",
                reason: Some("autostart_release_failed"),
                message: Some("目標監管模式無法啟用，journal 保留供 setup 復原".into()),
                detail,
            };
            let _ = record;
            fail_report(&mut report, StepName::StartAndVerify, failure);
            drop(lock);
            return report;
        }
    }
    let started = start_and_verify(ctx, &root, target_mode, &endpoint, original.daemon.is_some(), original.daemon.is_some(), &manifest, with_sudo, original.daemon.as_ref());
    crash_if("start_and_verify:started");
    match started {
        Ok(detail) => {
            add_step(&mut report, StepName::StartAndVerify, true, detail);
            crash_if("start_and_verify:after_effect");
            clear_journal(ctx, &root, &journal);
            report.ok = true;
            drop(lock);
            report
        }
        Err(failure) => revert_after_failure(ctx, root, journal, original, backup, report, StepName::StartAndVerify, failure, &mut held, with_sudo, target_mode),
    }
}

fn service_failure_as_op(error: sw_pal::service_ctl::ServiceResult, phase: &'static str, reason: &'static str) -> OpFailure {
    let code = error
        .error_code
        .as_deref()
        .and_then(ErrorCode::parse)
        .unwrap_or(ErrorCode::InternalError);
    let mut detail = PyDict::new();
    detail.insert("hint", opt_str(error.hint.as_deref()));
    OpFailure { code: code.as_str(), phase, reason: Some(reason), message: error.hint, detail }
}

fn service_result_failure(mode: &SupervisionMode, action: &str, message: String) -> sw_pal::service_ctl::ServiceResult {
    sw_pal::service_ctl::ServiceResult {
        ok: false,
        mode: mode.as_str().to_string(),
        action: action.to_string(),
        rc: Some(1),
        stdout: None,
        stderr: Some(message.clone()),
        error_code: Some(ErrorCode::InternalError.as_str().into()),
        hint: Some(message),
        phase: Some("supervisor_install".into()),
    }
}

fn release_held(ctx: &mut CliCtx<'_>, held: &mut HeldAutostart, enabled: Option<bool>) {
    if held.record.kind != AutostartHoldKind::NoSupervisor {
        let _ = ctx.p.service.release_autostart(&held.record, enabled, held.with_sudo);
    }
    ctx.autostart_lock.take();
}

fn release_held_checked(
    ctx: &mut CliCtx<'_>,
    held: &mut HeldAutostart,
    enabled: Option<bool>,
) -> Result<(), OpFailure> {
    if held.record.kind == AutostartHoldKind::NoSupervisor {
        ctx.autostart_lock.take();
        return Ok(());
    }
    match ctx.p.service.release_autostart(&held.record, enabled, held.with_sudo) {
        Ok(_) => {
            ctx.autostart_lock.take();
            Ok(())
        }
        Err(error) => Err(service_failure_as_op(
            error,
            "update_autostart_release",
            "autostart_release_failed",
        )),
    }
}

fn stop_supervisor(
    ctx: &mut CliCtx<'_>, mode: &SupervisionMode, endpoint: &str, with_sudo: bool,
    daemon: Option<&DaemonSnapshot>, enabled: Option<bool>, journal: &mut Journal, root: &InstallRoot,
) -> Result<(HeldAutostart, PyDict, Vec<HandoffLink>), (OpFailure, bool)> {
    let hold_plan = journal.original.as_ref().map(hold_record_from_plan);
    let hold = if mode == &SupervisionMode::OnDemand {
        AutostartHoldRecord {
            mode: mode.clone(),
            kind: AutostartHoldKind::NoSupervisor,
            name: None,
            dropin_path: None,
            prior_enabled: None,
            prior_restart: None,
            ran: Vec::new(),
        }
    } else {
        match ctx.p.service.hold_autostart(mode, with_sudo) {
            Ok(hold) => hold_plan.as_ref().map_or(hold.clone(), |plan| merge_hold_plan(plan, hold)),
            Err(error) => {
                let failure = service_failure_as_op(error, "update_autostart_hold", "autostart_hold_unverified");
                return Err((failure, false));
            }
        }
    };
    if mode == &SupervisionMode::OnDemand {
        if let Err(code) = acquire_switch_autostart_lock(ctx) {
            let failure = OpFailure { code: code.as_str(), phase: "update_autostart_hold", reason: Some("autostart_lock_timeout"), message: Some("無法取得 autostart.lock；請稍後重試".into()), detail: PyDict::new() };
            return Err((failure, false));
        }
    }
    let mut held = HeldAutostart { record: hold, with_sudo };
    crash_if("stop_supervisor:after_hold");
    if let Some(original) = journal.original.as_mut() {
        original.autostart = autostart_plan(&held.record, enabled);
    }
    if write_journal(ctx, root, journal).is_err() {
        release_held(ctx, &mut held, enabled);
        return Err((failure(ErrorCode::InternalError, "update_journal", Some("journal_write_failed"), None), false));
    }
    let mut chain = Vec::new();
    let mut detail = PyDict::new();
    let stop = if daemon.is_some() {
        graceful_stop::graceful_stop_with_existing_hold(
            ctx,
            mode,
            endpoint,
            with_sudo,
            true,
            "update_confirm_released",
            StopRpc::Handoff { reason: if journal.op == "rollback" { "rollback" } else { "update" } },
            Some(held.record.clone()),
        )
    } else {
        if let Err(error) = graceful_stop::ensure_released_or_rehandoff(ctx, mode, endpoint, with_sudo, if journal.op == "rollback" { "rollback" } else { "update" }, "update_safety_check", &mut chain) {
            if matches!(error.cause(), StopFailure::Timeout { .. }) {
                release_held(ctx, &mut held, enabled);
                return Err((stop_failure_to_op(&error, "update_confirm_released", &chain), true));
            }
            release_held(ctx, &mut held, enabled);
            return Err((stop_failure_to_op(&error, "update_safety_check", &chain), false));
        }
        detail.insert("handoff", str_value("not_requested"));
        detail.insert("autostart_hold", autostart_record_value(&held.record, enabled));
        return Ok((held, detail, chain));
    };
    match stop {
        Ok(report) => {
            let chain = report.chain.clone();
            detail.insert("autostart_hold", autostart_record_value(report.hold.as_ref().unwrap_or(&held.record), enabled));
            if let Some(token) = report.handoff_token { detail.insert("handoff_token", str_value(token)); }
            detail.insert("graceful", PyValue::Bool(report.graceful));
            detail.insert("handoff_chain", PyValue::List(report.chain.iter().map(|link| PyValue::Dict(PyDict::from_pairs(vec![
                ("pid".into(), link.pid.map(|pid| PyValue::Int(PyInt::Small(i128::from(pid)))).unwrap_or(PyValue::Null)),
                ("relaunched".into(), PyValue::Bool(link.relaunched)),
                ("token".into(), str_value(link.token.clone())),
            ]))).collect()));
            if let Some(record) = report.hold { held.record = record; }
            Ok((held, detail, chain))
        }
        Err(error) => {
            let timeout = matches!(error.cause(), StopFailure::Timeout { .. });
            if timeout {
                let mut timed_out = journal.clone();
                timed_out.step = JournalStep::ConfirmReleased;
                if write_journal(ctx, root, &timed_out).is_ok() {
                    *journal = timed_out;
                }
            }
            let phase = if timeout || matches!(error.cause(), StopFailure::Relaunched { .. } | StopFailure::RelaunchLoop { .. }) {
                "update_confirm_released"
            } else {
                "update_safety_check"
            };
            let failure = stop_failure_to_op(&error, phase, error.chain());
            if !timeout {
                if let Some(record) = error.hold() { held.record = record.clone(); }
                release_held(ctx, &mut held, enabled);
            }
            Err((failure, timeout))
        }
    }
}

fn install_supervisor_held(
    ctx: &mut CliCtx<'_>,
    root: &InstallRoot,
    mode: &SupervisionMode,
    endpoint: &str,
    with_sudo: bool,
) -> Result<(), sw_pal::service_ctl::ServiceResult> {
    if *mode == SupervisionMode::OnDemand { return Ok(()); }
    if *mode == SupervisionMode::SystemdSystem && !with_sudo {
        return Err(sw_pal::service_ctl::ServiceResult {
            ok: false, mode: mode.as_str().to_string(), action: "install".into(), rc: Some(1), stdout: None, stderr: None,
            error_code: Some(ErrorCode::NeedsSudo.as_str().into()), hint: Some("systemd-system 更新須加 --with-sudo".into()), phase: Some("supervisor_install"),
        });
    }
    let daemon = root.bin.join(if ctx.p.caps.path_flavor == sw_pal::PalPathFlavor::Windows { "serialwrapd.exe" } else { "serialwrapd" });
    let mut exec = vec![daemon.to_string_lossy().into_owned(), "--socket".into(), endpoint.to_string()];
    let profiles = PathBuf::from(&ctx.paths.config_dir).join("profiles");
    exec.extend(["--profile-dir".into(), profiles.to_string_lossy().into_owned()]);
    let definition_path = match mode {
        SupervisionMode::WindowsTask => Some(root.supervisor.join("serialwrap.task.xml")),
        SupervisionMode::SystemdUser => Some(systemd_user_unit_path(ctx)),
        SupervisionMode::SystemdSystem => Some(PathBuf::from("/etc/systemd/system/serialwrap.service")),
        _ => None,
    };
    if *mode == SupervisionMode::WindowsTask {
        let path = definition_path.as_ref().expect("task definition");
        if let Some(parent) = path.parent() { ctx.p.fs.mkdirs(parent).map_err(|error| service_result_failure(mode, "install", error.py_message))?; }
        let command = exec[0].clone();
        let arguments = exec.iter().skip(1).map(|value| if value.contains(' ') { format!("\"{value}\"") } else { value.clone() }).collect::<Vec<_>>().join(" ");
        let xml = sw_pal::service_ctl::render_task_xml(&sw_pal::service_ctl::TaskXmlSpec {
            name: "serialwrap".into(),
            user_id: ctx.p.env.user_identity(),
            command,
            arguments,
            working_dir: root.bin.to_string_lossy().into_owned(),
            enabled: false,
        });
        let bytes = sw_pal::service_ctl::encode_utf16le_bom(&xml);
        ctx.p.fs.atomic_write(path, &bytes, None).map_err(|error| service_result_failure(mode, "install", error.py_message))?;
    } else if *mode == SupervisionMode::SystemdUser {
        let path = definition_path.as_ref().expect("systemd definition");
        let body = crate::supervisor_defs::render_user_unit(&format!("{} --socket {} --profile-dir {}", daemon.display(), endpoint, profiles.display()));
        if let Some(parent) = path.parent() { ctx.p.fs.mkdirs(parent).map_err(|error| service_result_failure(mode, "install", error.py_message))?; }
        ctx.p.fs.atomic_write(path, body.as_bytes(), None).map_err(|error| service_result_failure(mode, "install", error.py_message))?;
    } else if *mode == SupervisionMode::SystemdSystem {
        let home = crate::setup_cmd::home_dir(ctx);
        let unit = crate::setup_cmd::stage_system_unit(ctx, &home)
            .map_err(|error| service_result_failure(mode, "install", format!("system unit staging 失敗：{error:?}")))?;
        let profiles = crate::setup_cmd::stage_system_profiles(ctx, &home)
            .map_err(|error| service_result_failure(mode, "install", format!("system profile staging 失敗：{error:?}")))?;
        let commands = [
            vec!["sudo".into(), "install".into(), "-m".into(), "0644".into(), unit.to_string_lossy().into_owned(), "/etc/systemd/system/serialwrap.service".into()],
            vec!["sudo".into(), "mkdir".into(), "-p".into(), "/etc/serialwrap/profiles".into()],
            vec!["sudo".into(), "cp".into(), "-r".into(), format!("{}/.", profiles.display()), "/etc/serialwrap/profiles".into()],
        ];
        for argv in commands {
            let result = ctx.p.env.run(&argv, Some(Duration::from_secs(30)));
            if !result.is_ok_and(|output| output.rc == 0) {
                return Err(service_result_failure(mode, "install", format!("system unit 安裝失敗：{argv:?}")));
            }
        }
    }
    let definition = sw_pal::service_ctl::SupervisorDefinition {
        mode: mode.clone(),
        name: "serialwrap".into(),
        exec,
        working_dir: Some(root.bin.clone()),
        run_user: (*mode == SupervisionMode::SystemdSystem).then(|| ctx.p.env.user_identity()),
        env: Vec::new(),
        definition_path,
        register_held: true,
    };
    let records = ctx.p.service.install(&definition, with_sudo);
    if let Some(record) = records.iter().find(|record| record.rc != 0) {
        return Err(sw_pal::service_ctl::ServiceResult {
            ok: false,
            mode: mode.as_str().to_string(),
            action: "install".into(),
            rc: Some(record.rc),
            stdout: None,
            stderr: None,
            error_code: Some(ErrorCode::InternalError.as_str().into()),
            hint: Some("監管定義安裝失敗".into()),
            phase: Some("supervisor_install".into()),
        });
    }
    if matches!(mode, SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem) {
        let argv = if *mode == SupervisionMode::SystemdUser { vec!["systemctl".into(), "--user".into(), "daemon-reload".into()] }
            else if with_sudo { vec!["sudo".into(), "systemctl".into(), "daemon-reload".into()] }
            else { vec!["systemctl".into(), "daemon-reload".into()] };
        if ctx.p.env.run(&argv, None).is_err() {
            return Err(sw_pal::service_ctl::ServiceResult {
                ok: false, mode: mode.as_str().to_string(), action: "daemon-reload".into(), rc: Some(1), stdout: None, stderr: None,
                error_code: Some(ErrorCode::InternalError.as_str().into()), hint: Some("systemd daemon-reload 失敗".into()), phase: Some("supervisor_install".into()),
            });
        }
    }
    Ok(())
}

fn switch_entries_with_crash(ctx: &CliCtx<'_>, root: &InstallRoot, version_dir: &Path) -> Result<install_layout::SwitchReport, LayoutError> {
    let report = install_layout::switch_entries(ctx, root, version_dir)?;
    crash_if("entries_switching:after_first_rename");
    Ok(report)
}

fn start_and_verify(
    ctx: &mut CliCtx<'_>,
    root: &InstallRoot,
    mode: &SupervisionMode,
    endpoint: &str,
    should_start: bool,
    start_if_needed: bool,
    manifest: &sw_proto::release_manifest::ReleaseManifest,
    with_sudo: bool,
    daemon_snapshot: Option<&DaemonSnapshot>,
) -> Result<PyDict, OpFailure> {
    let cli_name = if ctx.p.caps.path_flavor == sw_pal::PalPathFlavor::Windows { "serialwrap.exe" } else { "serialwrap" };
    let cli_path = root.versions.join(&manifest.bundle_version).join("bin").join(cli_name);
    let mut child = None;
    if should_start && start_if_needed {
        if mode.is_supervised() {
            let start = ctx.p.service.action(mode, ServiceAction::Start, with_sudo);
            if !start.ok || start.rc.is_some_and(|rc| rc != 0) {
                return Err(service_failure_as_op(start, "update_start_and_verify", "supervisor_start_failed"));
            }
        } else {
            acquire_switch_autostart_lock(ctx).map_err(|code| OpFailure { code: code.as_str(), phase: "update_autostart_hold", reason: Some("autostart_lock_timeout"), message: Some("無法取得 autostart.lock；請稍後重試".into()), detail: PyDict::new() })?;
            child = Some(spawn_daemon(ctx, root, endpoint).map_err(|message| OpFailure { code: ErrorCode::DaemonExited.as_str(), phase: "update_start_and_verify", reason: Some("daemon_start_failed"), message: Some(message), detail: PyDict::new() })?);
        }
    }
    let mut ping = None;
    if should_start {
        let attempts = if ctx.p.caps.path_flavor == sw_pal::PalPathFlavor::Windows { 50 } else { 15 };
        for _ in 0..attempts {
            ctx.p.clock.sleep(Duration::from_millis(200));
            if let Some(child) = child.as_ref() {
                if let Some(returncode) = ctx.p.process.poll(child) {
                    let mut detail = PyDict::new();
                    detail.insert("pid", PyValue::Int(PyInt::Small(i128::from(child.pid))));
                    detail.insert("returncode", PyValue::Int(PyInt::Small(i128::from(returncode))));
                    return Err(OpFailure { code: ErrorCode::DaemonExited.as_str(), phase: "update_start_and_verify", reason: Some("daemon_start_failed"), message: Some("daemon 啟動後立即結束".into()), detail });
                }
            }
            if let Some(response) = rpc(ctx, endpoint, "health.ping", 0.5).filter(|response| response.get_truthy("ok")) {
                ping = Some(response);
                break;
            }
        }
        if ping.is_none() {
            let mut detail = PyDict::new();
            detail.insert("probe", str_value("health.ping"));
            detail.insert("health", str_value("unknown"));
            return Err(OpFailure { code: ErrorCode::Timeout.as_str(), phase: "update_start_and_verify", reason: Some("health_failed"), message: Some("新版 daemon 在啟動時限內未通過 health.ping".into()), detail });
        }
    }
    let version_out = ctx.p.env.run(&[cli_path.to_string_lossy().into_owned(), "--version".into()], Some(Duration::from_secs(5)))
        .map_err(|error| OpFailure { code: ErrorCode::InternalError.as_str(), phase: "update_start_and_verify", reason: Some("cli_version_failed"), message: Some(error.py_message), detail: PyDict::new() })?;
    let cli_version = version_out.stdout.trim().strip_prefix("serialwrap ").unwrap_or("").trim();
    if version_out.rc != 0 || cli_version.is_empty() {
        let mut detail = PyDict::new();
        detail.insert("returncode", PyValue::Int(PyInt::Small(i128::from(version_out.rc))));
        detail.insert("stdout", str_value(version_out.stdout));
        detail.insert("stderr", str_value(version_out.stderr));
        return Err(OpFailure { code: ErrorCode::InternalError.as_str(), phase: "update_start_and_verify", reason: Some("cli_version_failed"), message: Some("已切換 CLI 無法回報版本".into()), detail });
    }
    let mut cli_digests = Vec::new();
    for (name, path) in [("stable_entry:serialwrap", cli_path), ("stable_entry:serialwrapd", root.bin.join(if ctx.p.caps.path_flavor == sw_pal::PalPathFlavor::Windows { "serialwrapd.exe" } else { "serialwrapd" }))] {
        match hash_path(ctx, &path) {
            Ok(digest) => cli_digests.push((name.into(), digest)),
            Err(error) => return Err(layout_op_failure(error, "update_start_and_verify")),
        }
    }
    if cli_version != manifest.bundle_version {
        let mut detail = PyDict::new();
        detail.insert("expected_version", str_value(manifest.bundle_version.clone()));
        detail.insert("actual_version", str_value(cli_version));
        return Err(OpFailure { code: ErrorCode::InternalError.as_str(), phase: "update_start_and_verify", reason: Some("pairing_mismatch"), message: Some("CLI 回報的版本與目標 bundle 不符".into()), detail });
    }
    let status = if should_start {
        checked_rpc(ctx, endpoint, "health.status", 1.0).map_err(|(code, detail)| OpFailure { code, phase: "update_start_and_verify", reason: Some("health_failed"), message: Some("新版 daemon health.status 失敗".into()), detail })?
    } else {
        PyDict::new()
    };
    if !status.get_truthy("ok") {
        if should_start { return Err(OpFailure { code: ErrorCode::InternalError.as_str(), phase: "update_start_and_verify", reason: Some("health_failed"), message: Some("新版 daemon health.status 未回報成功".into()), detail: status }); }
    }
    let reported_version = if should_start { status.get("version").and_then(as_str).or_else(|| status.get("bundle_version").and_then(as_str)) } else { None };
    if should_start && reported_version.is_none() {
        return Err(OpFailure { code: ErrorCode::InvalidResponse.as_str(), phase: "update_start_and_verify", reason: Some("pairing_mismatch"), message: Some("新版 daemon health.status 缺少版本欄位".into()), detail: status });
    }
    if reported_version.is_some_and(|version| version != manifest.bundle_version) {
        let mut detail = PyDict::new();
        detail.insert("expected_version", str_value(manifest.bundle_version.clone()));
        detail.insert("actual_version", opt_str(reported_version));
        return Err(OpFailure { code: ErrorCode::InternalError.as_str(), phase: "update_start_and_verify", reason: Some("pairing_mismatch"), message: Some("新版 daemon 回報的版本與目標版本不符".into()), detail });
    }
    let diffs = install_layout::check_pairing(manifest, cli_version, reported_version, &cli_digests);
    if !diffs.is_empty() {
        let mut detail = PyDict::new();
        detail.insert("pairing_diffs", diffs_value(&diffs));
        return Err(OpFailure { code: ErrorCode::InternalError.as_str(), phase: "update_start_and_verify", reason: Some("pairing_mismatch"), message: Some("CLI、daemon 與 release manifest 配對不符".into()), detail });
    }
    if should_start {
    let wal = status.get("wal").and_then(|value| dict_value(Some(value))).and_then(|wal| wal.get("healthy"));
    if !matches!(wal, Some(PyValue::Bool(true))) {
        let mut detail = status.clone();
        detail.insert("probe", str_value("health.status.wal.healthy"));
        return Err(OpFailure { code: ErrorCode::InternalError.as_str(), phase: "update_start_and_verify", reason: Some("wal_unhealthy"), message: Some("新版 daemon health.status 缺少健康的 WAL 狀態".into()), detail });
    }
    let sessions = checked_rpc(ctx, endpoint, "session.list", 1.0).map_err(|(code, detail)| OpFailure { code, phase: "update_start_and_verify", reason: Some("state_incompatible"), message: Some("新版 daemon session.list 失敗".into()), detail })?;
    let session_list = match sessions.get("sessions") {
        Some(PyValue::List(items)) => items,
        _ => return Err(OpFailure { code: ErrorCode::InvalidResponse.as_str(), phase: "update_start_and_verify", reason: Some("state_incompatible"), message: Some("新版 daemon session.list 格式無效".into()), detail: sessions }),
    };
    let mut expected = Vec::<(String, bool)>::new();
    if let Some(snapshot) = daemon_snapshot {
        for old in &snapshot.sessions {
            let com = old.get("com").and_then(as_str).ok_or_else(|| OpFailure { code: ErrorCode::InvalidResponse.as_str(), phase: "update_start_and_verify", reason: Some("state_incompatible"), message: Some("停止前 session snapshot 缺少 COM".into()), detail: PyDict::new() })?;
            let was_released = old.get("state").and_then(as_str).is_some_and(|state| state.as_bytes().eq_ignore_ascii_case(RELEASED_SESSION_STATE));
            expected.push((com.to_string(), was_released));
        }
    } else {
        let state_path = PathBuf::from(&ctx.paths.state_dir).join("state.json");
        if ctx.p.fs.exists(&state_path) {
            let bytes = ctx.p.fs.read(&state_path).map_err(|error| OpFailure { code: ErrorCode::InternalError.as_str(), phase: "update_start_and_verify", reason: Some("state_incompatible"), message: Some(error.py_message), detail: PyDict::new() })?;
            let prior = pyjson::loads(&String::from_utf8_lossy(&bytes)).map_err(|error| OpFailure { code: ErrorCode::InvalidResponse.as_str(), phase: "update_start_and_verify", reason: Some("state_incompatible"), message: Some(error.to_string()), detail: PyDict::new() })?;
            if let Some(released) = dict_value(Some(&prior)).and_then(|state| state.get("released")).and_then(|value| dict_value(Some(value))) {
                expected.extend(released.0.keys().map(|com| (com.clone(), true)));
            }
        }
    }
    for (com, was_released) in expected {
        let current = session_list.iter().filter_map(|item| dict_value(Some(item))).find(|session| session.get("com").and_then(as_str) == Some(com.as_str()));
        let Some(current) = current else {
            return Err(OpFailure { code: ErrorCode::InvalidResponse.as_str(), phase: "update_start_and_verify", reason: Some("state_incompatible"), message: Some(format!("新版 daemon 未列出停止前的 session {com}")), detail: PyDict::new() });
        };
        if was_released && !current.get("state").and_then(as_str).is_some_and(|state| state.as_bytes().eq_ignore_ascii_case(RELEASED_SESSION_STATE)) {
            return Err(OpFailure { code: ErrorCode::InternalError.as_str(), phase: "update_start_and_verify", reason: Some("state_incompatible"), message: Some(format!("新版 daemon 自動附掛了 state.json 原本為 RELEASED 的 session {com}")), detail: PyDict::new() });
        }
    }
    for (method, key) in [("alias.list", "aliases"), ("device.list", "devices")] {
        let response = checked_rpc(ctx, endpoint, method, 1.0).map_err(|(code, detail)| OpFailure { code, phase: "update_start_and_verify", reason: Some("smoke_failed"), message: Some(format!("新版 daemon {method} 失敗")), detail })?;
        if !matches!(response.get(key), Some(PyValue::List(_))) {
            return Err(OpFailure { code: ErrorCode::InvalidResponse.as_str(), phase: "update_start_and_verify", reason: Some("smoke_failed"), message: Some(format!("新版 daemon {method} 回應格式無效")), detail: response });
        }
    }
    }
    if should_start && start_if_needed && *mode == SupervisionMode::OnDemand {
        ctx.autostart_lock.take();
    }
    let mut detail = PyDict::new();
    detail.insert("daemon_version", opt_str(reported_version));
    detail.insert("started", PyValue::Bool(should_start));
    if !should_start { detail.insert("reason", str_value("daemon_was_not_running")); }
    Ok(detail)
}

fn revert_after_failure(
    ctx: &mut CliCtx<'_>,
    root: InstallRoot,
    mut journal: Journal,
    original: OriginalSnapshot,
    backup: BackupSet,
    mut report: UpdateReport,
    failed_step: StepName,
    failure: OpFailure,
    held: &mut HeldAutostart,
    with_sudo: bool,
    target_mode: &SupervisionMode,
) -> UpdateReport {
    if report.error_code.is_none() { fail_report(&mut report, failed_step, failure.clone()); }
    let failure_path = backup.dir.join("failure.json");
    let mut failure_doc = PyDict::new();
    failure_doc.insert("error_code", str_value(failure.code));
    failure_doc.insert("phase", str_value(failure.phase));
    failure_doc.insert("reason", opt_str(failure.reason));
    failure_doc.insert("message", opt_str(failure.message.as_deref()));
    failure_doc.insert("failed_step", str_value(failed_step.as_str()));
    failure_doc.insert("steps", report_value(&report).get("steps").cloned().unwrap_or(PyValue::List(Vec::new())));
    failure_doc.insert("detail", PyValue::Dict(failure.detail.clone()));
    if let Some(returncode) = failure.detail.get("returncode") {
        failure_doc.insert("returncode", returncode.clone());
    }
    let _ = write_json(ctx, &failure_path, &PyValue::Dict(failure_doc.clone()));
    let mut target_hold = None;
    let reusable_hold = (target_mode == &original.supervisor.mode
        && held.record.kind != AutostartHoldKind::NoSupervisor
        && ctx.p.service.autostart_hold_state(target_mode) == HoldState::Held)
        .then(|| held.record.clone());
    match stop_for_revert_with_hold(ctx, target_mode, &original.supervisor.endpoint, with_sudo, "update_revert", reusable_hold) {
        Ok(RevertStop::Deferred(error)) => {
            let recovery = recovery_deferred("start_and_verify", error, ctx, &original.supervisor.endpoint, "update_revert");
            report.revert_pending = true;
            report.revert_error = Some(recovery.detail.clone());
            failure_doc.insert("revert_deferred", PyValue::Bool(true));
            failure_doc.insert("revert_error", PyValue::Dict(recovery.detail));
            let _ = write_json(ctx, &failure_path, &PyValue::Dict(failure_doc));
            release_held(ctx, held, original.supervisor.enabled);
            return report;
        }
        Err(error) => {
            let recovery = recovery_stop_error("start_and_verify", &error, ctx, &original.supervisor.endpoint);
            report.revert_pending = true;
            report.revert_error = Some(recovery.detail.clone());
            failure_doc.insert("revert_deferred", PyValue::Bool(true));
            failure_doc.insert("revert_error", PyValue::Dict(recovery.detail));
            let _ = write_json(ctx, &failure_path, &PyValue::Dict(failure_doc));
            release_held(ctx, held, original.supervisor.enabled);
            return report;
        }
        Ok(RevertStop::Absent { hold }) => target_hold = hold,
        Ok(RevertStop::Stopped { hold: Some(new_hold), .. }) => target_hold = Some(new_hold),
        Ok(RevertStop::Stopped { hold: None, .. }) => {}
    }
    let holders_status = match confirm_released(ctx, &original.supervisor.endpoint, original.daemon.as_ref()) {
        Ok(status) => status,
        Err(failure) => {
            let detail = op_failure_detail(&failure);
            report.revert_pending = true;
            report.revert_error = Some(detail.clone());
            failure_doc.insert("revert_error", PyValue::Dict(detail));
            let _ = write_json(ctx, &failure_path, &PyValue::Dict(failure_doc));
            return report;
        }
    };
    journal.step = JournalStep::RevertToPrevious;
    if write_journal(ctx, &root, &journal).is_err() {
        report.revert_pending = true;
        return report;
    }
    crash_if("revert_to_previous:before_effect");
    let backup_used = backup.dir.as_path();
    let restore_result = restore_original(ctx, &root, &journal, Some(backup_used));
    if restore_result.is_ok() {
        match restore_supervisor_and_daemon(ctx, &root, &original, Some(backup_used), None, target_hold.as_ref(), with_sudo) {
            Ok(mut measured) => {
                if let Some(status) = holders_status {
                    measured.insert("holders_status", str_value(status));
                }
                report.reverted = true;
                report.revert_pending = false;
                add_step(&mut report, StepName::RevertToPrevious, true, measured);
                clear_journal(ctx, &root, &journal);
            }
            Err(message) => {
                report.revert_pending = true;
                let detail = PyDict::from_pairs(vec![("message".into(), str_value(message))]);
                failure_doc.insert("revert_error", PyValue::Dict(detail.clone()));
                let _ = write_json(ctx, &failure_path, &PyValue::Dict(failure_doc));
                report.revert_error = Some(detail);
                return report;
            }
        }
    } else {
        report.revert_pending = true;
        report.revert_error = Some(PyDict::from_pairs(vec![("message".into(), str_value(restore_result.unwrap_err()))]));
    }
    let _ = with_sudo;
    crash_if("revert_to_previous:after_effect");
    report
}

fn stop_failure_to_op(error: &StopFailure, phase: &'static str, chain: &[HandoffLink]) -> OpFailure {
    let mut detail = PyDict::new();
    let (code, reason, message) = match error.cause() {
        StopFailure::Handoff(HandoffError::Busy { code, busy, .. }) | StopFailure::Relaunched { err: HandoffError::Busy { code, busy, .. }, .. } => {
            detail.insert("handoff", str_value("refused"));
            detail.insert("busy", PyValue::List(busy.clone()));
            (if code == ErrorCode::FlashingBusy.as_str() { ErrorCode::FlashingBusy } else { ErrorCode::SessionBusy }, None, Some("daemon 原子交接判定忙碌，拒絕更新／回滾".to_string()))
        }
        StopFailure::Handoff(HandoffError::Unconfirmed { code, probe, outcome, pids }) | StopFailure::Relaunched { err: HandoffError::Unconfirmed { code, probe, outcome, pids }, .. } => {
            detail.insert("probe", str_value(*probe));
            detail.insert("handoff_outcome", str_value(*outcome));
            detail.insert("handoff", str_value("refused"));
            if !pids.is_empty() {
                detail.insert("pids", PyValue::List(pids.iter().map(|pid| PyValue::Int(PyInt::Small(i128::from(*pid)))).collect()));
                detail.insert("hint", str_value("請先執行 serialwrap daemon status 確認狀態；確認安全後再執行 serialwrap daemon stop，再重試 setup"));
                (ErrorCode::DeviceStillHeld, Some("daemon_presence_unknown"), Some("daemon 仍有行程但無法確認交接結果；保留安裝 journal".into()))
            } else {
                let mapped = ErrorCode::parse(code).unwrap_or(ErrorCode::InternalError);
                detail.insert("hint", str_value("請執行 serialwrap daemon status 確認狀態；確認安全後再執行 serialwrap daemon stop，之後重試 setup"));
                (mapped, None, Some("無法確認 daemon 交接停止結果；請執行 serialwrap daemon status 確認狀態後重試".into()))
            }
        }
        StopFailure::RelaunchLoop { pids } => {
            detail.insert("reason", str_value("relaunch_loop"));
            detail.insert("pids", PyValue::List(pids.iter().map(|pid| PyValue::Int(PyInt::Small(i128::from(*pid)))).collect()));
            (ErrorCode::DeviceStillHeld, Some("relaunch_loop"), Some("監管模式在交接期間反覆重新啟動 daemon".into()))
        }
        StopFailure::Timeout { id, .. } => {
            detail.insert("pids", PyValue::List(id.tree.iter().map(|item| PyValue::Int(PyInt::Small(i128::from(item.pid)))).collect()));
            (ErrorCode::Timeout, None, Some("舊 daemon 在交接後仍未結束；請確認狀態後重試".into()))
        }
        StopFailure::Hold(service) | StopFailure::Service(service) => {
            detail.insert("hint", opt_str(service.hint.as_deref()));
            (ErrorCode::InternalError, Some("autostart_hold_unverified"), service.hint.clone())
        }
        other => (ErrorCode::DeviceStillHeld, None, Some(format!("daemon 交接失敗：{other:?}"))),
    };
    if let StopFailure::Relaunched { pid, .. } = error.cause() {
        detail.insert("relaunched", PyValue::Dict(PyDict::from_pairs(vec![
            ("chain_len".into(), PyValue::Int(PyInt::Small(chain.len() as i128))),
            ("pid".into(), PyValue::Int(PyInt::Small(i128::from(*pid)))),
        ])));
    }
    OpFailure { code: code.as_str(), phase, reason, message, detail }
}

pub(crate) fn recover_autostart_hold(_ctx: &mut CliCtx) -> Result<Option<PyDict>, PyDict> {
    let path = autostart_hold_path(_ctx);
    if !_ctx.p.fs.exists(&path) {
        return Ok(None);
    }
    let bytes = _ctx.p.fs.read(&path).map_err(|error| internal_error_dict("supervision_set_recover", "autostart_hold_read", &error.py_message))?;
    let parsed = pyjson::loads(&String::from_utf8_lossy(&bytes)).map_err(|error| internal_error_dict("supervision_set_recover", "autostart_hold_corrupt", &error.to_string()))?;
    let PyValue::Dict(record) = parsed else {
        return Err(internal_error_dict("supervision_set_recover", "autostart_hold_corrupt", "autostart-hold.json 頂層必須為物件"));
    };
    if record.get("schema_version") != Some(&PyValue::Int(PyInt::Small(1)))
        || record.get("op").and_then(as_str) != Some("supervision_set")
        || record.get("old_mode").or_else(|| record.get("mode")).and_then(as_str).is_none()
        || record.get("target_mode").and_then(as_str).is_none()
    {
        let mut response = internal_error_dict("supervision_set_recover", "autostart_hold_ambiguous", "autostart-hold.json 欄位不足或版本不支援");
        response.insert("autostart_hold", PyValue::Dict(record));
        return Err(response);
    }
    let old_mode = record.get("old_mode").or_else(|| record.get("mode")).and_then(as_str).map(SupervisionMode::parse).unwrap_or(SupervisionMode::OnDemand);
    let target = record.get("target_mode").and_then(as_str).map(SupervisionMode::parse).unwrap_or(SupervisionMode::OnDemand);
    let current_mode = configured_mode_checked(_ctx).map_err(|message| internal_error_dict("supervision_set_recover", "config_read_failed", &message))?;
    let enabled = match record.get("prior_enabled") {
        Some(PyValue::Bool(value)) => Some(*value),
        _ => None,
    };
    let target_state = _ctx.p.service.query(&target);
    // on-demand 沒有監管項；仍在跑的舊 daemon 不能當成目標監管已啟動。
    let target_is_absent_or_stopped = target == SupervisionMode::OnDemand
        || matches!(target_state, TaskQuery::NotFound | TaskQuery::NotRunning(_));
    if current_mode != old_mode && current_mode != target {
        let mut response = internal_error_dict("supervision_set_recover", "autostart_hold_ambiguous", "config.yaml 的監管模式與 hold 紀錄不一致");
        response.insert("autostart_hold", PyValue::Dict(record));
        return Err(response);
    }
    if current_mode == old_mode && !target_is_absent_or_stopped {
        let mut response = internal_error_dict("supervision_set_recover", "autostart_hold_ambiguous", "設定仍指向舊模式，但目標監管項已在執行或狀態不明");
        response.insert("autostart_hold", PyValue::Dict(record));
        return Err(response);
    }
    let hold = hold_from_record(&record, old_mode.clone());
    let restore_enabled = (current_mode == old_mode).then_some(enabled).flatten();
    _ctx.p.service.release_autostart(&hold, restore_enabled, record.get_truthy("with_sudo")).map_err(|error| {
        internal_error_dict("supervision_set_recover", "autostart_release_failed", error.hint.as_deref().unwrap_or("無法撤銷自動啟動 hold"))
    })?;
    remove_hold_file(_ctx, &path).map_err(|error| internal_error_dict("supervision_set_recover", "autostart_release_failed", &error))?;
    let mut result = PyDict::new();
    result.insert("old_mode", PyValue::Str(old_mode.as_str().into()));
    result.insert("target_mode", PyValue::Str(target.as_str().into()));
    result.insert("action", PyValue::Str(if current_mode == old_mode { "restored" } else { "released_only" }.into()));
    Ok(Some(result))
}

fn static_phase(value: &str) -> Option<&'static str> {
    Some(match value {
        "install_lock" => "install_lock",
        "update_verify" => "update_verify",
        "rollback_verify" => "rollback_verify",
        "update_safety_check" => "update_safety_check",
        "update_autostart_hold" => "update_autostart_hold",
        "update_confirm_released" => "update_confirm_released",
        "update_journal" => "update_journal",
        "update_start_and_verify" => "update_start_and_verify",
        "update_revert" => "update_revert",
        "install_recover" => "install_recover",
        _ => return None,
    })
}

fn layout_failure(
    op: &'static str,
    from: Option<String>,
    to: String,
    step: StepName,
    phase: &'static str,
    error: LayoutError,
) -> UpdateReport {
    let (code, reason, message, detail) = layout_op_parts(error, phase);
    let mut report = new_report(op, from, to);
    fail_report(&mut report, step, OpFailure { code, phase, reason, message, detail });
    report
}

fn layout_op_failure(error: LayoutError, phase: &'static str) -> OpFailure {
    let (code, reason, message, detail) = layout_op_parts(error, phase);
    OpFailure { code, phase, reason, message, detail }
}

fn layout_op_parts(error: LayoutError, phase: &'static str) -> (&'static str, Option<&'static str>, Option<String>, PyDict) {
    match error {
        LayoutError::BundleVerificationFailed { diffs, .. } => {
            let mut detail = PyDict::new();
            detail.insert("diffs", diffs_value(&diffs));
            (ErrorCode::ChecksumVerifyFailed.as_str(), None, None, detail)
        }
        LayoutError::ExistingCorrupt(path) => {
            let mut detail = PyDict::new();
            detail.insert("path", str_value(path.display().to_string()));
            (ErrorCode::ChecksumVerifyFailed.as_str(), None, Some("已存在的版本目錄驗證失敗，拒絕覆寫".into()), detail)
        }
        LayoutError::ManifestNotFound(message) | LayoutError::Parse { message, .. } => {
            let mut detail = PyDict::new();
            detail.insert("message", str_value(message.clone()));
            (ErrorCode::InvalidArgs.as_str(), Some("manifest_invalid"), Some(message), detail)
        }
        LayoutError::Io { path, message } => {
            let mut detail = PyDict::new();
            detail.insert("path", str_value(path.display().to_string()));
            detail.insert("message", str_value(message.clone()));
            (ErrorCode::InternalError.as_str(), Some(if phase == "update_journal" { "journal_write_failed" } else { "install_io" }), Some(message), detail)
        }
        LayoutError::RevertEntriesFailed(errors) => {
            let mut detail = PyDict::new();
            detail.insert("errors", PyValue::List(errors.iter().map(|error| str_value(error.to_string())).collect()));
            (ErrorCode::InternalError.as_str(), Some("pairing_mismatch"), Some("安裝入口無法完整還原".into()), detail)
        }
        LayoutError::RevertFailed { original, revert } => {
            let mut detail = PyDict::new();
            detail.insert("original", str_value(original.to_string()));
            detail.insert("revert", str_value(revert.to_string()));
            (ErrorCode::InternalError.as_str(), Some("pairing_mismatch"), Some("安裝入口切換失敗且回復未完成".into()), detail)
        }
    }
}

fn journal_failure(mut report: UpdateReport, step: StepName, journal_step: &'static str, error: LayoutError) -> UpdateReport {
    let mut detail = PyDict::new();
    detail.insert("journal_step", str_value(journal_step));
    detail.insert("message", str_value(error.to_string()));
    fail_report(&mut report, step, OpFailure {
        code: ErrorCode::InternalError.as_str(),
        phase: "update_journal",
        reason: Some("journal_write_failed"),
        message: Some(error.to_string()),
        detail,
    });
    report
}

fn clear_journal(ctx: &CliCtx<'_>, root: &InstallRoot, _journal: &Journal) {
    let path = root.root.join("install-journal.json");
    if ctx.p.fs.exists(&path) { let _ = ctx.p.fs.remove_file(&path); }
    let snapshot = root.root.join("install-journal.snapshot");
    if ctx.p.fs.exists(&snapshot) { let _ = ctx.p.fs.remove_tree(&snapshot); }
    let _ = ctx.p.fs.sync_dir(&root.root);
}

fn version_dir(root: &InstallRoot, recorded: &str, version: &str) -> PathBuf {
    let path = PathBuf::from(recorded);
    if path.is_absolute() { path } else if recorded.starts_with("versions/") || recorded.starts_with("versions\\") {
        root.versions.join(version)
    } else {
        root.versions.join(version)
    }
}

fn read_manifest_if_present(ctx: &CliCtx<'_>, dir: &Path) -> Result<Option<(PathBuf, sw_proto::release_manifest::ReleaseManifest)>, LayoutError> {
    let path = dir.join("release-manifest.json");
    if !ctx.p.fs.exists(&path) { return Ok(None); }
    let bytes = ctx.p.fs.read(&path).map_err(|error| layout_io(&path, error.py_message))?;
    let text = String::from_utf8(bytes).map_err(|error| layout_io(&path, error.to_string()))?;
    let manifest = sw_proto::release_manifest::parse_manifest(&text).map_err(|error| LayoutError::Parse { path: path.clone(), message: error.to_string() })?;
    Ok(Some((path, manifest)))
}

fn load_bundle_manifest(ctx: &CliCtx<'_>, dir: &Path) -> Result<(PathBuf, sw_proto::release_manifest::ReleaseManifest), LayoutError> {
    let path = if ctx.p.fs.is_file(dir) { dir.to_path_buf() } else { dir.join("release-manifest.json") };
    if !ctx.p.fs.exists(&path) {
        return Err(LayoutError::ManifestNotFound(format!("bundle 缺少 release-manifest.json：{}", path.display())));
    }
    let bytes = ctx.p.fs.read(&path).map_err(|error| layout_io(&path, error.py_message))?;
    let text = String::from_utf8(bytes).map_err(|error| layout_io(&path, error.to_string()))?;
    let manifest = sw_proto::release_manifest::parse_manifest(&text).map_err(|error| LayoutError::Parse { path: path.clone(), message: error.to_string() })?;
    Ok((path, manifest))
}

fn schemas_incompatible(current: Option<&str>, target: Option<&str>) -> bool {
    match (current, target) {
        (Some(current), Some(target)) => current != target,
        _ => true,
    }
}

fn schema_failure(phase: &'static str, current: Option<&str>, target: Option<&str>) -> OpFailure {
    let mut detail = PyDict::new();
    detail.insert("current_state_schema", opt_str(current));
    detail.insert("target_state_schema", opt_str(target));
    OpFailure {
        code: ErrorCode::InvalidArgs.as_str(),
        phase,
        reason: Some("state_schema_incompatible"),
        message: Some("目前版本與目標版本的 state schema 不相容".into()),
        detail,
    }
}

fn diffs_value(diffs: &[install_layout::PairingDiff]) -> PyValue {
    PyValue::List(diffs.iter().map(|diff| PyValue::Dict(PyDict::from_pairs(vec![
        ("actual".into(), str_value(diff.actual.clone())),
        ("expected".into(), str_value(diff.expected.clone())),
        ("field".into(), str_value(diff.field.clone())),
    ]))).collect())
}

fn snapshot_hashes(ctx: &CliCtx<'_>, dir: &Path) -> Result<Vec<(String, String)>, LayoutError> {
    let mut files = Vec::new();
    collect_files(ctx, dir, dir, &mut files)?;
    let mut hashes = Vec::with_capacity(files.len());
    for (relative, path) in files {
        hashes.push((relative, hash_path(ctx, &path)?));
    }
    hashes.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    Ok(hashes)
}

fn allocate_backup_dir(ctx: &CliCtx<'_>, root: &InstallRoot, from: Option<&str>, to: &str) -> PathBuf {
    let stem = format!("{}-to-{to}-{}", from.unwrap_or("none"), utc_compact_timestamp());
    let mut path = root.backups.join(&stem);
    let mut suffix = 1u32;
    while ctx.p.fs.exists(&path) {
        path = root.backups.join(format!("{stem}-{suffix:03}"));
        suffix += 1;
    }
    path
}

fn utc_compact_timestamp() -> String {
    let seconds = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    let days = (seconds / 86_400) as i64;
    let day_seconds = seconds % 86_400;
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let day_of_era = z - era * 146_097;
    let year_of_era = (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += if month <= 2 { 1 } else { 0 };
    let hour = day_seconds / 3_600;
    let minute = (day_seconds % 3_600) / 60;
    let second = day_seconds % 60;
    format!("{year:04}{month:02}{day:02}-{hour:02}{minute:02}{second:02}")
}

fn current_release(version: &str, dir: &Path, current: Option<&CurrentRelease>, backup: &Path) -> CurrentRelease {
    let recorded_dir = format!("versions/{version}");
    CurrentRelease {
        bundle_version: version.to_string(),
        dir: recorded_dir,
        previous: current.map(|current| install_layout::ReleaseRef {
            bundle_version: current.bundle_version.clone(),
            dir: current.dir.clone(),
        }),
        backup_dir: Some(backup.to_string_lossy().into_owned()),
        switched_at: SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs().to_string(),
    }
}

fn query_supervisor(ctx: &CliCtx<'_>, mode: &SupervisionMode, endpoint: &str, daemon_running: bool) -> Result<SupervisorSnapshot, OpFailure> {
    if *mode == SupervisionMode::OnDemand {
        return Ok(SupervisorSnapshot { mode: mode.clone(), endpoint: endpoint.to_string(), enabled: None, running: daemon_running, registration: None });
    }
    let state = ctx.p.service.query(mode);
    let enabled = mode_enabled(&state);
    let running = matches!(state, TaskQuery::Running);
    if let TaskQuery::QueryFailed(message) = state {
        return Err(OpFailure { code: ErrorCode::InternalError.as_str(), phase: "update_safety_check", reason: Some("supervisor_query_failed"), message: Some(message), detail: PyDict::new() });
    }
    let root = install_layout::resolve_install_root(ctx);
    let registration = match mode {
        SupervisionMode::WindowsTask => Some(root.supervisor.join("serialwrap.task.xml")),
        SupervisionMode::SystemdUser => Some(systemd_user_unit_path(ctx)),
        SupervisionMode::SystemdSystem => Some(PathBuf::from("/etc/systemd/system/serialwrap.service")),
        _ => None,
    };
    Ok(SupervisorSnapshot { mode: mode.clone(), endpoint: endpoint.to_string(), enabled, running: running || daemon_running, registration })
}

enum EndpointPresence {
    Present(Vec<u32>),
    Absent,
    Unknown,
}

fn endpoint_presence(ctx: &CliCtx<'_>, endpoint: &str) -> EndpointPresence {
    if rpc(ctx, endpoint, "health.ping", 0.25).is_some_and(|response| response.get_truthy("ok")) {
        return EndpointPresence::Present(Vec::new());
    }
    match ctx.p.procs.serialwrapd_instances() {
        Ok(daemons) => {
            let pids = matching_daemons(ctx, endpoint, &daemons).iter().map(|daemon| daemon.pid).collect::<Vec<_>>();
            if pids.is_empty() { EndpointPresence::Absent } else { EndpointPresence::Present(pids) }
        }
        Err(_) => EndpointPresence::Unknown,
    }
}

fn confirm_released(ctx: &CliCtx<'_>, endpoint: &str, daemon: Option<&DaemonSnapshot>) -> Result<Option<&'static str>, OpFailure> {
    match endpoint_presence(ctx, endpoint) {
        EndpointPresence::Present(pids) => {
        let mut detail = PyDict::new();
        detail.insert("pids", PyValue::List(pids.into_iter().map(|pid| PyValue::Int(PyInt::Small(i128::from(pid)))).collect()));
        return Err(OpFailure { code: ErrorCode::Timeout.as_str(), phase: "update_confirm_released", reason: None, message: Some("handoff grant 後 daemon 仍可連線".into()), detail });
        }
        EndpointPresence::Unknown => {
            let mut detail = PyDict::new();
            detail.insert("holder_unknown", PyValue::Bool(true));
            return Err(OpFailure { code: ErrorCode::DeviceStillHeld.as_str(), phase: "update_confirm_released", reason: None, message: Some("無法確認 daemon 行程是否已結束".into()), detail });
        }
        EndpointPresence::Absent => {}
    }
    let mut holders_status = None;
    if let Some(snapshot) = daemon {
        if !snapshot.attached_paths.is_empty() {
            let (holders, status) = ctx.p.procs.serialwrapd_tty_holders(&snapshot.attached_paths);
            if status == sw_pal::proc_scan::HolderScanStatus::Unsupported {
                holders_status = Some("unsupported");
            }
            if !holders.is_empty() || !matches!(status, sw_pal::proc_scan::HolderScanStatus::Ok | sw_pal::proc_scan::HolderScanStatus::Unsupported) {
                let mut detail = PyDict::new();
                detail.insert("pids", PyValue::List(holders.iter().map(|pid| PyValue::Int(PyInt::Small(i128::from(*pid)))).collect()));
                detail.insert("holder_unknown", PyValue::Bool(!matches!(status, sw_pal::proc_scan::HolderScanStatus::Ok | sw_pal::proc_scan::HolderScanStatus::Unsupported)));
                if status == sw_pal::proc_scan::HolderScanStatus::Unsupported {
                    detail.insert("holders_status", str_value("unsupported"));
                }
                return Err(OpFailure { code: ErrorCode::DeviceStillHeld.as_str(), phase: "update_confirm_released", reason: None, message: None, detail });
            }
        }
    }
    Ok(holders_status)
}

fn autostart_plan(hold: &AutostartHoldRecord, enabled: Option<bool>) -> AutostartPlan {
    AutostartPlan {
        kind: hold.kind,
        dropin_path: hold.dropin_path.as_ref().map(|path| path.to_string_lossy().into_owned()),
        prior_enabled: hold.prior_enabled.or(enabled),
        prior_restart: hold.prior_restart.clone(),
    }
}

fn autostart_record_value(hold: &AutostartHoldRecord, enabled: Option<bool>) -> PyValue {
    PyValue::Dict(PyDict::from_pairs(vec![
        ("kind".into(), str_value(kind_value(hold.kind))),
        ("mode".into(), str_value(hold.mode.as_str().to_string())),
        ("prior_enabled".into(), opt_bool(hold.prior_enabled.or(enabled))),
        ("prior_restart".into(), opt_str(hold.prior_restart.as_deref())),
    ]))
}

fn mode_enabled(state: &TaskQuery) -> Option<bool> {
    match state {
        TaskQuery::Running => Some(true),
        TaskQuery::NotRunning(value) => Some(!value.eq_ignore_ascii_case("disabled")),
        TaskQuery::NotFound | TaskQuery::QueryFailed(_) => None,
    }
}

fn handoff_from_stop(error: &StopFailure) -> HandoffError {
    match error.cause() {
        StopFailure::Handoff(error) | StopFailure::Relaunched { err: error, .. } => error.clone(),
        StopFailure::RelaunchLoop { pids } => HandoffError::Unconfirmed { code: ErrorCode::DeviceStillHeld.as_str().into(), probe: "daemon.handoff_stop", outcome: "unknown", pids: pids.clone() },
        StopFailure::Timeout { id, .. } => HandoffError::Unconfirmed { code: ErrorCode::Timeout.as_str().into(), probe: "daemon.handoff_stop", outcome: "unknown", pids: id.tree.iter().map(|item| item.pid).collect() },
        _ => HandoffError::Unconfirmed { code: ErrorCode::InternalError.as_str().into(), probe: "daemon.handoff_stop", outcome: "unknown", pids: Vec::new() },
    }
}

fn dict_value(value: Option<&PyValue>) -> Option<&PyDict> {
    match value { Some(PyValue::Dict(dict)) => Some(dict), _ => None }
}

fn string_field(dict: &PyDict, field: &str) -> Result<String, String> {
    dict.get(field).and_then(as_str).map(str::to_owned).ok_or_else(|| format!("journal 欄位 {field} 缺少或非字串"))
}

fn optional_string_field(dict: &PyDict, field: &str) -> Result<Option<String>, String> {
    match dict.get(field) {
        None | Some(PyValue::Null) => Ok(None),
        Some(PyValue::Str(value)) => Ok(Some(value.clone())),
        Some(_) => Err(format!("journal 欄位 {field} 必須為字串或 null")),
    }
}

fn optional_bool_field(dict: &PyDict, field: &str) -> Result<Option<bool>, String> {
    match dict.get(field) {
        None | Some(PyValue::Null) => Ok(None),
        Some(PyValue::Bool(value)) => Ok(Some(*value)),
        Some(_) => Err(format!("journal 欄位 {field} 必須為布林或 null")),
    }
}

fn required_bool_field(dict: &PyDict, field: &str) -> Result<bool, String> {
    match dict.get(field) {
        Some(PyValue::Bool(value)) => Ok(*value),
        _ => Err(format!("journal 欄位 {field} 缺少或非布林")),
    }
}

fn optional_u64(value: Option<&PyValue>) -> Option<u64> {
    match value {
        Some(PyValue::Int(PyInt::Small(number))) => u64::try_from(*number).ok(),
        Some(PyValue::Int(PyInt::Big(number))) => number.parse().ok(),
        _ => None,
    }
}

fn parse_journal(value: &PyDict) -> Result<Journal, String> {
    let step_text = string_field(value, "step")?;
    let step = match step_text.as_str() {
        "lock" => JournalStep::Lock,
        "verify" => JournalStep::Verify,
        "safety_check" => JournalStep::SafetyCheck,
        "snapshot_taken" => JournalStep::SnapshotTaken,
        "stop_supervisor" => JournalStep::StopSupervisor,
        "confirm_released" => JournalStep::ConfirmReleased,
        "backup_started" => JournalStep::BackupStarted,
        "backup_complete" => JournalStep::BackupComplete,
        "entries_switching" => JournalStep::EntriesSwitching,
        "start_and_verify" => JournalStep::StartAndVerify,
        "revert_to_previous" => JournalStep::RevertToPrevious,
        _ => return Err(format!("journal step 不支援：{step_text}")),
    };
    let snapshot_sha256 = match value.get("snapshot_sha256") {
        None | Some(PyValue::Null) => None,
        Some(PyValue::List(items)) => {
            let mut parsed = Vec::with_capacity(items.len());
            for item in items {
                let PyValue::List(pair) = item else { return Err("snapshot_sha256 每項必須為陣列".into()); };
                if pair.len() != 2 { return Err("snapshot_sha256 每項必須有 path/hash 兩欄".into()); }
                let (Some(name), Some(hash)) = (as_str(&pair[0]), as_str(&pair[1])) else { return Err("snapshot_sha256 path/hash 必須為字串".into()); };
                parsed.push((name.to_string(), hash.to_string()));
            }
            Some(parsed)
        }
        Some(_) => return Err("snapshot_sha256 必須為陣列或 null".into()),
    };
    Ok(Journal {
        op: string_field(value, "op")?,
        from: optional_string_field(value, "from")?,
        to: string_field(value, "to")?,
        step,
        started_at: string_field(value, "started_at")?,
        original: match value.get("original") {
            None | Some(PyValue::Null) => None,
            Some(_) => Some(parse_original(dict_value(value.get("original")).ok_or("original 必須為物件")?)?),
        },
        snapshot_dir: optional_string_field(value, "snapshot_dir")?,
        snapshot_sha256,
        backup_dir: optional_string_field(value, "backup_dir")?,
        backup_manifest_sha256: optional_string_field(value, "backup_manifest_sha256")?,
        target_autostart: match dict_value(value.get("target_autostart")) {
            Some(target) => Some(parse_autostart_plan(target)?),
            None => None,
        },
    })
}

fn parse_original(value: &PyDict) -> Result<OriginalSnapshot, String> {
    let supervisor = dict_value(value.get("supervisor")).ok_or("original.supervisor 必須為物件")?;
    let mode = SupervisionMode::parse(&string_field(supervisor, "mode")?);
    let registration = optional_string_field(supervisor, "registration")?.map(PathBuf::from);
    let supervisor = SupervisorSnapshot {
        mode,
        endpoint: string_field(supervisor, "endpoint")?,
        enabled: optional_bool_field(supervisor, "enabled")?,
        running: required_bool_field(supervisor, "running")?,
        registration,
    };
    let entries = match value.get("entries") {
        Some(PyValue::List(entries)) => entries.iter().map(parse_entry).collect::<Result<Vec<_>, _>>()?,
        _ => return Err("original.entries 必須為陣列".into()),
    };
    let daemon_start = match value.get("daemon_start").and_then(as_str) {
        Some("none") => "none",
        Some("on_demand_internal") => "on_demand_internal",
        Some("supervised") => "supervised",
        _ => return Err("original.daemon_start 無效".into()),
    };
    let autostart = parse_autostart_plan(dict_value(value.get("autostart")).ok_or("original.autostart 必須為物件")?)?;
    Ok(OriginalSnapshot {
        current: match value.get("current") {
            None | Some(PyValue::Null) => None,
            Some(PyValue::Dict(current)) => Some(parse_current(current)?),
            _ => return Err("original.current 必須為物件或 null".into()),
        },
        entries,
        config_sha256: optional_string_field(value, "config_sha256")?,
        supervisor,
        daemon: match value.get("daemon") {
            None | Some(PyValue::Null) => None,
            Some(PyValue::Dict(daemon)) => Some(parse_daemon(daemon)?),
            _ => return Err("original.daemon 必須為物件或 null".into()),
        },
        daemon_start,
        autostart,
    })
}

fn parse_current(value: &PyDict) -> Result<CurrentRelease, String> {
    let previous = match value.get("previous") {
        None | Some(PyValue::Null) => None,
        Some(PyValue::Dict(previous)) => Some(install_layout::ReleaseRef {
            bundle_version: string_field(previous, "bundle_version")?,
            dir: string_field(previous, "dir")?,
        }),
        _ => return Err("current.previous 必須為物件或 null".into()),
    };
    Ok(CurrentRelease {
        bundle_version: string_field(value, "bundle_version")?,
        dir: string_field(value, "dir")?,
        previous,
        backup_dir: optional_string_field(value, "backup_dir")?,
        switched_at: string_field(value, "switched_at")?,
    })
}

fn parse_entry(value: &PyValue) -> Result<EntrySnapshot, String> {
    let Some(dict) = dict_value(Some(value)) else { return Err("entry snapshot 必須為物件".into()); };
    let name = string_field(dict, "name")?;
    match string_field(dict, "kind")?.as_str() {
        "symlink" => Ok(EntrySnapshot::Symlink { name, target: string_field(dict, "target")? }),
        "copy" => Ok(EntrySnapshot::Copy { name, sha256: string_field(dict, "sha256")?, size: optional_u64(dict.get("size")).ok_or("entry size 必須為非負整數")? }),
        "absent" => Ok(EntrySnapshot::Absent { name }),
        kind => Err(format!("entry kind 不支援：{kind}")),
    }
}

fn parse_autostart_plan(value: &PyDict) -> Result<AutostartPlan, String> {
    let mode = value.get("mode").and_then(as_str).map(SupervisionMode::parse).unwrap_or(SupervisionMode::OnDemand);
    let kind = match value.get("kind").and_then(as_str) {
        Some("systemd_dropin") => AutostartHoldKind::SystemdDropIn,
        Some("task_disabled") => AutostartHoldKind::TaskDisabled,
        Some("service_disabled") => AutostartHoldKind::ServiceDisabled,
        Some("no_supervisor") => AutostartHoldKind::NoSupervisor,
        _ => hold_kind(&mode),
    };
    Ok(AutostartPlan {
        kind,
        dropin_path: optional_string_field(value, "dropin_path")?,
        prior_enabled: optional_bool_field(value, "prior_enabled")?,
        prior_restart: optional_string_field(value, "prior_restart")?,
    })
}

fn parse_daemon(value: &PyDict) -> Result<DaemonSnapshot, String> {
    let id = dict_value(value.get("id")).ok_or("daemon.id 必須為物件")?;
    let mut tree = Vec::new();
    if let Some(PyValue::List(items)) = id.get("tree") {
        for item in items {
            let item = dict_value(Some(item)).ok_or("daemon.id.tree 項目必須為物件")?;
            let pid = as_u32(item.get("pid").ok_or("daemon pid 缺少")?).ok_or("daemon pid 無效")?;
            tree.push(ProcIdentity { pid, start_ticks: optional_u64(item.get("start_ticks")) });
        }
    }
    let attached_paths = match value.get("attached_paths") {
        Some(PyValue::List(items)) => items.iter().map(|item| as_str(item).map(PathBuf::from).ok_or("daemon attached path 必須為字串".to_string())).collect::<Result<Vec<_>, _>>()?,
        _ => return Err("daemon.attached_paths 必須為陣列".into()),
    };
    let sessions = match value.get("sessions") {
        Some(PyValue::List(items)) => items.iter().map(|item| dict_value(Some(item)).cloned().ok_or("daemon session 必須為物件".to_string())).collect::<Result<Vec<_>, _>>()?,
        _ => return Err("daemon.sessions 必須為陣列".into()),
    };
    Ok(DaemonSnapshot {
        id: DaemonIdentity { pid: id.get("pid").and_then(as_u32), tree, known: required_bool_field(id, "known")? },
        attached_paths,
        sessions,
    })
}

fn recovery_error(from_step: &str, code: &'static str, reason: &'static str, message: impl Into<String>) -> RecoveryReport {
    let mut detail = PyDict::new();
    detail.insert("message", str_value(message.into()));
    RecoveryReport {
        ok: false,
        from_step: recovery_step(from_step),
        backup_used: false,
        journal_kept: true,
        restored: PyDict::new(),
        error_code: Some(code),
        reason: Some(reason.into()),
        message: detail.get("message").and_then(as_str).map(str::to_owned),
        detail,
    }
}

fn recovery_error_from_op(from_step: &str, reason: &'static str, failure: OpFailure) -> RecoveryReport {
    let mut report = recovery_error(from_step, failure.code, reason, failure.message.unwrap_or_else(|| "復原前仍有序列埠持有狀態，保留 journal".into()));
    for (key, value) in failure.detail.0 {
        report.detail.insert(&key, value);
    }
    report
}

fn op_failure_detail(failure: &OpFailure) -> PyDict {
    let mut detail = failure.detail.clone();
    detail.insert("error_code", str_value(failure.code));
    detail.insert("phase", str_value(failure.phase));
    detail.insert("reason", opt_str(failure.reason));
    detail.insert("message", opt_str(failure.message.as_deref()));
    detail
}

fn recovery_step(value: &str) -> &'static str {
    match value {
        "lock" => "lock",
        "verify" => "verify",
        "safety_check" => "safety_check",
        "snapshot_taken" => "snapshot_taken",
        "stop_supervisor" => "stop_supervisor",
        "confirm_released" => "confirm_released",
        "backup_started" => "backup_started",
        "backup_complete" => "backup_complete",
        "entries_switching" => "entries_switching",
        "start_and_verify" => "start_and_verify",
        "revert_to_previous" => "revert_to_previous",
        _ => "unknown",
    }
}

fn finish_recovery(
    ctx: &mut CliCtx<'_>,
    root: &InstallRoot,
    journal_path: &Path,
    journal: &Journal,
    backup_used: bool,
    restored: PyDict,
) -> Option<RecoveryReport> {
    if ctx.p.fs.exists(journal_path) {
        if let Err(error) = ctx.p.fs.remove_file(journal_path) {
            return Some(recovery_error(journal.step.as_str(), ErrorCode::InternalError.as_str(), "journal_cleanup_failed", error.py_message));
        }
    }
    let snapshot = root.root.join("install-journal.snapshot");
    if ctx.p.fs.exists(&snapshot) {
        if let Err(error) = ctx.p.fs.remove_tree(&snapshot) {
            return Some(recovery_error(journal.step.as_str(), ErrorCode::InternalError.as_str(), "snapshot_cleanup_failed", error.py_message));
        }
    }
    let _ = ctx.p.fs.sync_dir(&root.root);
    Some(RecoveryReport {
        ok: true,
        from_step: journal.step.as_str(),
        backup_used,
        journal_kept: false,
        restored,
        error_code: None,
        reason: None,
        message: None,
        detail: PyDict::new(),
    })
}

fn verify_snapshot(ctx: &CliCtx<'_>, journal: &Journal) -> Result<(), (&'static str, String)> {
    let dir = journal.snapshot_dir.as_deref().map(PathBuf::from)
        .ok_or(("snapshot_missing", "journal 缺少 snapshot_dir".to_string()))?;
    let expected = journal.snapshot_sha256.as_ref()
        .ok_or(("snapshot_missing", "journal 缺少 snapshot_sha256".to_string()))?;
    if !ctx.p.fs.is_dir(&dir) { return Err(("snapshot_missing", "原始快照目錄不存在".into())); }
    let actual = snapshot_hashes(ctx, &dir).map_err(|error| ("snapshot_mismatch", error.to_string()))?;
    if actual != *expected { return Err(("snapshot_mismatch", "原始快照檔案清單或 SHA-256 不一致".into())); }
    Ok(())
}

fn original_install_state_matches(
    ctx: &CliCtx<'_>,
    root: &InstallRoot,
    original: &OriginalSnapshot,
) -> Result<bool, String> {
    let current = install_layout::read_current(root).map_err(|error| error.to_string())?;
    if current != original.current || capture_entries(ctx, root).map_err(|error| error.to_string())? != original.entries {
        return Ok(false);
    }
    let config_path = crate::endpoint_resolve::config_write_path(ctx);
    match (&original.config_sha256, ctx.p.fs.exists(&config_path)) {
        (None, false) => Ok(true),
        (Some(expected), true) => {
            let bytes = ctx.p.fs.read(&config_path).map_err(|error| error.py_message)?;
            Ok(sha256_hex(&bytes) == *expected)
        }
        _ => Ok(false),
    }
}

fn verified_backup(ctx: &CliCtx<'_>, journal: &Journal) -> Result<PathBuf, String> {
    let dir = journal.backup_dir.as_deref().map(PathBuf::from).ok_or("journal 缺少 backup_dir")?;
    let manifest_path = dir.join("manifest.json");
    let expected_manifest = journal.backup_manifest_sha256.as_deref().ok_or("journal 缺少 backup_manifest_sha256")?;
    let actual_manifest = hash_path(ctx, &manifest_path).map_err(|error| error.to_string())?;
    if actual_manifest != expected_manifest { return Err("備份 manifest SHA-256 不一致".into()); }
    let bytes = ctx.p.fs.read(&manifest_path).map_err(|error| error.py_message)?;
    let parsed = pyjson::loads(&String::from_utf8_lossy(&bytes)).map_err(|error| error.to_string())?;
    let PyValue::Dict(manifest) = parsed else { return Err("備份 manifest 頂層必須為物件".into()); };
    let Some(PyValue::List(files)) = manifest.get("files") else { return Err("備份 manifest 缺少 files 清單".into()); };
    for item in files {
        let Some(item) = dict_value(Some(item)) else { return Err("備份 manifest files 項目必須為物件".into()); };
        let relative = string_field(item, "relative")?;
        let expected = string_field(item, "sha256")?;
        let path = dir.join(relative);
        let actual = hash_path(ctx, &path).map_err(|error| error.to_string())?;
        if actual != expected { return Err(format!("備份檔案雜湊不符：{}", path.display())); }
    }
    Ok(dir)
}

fn read_backup_set(ctx: &CliCtx<'_>, root: &InstallRoot, dir: &Path) -> Result<BackupSet, String> {
    if !is_path_within(dir, &root.backups, ctx.p.caps.path_flavor == sw_pal::PalPathFlavor::Windows) {
        return Err("Previous 備份目錄不在 install root/backups 內".into());
    }
    let manifest_path = dir.join("manifest.json");
    let bytes = ctx.p.fs.read(&manifest_path).map_err(|error| error.py_message)?;
    let parsed = pyjson::loads(&String::from_utf8_lossy(&bytes)).map_err(|error| error.to_string())?;
    let PyValue::Dict(manifest) = parsed else { return Err("備份 manifest 頂層必須為物件".into()); };
    let Some(PyValue::List(files)) = manifest.get("files") else { return Err("備份 manifest 缺少 files 清單".into()); };
    for item in files {
        let Some(item) = dict_value(Some(item)) else { return Err("備份 manifest files 項目必須為物件".into()); };
        let relative = string_field(item, "relative")?;
        let relative_path = Path::new(&relative);
        if relative_path.is_absolute() || relative_path.components().any(|part| matches!(part, std::path::Component::ParentDir | std::path::Component::RootDir | std::path::Component::Prefix(_))) {
            return Err("備份 manifest 含有越界檔案路徑".into());
        }
        let expected = string_field(item, "sha256")?;
        let path = dir.join(relative_path);
        let actual = hash_path(ctx, &path).map_err(|error| error.to_string())?;
        if actual != expected { return Err(format!("備份檔案雜湊不符：{}", path.display())); }
    }
    let registration_path = dir.join("registration.json");
    let registration_bytes = ctx.p.fs.read(&registration_path).map_err(|error| error.py_message)?;
    let registration = pyjson::loads(&String::from_utf8_lossy(&registration_bytes)).map_err(|error| error.to_string())?;
    let PyValue::Dict(registration) = registration else { return Err("registration.json 頂層必須為物件".into()); };
    let mode_name = string_field(&registration, "mode")?;
    let mode = SupervisionMode::parse(&mode_name);
    let supervisor = SupervisorSnapshot {
        mode,
        endpoint: string_field(&registration, "endpoint")?,
        enabled: optional_bool_field(&registration, "enabled")?,
        running: required_bool_field(&registration, "running")?,
        registration: optional_string_field(&registration, "registration")?.map(PathBuf::from),
    };
    Ok(BackupSet { dir: dir.to_path_buf(), supervisor })
}

fn is_path_within(path: &Path, base: &Path, windows: bool) -> bool {
    if !windows { return path.starts_with(base); }
    let path = path.to_string_lossy().replace('/', "\\");
    let base = base.to_string_lossy().replace('/', "\\").trim_end_matches('\\').to_string();
    path == base || path.strip_prefix(&base).is_some_and(|suffix| suffix.starts_with('\\'))
}

fn preserve_partial_backup(ctx: &CliCtx<'_>, journal: &Journal) {
    let Some(path) = journal.backup_dir.as_deref().map(PathBuf::from) else { return; };
    let partial = PathBuf::from(format!("{}.partial", path.display()));
    let incomplete = PathBuf::from(format!("{}.incomplete", path.display()));
    if ctx.p.fs.exists(&partial) && !ctx.p.fs.exists(&incomplete) {
        let _ = ctx.p.fs.replace(&partial, &incomplete);
    }
}

fn restore_original(ctx: &mut CliCtx<'_>, root: &InstallRoot, journal: &Journal, backup: Option<&Path>) -> Result<(), String> {
    let original = journal.original.as_ref().ok_or("journal 缺少 original snapshot")?;
    let snapshot = journal.snapshot_dir.as_deref().map(PathBuf::from).ok_or("journal 缺少 snapshot_dir")?;
    let source = backup.unwrap_or(&snapshot);
    // state.json 與 benches.state.json 可能在新版運作期間更新（例如 RELEASED）；
    // 回退只還原設定，不可用舊備份覆蓋目前的持久化狀態。
    for (name, target) in [("config.yaml", crate::endpoint_resolve::config_write_path(ctx))] {
        let from = source.join(name);
        if ctx.p.fs.exists(&from) {
            copy_replace(ctx, &from, &target).map_err(|error| error.to_string())?;
        } else if name == "config.yaml" && ctx.p.fs.exists(&snapshot.join(name)) {
            copy_replace(ctx, &snapshot.join(name), &target).map_err(|error| error.to_string())?;
        }
    }
    for name in ["profiles", "supervisor"] {
        let from = source.join(name);
        if ctx.p.fs.is_dir(&from) {
            let target = if name == "profiles" { PathBuf::from(&ctx.paths.config_dir).join(name) } else { root.supervisor.clone() };
            if ctx.p.fs.exists(&target) { ctx.p.fs.remove_tree(&target).map_err(|error| error.py_message)?; }
            ctx.p.fs.copy_tree(&from, &target).map_err(|error| error.py_message)?;
            sync_tree(ctx, &target).map_err(|error| error.to_string())?;
        }
    }
    let current_snapshot = snapshot.join("current-release.json");
    if ctx.p.fs.exists(&current_snapshot) {
        copy_replace(ctx, &current_snapshot, &root.current_file).map_err(|error| error.to_string())?;
    } else if ctx.p.fs.exists(&root.current_file) {
        ctx.p.fs.remove_file(&root.current_file).map_err(|error| error.py_message)?;
    }
    let entry_snapshot = snapshot.join("entries");
    for entry in &original.entries {
        let name = match entry { EntrySnapshot::Symlink { name, .. } | EntrySnapshot::Copy { name, .. } | EntrySnapshot::Absent { name } => name };
        let target = root.bin.join(name);
        if ctx.p.fs.exists(&target) || ctx.p.fs.read_link(&target).map_err(|error| error.py_message)?.is_some() {
            ctx.p.fs.remove_file(&target).map_err(|error| error.py_message)?;
        }
        match entry {
            EntrySnapshot::Absent { .. } => {}
            EntrySnapshot::Copy { name, .. } => copy_replace(ctx, &entry_snapshot.join(name), &target).map_err(|error| error.to_string())?,
            EntrySnapshot::Symlink { name, target: link } => {
                if let Some(parent) = target.parent() { ctx.p.fs.mkdirs(parent).map_err(|error| error.py_message)?; }
                let temp = target.with_extension("restore.tmp");
                if ctx.p.fs.exists(&temp) { ctx.p.fs.remove_file(&temp).map_err(|error| error.py_message)?; }
                ctx.p.fs.symlink_file(&temp, Path::new(link)).map_err(|error| error.py_message)?;
                ctx.p.fs.replace(&temp, &target).map_err(|error| error.py_message)?;
                let _ = name;
            }
        }
    }
    let _ = original;
    Ok(())
}

fn restore_supervisor_and_daemon(
    ctx: &mut CliCtx<'_>,
    root: &InstallRoot,
    original: &OriginalSnapshot,
    backup: Option<&Path>,
    _handoff: Option<&str>,
    target_hold: Option<&AutostartHoldRecord>,
    with_sudo: bool,
) -> Result<PyDict, String> {
    let registration_dir = backup.map(Path::to_path_buf).unwrap_or_else(|| journal_snapshot_path(root));
    restore_registration(ctx, &registration_dir, &original.supervisor).map_err(|error| error.to_string())?;
    crash_if("restore_supervisor_and_daemon:after_registration");
    let mode = &original.supervisor.mode;
    let active_mode = target_hold.map(|hold| &hold.mode).unwrap_or(mode);
    let mut restore_handoff_chain = Vec::new();
    graceful_stop::ensure_released_or_rehandoff(
        ctx,
        active_mode,
        &original.supervisor.endpoint,
        with_sudo,
        "revert",
        "install_recover",
        &mut restore_handoff_chain,
    )
    .map_err(|error| format!("DEVICE_STILL_HELD: 還原監管註冊後無法確認 daemon 已交接：{error:?}"))?;
    if let Some(target_hold) = target_hold.filter(|hold| hold.kind != AutostartHoldKind::NoSupervisor) {
        let restore_enabled = (target_hold.mode == *mode).then_some(original.supervisor.enabled).flatten();
        ctx.p.service.release_autostart(target_hold, restore_enabled, with_sudo)
            .map_err(|error| format!("AUTOSTART_RELEASE_FAILED:{}", error.hint.unwrap_or_else(|| "目標監管 hold 還原失敗".into())))?;
    }
    let old_hold = AutostartHoldRecord {
        mode: mode.clone(),
        kind: original.autostart.kind,
        name: None,
        dropin_path: original.autostart.dropin_path.as_deref().map(PathBuf::from),
        prior_enabled: original.autostart.prior_enabled,
        prior_restart: original.autostart.prior_restart.clone(),
        ran: Vec::new(),
    };
    if target_hold.map_or(true, |hold| hold.mode != *mode) && *mode != SupervisionMode::OnDemand {
        ctx.p.service.release_autostart(&old_hold, original.supervisor.enabled, with_sudo)
            .map_err(|error| format!("AUTOSTART_RELEASE_FAILED:{}", error.hint.unwrap_or_else(|| "原監管 hold 還原失敗".into())))?;
    }
    if mode.is_supervised() {
        if let Some(enabled) = original.supervisor.enabled {
            let records = ctx.p.service.set_enabled(mode, enabled, with_sudo);
            if records.iter().any(|record| record.rc != 0) { return Err("監管 enabled 狀態還原失敗".into()); }
        }
    }
    verify_restored(ctx, root, original, original.daemon.is_some(), original.daemon.is_some())
}

fn hold_record_from_plan(original: &OriginalSnapshot) -> AutostartHoldRecord {
    AutostartHoldRecord {
        mode: original.supervisor.mode.clone(),
        kind: original.autostart.kind,
        name: None,
        dropin_path: original.autostart.dropin_path.as_deref().map(PathBuf::from),
        prior_enabled: original.autostart.prior_enabled,
        prior_restart: original.autostart.prior_restart.clone(),
        ran: Vec::new(),
    }
}

fn journal_snapshot_path(root: &InstallRoot) -> PathBuf {
    root.root.join("install-journal.snapshot")
}

fn spawn_daemon(ctx: &mut CliCtx<'_>, root: &InstallRoot, endpoint: &str) -> Result<sw_pal::traits::ChildHandle, String> {
    let profile_dir = ctx.paths.profile_dir.clone();
    let located = crate::commands::daemon_cmd::locate_daemon_argv(ctx).ok();
    let stable_name = if ctx.p.caps.path_flavor == sw_pal::PalPathFlavor::Windows { "serialwrapd.exe" } else { "serialwrapd" };
    let stable_daemon = root.bin.join(stable_name);
    let mut argv = if ctx.p.fs.is_file(&stable_daemon) {
        vec![stable_daemon.to_string_lossy().into_owned()]
    } else {
        located.ok_or_else(|| "找不到可啟動的 serialwrapd".to_string())?
    };
    argv.extend([
        "--profile-dir".into(), profile_dir.clone(),
        "--socket".into(), endpoint.into(),
        "--lock".into(), ctx.paths.lock_path.clone(),
    ]);
    let env_files = crate::commands::daemon_cmd::resolve_daemon_start_env_files(ctx, &profile_dir);
    let (daemon_env, _) = crate::commands::daemon_cmd::load_daemon_env(ctx, &env_files)
        .map_err(|failure| format!("無法載入 daemon 環境 {}：{}", failure.path, failure.message))?;
    ctx.p.process.spawn_daemon(&argv, &daemon_env, true)
        .map_err(|error| format!("DAEMON_EXITED:{}", error.py_message))
}

fn restored_value(original: &OriginalSnapshot) -> PyDict {
    let mut supervisor = PyDict::new();
    supervisor.insert("enabled", opt_bool(original.supervisor.enabled));
    supervisor.insert("mode", str_value(original.supervisor.mode.as_str().to_string()));
    supervisor.insert("running", PyValue::Bool(original.supervisor.running));
    let mut restored = PyDict::new();
    restored.insert("supervisor", PyValue::Dict(supervisor));
    restored
}

fn verify_restored(
    ctx: &mut CliCtx<'_>,
    root: &InstallRoot,
    original: &OriginalSnapshot,
    should_start: bool,
    start_if_needed: bool,
) -> Result<PyDict, String> {
    let mut restored = restored_value(original);
    let Some(current) = original.current.as_ref() else {
        if !matches!(endpoint_presence(ctx, &original.supervisor.endpoint), EndpointPresence::Absent) {
            return Err("原始安裝沒有 current，但 daemon 仍存在或行程狀態不明".into());
        }
        return Ok(restored);
    };
    let directory = version_dir(root, &current.dir, &current.bundle_version);
    let (_, manifest) = read_manifest_if_present(ctx, &directory)
        .map_err(|error| error.to_string())?
        .ok_or_else(|| "原始版本缺少 release-manifest.json".to_string())?;
    let measured = start_and_verify(ctx, root, &original.supervisor.mode, &original.supervisor.endpoint,
        should_start, start_if_needed, &manifest, false, original.daemon.as_ref())
        .map_err(|failure| {
            let message = failure.message.unwrap_or_else(|| failure.code.into());
            if failure.code == ErrorCode::DaemonExited.as_str() {
                format!("DAEMON_EXITED:{message}")
            } else {
                let expected = failure.detail.get("expected_version").and_then(as_str).map(|value| format!(" expected_version={value}")).unwrap_or_default();
                let actual = failure.detail.get("actual_version").and_then(as_str).map(|value| format!(" actual_version={value}")).unwrap_or_default();
                format!("{}: {message}{expected}{actual}", failure.reason.unwrap_or("restore_verify_failed"))
            }
        })?;
    if let Some(version) = measured.get("daemon_version").cloned() {
        restored.insert("daemon_version", version);
    }
    restored.insert("started", measured.get("started").cloned().unwrap_or(PyValue::Bool(false)));
    Ok(restored)
}

fn recovery_deferred(from_step: &str, error: HandoffError, _ctx: &CliCtx<'_>, _endpoint: &str, phase: &str) -> RecoveryReport {
    let (code, mut detail) = match error {
        HandoffError::Busy { code, busy, .. } => {
            let code = ErrorCode::parse(&code).unwrap_or(ErrorCode::InternalError).as_str();
            (code, PyDict::from_pairs(vec![("busy".into(), PyValue::List(busy)), ("handoff".into(), str_value("refused"))]))
        }
        HandoffError::Unconfirmed { code, probe, outcome, pids } => {
            let code = if pids.is_empty() { ErrorCode::parse(&code).unwrap_or(ErrorCode::InternalError).as_str() } else { ErrorCode::DeviceStillHeld.as_str() };
            let mut detail = PyDict::from_pairs(vec![("handoff".into(), str_value("refused")), ("handoff_outcome".into(), str_value(outcome)), ("probe".into(), str_value(probe))]);
            detail.insert("hint", str_value("請執行 serialwrap daemon status 確認狀態；確認安全後再執行 serialwrap daemon stop，之後重試 setup"));
            if !pids.is_empty() {
                detail.insert("pids", PyValue::List(pids.iter().map(|pid| PyValue::Int(PyInt::Small(i128::from(*pid)))).collect()));
            }
            (code, detail)
        }
    };
    RecoveryReport { ok: false, from_step: recovery_step(from_step), backup_used: false, journal_kept: true, restored: PyDict::new(), error_code: Some(code), reason: Some("recovery_deferred".into()), message: Some("daemon 仍忙碌或無法確認交接；保留 journal 待重試".into()), detail: { detail.insert("error_code", str_value(code)); detail.insert("phase", str_value(phase)); detail } }
}

fn recovery_stop_error(from_step: &str, error: &StopFailure, ctx: &CliCtx<'_>, endpoint: &str) -> RecoveryReport {
    match error.cause() {
        StopFailure::Handoff(handoff) | StopFailure::Relaunched { err: handoff, .. } => recovery_deferred(from_step, handoff.clone(), ctx, endpoint, "install_recover"),
        StopFailure::Timeout { .. } => recovery_error(from_step, ErrorCode::Timeout.as_str(), "handoff_timeout", "daemon 交接逾時；保留 journal 待確認"),
        StopFailure::RelaunchLoop { .. } => recovery_error(from_step, ErrorCode::DeviceStillHeld.as_str(), "relaunch_loop", "監管模式反覆重新啟動 daemon"),
        StopFailure::Hold(service) | StopFailure::Service(service) => recovery_error(from_step, ErrorCode::InternalError.as_str(), "autostart_hold_unverified", service.hint.as_deref().unwrap_or("無法抑制監管自動啟動")),
        other => recovery_error(from_step, ErrorCode::DeviceStillHeld.as_str(), "handoff_failed", format!("daemon 交接失敗：{other:?}")),
    }
}

fn layout_io(path: &Path, message: impl Into<String>) -> LayoutError {
    LayoutError::Io { path: path.to_path_buf(), message: message.into() }
}

fn json_bytes(value: &PyValue) -> Vec<u8> {
    let opts = DumpOpts { sep: Sep::Indent(2), ensure_ascii: false, sort_keys: true, allow_nan: true };
    let mut bytes = pyjson::dumps(value, &opts).into_bytes();
    bytes.push(b'\n');
    bytes
}

fn write_json(ctx: &CliCtx<'_>, path: &Path, value: &PyValue) -> Result<(), LayoutError> {
    if let Some(parent) = path.parent() {
        ctx.p.fs.mkdirs(parent).map_err(|error| layout_io(parent, error.py_message))?;
    }
    ctx.p.fs.atomic_write(path, &json_bytes(value), None).map_err(|error| layout_io(path, error.py_message))?;
    if let Some(parent) = path.parent() {
        ctx.p.fs.sync_dir(parent).map_err(|error| layout_io(parent, error.py_message))?;
    }
    Ok(())
}

fn hash_path(ctx: &CliCtx<'_>, path: &Path) -> Result<String, LayoutError> {
    let bytes = ctx.p.fs.read(path).map_err(|error| layout_io(path, error.py_message))?;
    Ok(sha256_hex(&bytes))
}

fn copy_file(ctx: &CliCtx<'_>, from: &Path, to: &Path) -> Result<(), LayoutError> {
    if let Some(parent) = to.parent() {
        ctx.p.fs.mkdirs(parent).map_err(|error| layout_io(parent, error.py_message))?;
    }
    ctx.p.fs.copy_preserving(from, to).map_err(|error| layout_io(to, error.py_message))?;
    ctx.p.fs.sync_file(to).map_err(|error| layout_io(to, error.py_message))?;
    if let Some(parent) = to.parent() {
        ctx.p.fs.sync_dir(parent).map_err(|error| layout_io(parent, error.py_message))?;
    }
    Ok(())
}

fn copy_optional(ctx: &CliCtx<'_>, from: &Path, to: &Path, files: &mut Vec<(String, PathBuf)>) -> Result<(), LayoutError> {
    if ctx.p.fs.exists(from) {
        copy_file(ctx, from, to)?;
        files.push((to.file_name().unwrap_or_default().to_string_lossy().into_owned(), to.to_path_buf()));
    }
    Ok(())
}

fn copy_replace(ctx: &CliCtx<'_>, from: &Path, to: &Path) -> Result<(), LayoutError> {
    if let Some(parent) = to.parent() {
        ctx.p.fs.mkdirs(parent).map_err(|error| layout_io(parent, error.py_message))?;
    }
    let temp = to.with_extension("restore.tmp");
    if ctx.p.fs.exists(&temp) { ctx.p.fs.remove_file(&temp).map_err(|error| layout_io(&temp, error.py_message))?; }
    copy_file(ctx, from, &temp)?;
    ctx.p.fs.replace(&temp, to).map_err(|error| layout_io(to, error.py_message))?;
    if let Some(parent) = to.parent() {
        ctx.p.fs.sync_dir(parent).map_err(|error| layout_io(parent, error.py_message))?;
    }
    Ok(())
}

fn sync_tree(ctx: &CliCtx<'_>, dir: &Path) -> Result<(), LayoutError> {
    for name in ctx.p.fs.list_dir(dir).map_err(|error| layout_io(dir, error.py_message))? {
        let path = dir.join(name);
        if ctx.p.fs.is_dir(&path) {
            sync_tree(ctx, &path)?;
        } else if ctx.p.fs.is_file(&path) {
            ctx.p.fs.sync_file(&path).map_err(|error| layout_io(&path, error.py_message))?;
        }
    }
    ctx.p.fs.sync_dir(dir).map_err(|error| layout_io(dir, error.py_message))
}

fn collect_files(ctx: &CliCtx<'_>, base: &Path, directory: &Path, files: &mut Vec<(String, PathBuf)>) -> Result<(), LayoutError> {
    let names = ctx.p.fs.list_dir(directory).map_err(|error| layout_io(directory, error.py_message))?;
    for name in names {
        let path = directory.join(&name);
        if ctx.p.fs.is_dir(&path) {
            collect_files(ctx, base, &path, files)?;
        } else if ctx.p.fs.is_file(&path) {
            let relative = path.strip_prefix(base).unwrap_or(&path).to_string_lossy().replace('\\', "/");
            files.push((relative, path));
        }
    }
    Ok(())
}

fn restore_registration(ctx: &mut CliCtx<'_>, backup: &Path, snapshot: &SupervisorSnapshot) -> Result<(), LayoutError> {
    let root = install_layout::resolve_install_root(ctx);
    let task_source = backup.join("registered.task.xml");
    if snapshot.mode == SupervisionMode::WindowsTask {
        if ctx.p.fs.exists(&task_source) {
        let source = ctx.p.fs.read(&task_source).map_err(|error| layout_io(&task_source, error.py_message))?;
        let text = decode_utf16_xml(&source).unwrap_or_else(|| String::from_utf8_lossy(&source).into_owned());
            let held_xml = task_xml_with_enabled(&text, false).ok_or_else(|| layout_io(&task_source, "匯出的 Task XML 缺少唯一的 Settings/Enabled 欄位"))?;
        let held_path = backup.join("registered.task.held.xml");
            ctx.p.fs.atomic_write(&held_path, &sw_pal::service_ctl::encode_utf16le_bom(&held_xml), None).map_err(|error| layout_io(&held_path, error.py_message))?;
            let output = ctx.p.env.run(&[
                "schtasks".into(), "/Create".into(), "/XML".into(), held_path.to_string_lossy().into_owned(),
                "/TN".into(), "serialwrap".into(), "/F".into(),
            ], Some(Duration::from_secs(15))).map_err(|error| layout_io(&held_path, error.py_message))?;
            if output.rc != 0 {
                return Err(layout_io(&held_path, format!("還原 Windows 工作註冊失敗（rc={}）：{}", output.rc, output.stderr.trim())));
            }
        } else {
            match ctx.p.service.query(&snapshot.mode) {
                TaskQuery::NotFound => {}
                TaskQuery::QueryFailed(message) => return Err(layout_io(&backup.join("registered.task.xml"), format!("無法確認目前 Windows 工作註冊：{message}"))),
                TaskQuery::Running | TaskQuery::NotRunning(_) => {
                    let records = ctx.p.service.uninstall(&snapshot.mode, false);
                    if records.iter().any(|record| record.rc != 0) {
                        return Err(layout_io(&backup.join("registered.task.xml"), "移除沒有備份的 Windows 工作註冊失敗"));
                    }
                }
            }
        }
    }
    let exec = if ctx.p.caps.path_flavor == sw_pal::PalPathFlavor::Windows {
        root.bin.join("serialwrapd.exe")
    } else {
        root.bin.join("serialwrapd")
    };
    let definition_path = match snapshot.mode {
        SupervisionMode::WindowsTask => Some(backup.join("registered.task.held.xml")),
        SupervisionMode::SystemdUser => Some(systemd_user_unit_path(ctx)),
        SupervisionMode::SystemdSystem => Some(PathBuf::from("/etc/systemd/system/serialwrap.service")),
        _ => None,
    };
    if snapshot.mode == SupervisionMode::SystemdUser {
        let unit = definition_path.as_ref().expect("systemd definition path");
        let saved = backup.join("registered.unit");
        if let Some(parent) = unit.parent() { ctx.p.fs.mkdirs(parent).map_err(|error| layout_io(parent, error.py_message))?; }
        if ctx.p.fs.exists(&saved) {
            copy_replace(ctx, &saved, unit)?;
            let argv = vec!["systemctl".into(), "--user".into(), "daemon-reload".into()];
            if !ctx.p.env.run(&argv, None).is_ok_and(|output| output.rc == 0) {
                return Err(layout_io(unit, "systemd daemon-reload 失敗"));
            }
        } else if ctx.p.fs.exists(unit) {
            ctx.p.fs.remove_file(unit).map_err(|error| layout_io(unit, error.py_message))?;
            let argv = vec!["systemctl".into(), "--user".into(), "daemon-reload".into()];
            if !ctx.p.env.run(&argv, None).is_ok_and(|output| output.rc == 0) {
                return Err(layout_io(unit, "systemd daemon-reload 失敗"));
            }
        }
    } else if snapshot.mode == SupervisionMode::SystemdSystem {
        let saved = backup.join("registered.unit");
        if ctx.p.fs.exists(&saved) {
            let argv = vec!["sudo".into(), "install".into(), "-m".into(), "0644".into(), saved.to_string_lossy().into_owned(), "/etc/systemd/system/serialwrap.service".into()];
            if !ctx.p.env.run(&argv, Some(Duration::from_secs(30))).is_ok_and(|output| output.rc == 0) {
                return Err(layout_io(&saved, "systemd-system unit 還原失敗"));
            }
        } else {
            let argv = vec!["sudo".into(), "rm".into(), "-f".into(), "/etc/systemd/system/serialwrap.service".into()];
            if !ctx.p.env.run(&argv, Some(Duration::from_secs(30))).is_ok_and(|output| output.rc == 0) {
                return Err(layout_io(&saved, "移除沒有備份的 systemd-system unit 失敗"));
            }
        }
        let argv = vec!["sudo".into(), "systemctl".into(), "daemon-reload".into()];
        if !ctx.p.env.run(&argv, Some(Duration::from_secs(30))).is_ok_and(|output| output.rc == 0) {
            return Err(layout_io(&saved, "systemd-system daemon-reload 失敗"));
        }
    } else if snapshot.mode == SupervisionMode::WindowsService {
        let def = sw_pal::SupervisorDefinition {
            mode: snapshot.mode.clone(),
            name: "serialwrap".into(),
            exec: vec![exec.to_string_lossy().into_owned(), "--socket".into(), snapshot.endpoint.clone()],
            working_dir: Some(root.bin.clone()),
            run_user: None,
            env: Vec::new(),
            definition_path,
            register_held: true,
        };
        let records = ctx.p.service.install(&def, false);
        if records.iter().any(|record| record.rc != 0) {
            return Err(layout_io(&root.supervisor, "重新註冊監管定義失敗"));
        }
    }
    Ok(())
}

fn export_task_registration(ctx: &CliCtx<'_>, destination: &Path) -> Result<(), LayoutError> {
    let argv = vec!["schtasks".into(), "/Query".into(), "/TN".into(), "serialwrap".into(), "/XML".into()];
    let output = ctx.p.env.run(&argv, Some(Duration::from_secs(15)))
        .map_err(|error| layout_io(destination, error.py_message))?;
    if output.rc != 0 || !output.stdout.contains("<Task") {
        return Err(layout_io(destination, format!("無法匯出已註冊的 Windows Task XML（rc={}）：{}", output.rc, output.stderr.trim())));
    }
    let xml = output.stdout.trim();
    if task_xml_with_enabled(xml, true).is_none() {
        return Err(layout_io(destination, "匯出的 Windows Task XML 缺少唯一的 Settings/Enabled 欄位"));
    }
    if let Some(parent) = destination.parent() {
        ctx.p.fs.mkdirs(parent).map_err(|error| layout_io(parent, error.py_message))?;
    }
    ctx.p.fs.atomic_write(destination, &sw_pal::service_ctl::encode_utf16le_bom(xml), None)
        .map_err(|error| layout_io(destination, error.py_message))?;
    if let Some(parent) = destination.parent() {
        ctx.p.fs.sync_dir(parent).map_err(|error| layout_io(parent, error.py_message))?;
    }
    Ok(())
}

fn task_xml_with_enabled(xml: &str, enabled: bool) -> Option<String> {
    let settings_start = xml.find("<Settings")?;
    let content_start = xml[settings_start..].find('>')? + settings_start + 1;
    let settings_end = xml[content_start..].find("</Settings>")? + content_start;
    let settings = &xml[content_start..settings_end];
    let open = "<Enabled>";
    let close = "</Enabled>";
    let enabled_start = settings.find(open)? + open.len();
    let enabled_end = settings[enabled_start..].find(close)? + enabled_start;
    if settings[enabled_end + close.len()..].contains(open) {
        return None;
    }
    let value = if enabled { "true" } else { "false" };
    let mut output = String::with_capacity(xml.len() + value.len());
    output.push_str(&xml[..content_start + enabled_start]);
    output.push_str(value);
    output.push_str(&xml[content_start + enabled_end..]);
    Some(output)
}

fn decode_utf16_xml(bytes: &[u8]) -> Option<String> {
    let body = bytes.strip_prefix(&[0xFF, 0xFE])?;
    let words = body.chunks_exact(2).map(|chunk| u16::from_le_bytes([chunk[0], chunk[1]])).collect::<Vec<_>>();
    String::from_utf16(&words).ok()
}

fn capture_entries(ctx: &CliCtx<'_>, root: &InstallRoot) -> Result<Vec<EntrySnapshot>, LayoutError> {
    let mut entries = Vec::new();
    for name in &root.entry_names {
        let path = root.bin.join(name);
        match ctx.p.fs.read_link(&path).map_err(|error| layout_io(&path, error.py_message))? {
            Some(target) => entries.push(EntrySnapshot::Symlink { name: name.clone(), target: target.to_string_lossy().into_owned() }),
            None if ctx.p.fs.exists(&path) && ctx.p.fs.is_file(&path) => {
                let bytes = ctx.p.fs.read(&path).map_err(|error| layout_io(&path, error.py_message))?;
                entries.push(EntrySnapshot::Copy { name: name.clone(), sha256: sha256_hex(&bytes), size: bytes.len() as u64 });
            }
            None => entries.push(EntrySnapshot::Absent { name: name.clone() }),
        }
    }
    Ok(entries)
}

fn str_value(value: impl Into<String>) -> PyValue { PyValue::Str(value.into()) }
fn opt_str(value: Option<&str>) -> PyValue { value.map(|v| PyValue::Str(v.to_string())).unwrap_or(PyValue::Null) }
fn opt_bool(value: Option<bool>) -> PyValue { value.map(PyValue::Bool).unwrap_or(PyValue::Null) }

fn entry_value(entry: &EntrySnapshot) -> PyValue {
    let mut value = PyDict::new();
    match entry {
        EntrySnapshot::Symlink { name, target } => { value.insert("kind", str_value("symlink")); value.insert("name", str_value(name.clone())); value.insert("target", str_value(target.clone())); }
        EntrySnapshot::Copy { name, sha256, size } => { value.insert("kind", str_value("copy")); value.insert("name", str_value(name.clone())); value.insert("sha256", str_value(sha256.clone())); value.insert("size", PyValue::Int(PyInt::Small(i128::from(*size)))); }
        EntrySnapshot::Absent { name } => { value.insert("kind", str_value("absent")); value.insert("name", str_value(name.clone())); }
    }
    PyValue::Dict(value)
}

fn entries_value(entries: &[EntrySnapshot]) -> PyValue { PyValue::List(entries.iter().map(entry_value).collect()) }

fn current_value(current: Option<&CurrentRelease>) -> PyValue {
    let Some(current) = current else { return PyValue::Null; };
    let mut value = PyDict::new();
    value.insert("backup_dir", opt_str(current.backup_dir.as_deref()));
    value.insert("bundle_version", str_value(current.bundle_version.clone()));
    value.insert("dir", str_value(current.dir.clone()));
    value.insert("previous", current.previous.as_ref().map(|previous| {
        PyValue::Dict(PyDict::from_pairs(vec![
            ("bundle_version".into(), str_value(previous.bundle_version.clone())),
            ("dir".into(), str_value(previous.dir.clone())),
        ]))
    }).unwrap_or(PyValue::Null));
    value.insert("switched_at", str_value(current.switched_at.clone()));
    PyValue::Dict(value)
}

fn kind_value(kind: AutostartHoldKind) -> &'static str {
    match kind { AutostartHoldKind::SystemdDropIn => "systemd_dropin", AutostartHoldKind::TaskDisabled => "task_disabled", AutostartHoldKind::ServiceDisabled => "service_disabled", AutostartHoldKind::NoSupervisor => "no_supervisor" }
}

fn autostart_value(plan: &AutostartPlan) -> PyValue {
    PyValue::Dict(PyDict::from_pairs(vec![
        ("dropin_path".into(), opt_str(plan.dropin_path.as_deref())),
        ("kind".into(), str_value(kind_value(plan.kind))),
        ("prior_enabled".into(), opt_bool(plan.prior_enabled)),
        ("prior_restart".into(), opt_str(plan.prior_restart.as_deref())),
    ]))
}

fn supervisor_value(snapshot: &SupervisorSnapshot) -> PyValue {
    PyValue::Dict(PyDict::from_pairs(vec![
        ("enabled".into(), opt_bool(snapshot.enabled)),
        ("endpoint".into(), str_value(snapshot.endpoint.clone())),
        ("mode".into(), str_value(snapshot.mode.as_str().to_string())),
        ("registration".into(), opt_str(snapshot.registration.as_ref().map(|path| path.to_string_lossy()).as_deref())),
        ("running".into(), PyValue::Bool(snapshot.running)),
    ]))
}

fn daemon_value(snapshot: Option<&DaemonSnapshot>) -> PyValue {
    let Some(snapshot) = snapshot else { return PyValue::Null; };
    let mut id = PyDict::new();
    id.insert("known", PyValue::Bool(snapshot.id.known));
    id.insert("pid", snapshot.id.pid.map(|pid| PyValue::Int(PyInt::Small(i128::from(pid)))).unwrap_or(PyValue::Null));
    id.insert("tree", PyValue::List(snapshot.id.tree.iter().map(|item| PyValue::Dict(PyDict::from_pairs(vec![
        ("pid".into(), PyValue::Int(PyInt::Small(i128::from(item.pid)))),
        ("start_ticks".into(), item.start_ticks.map(|n| PyValue::Int(PyInt::Small(i128::from(n)))).unwrap_or(PyValue::Null)),
    ]))).collect()));
    let sessions = snapshot.sessions.iter().map(|session| PyValue::Dict(session.clone())).collect();
    PyValue::Dict(PyDict::from_pairs(vec![
        ("attached_paths".into(), PyValue::List(snapshot.attached_paths.iter().map(|path| str_value(path.to_string_lossy().into_owned())).collect())),
        ("id".into(), PyValue::Dict(id)),
        ("sessions".into(), PyValue::List(sessions)),
    ]))
}

fn original_value(original: &OriginalSnapshot) -> PyValue {
    PyValue::Dict(PyDict::from_pairs(vec![
        ("autostart".into(), autostart_value(&original.autostart)),
        ("config_sha256".into(), opt_str(original.config_sha256.as_deref())),
        ("current".into(), current_value(original.current.as_ref())),
        ("daemon".into(), daemon_value(original.daemon.as_ref())),
        ("daemon_start".into(), str_value(original.daemon_start)),
        ("entries".into(), entries_value(&original.entries)),
        ("supervisor".into(), supervisor_value(&original.supervisor)),
    ]))
}

fn journal_value(journal: &Journal) -> PyValue {
    let mut value = PyDict::new();
    value.insert("backup_dir", opt_str(journal.backup_dir.as_deref()));
    value.insert("backup_manifest_sha256", opt_str(journal.backup_manifest_sha256.as_deref()));
    value.insert("from", opt_str(journal.from.as_deref()));
    value.insert("op", str_value(journal.op.clone()));
    value.insert("original", journal.original.as_ref().map(|original| original_value(original)).unwrap_or(PyValue::Null));
    value.insert("snapshot_dir", opt_str(journal.snapshot_dir.as_deref()));
    value.insert("snapshot_sha256", journal.snapshot_sha256.as_ref().map(|items| PyValue::List(items.iter().map(|(name, digest)| PyValue::List(vec![str_value(name.clone()), str_value(digest.clone())])).collect())).unwrap_or(PyValue::Null));
    value.insert("started_at", str_value(journal.started_at.clone()));
    value.insert("step", str_value(journal.step.as_str().to_string()));
    value.insert("target_autostart", journal.target_autostart.as_ref().map(autostart_value).unwrap_or(PyValue::Null));
    value.insert("to", str_value(journal.to.clone()));
    PyValue::Dict(value)
}

pub fn run_supervision_switch(
    ctx: &mut CliCtx<'_>,
    target: &SupervisionMode,
    with_sudo: bool,
    force_requested: bool,
) -> i32 {
    let recovered = match recover_autostart_hold(ctx) {
        Ok(value) => value,
        Err(response) => return emit(ctx, response, "supervision-mode", 2),
    };
    let old = match configured_mode_checked(ctx) {
        Ok(mode) => mode,
        Err(message) => {
            let response = internal_error_dict("supervision_set_safety_check", "config_read_failed", &message);
            return emit(ctx, response, "supervision-mode", 2);
        }
    };
    let endpoint = crate::endpoint_resolve::local_default_endpoint(ctx);
    let snapshot = match safety_check(ctx, &endpoint) {
        Ok(snapshot) => snapshot,
        Err((code, mut detail)) => {
            if code == ErrorCode::FlashingBusy.as_str() {
                detail.insert("message", PyValue::Str("flash 進行中，拒絕切換監管模式（--force 不適用，請待 flash 完成後重試）".into()));
            }
            return emit_switch_refusal(ctx, &old, code, detail, force_requested);
        }
    };
    crash_if("supervision_set:after_safety_check");
    let root = install_layout::resolve_install_root(ctx);
    let home = ctx.p.env.current_user_home().map(PathBuf::from).or_else(|| ctx.env.get("HOME").map(PathBuf::from)).unwrap_or_default();
    let socket = switch_socket(ctx, target, &endpoint);
    let mut request = crate::setup_cmd::ReconcileRequest {
        old_mode: old.clone(),
        target_mode: target.clone(),
        home,
        daemon_running: snapshot.is_some(),
        daemon_id: snapshot.as_ref().map(|snapshot| snapshot.id.clone()).unwrap_or_default(),
        old_endpoint: endpoint.clone(),
        any_flashing: false,
        with_sudo,
        force: false,
        hold_target: false,
        socket_path: Some(socket),
        config_path: crate::endpoint_resolve::config_write_path(ctx),
        old_stop: crate::setup_cmd::OldStop::Reconcile,
    };
    let must_handoff = old != *target && crate::setup_cmd::reconcile_stops_old(ctx, &request);
    if !must_handoff {
        let result = crate::setup_cmd::reconcile(ctx, &request);
        return emit_switch_result(ctx, &old, result, recovered, force_requested, None);
    }

    let service_state = if old == SupervisionMode::OnDemand {
        TaskQuery::NotFound
    } else {
        ctx.p.service.query(&old)
    };
    let enabled_before = match &service_state {
        TaskQuery::NotRunning(state) if state.eq_ignore_ascii_case("disabled") => Some(false),
        TaskQuery::Running | TaskQuery::NotRunning(_) => Some(true),
        TaskQuery::NotFound | TaskQuery::QueryFailed(_) => None,
    };
    let running_before = if old == SupervisionMode::OnDemand {
        snapshot.is_some()
    } else {
        matches!(&service_state, TaskQuery::Running)
    };
    let hold_plan = match planned_switch_hold(ctx, &old, enabled_before) {
        Ok(plan) => plan,
        Err(message) => {
            let mut response = internal_error_dict("supervision_set_safety_check", "autostart_hold_plan_failed", &message);
            response.insert("previous_mode", PyValue::Str(old.as_str().into()));
            return emit(ctx, response, "supervision-mode", 2);
        }
    };
    let placeholder = hold_record(&old, target, enabled_before, running_before, &endpoint, with_sudo, Some(&hold_plan));
    let hold_path = autostart_hold_path(ctx);
    if let Err(error) = write_autostart_hold(ctx, &hold_path, &placeholder) {
        let mut response = output_error(ErrorCode::InternalError, &error);
        response.insert("phase", PyValue::Str("supervision_set_safety_check".into()));
        response.insert("reason", PyValue::Str("autostart_hold_record".into()));
        return emit(ctx, response, "supervision-mode", 2);
    }

    let mut hold = None;
    let mut handoff_token = String::new();
    let mut ran = Vec::new();
    let mut chain = Vec::new();
    let on_demand_hold = old == SupervisionMode::OnDemand;
    if on_demand_hold {
        if let Err(code) = acquire_switch_autostart_lock(ctx) {
            let _ = remove_hold_file(ctx, &hold_path);
            let mut response = output_error(code, "無法取得 autostart.lock；請稍後重試");
            response.insert("phase", PyValue::Str("supervision_set_safety_check".into()));
            response.insert("reason", PyValue::Str("autostart_lock_timeout".into()));
            return emit(ctx, response, "supervision-mode", 2);
        }
    } else {
        match ctx.p.service.hold_autostart(&old, with_sudo) {
            Ok(record) => hold = Some(merge_hold_plan(&hold_plan, record)),
            Err(error) => {
                let _ = remove_hold_file(ctx, &hold_path);
                let mut response = output_error(ErrorCode::InternalError, error.hint.as_deref().unwrap_or("無法抑制監管自動啟動"));
                response.insert("phase", PyValue::Str("supervision_set_safety_check".into()));
                response.insert("reason", PyValue::Str("autostart_hold_unverified".into()));
                return emit(ctx, response, "supervision-mode", 2);
            }
        }
        if ctx.p.service.autostart_hold_state(&old) != HoldState::Held {
            if let Some(record) = hold.as_ref() { let _ = ctx.p.service.release_autostart(record, enabled_before, with_sudo); }
            let _ = remove_hold_file(ctx, &hold_path);
            let mut response = output_error(ErrorCode::InternalError, "無法確認自動啟動 hold 已生效");
            response.insert("phase", PyValue::Str("supervision_set_safety_check".into()));
            response.insert("reason", PyValue::Str("autostart_hold_unverified".into()));
            return emit(ctx, response, "supervision-mode", 2);
        }
    }
    let persisted = hold_record(&old, target, enabled_before, running_before, &endpoint, with_sudo, hold.as_ref());
    if let Err(error) = write_autostart_hold(ctx, &hold_path, &persisted) {
        if let Some(record) = hold.as_ref() { let _ = ctx.p.service.release_autostart(record, enabled_before, with_sudo); }
        ctx.autostart_lock.take();
        let _ = remove_hold_file(ctx, &hold_path);
        let mut response = output_error(ErrorCode::InternalError, &error);
        response.insert("phase", PyValue::Str("supervision_set_safety_check".into()));
        response.insert("reason", PyValue::Str("autostart_hold_record".into()));
        return emit(ctx, response, "supervision-mode", 2);
    }
    crash_if("supervision_set:after_hold");

    if snapshot.is_some() {
        match graceful_stop::graceful_stop_with_existing_hold(
            ctx,
            &old,
            &endpoint,
            with_sudo,
            true,
            "supervision_set_safety_check",
            StopRpc::Handoff { reason: "supervision_set" },
            hold.clone(),
        ) {
            Ok(report) => {
                chain = report.chain;
                handoff_token = report.handoff_token.unwrap_or_default();
                ran = report.ran;
                if let Some(actual) = report.hold {
                    hold = Some(merge_hold_plan(&hold_plan, actual));
                    let persisted = hold_record(&old, target, enabled_before, running_before, &endpoint, with_sudo, hold.as_ref());
                    if let Err(error) = write_autostart_hold(ctx, &hold_path, &persisted) {
                        let mut response = internal_error_dict("supervision_set_safety_check", "autostart_hold_record", &error);
                        response.insert("previous_mode", PyValue::Str(old.as_str().into()));
                        finish_switch_hold(ctx, &hold_path, hold.as_ref(), enabled_before, with_sudo, &mut response);
                        return emit(ctx, response, "supervision-mode", 2);
                    }
                }
            }
            Err(error) => {
                if let Some(actual) = error.hold() {
                    hold = Some(merge_hold_plan(&hold_plan, actual.clone()));
                    let persisted = hold_record(&old, target, enabled_before, running_before, &endpoint, with_sudo, hold.as_ref());
                    let _ = write_autostart_hold(ctx, &hold_path, &persisted);
                }
                let timeout = matches!(error.cause(), StopFailure::Timeout { .. });
                let mut response = stop_failure_response(&old, &error, force_requested);
                if timeout {
                    ctx.autostart_lock.take();
                    return emit(ctx, response, "supervision-mode", 1);
                }
                finish_switch_hold(ctx, &hold_path, hold.as_ref(), enabled_before, with_sudo, &mut response);
                return emit(ctx, response, "supervision-mode", stop_failure_exit(&error));
            }
        }
    } else if let Err(error) = graceful_stop::ensure_released_or_rehandoff(
        ctx,
        &old,
        &endpoint,
        with_sudo,
        "supervision_set",
        "supervision_set_safety_check",
        &mut chain,
    ) {
        let mut response = stop_failure_response(&old, &error, force_requested);
        let pid = crate::commands::daemon_cmd::capture_daemon_identity_public(ctx, &endpoint).pid
            .or_else(|| rpc(ctx, &endpoint, "health.status", 1.0).and_then(|status| status.get("pid").and_then(as_u32)));
        if let Some(pid) = pid {
            response.insert("relaunched", PyValue::Dict(PyDict::from_pairs(vec![("pid".into(), PyValue::Int(PyInt::Small(i128::from(pid))))])));
        }
        finish_switch_hold(ctx, &hold_path, hold.as_ref(), enabled_before, with_sudo, &mut response);
        return emit(ctx, response, "supervision-mode", stop_failure_exit(&error));
    }
    request.old_stop = crate::setup_cmd::OldStop::HandedOff { token: handoff_token, ran };
    let result = crate::setup_cmd::reconcile(ctx, &request);
    match &result {
        Err(crate::setup_cmd::ReconcileError::WaitOldExitTimeout { .. }) => {
            let mut response = output_error(ErrorCode::Timeout, "舊 daemon 在等待期間仍未結束；自動啟動 hold 保留，請確認狀態後重跑 supervision-mode set");
            response.insert("phase", PyValue::Str("reconcile_wait_old_exit".into()));
            response.insert("previous_mode", PyValue::Str(old.as_str().into()));
            if force_requested { response.insert("force_ignored", PyValue::Bool(true)); }
            return emit(ctx, response, "supervision-mode", 1);
        }
        _ => {}
    }
    let mut release_failure = None;
    let restore_enabled = match &result {
        Ok(_) => None,
        Err(crate::setup_cmd::ReconcileError::WaitOldExitTimeout { .. }) => None,
        Err(crate::setup_cmd::ReconcileError::FlashingBusy(_) | crate::setup_cmd::ReconcileError::Config(_)) => enabled_before,
    };
    if let Err(error) = release_switch_hold(ctx, hold.as_ref(), restore_enabled, with_sudo) {
        release_failure = Some(error);
    }
    if let Some(error) = release_failure {
        let mut response = output_error(ErrorCode::InternalError, error.hint.as_deref().unwrap_or("撤銷自動啟動 hold 失敗"));
        response.insert("phase", PyValue::Str("supervision_set_release".into()));
        response.insert("previous_mode", PyValue::Str(old.as_str().into()));
        response.insert("reason", PyValue::Str("autostart_release_failed".into()));
        response.insert("autostart_release_error", PyValue::Dict(service_error_value(&error)));
        if force_requested { response.insert("force_ignored", PyValue::Bool(true)); }
        ctx.autostart_lock.take();
        return emit(ctx, response, "supervision-mode", 2);
    }
    ctx.autostart_lock.take();
    if let Err(error) = remove_hold_file(ctx, &hold_path) {
        let mut response = output_error(ErrorCode::InternalError, &error);
        response.insert("phase", PyValue::Str("supervision_set_release".into()));
        response.insert("reason", PyValue::Str("autostart_release_failed".into()));
        response.insert("autostart_release_error", PyValue::Str(error));
        return emit(ctx, response, "supervision-mode", 2);
    }
    emit_switch_result(ctx, &old, result, recovered, force_requested, Some(chain))
}

fn configured_mode_checked(ctx: &CliCtx<'_>) -> Result<SupervisionMode, String> {
    let config = endpoint_resolve::load_runtime_config(ctx).map_err(|error| error.to_string())?;
    Ok(config.mode().filter(|value| py_truthy_of(value)).map(|value| SupervisionMode::parse(&py_str_of(value))).unwrap_or(SupervisionMode::OnDemand))
}

fn systemd_user_unit_path(ctx: &CliCtx<'_>) -> PathBuf {
    let config_home = ctx.p.env.var("XDG_CONFIG_HOME")
        .filter(|path| !path.is_empty())
        .map(PathBuf::from)
        .or_else(|| ctx.p.env.current_user_home().map(|home| PathBuf::from(home).join(".config")))
        .unwrap_or_else(|| PathBuf::from(&ctx.paths.config_dir).parent().unwrap_or_else(|| Path::new(".")).to_path_buf());
    config_home.join("systemd/user/serialwrap.service")
}

fn switch_socket(ctx: &CliCtx<'_>, target: &SupervisionMode, endpoint: &str) -> String {
    if *target == SupervisionMode::SystemdSystem {
        "/run/serialwrap/serialwrapd.sock".into()
    } else if matches!(target, SupervisionMode::WindowsTask | SupervisionMode::WindowsService)
        || crate::endpoint_resolve::rpc_backend_is_win(ctx)
    {
        crate::endpoint_resolve::local_default_endpoint(ctx)
    } else {
        endpoint.to_string()
    }
}

fn hold_kind(mode: &SupervisionMode) -> AutostartHoldKind {
    match mode {
        SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem => AutostartHoldKind::SystemdDropIn,
        SupervisionMode::WindowsTask => AutostartHoldKind::TaskDisabled,
        SupervisionMode::WindowsService => AutostartHoldKind::ServiceDisabled,
        _ => AutostartHoldKind::NoSupervisor,
    }
}

fn hold_kind_name(kind: AutostartHoldKind) -> &'static str {
    match kind {
        AutostartHoldKind::SystemdDropIn => "systemd_dropin",
        AutostartHoldKind::TaskDisabled => "task_disabled",
        AutostartHoldKind::ServiceDisabled => "service_disabled",
        AutostartHoldKind::NoSupervisor => "no_supervisor",
    }
}

fn parse_hold_kind(value: Option<&PyValue>, mode: &SupervisionMode) -> AutostartHoldKind {
    match value.and_then(as_str) {
        Some("systemd_dropin") => AutostartHoldKind::SystemdDropIn,
        Some("task_disabled") => AutostartHoldKind::TaskDisabled,
        Some("service_disabled") => AutostartHoldKind::ServiceDisabled,
        Some("no_supervisor") => AutostartHoldKind::NoSupervisor,
        _ => hold_kind(mode),
    }
}

fn autostart_hold_path(ctx: &CliCtx<'_>) -> PathBuf {
    ctx.env.get("SERIALWRAP_STATE_DIR").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(&ctx.paths.state_dir)).join("autostart-hold.json")
}

fn acquire_switch_autostart_lock(ctx: &mut CliCtx<'_>) -> Result<(), ErrorCode> {
    if ctx.autostart_lock.as_ref().is_some_and(|guard| guard.is_held()) {
        return Ok(());
    }
    let path = PathBuf::from(&ctx.paths.run_dir).join("autostart.lock");
    if ctx.p.fs.mkdirs(path.parent().unwrap_or_else(|| Path::new("."))).is_err() {
        return Err(ErrorCode::InternalError);
    }
    let started = ctx.p.clock.mono_ns();
    loop {
        match ctx.p.fs.file_lock_exclusive(&path, false) {
            Ok(guard) => {
                ctx.autostart_lock = Some(guard);
                return Ok(());
            }
            Err(error) if error.kind == sw_pal::PalErrorKind::WouldBlock => {
                if ctx.p.clock.mono_ns().saturating_sub(started) >= 15_000_000_000 {
                    return Err(ErrorCode::Timeout);
                }
                ctx.p.clock.sleep(Duration::from_millis(200));
            }
            Err(_) => return Err(ErrorCode::InternalError),
        }
    }
}

fn hold_record(
    mode: &SupervisionMode,
    target: &SupervisionMode,
    enabled_before: Option<bool>,
    running_before: bool,
    endpoint: &str,
    with_sudo: bool,
    hold: Option<&AutostartHoldRecord>,
) -> PyDict {
    let kind = hold.map(|hold| hold.kind).unwrap_or_else(|| hold_kind(mode));
    let mut record = PyDict::new();
    record.insert("schema_version", PyValue::Int(PyInt::Small(1)));
    record.insert("op", PyValue::Str("supervision_set".into()));
    record.insert("old_mode", PyValue::Str(mode.as_str().into()));
    record.insert("target_mode", PyValue::Str(target.as_str().into()));
    record.insert("enabled_before", enabled_before.map_or(PyValue::Null, PyValue::Bool));
    record.insert("running_before", PyValue::Bool(running_before));
    record.insert("created_at", PyValue::Int(PyInt::Small(SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() as i128)));
    record.insert("endpoint", PyValue::Str(endpoint.into()));
    record.insert("with_sudo", PyValue::Bool(with_sudo));
    record.insert("kind", PyValue::Str(hold_kind_name(kind).into()));
    record.insert("name", hold.and_then(|hold| hold.name.clone()).map_or(PyValue::Null, PyValue::Str));
    record.insert("dropin_path", hold.and_then(|hold| hold.dropin_path.as_ref()).map(|path| PyValue::Str(path.display().to_string())).unwrap_or(PyValue::Null));
    record.insert("prior_enabled", hold.and_then(|hold| hold.prior_enabled).map_or(enabled_before.map_or(PyValue::Null, PyValue::Bool), PyValue::Bool));
    record.insert("prior_restart", hold.and_then(|hold| hold.prior_restart.clone()).map_or(PyValue::Null, PyValue::Str));
    let mut plan = PyDict::new();
    plan.insert("kind", PyValue::Str(hold_kind_name(kind).into()));
    plan.insert("enabled_before", enabled_before.map_or(PyValue::Null, PyValue::Bool));
    plan.insert("dropin_path", hold.and_then(|hold| hold.dropin_path.as_ref()).map(|path| PyValue::Str(path.display().to_string())).unwrap_or(PyValue::Null));
    plan.insert("prior_enabled", hold.and_then(|hold| hold.prior_enabled).map_or(enabled_before.map_or(PyValue::Null, PyValue::Bool), PyValue::Bool));
    plan.insert("prior_restart", hold.and_then(|hold| hold.prior_restart.clone()).map_or(PyValue::Null, PyValue::Str));
    record.insert("plan", PyValue::Dict(plan));
    record
}

fn planned_switch_hold(
    ctx: &CliCtx<'_>,
    mode: &SupervisionMode,
    enabled_before: Option<bool>,
) -> Result<AutostartHoldRecord, String> {
    let (kind, name, dropin_path, prior_restart) = match mode {
        SupervisionMode::SystemdUser | SupervisionMode::SystemdSystem => {
            let argv = if *mode == SupervisionMode::SystemdUser {
                vec!["systemctl".into(), "--user".into(), "show".into(), "serialwrap".into(), "-p".into(), "Restart".into(), "--value".into()]
            } else {
                vec!["systemctl".into(), "show".into(), "serialwrap".into(), "-p".into(), "Restart".into(), "--value".into()]
            };
            let output = ctx.p.env.run(&argv, Some(Duration::from_secs(2)))
                .map_err(|error| format!("讀取監管 Restart 設定失敗：{}", error.py_message))?;
            if output.rc != 0 {
                return Err(format!("讀取監管 Restart 設定失敗（exit {}）：{}", output.rc, output.stderr.trim()));
            }
            let unit = if *mode == SupervisionMode::SystemdUser {
                systemd_user_unit_path(ctx)
            } else {
                PathBuf::from("/etc/systemd/system/serialwrap.service")
            };
            let dropin = unit.parent().unwrap_or_else(|| Path::new("."))
                .join("serialwrap.service.d/90-serialwrap-update-hold.conf");
            (AutostartHoldKind::SystemdDropIn, Some("serialwrap".into()), Some(dropin), (!output.stdout.trim().is_empty()).then(|| output.stdout.trim().to_string()))
        }
        SupervisionMode::WindowsTask => (AutostartHoldKind::TaskDisabled, Some("serialwrap".into()), None, None),
        SupervisionMode::WindowsService => (AutostartHoldKind::ServiceDisabled, Some("serialwrap".into()), None, None),
        SupervisionMode::OnDemand | SupervisionMode::Other(_) => (AutostartHoldKind::NoSupervisor, None, None, None),
    };
    Ok(AutostartHoldRecord {
        mode: mode.clone(),
        kind,
        name,
        dropin_path,
        prior_enabled: enabled_before,
        prior_restart,
        ran: Vec::new(),
    })
}

fn merge_hold_plan(plan: &AutostartHoldRecord, mut actual: AutostartHoldRecord) -> AutostartHoldRecord {
    if actual.name.is_none() { actual.name = plan.name.clone(); }
    if actual.dropin_path.is_none() { actual.dropin_path = plan.dropin_path.clone(); }
    if actual.prior_enabled.is_none() { actual.prior_enabled = plan.prior_enabled; }
    if actual.prior_restart.is_none() { actual.prior_restart = plan.prior_restart.clone(); }
    actual
}

fn hold_from_record(record: &PyDict, mode: SupervisionMode) -> AutostartHoldRecord {
    let dropin_path = record.get("dropin_path").and_then(as_str).map(PathBuf::from);
    let prior_enabled = match record.get("prior_enabled") { Some(PyValue::Bool(value)) => Some(*value), _ => None };
    let prior_restart = record.get("prior_restart").and_then(as_str).map(str::to_owned);
    let name = record.get("name").and_then(as_str).map(str::to_owned);
    AutostartHoldRecord { kind: parse_hold_kind(record.get("kind"), &mode), mode, name, dropin_path, prior_enabled, prior_restart, ran: Vec::new() }
}

fn write_autostart_hold(ctx: &CliCtx<'_>, path: &Path, record: &PyDict) -> Result<(), String> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    ctx.p.fs.mkdirs(parent).map_err(|error| error.py_message.clone())?;
    let mut bytes = pyjson::dumps_dict(record, &DumpOpts { sep: Sep::Indent(2), ensure_ascii: false, sort_keys: true, allow_nan: true }).into_bytes();
    bytes.push(b'\n');
    ctx.p.fs.atomic_write(path, &bytes, Some(0o600)).map_err(|error| error.py_message.clone())?;
    ctx.p.fs.sync_dir(parent).map_err(|error| error.py_message.clone())
}

fn remove_hold_file(ctx: &CliCtx<'_>, path: &Path) -> Result<(), String> {
    if ctx.p.fs.exists(path) {
        ctx.p.fs.remove_file(path).map_err(|error| error.py_message.clone())?;
        if let Some(parent) = path.parent() { ctx.p.fs.sync_dir(parent).map_err(|error| error.py_message.clone())?; }
    }
    Ok(())
}

fn internal_error_dict(phase: &str, reason: &str, message: &str) -> PyDict {
    let mut response = output_error(ErrorCode::InternalError, message);
    response.insert("phase", PyValue::Str(phase.into()));
    response.insert("reason", PyValue::Str(reason.into()));
    response
}

fn output_error(code: ErrorCode, message: &str) -> PyDict {
    crate::output::error_resp(code, Some(message))
}

fn emit(ctx: &mut CliCtx<'_>, response: PyDict, context: &str, exit: i32) -> i32 {
    let _ = crate::output::print_json(&mut ctx.io, &response);
    let _ = crate::output::mirror_err(&mut ctx.io, &response, Some(context));
    exit
}

fn emit_switch_refusal(ctx: &mut CliCtx<'_>, old: &SupervisionMode, code: &'static str, mut detail: PyDict, force: bool) -> i32 {
    detail.insert("ok", PyValue::Bool(false));
    detail.insert("error_code", PyValue::Str(code.into()));
    detail.insert("phase", PyValue::Str("supervision_set_safety_check".into()));
    detail.insert("previous_mode", PyValue::Str(old.as_str().into()));
    if force { detail.insert("force_ignored", PyValue::Bool(true)); }
    if let Some(PyValue::Str(probe)) = detail.get("probe") {
        let _ = probe;
    }
    emit(ctx, detail, "supervision-mode", 2)
}

fn release_switch_hold(
    ctx: &CliCtx<'_>,
    hold: Option<&AutostartHoldRecord>,
    enabled: Option<bool>,
    with_sudo: bool,
) -> Result<(), sw_pal::service_ctl::ServiceResult> {
    if let Some(hold) = hold {
        ctx.p.service.release_autostart(hold, enabled, with_sudo)?;
    }
    Ok(())
}

fn service_error_value(error: &sw_pal::service_ctl::ServiceResult) -> PyDict {
    let mut detail = PyDict::new();
    detail.insert("error_code", str_value(error.error_code.as_deref().unwrap_or(ErrorCode::InternalError.as_str())));
    detail.insert("phase", str_value(error.phase.as_deref().unwrap_or("autostart_release")));
    detail.insert("hint", opt_str(error.hint.as_deref()));
    detail.insert("rc", error.rc.map(|rc| PyValue::Int(PyInt::Small(i128::from(rc)))).unwrap_or(PyValue::Null));
    if let Some(stderr) = &error.stderr { detail.insert("stderr", str_value(stderr.clone())); }
    PyDict::from_pairs(vec![("release".into(), PyValue::Dict(detail))])
}

fn finish_switch_hold(
    ctx: &mut CliCtx<'_>,
    path: &Path,
    hold: Option<&AutostartHoldRecord>,
    enabled: Option<bool>,
    with_sudo: bool,
    response: &mut PyDict,
) {
    match release_switch_hold(ctx, hold, enabled, with_sudo) {
        Ok(()) => {
            if let Err(error) = remove_hold_file(ctx, path) {
                response.insert("autostart_release_error", PyValue::Str(error));
            }
        }
        Err(error) => {
            response.insert("autostart_release_error", PyValue::Dict(service_error_value(&error)));
        }
    }
    ctx.autostart_lock.take();
}

fn stop_failure_exit(error: &StopFailure) -> i32 {
    match error.cause() {
        StopFailure::Timeout { .. } => 1,
        _ => 2,
    }
}

fn stop_failure_response(old: &SupervisionMode, error: &StopFailure, force: bool) -> PyDict {
    let mut response = match error.cause() {
        StopFailure::Handoff(HandoffError::Busy { code, busy, .. })
        | StopFailure::Relaunched { err: HandoffError::Busy { code, busy, .. }, .. } => {
            let mut response = output_error(if code == ErrorCode::FlashingBusy.as_str() { ErrorCode::FlashingBusy } else { ErrorCode::SessionBusy }, "daemon 仍有操作進行中，拒絕切換監管模式");
            response.insert("handoff", PyValue::Str("refused".into()));
            response.insert("busy", PyValue::List(busy.clone()));
            if let StopFailure::Relaunched { pid, .. } = error.cause() {
                response.insert("relaunched", PyValue::Dict(PyDict::from_pairs(vec![("pid".into(), PyValue::Int(PyInt::Small(i128::from(*pid))))])));
            }
            response
        }
        StopFailure::Handoff(HandoffError::Unconfirmed { code, probe, outcome, pids })
        | StopFailure::Relaunched { err: HandoffError::Unconfirmed { code, probe, outcome, pids }, .. } => {
            let code = if pids.is_empty() { ErrorCode::parse(code).unwrap_or(ErrorCode::InternalError) } else { ErrorCode::DeviceStillHeld };
            let message = if pids.is_empty() { "無法確認 daemon 交接停止結果；請執行 serialwrap daemon status 確認狀態後重試" } else { "daemon 仍有行程但無法確認交接結果；保留安裝 journal" };
            let mut response = output_error(code, message);
            response.insert("probe", PyValue::Str((*probe).into()));
            response.insert("handoff_outcome", PyValue::Str((*outcome).into()));
            response.insert("hint", PyValue::Str("請執行 serialwrap daemon status 確認狀態；確認安全後再執行 serialwrap daemon stop，之後重試 supervision-mode set".into()));
            if !pids.is_empty() {
                response.insert("pids", PyValue::List(pids.iter().map(|pid| PyValue::Int(PyInt::Small(i128::from(*pid)))).collect()));
            }
            response
        }
        StopFailure::Timeout { id, .. } => {
            let mut response = output_error(ErrorCode::Timeout, "舊 daemon 在交接後仍未結束；自動啟動 hold 保留，請確認狀態後重跑 supervision-mode set");
            response.insert("pids", PyValue::List(id.tree.iter().map(|item| PyValue::Int(PyInt::Small(i128::from(item.pid)))).collect()));
            response.insert("phase", PyValue::Str("reconcile_wait_old_exit".into()));
            response.insert("hint", PyValue::Str("舊 daemon 未在時限內結束；監管拉起維持抑制（autostart-hold.json 保留）。確認舊 daemon 已結束後重跑 supervision-mode set 或 setup，會先依紀錄還原監管再繼續".into()));
            response
        }
        StopFailure::Hold(service) | StopFailure::Service(service) => {
            let mut response = output_error(ErrorCode::InternalError, service.hint.as_deref().unwrap_or("監管服務操作失敗"));
            response.insert("reason", PyValue::Str("autostart_hold_unverified".into()));
            response
        }
        StopFailure::RelaunchLoop { pids } => {
            let mut response = output_error(ErrorCode::DeviceStillHeld, "監管模式在交接期間反覆重新啟動 daemon");
            response.insert("reason", PyValue::Str("relaunch_loop".into()));
            response.insert("pids", PyValue::List(pids.iter().map(|pid| PyValue::Int(PyInt::Small(i128::from(*pid)))).collect()));
            response
        }
        other => output_error(ErrorCode::DeviceStillHeld, &format!("監管模式切換停止 daemon 失敗：{other:?}")),
    };
    response.insert("phase", PyValue::Str(if matches!(error.cause(), StopFailure::Timeout { .. }) { "reconcile_wait_old_exit" } else { "supervision_set_safety_check" }.into()));
    response.insert("previous_mode", PyValue::Str(old.as_str().into()));
    if force { response.insert("force_ignored", PyValue::Bool(true)); }
    response
}

fn emit_switch_result(
    ctx: &mut CliCtx<'_>,
    old: &SupervisionMode,
    result: Result<PyDict, crate::setup_cmd::ReconcileError>,
    recovered: Option<PyDict>,
    force: bool,
    _chain: Option<Vec<HandoffLink>>,
) -> i32 {
    match result {
        Ok(setup) => {
            let mut response = PyDict::new();
            response.insert("ok", PyValue::Bool(true));
            response.insert("previous_mode", PyValue::Str(old.as_str().into()));
            response.insert("setup", PyValue::Dict(setup));
            if force { response.insert("force_ignored", PyValue::Bool(true)); }
            if let Some(recovered) = recovered { response.insert("recovered_autostart_hold", PyValue::Dict(recovered)); }
            let _ = crate::output::print_json(&mut ctx.io, &response);
            0
        }
        Err(crate::setup_cmd::ReconcileError::WaitOldExitTimeout { mut partial }) => {
            partial.insert("ok", PyValue::Bool(false));
            partial.insert("error_code", PyValue::Str(ErrorCode::Timeout.as_str().into()));
            partial.insert("phase", PyValue::Str("reconcile_wait_old_exit".into()));
            partial.insert("previous_mode", PyValue::Str(old.as_str().into()));
            if force { partial.insert("force_ignored", PyValue::Bool(true)); }
            emit(ctx, partial, "supervision-mode", 1)
        }
        Err(crate::setup_cmd::ReconcileError::Config(message)) => {
            let mut response = output_error(ErrorCode::InternalError, &message);
            response.insert("phase", PyValue::Str("supervision_set_reconcile".into()));
            response.insert("previous_mode", PyValue::Str(old.as_str().into()));
            if force { response.insert("force_ignored", PyValue::Bool(true)); }
            emit(ctx, response, "supervision-mode", 2)
        }
        Err(crate::setup_cmd::ReconcileError::FlashingBusy(message)) => {
            let mut response = output_error(ErrorCode::FlashingBusy, &message);
            response.insert("phase", PyValue::Str("supervision_set_safety_check".into()));
            response.insert("previous_mode", PyValue::Str(old.as_str().into()));
            if force { response.insert("force_ignored", PyValue::Bool(true)); }
            emit(ctx, response, "supervision-mode", 2)
        }
    }
}
