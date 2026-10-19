//! 共用 `graceful_stop`（P4-T7／T10 承接）：先停用拉起能力 → RPC `daemon.stop`（2.0 s）→ 等待結束（≤10 s）→ 逾時才硬終止或回 `TIMEOUT`。

use std::time::Duration;
use sw_pal::proc_scan::ProcIdentity;
use sw_pal::service_ctl::{AutostartHoldRecord, ServiceResult, SupervisionMode};
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
    Timeout { phase: &'static str, id: DaemonIdentity },
    Service(ServiceResult),
    Handoff(HandoffError),
    Relaunched { pid: u32, err: HandoffError },
    Hold(ServiceResult),
    RelaunchLoop { pids: Vec<u32> },
}

impl StopFailure {
    pub fn hold(&self) -> Option<&AutostartHoldRecord> {
        None
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

pub fn graceful_stop(
    _ctx: &mut CliCtx,
    _mode: &SupervisionMode,
    _endpoint: &str,
    _with_sudo: bool,
    _keep_disabled: bool,
    _phase: &'static str,
) -> Result<StopReport, StopFailure> {
    todo!("not implemented: P4-T7")
}

pub fn graceful_stop_with(
    _ctx: &mut CliCtx,
    _mode: &SupervisionMode,
    _endpoint: &str,
    _with_sudo: bool,
    _keep_disabled: bool,
    _phase: &'static str,
    _rpc: StopRpc,
) -> Result<StopReport, StopFailure> {
    todo!("not implemented: P4-T7")
}

pub fn wait_daemon_gone(
    _ctx: &CliCtx,
    _endpoint: &str,
    _id: &DaemonIdentity,
    _timeout: Duration,
) -> bool {
    todo!("not implemented: P4-T7")
}
