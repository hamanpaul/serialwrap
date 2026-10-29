//! OD-48（含 2026-10-29 修訂）：0.4.0 `setup` 遇到不支援 `daemon.handoff_stop` 的 0.3.x Python daemon
//! 時改走舊版停止路徑（設計 v2 D1、D3～D5）。
//!
//! 只由 `rollback.rs` 的 install／update（`stop_supervisor`）、回退（`stop_for_revert_with_hold`）與
//! `recover_journal` 呼叫；`supervision-mode set` 不取得此能力（D2）。不送 SIGKILL／TerminateProcess（D4），
//! 失敗時不重啟任何 daemon（OD-48 修訂，D6 由呼叫端處理）。

use std::path::{Path, PathBuf};
use std::time::Duration;

use sw_pal::proc_scan::{ArgvFlavor, DaemonProc, HolderScanStatus, ProcDetails, ProcIdentity};
use sw_pal::service_ctl::{ServiceAction, SupervisionMode};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};

use crate::ctx::CliCtx;
use crate::graceful_stop::{HandoffError, StopFailure, MAX_HANDOFF_CHAIN};

/// D5：「已停」條件須連續維持的穩定窗。
pub(crate) const STABILITY_WINDOW: Duration = Duration::from_secs(5);
/// D5：等待上限（涵蓋 0.3.2 在 Python 3.12 的 executor 收尾，約 300 s）。
pub(crate) const STOP_WAIT_LIMIT: Duration = Duration::from_secs(330);
const POLL: Duration = Duration::from_millis(200);
/// D3.5：legacy 擷取檔（獨立於 install-journal.snapshot，sha256 記進 journal）。
pub(crate) const CAPTURE_FILE: &str = "install-journal.legacy.json";

/// Linux 上可辨識的外部監管者（父行程 basename）；Windows 為 NSSM、服務控制管理員與 Task Scheduler 宿主。
const EXTERNAL_SUPERVISORS: &[&str] = &[
    "supervisord", "runsv", "s6-supervise", "supervise", "monit", "circusd", "pm2", "forever",
    "nssm", "services", "taskhostw", "taskeng", "svchost",
];

/// D1 判定成立的 0.3.x daemon。
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct LegacyDaemon {
    pub endpoint: String,
    pub version: String,
    pub pid: u32,
    /// 行程樹（Windows one-file 為 bootloader 父行程＋Python 子行程）。
    pub tree: Vec<ProcIdentity>,
    pub details: ProcDetails,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum StopMethod {
    SystemctlStop,
    RpcDaemonStop,
}

impl StopMethod {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::SystemctlStop => "systemctl_stop",
            Self::RpcDaemonStop => "rpc_daemon_stop",
        }
    }
}

/// D3 檢查通過、D4 選定停止手段後的計畫（尚未送出任何 stop）。
#[derive(Clone, Debug)]
pub(crate) struct LegacyPlan {
    pub daemon: LegacyDaemon,
    pub method: StopMethod,
    /// MainPID 相符的 systemd 模式（D4）。
    pub unit_mode: Option<SupervisionMode>,
    pub main_pid: Option<u32>,
    pub old_mode: SupervisionMode,
    /// D3.2 基準：所有 serialwrapd 的 (pid, start_ticks)。
    pub baseline: Vec<ProcIdentity>,
    /// 他 uid、排除在 holder 掃描之外的 serialwrapd（報告標註）。
    pub other_uid: Vec<u32>,
    /// D5.2：該 daemon 的 session 所附掛的 UART。
    pub attached_paths: Vec<PathBuf>,
}

/// 停止完成的結果（D7 報告欄位 `legacy_stop`）。
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct LegacyStopped {
    pub method: StopMethod,
    pub from_version: String,
    pub waited_ms: u64,
    pub relaunch_count: usize,
    pub stopped: Vec<ProcIdentity>,
    pub holders_unsupported: bool,
    pub other_uid: Vec<u32>,
    /// 原 daemon 的 argv（D6 on-demand 啟動提示）。
    pub argv: Vec<String>,
    pub unit_mode: Option<SupervisionMode>,
}

/// legacy 路徑的失敗分類。
#[derive(Clone, Debug)]
pub(crate) enum LegacyFailure {
    /// 送出 stop 之前拒絕（D3）；舊 daemon 仍在跑，呼叫端撤銷 hold。
    Refused { code: &'static str, reason: &'static str, message: String, detail: PyDict },
    /// 已送出 stop 後失敗：`pending=true` 為 D5 逾時（hold 與 journal 保留）。
    AfterStop { code: &'static str, reason: &'static str, message: String, detail: PyDict, pending: bool, stopped: bool },
}

/// legacy 擷取檔與 journal 的寫入點（由 `rollback.rs` 提供；D3.5「寫入並 fsync、journal 更新後才送 stop」）。
pub(crate) trait LegacyPersist {
    fn captured(&mut self, ctx: &mut CliCtx<'_>, record: &PyDict) -> Result<(), String>;
    fn stage(&mut self, ctx: &mut CliCtx<'_>, stage: &'static str) -> Result<(), String>;
}

/// `HandoffError` 是否為 D1.1 的拒絕（`daemon.handoff_stop` 回 `METHOD_NOT_FOUND`），且未進入交接鏈。
pub(crate) fn is_method_not_found(error: &StopFailure) -> bool {
    error.chain().is_empty()
        && matches!(error.cause(), StopFailure::Handoff(HandoffError::Unconfirmed { code, outcome: "refused", .. }) if code == ErrorCode::MethodNotFound.as_str())
}

fn int(value: u64) -> PyValue {
    PyValue::Int(PyInt::Small(i128::from(value)))
}

fn str_value(value: impl Into<String>) -> PyValue {
    PyValue::Str(value.into())
}

fn opt_u64(value: Option<u64>) -> PyValue {
    value.map(int).unwrap_or(PyValue::Null)
}

fn as_u32(value: Option<&PyValue>) -> Option<u32> {
    match value {
        Some(PyValue::Int(PyInt::Small(number))) => u32::try_from(*number).ok(),
        _ => None,
    }
}

fn as_str(value: Option<&PyValue>) -> Option<&str> {
    match value {
        Some(PyValue::Str(value)) => Some(value),
        _ => None,
    }
}

fn call(ctx: &CliCtx<'_>, endpoint: &str, method: &str, timeout: f64) -> Option<PyDict> {
    crate::client::call(ctx, endpoint, method, &PyValue::Dict(PyDict::new()), timeout, &PyInt::Small(0))
        .ok()
        .map(|outcome| outcome.resp)
}

/// D1.2：`X.Y.Z` 是否小於 0.4.0；無法解析 → 不成立。
pub(crate) fn version_is_legacy(version: &str) -> bool {
    let mut parts = version.trim().split(|ch: char| ch == '.' || ch == '-' || ch == '+');
    let major = parts.next().and_then(|part| part.parse::<u64>().ok());
    let minor = parts.next().and_then(|part| part.parse::<u64>().ok());
    matches!((major, minor), (Some(0), Some(minor)) if minor < 4)
}

fn flavor(ctx: &CliCtx<'_>) -> ArgvFlavor {
    match ctx.p.caps.path_flavor {
        sw_pal::PalPathFlavor::Windows => ArgvFlavor::Windows,
        sw_pal::PalPathFlavor::Posix => ArgvFlavor::Posix,
    }
}

fn normalize(ctx: &CliCtx<'_>, path: &str) -> String {
    match ctx.p.caps.path_flavor {
        sw_pal::PalPathFlavor::Windows => path.replace('/', "\\").to_ascii_lowercase(),
        sw_pal::PalPathFlavor::Posix => path.to_string(),
    }
}

/// D1.4：路徑是否位於 `<install_root>/versions/` 底下（bundle 管理的 daemon）。
fn under_versions(ctx: &CliCtx<'_>, versions: &Path, path: &str) -> bool {
    let base = normalize(ctx, &versions.to_string_lossy());
    let sep = if ctx.p.caps.path_flavor == sw_pal::PalPathFlavor::Windows { '\\' } else { '/' };
    let base = base.trim_end_matches(sep).to_string();
    let path = normalize(ctx, path);
    path == base || path.starts_with(&format!("{base}{sep}"))
}

fn basename_lower(path: &str) -> String {
    let base = path.rsplit(|ch| ch == '/' || ch == '\\').next().unwrap_or(path).to_ascii_lowercase();
    base.strip_suffix(".exe").map(str::to_string).unwrap_or(base)
}

/// D1：判定 `endpoint` 上回 `METHOD_NOT_FOUND` 的 daemon 是否為 0.3.x Python daemon（全部成立才算）。
pub(crate) fn identify(
    ctx: &CliCtx<'_>,
    endpoint: &str,
    refusal: Option<&PyDict>,
    versions_dir: &Path,
) -> Result<LegacyDaemon, &'static str> {
    // D1.1：呼叫端已確認 METHOD_NOT_FOUND；回應原文缺漏時不推測。
    if let Some(refusal) = refusal {
        if as_str(refusal.get("error_code")) != Some(ErrorCode::MethodNotFound.as_str()) {
            return Err("not_method_not_found");
        }
    }
    // D1.2：版本取自拒絕回應，缺漏時以 health.status.version。
    let status = call(ctx, endpoint, "health.status", 1.0);
    let version = refusal
        .and_then(|refusal| as_str(refusal.get("version")))
        .or_else(|| status.as_ref().and_then(|status| as_str(status.get("version"))))
        .ok_or("version_unknown")?
        .to_string();
    if !version_is_legacy(&version) {
        return Err("version_not_legacy");
    }
    // D1.3：正向辨識行程。pid 以 health.status 為準並須出現在行程掃描中。
    let pid = status.as_ref().and_then(|status| as_u32(status.get("pid"))).ok_or("pid_unknown")?;
    let daemons = ctx.p.procs.serialwrapd_instances().map_err(|_| "process_scan_failed")?;
    let instance = daemons
        .iter()
        .find(|daemon| daemon.pid == pid || daemon.tree.iter().any(|item| item.pid == pid))
        .ok_or("process_not_found")?;
    let root = ctx.p.procs.proc_details(instance.pid).ok_or("process_details_unavailable")?;
    let exe = root.exe.clone().ok_or("process_exe_unknown")?;
    let flavor = flavor(ctx);
    let mut paths = vec![exe.clone()];
    let python_daemon = match flavor {
        ArgvFlavor::Posix => {
            let script = sw_pal::proc_scan::python_daemon_script(&root.argv, flavor);
            if !sw_pal::proc_scan::is_python_interpreter(&exe) || script.is_none() {
                return Err("not_python_daemon");
            }
            paths.extend(script.flatten());
            true
        }
        ArgvFlavor::Windows => {
            let mut matched = false;
            // python.exe -m sw_core.daemon（或 pipx script）單一行程。
            if sw_pal::proc_scan::is_python_interpreter(&exe) {
                if let Some(script) = sw_pal::proc_scan::python_daemon_script(&root.argv, flavor) {
                    paths.extend(script);
                    matched = true;
                }
            }
            // PyInstaller one-file／pipx launcher：serialwrapd.exe 父行程＋子行程（Python 或同映像 bootloader 子行程）。
            if !matched && basename_lower(&exe) == "serialwrapd" && instance.tree.len() >= 2 {
                for child in instance.tree.iter().filter(|item| item.pid != instance.pid) {
                    let details = ctx.p.procs.proc_details(child.pid).ok_or("process_details_unavailable")?;
                    if details.ppid != Some(instance.pid) {
                        return Err("process_tree_mismatch");
                    }
                    if let Some(child_exe) = details.exe {
                        paths.push(child_exe);
                    }
                    paths.extend(sw_pal::proc_scan::python_daemon_script(&details.argv, flavor).flatten());
                }
                matched = true;
            }
            matched
        }
    };
    if !python_daemon {
        return Err("not_python_daemon");
    }
    if let Some(argv0) = root.argv.first() {
        paths.push(argv0.clone());
    }
    // D1.4：執行檔、argv[0] 與 script 都不得位於 bundle 的 versions/ 底下。
    if paths.iter().any(|path| under_versions(ctx, versions_dir, path)) {
        return Err("bundle_managed_daemon");
    }
    let mut tree = instance.tree.clone();
    if !tree.iter().any(|item| item.pid == instance.pid) {
        tree.push(ProcIdentity { pid: instance.pid, start_ticks: root.start_ticks });
    }
    for item in &mut tree {
        if item.start_ticks.is_none() {
            item.start_ticks = ctx.p.procs.proc_details(item.pid).and_then(|details| details.start_ticks);
        }
    }
    tree.sort_by_key(|item| item.pid);
    Ok(LegacyDaemon { endpoint: endpoint.to_string(), version, pid: instance.pid, tree, details: root })
}

fn refused(code: ErrorCode, reason: &'static str, message: impl Into<String>, detail: PyDict) -> LegacyFailure {
    LegacyFailure::Refused { code: code.as_str(), reason, message: message.into(), detail }
}

fn identity_value(items: &[ProcIdentity]) -> PyValue {
    PyValue::List(items.iter().map(|item| PyValue::Dict(PyDict::from_pairs(vec![
        ("pid".into(), int(u64::from(item.pid))),
        ("start_ticks".into(), opt_u64(item.start_ticks)),
    ]))).collect())
}

fn pids_value(pids: &[u32]) -> PyValue {
    PyValue::List(pids.iter().map(|pid| int(u64::from(*pid))).collect())
}

fn same_identity(left: &ProcIdentity, right: &ProcIdentity) -> bool {
    left.pid == right.pid
        && match (left.start_ticks, right.start_ticks) {
            (Some(left), Some(right)) => left == right,
            _ => true,
        }
}

fn identities_of(ctx: &CliCtx<'_>, daemon: &DaemonProc) -> Vec<ProcIdentity> {
    let mut tree = daemon.tree.clone();
    if !tree.iter().any(|item| item.pid == daemon.pid) {
        tree.push(ProcIdentity { pid: daemon.pid, start_ticks: None });
    }
    for item in &mut tree {
        if item.start_ticks.is_none() {
            item.start_ticks = ctx.p.procs.proc_details(item.pid).and_then(|details| details.start_ticks);
        }
    }
    tree
}

fn uid_differs(ctx: &CliCtx<'_>, pid: u32) -> bool {
    let me = ctx.p.procs.current_uid();
    let uid = ctx.p.procs.proc_details(pid).and_then(|details| details.uid);
    matches!((me, uid), (Some(me), Some(uid)) if me != uid)
}

/// D3.2～D3.4 與 D4：送出任何 stop 之前完成的檢查，以及停止手段的選定。無副作用。
pub(crate) fn precheck(
    ctx: &mut CliCtx<'_>,
    daemon: LegacyDaemon,
    old_mode: &SupervisionMode,
) -> Result<LegacyPlan, LegacyFailure> {
    // D3.2 基準掃描：目標以外的同 uid daemon → fail-closed；他 uid 只標註。
    let daemons = ctx.p.procs.serialwrapd_instances().map_err(|_| {
        refused(ErrorCode::DeviceStillHeld, "holder_unknown", "無法掃描 serialwrapd 行程；拒絕停止舊 daemon", PyDict::from_pairs(vec![("holder_unknown".into(), PyValue::Bool(true))]))
    })?;
    let mut baseline = Vec::new();
    let mut others = Vec::new();
    let mut other_uid = Vec::new();
    for instance in &daemons {
        let identities = identities_of(ctx, instance);
        baseline.extend(identities.iter().cloned());
        let is_target = identities.iter().any(|item| daemon.tree.iter().any(|target| target.pid == item.pid));
        if is_target {
            continue;
        }
        if uid_differs(ctx, instance.pid) {
            other_uid.push(instance.pid);
        } else {
            others.push(instance.clone());
        }
    }
    if !others.is_empty() {
        let mut detail = PyDict::new();
        detail.insert("pids", pids_value(&others.iter().map(|item| item.pid).collect::<Vec<_>>()));
        detail.insert("sockets", PyValue::List(others.iter().map(|item| item.socket.clone().map(PyValue::Str).unwrap_or(PyValue::Null)).collect()));
        detail.insert("legacy_pid", int(u64::from(daemon.pid)));
        return Err(refused(ErrorCode::DeviceStillHeld, "other_daemon_present", "除舊 daemon 外仍有同一使用者的 serialwrapd 在執行；請先確認並停止後重試 setup", detail));
    }
    // D3.3 忙碌檢查（0.3.2 無 admission freeze；檢查到 stop 之間的 TOCTOU 為 SC 登記的已接受偏離）。
    let snapshot = match crate::rollback::safety_check(ctx, &daemon.endpoint) {
        Ok(Some(snapshot)) => snapshot,
        Ok(None) => {
            return Err(refused(ErrorCode::DeviceStillHeld, "daemon_presence_unknown", "舊 daemon 在停止前檢查期間失去回應；拒絕停止", PyDict::new()));
        }
        Err((code, detail)) => {
            let code = ErrorCode::parse(code).unwrap_or(ErrorCode::InternalError);
            return Err(refused(code, "legacy_busy", "舊 daemon 忙碌或狀態不明；拒絕停止，請待其完成後重試", detail));
        }
    };
    // D4：實際 pid 是否為 systemd unit 的 MainPID（user 與 system 都查，不只依 config 的模式）。
    let mut unit_mode = None;
    let mut main_pid = None;
    for mode in [SupervisionMode::SystemdUser, SupervisionMode::SystemdSystem] {
        if !ctx.p.service.supported_modes().contains(&mode) {
            continue;
        }
        if let Ok(Some(pid)) = ctx.p.service.main_pid(&mode) {
            if daemon.tree.iter().any(|item| item.pid == pid) {
                unit_mode = Some(mode);
                main_pid = Some(pid);
                break;
            }
        }
    }
    if let Some(mode) = unit_mode.as_ref().filter(|mode| *mode != old_mode) {
        let mut detail = PyDict::new();
        detail.insert("unit_mode", str_value(mode.as_str()));
        detail.insert("configured_mode", str_value(old_mode.as_str()));
        detail.insert("hint", str_value("config.yaml 的 supervision_mode 與實際監管 unit 不符；請以 --user／--system 指定實際模式後重試 setup"));
        return Err(refused(ErrorCode::DeviceStillHeld, "supervisor_mode_mismatch", "舊 daemon 由與設定不同的 systemd 模式監管；拒絕停止", detail));
    }
    // D3.4（OWNER-BATCH #3 裁決）：Linux 以 `/proc/<pid>/cgroup` 判定——daemon 落在非 serialwrap 的 systemd
    // service unit 或其他監管者的 cgroup → 停止前 fail-closed。父行程 basename 清單保留為輔助判定。
    if unit_mode.is_none() {
        for item in &daemon.tree {
            let owner = ctx.p.procs.proc_cgroup(item.pid).map(|content| sw_pal::proc_scan::classify_cgroup(&content));
            if let Some(sw_pal::proc_scan::CgroupOwner::ForeignUnit(unit)) = owner {
                let mut detail = PyDict::new();
                detail.insert("pid", int(u64::from(item.pid)));
                detail.insert("cgroup_unit", str_value(unit));
                detail.insert("hint", str_value("舊 daemon 位於 serialwrap 以外的 systemd unit 或監管者 cgroup；請先停用該監管者再重試 setup"));
                return Err(refused(ErrorCode::DeviceStillHeld, "external_supervisor", "舊 daemon 由外部監管者管理；拒絕停止", detail));
            }
        }
        if let Some(parent) = daemon.details.ppid.and_then(|ppid| ctx.p.procs.proc_details(ppid)) {
            let name = parent.exe.as_deref().map(basename_lower).unwrap_or_default();
            let wrapped = EXTERNAL_SUPERVISORS.contains(&name.as_str())
                || parent.argv.iter().take(2).any(|part| basename_lower(part) == "supervisord");
            if wrapped {
                let mut detail = PyDict::new();
                detail.insert("parent_pid", int(u64::from(parent.pid)));
                detail.insert("parent_exe", parent.exe.clone().map(PyValue::Str).unwrap_or(PyValue::Null));
                detail.insert("hint", str_value("舊 daemon 由外部監管者啟動（NSSM、Task Scheduler、supervisord 等）；請先停用該監管者再重試 setup"));
                return Err(refused(ErrorCode::DeviceStillHeld, "external_supervisor", "舊 daemon 由外部監管者管理；拒絕停止", detail));
            }
        }
    }
    let method = if unit_mode.is_some() { StopMethod::SystemctlStop } else { StopMethod::RpcDaemonStop };
    Ok(LegacyPlan {
        daemon,
        method,
        unit_mode,
        main_pid,
        old_mode: old_mode.clone(),
        baseline,
        other_uid,
        attached_paths: snapshot.attached_paths,
    })
}

/// D3.5：擷取紀錄（只含版本、pid、start_ticks、uid、exe、argv、停止手段、舊模式與 unit 名、MainPID；不含 env）。
pub(crate) fn capture_record(plan: &LegacyPlan) -> PyDict {
    let daemon = &plan.daemon;
    PyDict::from_pairs(vec![
        ("argv".into(), PyValue::List(daemon.details.argv.iter().cloned().map(PyValue::Str).collect())),
        ("exe".into(), daemon.details.exe.clone().map(PyValue::Str).unwrap_or(PyValue::Null)),
        ("main_pid".into(), plan.main_pid.map(|pid| int(u64::from(pid))).unwrap_or(PyValue::Null)),
        ("old_mode".into(), str_value(plan.old_mode.as_str())),
        ("pid".into(), int(u64::from(daemon.pid))),
        ("start_ticks".into(), opt_u64(daemon.details.start_ticks)),
        ("stop_method".into(), str_value(plan.method.as_str())),
        ("tree".into(), identity_value(&daemon.tree)),
        ("uid".into(), daemon.details.uid.map(|uid| int(u64::from(uid))).unwrap_or(PyValue::Null)),
        ("unit".into(), plan.unit_mode.as_ref().map(|_| str_value("serialwrap")).unwrap_or(PyValue::Null)),
        ("unit_mode".into(), plan.unit_mode.as_ref().map(|mode| str_value(mode.as_str())).unwrap_or(PyValue::Null)),
        ("version".into(), str_value(daemon.version.clone())),
    ])
}

/// D4：送出停止（不硬殺）。回傳 Err 代表停止命令本身失敗（stop 可能未生效）。
fn send_stop(ctx: &mut CliCtx<'_>, plan: &LegacyPlan, with_sudo: bool) -> Result<(), String> {
    match (plan.method, plan.unit_mode.as_ref()) {
        (StopMethod::SystemctlStop, Some(mode)) => {
            let result = ctx.p.service.action(mode, ServiceAction::Stop, with_sudo);
            if !result.ok || result.rc.is_some_and(|rc| rc != 0) {
                return Err(result.hint.unwrap_or_else(|| format!("systemctl stop 失敗（rc={:?}）", result.rc)));
            }
            Ok(())
        }
        _ => {
            // 0.3.2 的 daemon.stop 只設 stop_event 並立即回覆；之後的關機進度由 D5 判定，回應內容不採信。
            let _ = crate::client::call(ctx, &plan.daemon.endpoint, "daemon.stop", &PyValue::Dict(PyDict::new()), 2.0, &PyInt::Small(0));
            Ok(())
        }
    }
}

/// D3～D5 的完整停止：`daemon` 已由 D1 判定成立。`persist` 負責擷取檔與 journal（送 stop 之前完成）。
pub(crate) fn stop(
    ctx: &mut CliCtx<'_>,
    daemon: LegacyDaemon,
    old_mode: &SupervisionMode,
    with_sudo: bool,
    versions_dir: &Path,
    persist: &mut dyn LegacyPersist,
) -> Result<LegacyStopped, LegacyFailure> {
    let plan = precheck(ctx, daemon, old_mode)?;
    let from_version = plan.daemon.version.clone();
    let argv = plan.daemon.details.argv.clone();
    let unit_mode = plan.unit_mode.clone();
    let method = plan.method;
    let other_uid = plan.other_uid.clone();
    persist.captured(ctx, &capture_record(&plan)).map_err(|message| {
        refused(ErrorCode::InternalError, "journal_write_failed", format!("legacy 擷取檔或 journal 寫入失敗：{message}"), PyDict::new())
    })?;
    crate::rollback::legacy_crash_if("legacy_stop:after_capture");
    let started = ctx.p.clock.mono_ns();
    if let Err(message) = send_stop(ctx, &plan, with_sudo) {
        let alive = plan.daemon.tree.iter().any(|item| ctx.p.procs.identity_alive(item));
        let mut detail = PyDict::new();
        detail.insert("legacy_daemon", legacy_value(&plan.daemon));
        if alive {
            return Err(refused(ErrorCode::InternalError, "legacy_stop_failed", format!("舊 daemon 停止命令失敗：{message}"), detail));
        }
        return Err(LegacyFailure::AfterStop { code: ErrorCode::InternalError.as_str(), reason: "legacy_stop_failed", message, detail, pending: false, stopped: true });
    }
    persist.stage(ctx, "stop_sent").map_err(|message| LegacyFailure::AfterStop {
        code: ErrorCode::InternalError.as_str(), reason: "journal_write_failed", message, detail: PyDict::new(), pending: true, stopped: false,
    })?;
    crate::rollback::legacy_crash_if("legacy_stop:after_stop_sent");
    let waited = wait_stopped(ctx, plan, with_sudo, versions_dir, persist)?;
    persist.stage(ctx, "stopped").map_err(|message| LegacyFailure::AfterStop {
        code: ErrorCode::InternalError.as_str(), reason: "journal_write_failed", message, detail: PyDict::new(), pending: false, stopped: true,
    })?;
    crate::rollback::legacy_crash_if("legacy_stop:after_stopped");
    let elapsed = ctx.p.clock.mono_ns().saturating_sub(started);
    Ok(LegacyStopped {
        method,
        from_version,
        waited_ms: elapsed / 1_000_000,
        relaunch_count: waited.relaunch_count,
        stopped: waited.stopped,
        holders_unsupported: waited.holders_unsupported,
        other_uid,
        argv,
        unit_mode,
    })
}

struct Waited {
    relaunch_count: usize,
    stopped: Vec<ProcIdentity>,
    holders_unsupported: bool,
}

pub(crate) fn legacy_value(daemon: &LegacyDaemon) -> PyValue {
    PyValue::Dict(PyDict::from_pairs(vec![
        ("pid".into(), int(u64::from(daemon.pid))),
        ("start_ticks".into(), opt_u64(daemon.details.start_ticks)),
        ("tree".into(), identity_value(&daemon.tree)),
        ("version".into(), str_value(daemon.version.clone())),
    ]))
}

/// D5：行程樹全部結束、無同 uid 行程持有 UART、且與基準相比沒有新的 serialwrapd，連續維持穩定窗。
/// 出現新 serialwrapd → 視為重拉：以其自身端點重做 D1＋D3 再停，總停止次數上限 `MAX_HANDOFF_CHAIN`。
fn wait_stopped(
    ctx: &mut CliCtx<'_>,
    mut plan: LegacyPlan,
    with_sudo: bool,
    versions_dir: &Path,
    persist: &mut dyn LegacyPersist,
) -> Result<Waited, LegacyFailure> {
    let started = ctx.p.clock.mono_ns();
    let limit = STOP_WAIT_LIMIT.as_nanos() as u64;
    let window = STABILITY_WINDOW.as_nanos() as u64;
    let mut targets = plan.daemon.tree.clone();
    let mut known = plan.baseline.clone();
    let mut stops = 1usize;
    let mut relaunch_count = 0usize;
    let mut stable_since: Option<u64> = None;
    let mut holders_unsupported = false;
    let mut chain_pids = vec![plan.daemon.pid];
    loop {
        let now = ctx.p.clock.mono_ns();
        let alive = targets.iter().any(|item| ctx.p.procs.identity_alive(item));
        let (holders, status) = if plan.attached_paths.is_empty() {
            (Vec::new(), HolderScanStatus::Ok)
        } else {
            ctx.p.procs.uart_holders_same_uid(&plan.attached_paths)
        };
        if status == HolderScanStatus::Unsupported {
            holders_unsupported = true;
        }
        let holders_clear = holders.is_empty() && matches!(status, HolderScanStatus::Ok | HolderScanStatus::Unsupported);
        let scan = ctx.p.procs.serialwrapd_instances();
        let fresh = match &scan {
            Ok(daemons) => daemons
                .iter()
                .filter(|daemon| {
                    let ids = identities_of(ctx, daemon);
                    !ids.iter().all(|id| known.iter().any(|old| same_identity(id, old)))
                })
                .cloned()
                .collect::<Vec<_>>(),
            Err(_) => Vec::new(),
        };
        if let Some(relaunched) = fresh.first().cloned() {
            relaunch_count += 1;
            stable_since = None;
            chain_pids.push(relaunched.pid);
            if stops >= MAX_HANDOFF_CHAIN {
                let mut detail = PyDict::new();
                detail.insert("reason", str_value("relaunch_loop"));
                detail.insert("pids", pids_value(&chain_pids));
                return Err(LegacyFailure::AfterStop {
                    code: ErrorCode::DeviceStillHeld.as_str(),
                    reason: "relaunch_loop",
                    message: "舊 daemon 停止後反覆被重新啟動".into(),
                    detail,
                    pending: false,
                    stopped: !alive,
                });
            }
            let ids = identities_of(ctx, &relaunched);
            known.extend(ids.iter().cloned());
            let endpoint = relaunched.socket.clone().unwrap_or_else(|| plan.daemon.endpoint.clone());
            let next = restop(ctx, &endpoint, &plan.old_mode, with_sudo, versions_dir, persist, &known)?;
            if let Some(next) = next {
                targets.extend(next.daemon.tree.iter().cloned());
                known.extend(next.baseline.iter().cloned());
                let mut paths = std::mem::take(&mut plan.attached_paths);
                paths.extend(next.attached_paths.iter().cloned());
                plan = next;
                plan.attached_paths = paths;
            } else {
                targets.extend(ids);
            }
            stops += 1;
            continue;
        }
        if !alive && holders_clear && scan.is_ok() {
            let since = *stable_since.get_or_insert(now);
            if now.saturating_sub(since) >= window {
                return Ok(Waited { relaunch_count, stopped: targets, holders_unsupported });
            }
        } else {
            stable_since = None;
        }
        if now.saturating_sub(started) >= limit {
            let mut detail = PyDict::new();
            detail.insert("legacy_daemon", legacy_value(&plan.daemon));
            detail.insert("alive_pids", pids_value(&targets.iter().filter(|item| ctx.p.procs.identity_alive(item)).map(|item| item.pid).collect::<Vec<_>>()));
            detail.insert("holder_pids", pids_value(&holders));
            detail.insert("waited_ms", int(now.saturating_sub(started) / 1_000_000));
            return Err(LegacyFailure::AfterStop {
                code: ErrorCode::Timeout.as_str(),
                reason: "legacy_stop_pending",
                message: "舊 daemon 停止後在等待上限內未完全結束；保留 journal 與自動啟動 hold，未重啟任何 daemon".into(),
                detail,
                pending: true,
                stopped: false,
            });
        }
        ctx.p.clock.sleep(POLL);
    }
}

/// 對重拉的 daemon 重做 D1＋D3 後再停。Rust daemon（交接成功）回 `None`；交接本身即為停止。
fn restop(
    ctx: &mut CliCtx<'_>,
    endpoint: &str,
    old_mode: &SupervisionMode,
    with_sudo: bool,
    versions_dir: &Path,
    persist: &mut dyn LegacyPersist,
    known: &[ProcIdentity],
) -> Result<Option<LegacyPlan>, LegacyFailure> {
    let after_stop = |reason: &'static str, message: String, detail: PyDict| LegacyFailure::AfterStop {
        code: ErrorCode::DeviceStillHeld.as_str(), reason, message, detail, pending: false, stopped: true,
    };
    match crate::graceful_stop::request_handoff_once(ctx, endpoint, "update") {
        Ok(_) => return Ok(None),
        Err(HandoffError::Unconfirmed { code, .. }) if code == ErrorCode::MethodNotFound.as_str() => {}
        Err(HandoffError::Unconfirmed { code, .. }) if code == ErrorCode::SocketError.as_str() => {
            // 重拉者尚未開始服務 RPC：以行程辨識；無法辨識即 fail-closed。
        }
        Err(error) => {
            let mut detail = PyDict::new();
            detail.insert("handoff_error", str_value(format!("{error:?}")));
            return Err(after_stop("relaunched_daemon_refused", "重新出現的 daemon 拒絕停止".into(), detail));
        }
    }
    let refusal = crate::graceful_stop::take_last_handoff_refusal(endpoint);
    let daemon = identify(ctx, endpoint, refusal.as_ref(), versions_dir).map_err(|reason| {
        let mut detail = PyDict::new();
        detail.insert("legacy_check", str_value(reason));
        after_stop("relaunched_daemon_not_legacy", "重新出現的 daemon 無法辨識為 0.3.x；不停止".into(), detail)
    })?;
    let mut plan = match precheck(ctx, daemon, old_mode) {
        Ok(plan) => plan,
        Err(LegacyFailure::Refused { code, reason, message, detail }) => {
            return Err(LegacyFailure::AfterStop { code, reason, message, detail, pending: false, stopped: true });
        }
        Err(other) => return Err(other),
    };
    // 基準只看此前未見過的行程：先前目標仍在結束中，不得被當成「其他 daemon」。
    plan.baseline.retain(|id| !known.iter().any(|old| same_identity(id, old)));
    persist.captured(ctx, &capture_record(&plan)).map_err(|message| after_stop("journal_write_failed", message, PyDict::new()))?;
    send_stop(ctx, &plan, with_sudo).map_err(|message| after_stop("legacy_stop_failed", message, PyDict::new()))?;
    Ok(Some(plan))
}

impl LegacyStopped {
    /// D7 報告欄位 `legacy_stop`。
    pub(crate) fn value(&self) -> PyValue {
        let mut value = PyDict::from_pairs(vec![
            ("from_version".into(), str_value(self.from_version.clone())),
            ("method".into(), str_value(self.method.as_str())),
            ("relaunch_count".into(), int(self.relaunch_count as u64)),
            ("stability_window_ms".into(), int(STABILITY_WINDOW.as_millis() as u64)),
            ("waited_ms".into(), int(self.waited_ms)),
        ]);
        value.insert("stopped", identity_value(&self.stopped));
        if self.holders_unsupported {
            // D5.2：Windows 無 holder 掃描，以行程樹結束推定。
            value.insert("holders_status", str_value("unsupported"));
        }
        if !self.other_uid.is_empty() {
            value.insert("other_uid_pids", pids_value(&self.other_uid));
        }
        PyValue::Dict(value)
    }
}

/// D6：依原監管手段給的啟動指令（不保證 0.3.2 `daemon start` 可用）。
pub(crate) fn restart_hint(mode: &SupervisionMode, argv: &[String]) -> String {
    match mode {
        SupervisionMode::SystemdUser => "systemctl --user start serialwrap".into(),
        SupervisionMode::SystemdSystem => "sudo systemctl start serialwrap".into(),
        _ if argv.is_empty() => "請以原本的方式重新啟動 0.3.x daemon".into(),
        _ => format!(
            "請以原命令重新啟動舊 daemon：{}（0.3.2 的 serialwrap daemon start 在 pipx 安裝下可能無法使用）",
            argv.join(" ")
        ),
    }
}

/// D6 報告欄位：`legacy_daemon_stopped`、`restored` 與啟動指令。
pub(crate) fn annotate_failure(detail: &mut PyDict, stopped: bool, restored: bool, mode: &SupervisionMode, argv: &[String]) {
    detail.insert("legacy_daemon_stopped", PyValue::Bool(stopped));
    detail.insert("restored", PyValue::Bool(restored));
    if stopped {
        detail.insert("restart_hint", str_value(restart_hint(mode, argv)));
        detail.insert("message_detail", str_value(if restored { "舊 daemon 已停止，安裝已還原；未自動重啟任何 daemon" } else { "舊 daemon 已停止，安裝未完全還原；未自動重啟任何 daemon" }));
    }
}

/// D5（recover）：擷取檔中的行程是否已全部結束。
pub(crate) fn captured_alive(ctx: &CliCtx<'_>, capture: &PyDict) -> Vec<u32> {
    let mut alive = Vec::new();
    if let Some(PyValue::List(daemons)) = capture.get("daemons") {
        for record in daemons {
            let PyValue::Dict(record) = record else { continue };
            if let Some(PyValue::List(tree)) = record.get("tree") {
                for item in tree {
                    let PyValue::Dict(item) = item else { continue };
                    let Some(pid) = as_u32(item.get("pid")) else { continue };
                    let start_ticks = match item.get("start_ticks") {
                        Some(PyValue::Int(PyInt::Small(value))) => u64::try_from(*value).ok(),
                        _ => None,
                    };
                    if ctx.p.procs.identity_alive(&ProcIdentity { pid, start_ticks }) {
                        alive.push(pid);
                    }
                }
            }
        }
    }
    alive
}

/// 擷取檔第一筆的 argv 與舊模式（D6 啟動提示）。
pub(crate) fn captured_argv(capture: &PyDict) -> Vec<String> {
    match capture.get("daemons") {
        Some(PyValue::List(daemons)) => match daemons.first() {
            Some(PyValue::Dict(record)) => match record.get("argv") {
                Some(PyValue::List(argv)) => argv.iter().filter_map(|part| as_str(Some(part)).map(str::to_string)).collect(),
                _ => Vec::new(),
            },
            _ => Vec::new(),
        },
        _ => Vec::new(),
    }
}
