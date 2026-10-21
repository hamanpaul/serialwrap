//! 共用 daemon graceful stop：先抑制監管重拉，再送停止 RPC 並等待行程樹結束。

use std::path::PathBuf;
use std::time::Duration;

use sw_pal::proc_scan::ProcIdentity;
use sw_pal::service_ctl::{AutostartHoldRecord, ServiceResult, SupervisionMode};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};

use crate::client::{self, ClientCrash};
use crate::ctx::CliCtx;
pub use crate::rollback::HandoffError;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DaemonIdentity {
    pub pid: Option<u32>,
    pub tree: Vec<ProcIdentity>,
    pub known: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub enum StopFailure {
    Timeout {
        phase: &'static str,
        id: DaemonIdentity,
        ran: Vec<Vec<String>>,
    },
    Service(ServiceResult),
    Handoff(HandoffError),
    Relaunched {
        pid: u32,
        err: HandoffError,
    },
    Hold(ServiceResult),
    RelaunchLoop {
        pids: Vec<u32>,
    },
    HandoffContext {
        cause: Box<StopFailure>,
        hold: Option<AutostartHoldRecord>,
        chain: Vec<HandoffLink>,
    },
}

impl StopFailure {
    pub fn hold(&self) -> Option<&AutostartHoldRecord> {
        match self {
            Self::HandoffContext { hold, .. } => hold.as_ref(),
            _ => None,
        }
    }

    pub fn chain(&self) -> &[HandoffLink] {
        match self {
            Self::HandoffContext { chain, .. } => chain,
            _ => &[],
        }
    }

    pub fn cause(&self) -> &StopFailure {
        match self {
            Self::HandoffContext { cause, .. } => cause,
            other => other,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct HandoffLink {
    pub token: String,
    pub pid: Option<u32>,
    pub relaunched: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub struct StopReport {
    pub graceful: bool,
    pub ran: Vec<Vec<String>>,
    pub rc: Option<i32>,
    pub stdout: String,
    pub stderr: String,
    pub handoff_token: Option<String>,
    pub chain: Vec<HandoffLink>,
    pub hold: Option<AutostartHoldRecord>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StopRpc {
    Plain,
    Handoff { reason: &'static str },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HandoffWait {
    Gone,
    Relaunched(DaemonIdentity),
    TimedOut,
}

pub const MAX_HANDOFF_CHAIN: usize = 3;

pub fn graceful_stop(
    ctx: &mut CliCtx<'_>,
    mode: &SupervisionMode,
    endpoint: &str,
    with_sudo: bool,
    keep_disabled: bool,
    phase: &'static str,
) -> Result<StopReport, StopFailure> {
    graceful_stop_with(
        ctx,
        mode,
        endpoint,
        with_sudo,
        keep_disabled,
        phase,
        StopRpc::Plain,
    )
}

pub fn graceful_stop_with(
    ctx: &mut CliCtx<'_>,
    mode: &SupervisionMode,
    endpoint: &str,
    with_sudo: bool,
    keep_disabled: bool,
    phase: &'static str,
    rpc: StopRpc,
) -> Result<StopReport, StopFailure> {
    graceful_stop_with_existing_hold(ctx, mode, endpoint, with_sudo, keep_disabled, phase, rpc, None)
}

/// 使用呼叫端已取得並持久化的 hold 完成交接，避免重複改寫監管狀態。
pub fn graceful_stop_with_existing_hold(
    ctx: &mut CliCtx<'_>,
    mode: &SupervisionMode,
    endpoint: &str,
    with_sudo: bool,
    keep_disabled: bool,
    phase: &'static str,
    rpc: StopRpc,
    existing_hold: Option<AutostartHoldRecord>,
) -> Result<StopReport, StopFailure> {
    let mut id = crate::commands::daemon_cmd::capture_daemon_identity_public(ctx, endpoint);
    let initial_id = id.clone();
    let mut first_handoff_relaunched = false;
    let mut hold = existing_hold;
    let mut ran = Vec::new();
    let mut rc = None;
    let mut stdout = String::new();
    let mut stderr = String::new();

    match rpc {
        StopRpc::Plain => {
            if matches!(
                mode,
                SupervisionMode::WindowsTask | SupervisionMode::WindowsService
            ) {
                if matches!(mode, SupervisionMode::WindowsService) && !with_sudo {
                    return Err(StopFailure::Service(ServiceResult {
                        ok: false,
                        mode: mode.as_str().to_string(),
                        action: "stop".to_string(),
                        rc: None,
                        stdout: None,
                        stderr: None,
                        error_code: Some(ErrorCode::NeedsSudo.as_str()),
                        hint: Some("需系統管理員權限：請在系統管理員 PowerShell 執行 `sc.exe config serialwrap start= disabled`（或加 --with-sudo 讓本指令代跑）".to_string()),
                        phase: Some("service_disable"),
                    }));
                }
                let commands = ctx.p.service.set_enabled(mode, false, with_sudo);
                if let Some(failed) = commands.iter().find(|command| command.rc != 0) {
                    return Err(StopFailure::Service(service_failure(
                        mode,
                        "disable",
                        failed.rc,
                        "service_disable",
                    )));
                }
                append_commands(&mut ran, &commands);
            }
        }
        StopRpc::Handoff { .. } => {
            if matches!(mode, SupervisionMode::OnDemand) {
                if let Err(error) = acquire_autostart_lock(ctx) {
                    let mut failure = service_failure(mode, "hold_autostart", 1, "autostart_lock");
                    if error == ErrorCode::Timeout {
                        failure.error_code = Some(ErrorCode::Timeout.as_str());
                    }
                    return Err(with_handoff_context(
                        StopFailure::Hold(failure),
                        None,
                        Vec::new(),
                    ));
                }
            } else {
                if hold.is_none() {
                    let held = ctx
                        .p
                        .service
                        .hold_autostart(mode, with_sudo)
                        .map_err(StopFailure::Hold)?;
                    append_commands(&mut ran, &held.ran);
                    hold = Some(held);
                }
            }
            id = crate::commands::daemon_cmd::capture_daemon_identity_public(ctx, endpoint);
            first_handoff_relaunched = id != initial_id && id.known;
        }
    }

    if rpc == StopRpc::Plain {
        let _ = call_stop(ctx, endpoint, "daemon.stop", &PyDict::new());
        ran.push(vec!["serialwrap".into(), "daemon".into(), "stop".into()]);
        if !crate::commands::daemon_cmd::wait_daemon_gone(
            ctx,
            endpoint,
            &id,
            Duration::from_secs(10),
        ) {
            if !matches!(
                mode,
                SupervisionMode::WindowsTask | SupervisionMode::WindowsService
            ) {
                return Err(StopFailure::Timeout { phase, id, ran });
            }
            let hard = ctx.p.service.hard_stop(mode, with_sudo);
            record_hard_stop(&mut ran, mode, with_sudo, &hard);
            rc = hard.rc;
            stdout.push_str(hard.stdout.as_deref().unwrap_or(""));
            stderr.push_str(hard.stderr.as_deref().unwrap_or(""));
            if !hard.ok {
                if !keep_disabled {
                    restore_enabled(ctx, mode, with_sudo, &mut ran)?;
                }
                return Err(StopFailure::Service(hard));
            }
            if !crate::commands::daemon_cmd::wait_daemon_gone(
                ctx,
                endpoint,
                &id,
                Duration::from_secs(5),
            ) {
                if !keep_disabled {
                    restore_enabled(ctx, mode, with_sudo, &mut ran)?;
                }
                return Err(StopFailure::Timeout { phase, id, ran });
            }
            if !keep_disabled {
                restore_enabled(ctx, mode, with_sudo, &mut ran)?;
            }
            return Ok(StopReport {
                graceful: false,
                ran,
                rc,
                stdout,
                stderr,
                handoff_token: None,
                chain: Vec::new(),
                hold: None,
            });
        }
        if !keep_disabled
            && matches!(
                mode,
                SupervisionMode::WindowsTask | SupervisionMode::WindowsService
            )
        {
            restore_enabled(ctx, mode, with_sudo, &mut ran)?;
        }
        return Ok(StopReport {
            graceful: true,
            ran,
            rc,
            stdout,
            stderr,
            handoff_token: None,
            chain: Vec::new(),
            hold: None,
        });
    }

    let StopRpc::Handoff { reason } = rpc else {
        unreachable!()
    };
    let mut chain = Vec::new();
    loop {
        if chain.len() >= MAX_HANDOFF_CHAIN {
            let mut pids = chain.iter()
                .filter(|link: &&HandoffLink| link.relaunched)
                .filter_map(|link| link.pid)
                .collect::<Vec<_>>();
            if let Some(pid) = id.pid { pids.push(pid); }
            return Err(with_handoff_context(
                StopFailure::RelaunchLoop { pids },
                hold.clone(),
                chain,
            ));
        }
        let token = match request_handoff(ctx, endpoint, reason) {
            Ok(token) => token,
            Err(mut error) => {
                attach_handoff_pid(&mut error, id.pid);
                if chain.is_empty() {
                    return Err(with_handoff_context(
                        StopFailure::Handoff(error),
                        hold.clone(),
                        chain,
                    ));
                }
                return Err(with_handoff_context(
                    StopFailure::Relaunched {
                        pid: id.pid.unwrap_or_default(),
                        err: error,
                    },
                    hold.clone(),
                    chain,
                ));
            }
        };
        crate::rollback::test_pause_after("handoff_stop:replied");
        chain.push(HandoffLink {
            token: token.clone(),
            pid: id.pid,
            relaunched: !chain.is_empty() || first_handoff_relaunched,
        });
        first_handoff_relaunched = false;
        match wait_handoff_gone(ctx, endpoint, &id, Duration::from_secs(10)) {
            HandoffWait::Gone => {
                let hard_stopped = match ensure_released_or_rehandoff_inner(
                    ctx, mode, endpoint, with_sudo, reason, phase, &mut chain,
                ) {
                    Ok(value) => value,
                    Err(error) => {
                        return Err(with_handoff_context(error, hold.clone(), chain));
                    }
                };
                let final_token = chain.last().map(|link| link.token.clone()).or(Some(token));
                return Ok(StopReport {
                    graceful: !hard_stopped,
                    ran,
                    rc,
                    stdout,
                    stderr,
                    handoff_token: final_token,
                    chain,
                    hold,
                });
            }
            HandoffWait::Relaunched(new_id) => {
                id = new_id;
            }
            HandoffWait::TimedOut => {
                let current =
                    crate::commands::daemon_cmd::capture_daemon_identity_public(ctx, endpoint);
                if same_identity(&current, &id) && current.known {
                    if !mode.is_supervised() {
                        return Err(with_handoff_context(
                            StopFailure::Timeout {
                                phase,
                                id: current,
                                ran: ran.clone(),
                            },
                            hold.clone(),
                            chain,
                        ));
                    }
                    let hard = ctx.p.service.hard_stop(mode, with_sudo);
                    record_hard_stop(&mut ran, mode, with_sudo, &hard);
                    rc = hard.rc;
                    stdout.push_str(hard.stdout.as_deref().unwrap_or(""));
                    stderr.push_str(hard.stderr.as_deref().unwrap_or(""));
                    if !hard.ok {
                        return Err(with_handoff_context(
                            StopFailure::Service(hard),
                            hold.clone(),
                            chain,
                        ));
                    }
                    if !crate::commands::daemon_cmd::wait_daemon_gone(
                        ctx,
                        endpoint,
                        &current,
                        Duration::from_secs(5),
                    ) {
                        return Err(with_handoff_context(
                            StopFailure::Timeout {
                                phase,
                                id: current,
                                ran: ran.clone(),
                            },
                            hold.clone(),
                            chain,
                        ));
                    }
                    match ensure_released_or_rehandoff_inner(
                        ctx, mode, endpoint, with_sudo, reason, phase, &mut chain,
                    ) {
                        Ok(_) => {
                            let final_token =
                                chain.last().map(|link| link.token.clone()).or(Some(token));
                            return Ok(StopReport {
                                graceful: false,
                                ran,
                                rc,
                                stdout,
                                stderr,
                                handoff_token: final_token,
                                chain,
                                hold,
                            });
                        }
                        Err(error) => {
                            return Err(with_handoff_context(error, hold.clone(), chain));
                        }
                    }
                }
                if current.known {
                    id = current;
                    continue;
                }
                return Err(with_handoff_context(
                    StopFailure::Timeout {
                        phase,
                        id: current,
                        ran: ran.clone(),
                    },
                    hold.clone(),
                    chain,
                ));
            }
        }
    }
}

fn with_handoff_context(
    cause: StopFailure,
    hold: Option<AutostartHoldRecord>,
    chain: Vec<HandoffLink>,
) -> StopFailure {
    StopFailure::HandoffContext {
        cause: Box::new(cause),
        hold,
        chain,
    }
}

pub fn wait_handoff_gone(
    ctx: &CliCtx<'_>,
    endpoint: &str,
    id: &DaemonIdentity,
    timeout: Duration,
) -> HandoffWait {
    let started = ctx.p.clock.mono_ns();
    let limit = timeout.as_nanos().min(u64::MAX as u128) as u64;
    loop {
        let ping = client::call(
            ctx,
            endpoint,
            "health.ping",
            &PyValue::Dict(PyDict::new()),
            0.5,
            &PyInt::Small(0),
        )
        .ok()
        .is_some_and(|outcome| outcome.resp.get_truthy("ok"));
        let old_alive = id
            .tree
            .iter()
            .any(|identity| ctx.p.procs.identity_alive(identity));
        if !ping && !old_alive {
            return HandoffWait::Gone;
        }
        if ping {
            let status = client::call(
                ctx,
                endpoint,
                "health.status",
                &PyValue::Dict(PyDict::new()),
                1.0,
                &PyInt::Small(0),
            )
            .ok();
            let pid = status
                .as_ref()
                .and_then(|outcome| outcome.resp.get("pid"))
                .and_then(int_as_u32);
            if id.known && !id.tree.is_empty() {
                if let Some(pid) = pid {
                    if !id.tree.iter().any(|identity| identity.pid == pid) {
                        return HandoffWait::Relaunched(
                            crate::commands::daemon_cmd::capture_daemon_identity_public(
                                ctx, endpoint,
                            ),
                        );
                    }
                }
                if let Ok(daemons) = ctx.p.procs.serialwrapd_instances() {
                    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
                    let current = daemons.into_iter().find(|daemon| {
                        daemon.socket.as_deref().is_some_and(|socket| {
                            crate::commands::daemon_cmd::normalize_endpoint_for_context(
                                ctx, socket, &cwd,
                            ) == crate::commands::daemon_cmd::normalize_endpoint_for_context(
                                ctx, endpoint, &cwd,
                            )
                        })
                    });
                    if let Some(daemon) = current {
                        if !daemon.tree.iter().any(|candidate| {
                            id.tree.iter().any(|old| same_proc_identity(candidate, old))
                        }) {
                            let mut tree = daemon.tree;
                            if !tree.iter().any(|identity| identity.pid == daemon.pid) {
                                tree.push(ProcIdentity {
                                    pid: daemon.pid,
                                    start_ticks: None,
                                });
                            }
                            return HandoffWait::Relaunched(DaemonIdentity {
                                pid: Some(daemon.pid),
                                known: true,
                                tree,
                            });
                        }
                    }
                }
            }
        }
        if ctx.p.clock.mono_ns().saturating_sub(started) >= limit {
            return HandoffWait::TimedOut;
        }
        ctx.p.clock.sleep(Duration::from_millis(200));
    }
}

pub fn wait_daemon_gone(
    ctx: &CliCtx<'_>,
    endpoint: &str,
    id: &DaemonIdentity,
    timeout: Duration,
) -> bool {
    crate::commands::daemon_cmd::wait_daemon_gone(ctx, endpoint, id, timeout)
}

pub fn ensure_released_or_rehandoff(
    ctx: &mut CliCtx<'_>,
    mode: &SupervisionMode,
    endpoint: &str,
    with_sudo: bool,
    reason: &'static str,
    phase: &'static str,
    chain: &mut Vec<HandoffLink>,
) -> Result<(), StopFailure> {
    ensure_released_or_rehandoff_inner(ctx, mode, endpoint, with_sudo, reason, phase, chain)
        .map(|_| ())
}

fn ensure_released_or_rehandoff_inner(
    ctx: &mut CliCtx<'_>,
    mode: &SupervisionMode,
    endpoint: &str,
    with_sudo: bool,
    reason: &'static str,
    phase: &'static str,
    chain: &mut Vec<HandoffLink>,
) -> Result<bool, StopFailure> {
    let mut hard_stopped = false;
    loop {
        let ping = client::call(
            ctx,
            endpoint,
            "health.ping",
            &PyValue::Dict(PyDict::new()),
            0.5,
            &PyInt::Small(0),
        )
        .ok()
        .is_some_and(|outcome| outcome.resp.get_truthy("ok"));
        let current = scan_endpoint_identity(ctx, endpoint, phase)?;
        if !ping && current.is_none() {
            return Ok(hard_stopped);
        }
        let Some(id) = current else {
            return Err(StopFailure::Timeout {
                phase,
                id: DaemonIdentity::default(),
                ran: Vec::new(),
            });
        };
        if chain.len() >= MAX_HANDOFF_CHAIN {
            let mut pids = chain.iter()
                .filter(|link| link.relaunched)
                .filter_map(|link| link.pid)
                .collect::<Vec<_>>();
            if let Some(pid) = id.pid {
                pids.push(pid);
            }
            return Err(StopFailure::RelaunchLoop { pids });
        }
        let token = request_handoff(ctx, endpoint, reason).map_err(|mut err| {
            attach_handoff_pid(&mut err, id.pid);
            if chain.is_empty() {
                StopFailure::Handoff(err)
            } else {
                StopFailure::Relaunched {
                    pid: id.pid.unwrap_or_default(),
                    err,
                }
            }
        })?;
        let relaunched = !chain.is_empty();
        chain.push(HandoffLink {
            token,
            pid: id.pid,
            relaunched,
        });
        match wait_handoff_gone(ctx, endpoint, &id, Duration::from_secs(10)) {
            HandoffWait::Gone => continue,
            HandoffWait::Relaunched(_) => continue,
            HandoffWait::TimedOut => {
                let latest =
                    crate::commands::daemon_cmd::capture_daemon_identity_public(ctx, endpoint);
                if !latest.known || !same_identity(&latest, &id) || !mode.is_supervised() {
                    if latest.known {
                        continue;
                    }
                    return Err(StopFailure::Timeout {
                        phase,
                        id: latest,
                        ran: Vec::new(),
                    });
                }
                let hard = ctx.p.service.hard_stop(mode, with_sudo);
                if !hard.ok {
                    return Err(StopFailure::Service(hard));
                }
                hard_stopped = true;
                if !crate::commands::daemon_cmd::wait_daemon_gone(
                    ctx,
                    endpoint,
                    &latest,
                    Duration::from_secs(5),
                ) {
                    return Err(StopFailure::Timeout {
                        phase,
                        id: latest,
                        ran: Vec::new(),
                    });
                }
            }
        }
    }
}

fn scan_endpoint_identity(
    ctx: &CliCtx<'_>,
    endpoint: &str,
    phase: &'static str,
) -> Result<Option<DaemonIdentity>, StopFailure> {
    let daemons = ctx
        .p
        .procs
        .serialwrapd_instances()
        .map_err(|_| StopFailure::Timeout {
            phase,
            id: DaemonIdentity::default(),
            ran: Vec::new(),
        })?;
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let target = crate::commands::daemon_cmd::normalize_endpoint_for_context(ctx, endpoint, &cwd);
    let mut matched = None;
    for daemon in daemons {
        let Some(socket) = daemon.socket.as_deref() else {
            return Err(StopFailure::Timeout {
                phase,
                id: DaemonIdentity::default(),
                ran: Vec::new(),
            });
        };
        if crate::commands::daemon_cmd::normalize_endpoint_for_context(ctx, socket, &cwd) == target
        {
            let mut tree = daemon.tree;
            if !tree.iter().any(|identity| identity.pid == daemon.pid) {
                tree.push(ProcIdentity {
                    pid: daemon.pid,
                    start_ticks: None,
                });
            }
            matched = Some(DaemonIdentity {
                pid: Some(daemon.pid),
                known: !tree.is_empty(),
                tree,
            });
            break;
        }
    }
    Ok(matched)
}

fn attach_handoff_pid(error: &mut HandoffError, pid: Option<u32>) {
    if let (HandoffError::Unconfirmed { code, pids, .. }, Some(pid)) = (error, pid) {
        if code != ErrorCode::SocketError.as_str() && code != ErrorCode::Timeout.as_str() {
            return;
        }
        if !pids.contains(&pid) {
            pids.push(pid);
        }
    }
}

fn request_handoff(ctx: &CliCtx<'_>, endpoint: &str, reason: &str) -> Result<String, HandoffError> {
    let params = PyValue::Dict(PyDict::from_pairs(vec![(
        "reason".to_string(),
        PyValue::Str(reason.to_string()),
    )]));
    let (outcome, failure) = client::call_with_failure(
        ctx,
        endpoint,
        "daemon.handoff_stop",
        &params,
        2.0,
        &PyInt::Small(0),
    )
    .map_err(|error| HandoffError::Unconfirmed {
        code: ErrorCode::InternalError.as_str().to_string(),
        probe: "daemon.handoff_stop",
        outcome: match error {
            ClientCrash::InvalidTimeout(_) | ClientCrash::EncodeError(_) => "client_error",
        },
        pids: Vec::new(),
    })?;
    if let Some(failure) = failure {
        return Err(HandoffError::Unconfirmed {
            code: if failure.pal_kind == Some(sw_pal::PalErrorKind::TimedOut) { ErrorCode::Timeout.as_str() } else { ErrorCode::SocketError.as_str() }.to_string(),
            probe: "daemon.handoff_stop",
            outcome: match failure.phase { client::FailPhase::Connect => "refused", client::FailPhase::Send | client::FailPhase::Recv => "unknown" },
            pids: Vec::new(),
        });
    }
    match outcome.resp.get("ok") {
        Some(PyValue::Bool(true)) => {
            if matches!(outcome.resp.get("stopping"), Some(PyValue::Bool(true))) {
                if let Some(PyValue::Str(token)) = outcome.resp.get("handoff_token") {
                    if valid_handoff_token(token) {
                        return Ok(token.clone());
                    }
                }
            }
            Err(HandoffError::Unconfirmed {
                code: ErrorCode::InvalidResponse.as_str().to_string(),
                probe: "daemon.handoff_stop",
                outcome: "refused",
                pids: Vec::new(),
            })
        }
        Some(PyValue::Bool(false)) => {
            let code = outcome
                .resp
                .get("error_code")
                .and_then(|value| match value {
                    PyValue::Str(value) => Some(value.clone()),
                    _ => None,
                })
                .unwrap_or_else(|| "UNKNOWN_ERROR".to_string());
            if code == ErrorCode::FlashingBusy.as_str() || code == ErrorCode::SessionBusy.as_str() {
                let busy = outcome.resp.get("busy").and_then(|value| match value { PyValue::List(items) => Some(items.clone()), _ => None }).unwrap_or_default();
                let ops = busy.first()
                    .and_then(|value| match value { PyValue::Dict(item) => item.get("ops"), _ => None })
                    .and_then(|value| match value { PyValue::List(items) => Some(items.iter().filter_map(|value| match value { PyValue::Str(value) => Some(value.clone()), _ => None }).collect()), _ => None })
                    .or_else(|| outcome.resp.get("ops").and_then(|value| match value { PyValue::List(items) => Some(items.iter().filter_map(|value| match value { PyValue::Str(value) => Some(value.clone()), _ => None }).collect()), _ => None }))
                    .unwrap_or_default();
                Err(HandoffError::Busy { code, ops, busy })
            } else {
                Err(HandoffError::Unconfirmed {
                    code,
                    probe: "daemon.handoff_stop",
                    outcome: "refused",
                    pids: Vec::new(),
                })
            }
        }
        _ => Err(HandoffError::Unconfirmed {
            code: ErrorCode::InvalidResponse.as_str().to_string(),
            probe: "daemon.handoff_stop",
            outcome: "refused",
            pids: Vec::new(),
        }),
    }
}

fn valid_handoff_token(token: &str) -> bool {
    token.len() == 32
        && token
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn call_stop(
    ctx: &CliCtx<'_>,
    endpoint: &str,
    method: &str,
    params: &PyDict,
) -> Result<(), ClientCrash> {
    let outcome = client::call(
        ctx,
        endpoint,
        method,
        &PyValue::Dict(params.clone()),
        2.0,
        &PyInt::Small(0),
    )?;
    if outcome.resp.get_truthy("ok") {
        Ok(())
    } else {
        Ok(())
    }
}

fn acquire_autostart_lock(ctx: &mut CliCtx<'_>) -> Result<(), ErrorCode> {
    if ctx
        .autostart_lock
        .as_ref()
        .is_some_and(|guard| guard.is_held())
    {
        return Ok(());
    }
    let path = PathBuf::from(&ctx.paths.run_dir).join("autostart.lock");
    if let Some(parent) = path.parent() {
        ctx.p
            .fs
            .mkdirs(parent)
            .map_err(|_| ErrorCode::InternalError)?;
    }
    let start = ctx.p.clock.mono_ns();
    loop {
        match ctx.p.fs.file_lock_exclusive(&path, false) {
            Ok(guard) => {
                ctx.autostart_lock = Some(guard);
                return Ok(());
            }
            Err(error) if error.kind == sw_pal::PalErrorKind::WouldBlock => {
                if ctx.p.clock.mono_ns().saturating_sub(start) >= 15_000_000_000 {
                    return Err(ErrorCode::Timeout);
                }
                ctx.p.clock.sleep(Duration::from_millis(200));
            }
            Err(_) => return Err(ErrorCode::InternalError),
        }
    }
}

fn restore_enabled(
    ctx: &CliCtx<'_>,
    mode: &SupervisionMode,
    with_sudo: bool,
    ran: &mut Vec<Vec<String>>,
) -> Result<(), StopFailure> {
    let commands = ctx.p.service.set_enabled(mode, true, with_sudo);
    append_commands(ran, &commands);
    if let Some(failed) = commands.iter().find(|command| command.rc != 0) {
        return Err(StopFailure::Service(service_failure(
            mode,
            "enable",
            failed.rc,
            "service_enable",
        )));
    }
    Ok(())
}

fn append_commands(target: &mut Vec<Vec<String>>, commands: &[sw_pal::service_ctl::CmdRecord]) {
    target.extend(commands.iter().map(|command| command.argv.clone()));
}

fn record_hard_stop(
    ran: &mut Vec<Vec<String>>,
    mode: &SupervisionMode,
    with_sudo: bool,
    result: &ServiceResult,
) {
    if result.rc.is_none() {
        return;
    }
    let argv = match mode {
        SupervisionMode::WindowsTask => {
            vec!["schtasks", "/End", "/TN", "serialwrap"]
        }
        SupervisionMode::WindowsService => vec!["sc.exe", "stop", "serialwrap"],
        SupervisionMode::SystemdUser => {
            vec!["systemctl", "--user", "stop", "serialwrap"]
        }
        SupervisionMode::SystemdSystem if with_sudo => {
            vec!["sudo", "systemctl", "stop", "serialwrap"]
        }
        _ => return,
    };
    ran.push(argv.into_iter().map(str::to_string).collect());
}

fn service_failure(
    mode: &SupervisionMode,
    action: &str,
    rc: i32,
    phase: &'static str,
) -> ServiceResult {
    ServiceResult {
        ok: false,
        mode: mode.as_str().to_string(),
        action: action.to_string(),
        rc: Some(rc),
        stdout: None,
        stderr: None,
        error_code: Some(ErrorCode::InternalError.as_str()),
        hint: None,
        phase: Some(phase),
    }
}

pub(crate) fn same_identity(left: &DaemonIdentity, right: &DaemonIdentity) -> bool {
    left.known
        && right.known
        && !left.tree.is_empty()
        && left.tree.iter().all(|candidate| {
            right
                .tree
                .iter()
                .any(|other| same_proc_identity(candidate, other))
        })
}

fn same_proc_identity(left: &ProcIdentity, right: &ProcIdentity) -> bool {
    left.pid == right.pid
        && match (left.start_ticks, right.start_ticks) {
            (Some(left), Some(right)) => left == right,
            _ => true,
        }
}

fn int_as_u32(value: &PyValue) -> Option<u32> {
    match value {
        PyValue::Int(PyInt::Small(value)) => u32::try_from(*value).ok(),
        _ => None,
    }
}
