//! `daemon` 群組（start／stop／status／restart）與 SC-18 首次需求啟動（P4-T7 承接）。
//!
//! P4-T7 紅燈階段（TDD）：以下為計畫 Task 7「Produces」的公開介面骨架，本體一律 `todo!("P4-T7: …")`。
//! 目前 `daemon` 群組仍由 `delegate::DELEGATED_GROUPS` 委派（見 `tests/mgmt_daemon.rs` 的紅燈說明）。

use std::path::Path;

use sw_proto::pyjson::PyDict;
use sw_proto::pypath::PathFlavor;

use crate::argparse::Namespace;
use crate::client::RpcFailure;
use crate::ctx::CliCtx;
use crate::endpoint_resolve::ResolvedEndpoint;
use sw_pal::proc_scan::DaemonProc;

pub fn run_daemon_start(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    todo!("P4-T7: daemon start")
}

pub fn run_daemon_stop(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    todo!("P4-T7: daemon stop")
}

/// `health.status`，**不**經 SC-18。
pub fn run_daemon_status(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    todo!("P4-T7: daemon status")
}

pub fn run_daemon_restart(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    todo!("P4-T7: daemon restart")
}

/// DF-05 定位失敗。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DaemonLocateError {
    NotFound,
}

/// DF-05：launcher → 同層 `serialwrapd` → manifest Python runtime 的 argv 前綴。
pub fn locate_daemon_argv(ctx: &CliCtx<'_>) -> Result<Vec<String>, DaemonLocateError> {
    todo!("P4-T7: locate_daemon_argv")
}

/// SC-10：endpoint 正規化（`unix://` 去除；非 `tcp://` 絕對化＋normpath；Windows 再 normcase）。
pub fn normalize_endpoint_for_compare(s: &str, cwd: &Path, flavor: PathFlavor) -> String {
    todo!("P4-T7: normalize_endpoint_for_compare")
}

/// SC-10：找出與 `target` 衝突的另一個 daemon。
pub fn find_conflicting_daemon<'a>(
    target: &str,
    daemons: &'a [DaemonProc],
    cwd: &Path,
    flavor: PathFlavor,
) -> Option<&'a DaemonProc> {
    todo!("P4-T7: find_conflicting_daemon")
}

/// SC-18 的自動啟動決策。
#[derive(Debug)]
pub enum AutostartDecision {
    NotApplicable,
    Started { pid: u32 },
    AlreadyUp,
    Failed(PyDict),
}

/// SC-18（Rust-only，`caps.on_demand_autostart`）：僅在請求從未送出、endpoint 為預設本機 endpoint 時於跨行程鎖內自動啟動並重送一次。
pub fn maybe_autostart_on_demand(
    ctx: &mut CliCtx<'_>,
    method: &str,
    resolved: &ResolvedEndpoint,
    failure: Option<&RpcFailure>,
    resp: &PyDict,
) -> AutostartDecision {
    todo!("P4-T7: maybe_autostart_on_demand")
}
