//! RPC 指令組包與執行（P3-T14）：紅燈 stub。
#![allow(unused_variables)]

use crate::argparse::Namespace;
use crate::ctx::CliCtx;
use sw_proto::pyjson::PyValue;

/// 回傳 `(method, params)`。
pub fn request_for(ns: &Namespace) -> Option<(&'static str, PyValue)> {
    todo!("P3-T14: request_for")
}

/// Q-30。
pub fn effective_timeout_s(ns: &Namespace, method: &str) -> f64 {
    todo!("P3-T14: effective_timeout_s")
}

pub fn run_rpc(ctx: &mut CliCtx, ns: &Namespace, method: &str, params: PyValue) -> i32 {
    todo!("P3-T14: run_rpc")
}
