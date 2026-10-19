//! `supervision-mode` 群組（查詢與 `set MODE`；P4-T6 查詢、P4-T10 驗證、P4-T12 承接切換）。

use crate::argparse::Namespace;
use crate::ctx::CliCtx;
use sw_proto::config_types::RuntimeConfig;

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum ConfigErrorReport {
    PlainStderr,
    JsonInternalError,
}

pub fn run_supervision_mode(_ns: &Namespace, _ctx: &mut CliCtx) -> i32 {
    todo!("not implemented: P4-T6")
}

pub fn load_config_or_report(
    _ctx: &mut CliCtx,
    _kind: ConfigErrorReport,
) -> Result<RuntimeConfig, i32> {
    todo!("not implemented: P4-T6")
}

pub fn run_supervision_set(ns: &Namespace, ctx: &mut CliCtx, mode: &str) -> i32 {
    todo!("not implemented: P4-T10: run_supervision_set")
}
