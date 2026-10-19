//! 原生指令群組分派（P3-T14～P3-T15）。
//!
//! `dispatch` 依 `ns.path()` 做平面 match；未命中回 `INVALID_ARGS`（exit 2）。

use crate::argparse::Namespace;
use crate::ctx::CliCtx;
use crate::output;
use sw_proto::error_codes::ErrorCode;

pub mod benches_cmd;
pub mod connect_cmd;
pub mod daemon_cmd;
pub mod event_cmd;
pub mod profile_cmd;
pub mod remote_cmd;
pub mod rpc_cmds;
pub mod service_cmd;
pub mod skill_cmd;
pub mod supervision_cmd;

pub fn dispatch(ctx: &mut CliCtx, ns: &Namespace) -> i32 {
    let path = ns.path();
    match path.as_slice() {
        ["event", ..] => return event_cmd::dispatch_event(ctx, ns),
        ["remote", ..] => return remote_cmd::run_remote(ctx, ns),
        ["skill"] => return skill_cmd::run_skill(ctx, ns),
        ["service"] => return service_cmd::run_service(ns, ctx),
        ["supervision-mode"] => return supervision_cmd::run_supervision_mode(ns, ctx),
        ["profile", "test"] => return profile_cmd::run_profile_test(ctx, ns),
        ["cmd", "submit"] => {
            if let Some(code) = rpc_cmds::check_completion_regex(ctx, ns) {
                return code;
            }
        }
        _ => {}
    }
    match rpc_cmds::request_for(ns) {
        Some((method, params)) => rpc_cmds::run_rpc(ctx, ns, method, params),
        None => {
            let resp = output::error_resp(ErrorCode::InvalidArgs, None);
            let _ = output::print_json(&mut ctx.io, &resp);
            let _ = output::mirror_err(&mut ctx.io, &resp, None);
            2
        }
    }
}
