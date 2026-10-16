//! 原生指令群組分派（P3-T14～P3-T15）：紅燈 stub。
#![allow(unused_variables)]

use crate::argparse::Namespace;
use crate::ctx::CliCtx;

pub mod event_cmd;
pub mod profile_cmd;
pub mod rpc_cmds;
pub mod skill_cmd;

pub fn dispatch(ctx: &mut CliCtx, ns: &Namespace) -> i32 {
    todo!("P3-T14: dispatch")
}
