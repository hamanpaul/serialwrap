//! `benches` 群組（P4-T9 承接）。
#![allow(unused_variables)]

use crate::argparse::Namespace;
use crate::ctx::CliCtx;
use crate::remote_tunnel::TunnelError;
use sw_proto::pyjson::PyDict;

/// 執行原生 `benches` 指令（紅燈 stub）。
pub fn run_benches(ns: &Namespace, ctx: &mut CliCtx<'_>) -> i32 {
    unimplemented!("not implemented: P4-T9: benches_cmd::run_benches")
}

/// 由 `remote status` 回應抽取 `tunnels` 清單並驗證格式（紅燈 stub）。
pub fn status_tunnels_from(
    resp: &PyDict,
) -> Result<Vec<PyDict>, TunnelError> {
    unimplemented!("not implemented: P4-T9: benches_cmd::status_tunnels_from")
}
