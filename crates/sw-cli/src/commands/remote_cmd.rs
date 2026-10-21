//! remote 群組（-R／-L／status／close／stop 別名；P4-T8）。

use crate::argparse::Namespace;
use crate::ctx::CliCtx;

/// 執行原生 remote 指令。
pub fn run_remote(ctx: &mut CliCtx<'_>, ns: &Namespace) -> i32 {
    crate::remote_tunnel::run_remote(ns, ctx)
}
