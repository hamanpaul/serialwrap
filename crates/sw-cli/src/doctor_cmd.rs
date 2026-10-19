//! `doctor` 子命令（P4-T13 原生承接與 SC-25 version_pairing）。

use crate::argparse::Namespace;
use crate::ctx::CliCtx;
use sw_proto::pyjson::PyDict;

pub struct Check {
    pub check: &'static str,
    pub ok: bool,
    pub detail: String,
    pub fix: Option<String>,
    pub advisory: bool,
    pub extra: PyDict,
}

pub const DOCTOR_ADVISORY_CHECKS: &[&str] = &[
    "systemd",
    "wsl_systemd",
    "devices",
    "other_serialwrap_installs",
    "wal_dir",
    "serialwrap_minicom_on_path",
    "jq_on_path",
    "minicom_on_path",
    "profile_bootloader_prompts",
];

pub const DOCTOR_ADVISORY_CHECKS_WIN: &[&str] = &[
    "serialwrap_on_path",
    "serialwrapd_on_path",
    "daemon_endpoint",
    "devices",
    "other_serialwrap_installs",
    "wal_dir",
    "profile_bootloader_prompts",
];

pub fn run_doctor(_ctx: &CliCtx) -> Vec<Check> {
    todo!("not implemented: P4-T13")
}

pub fn run_doctor_cmd(_ns: &Namespace, _ctx: &mut CliCtx) -> i32 {
    todo!("not implemented: P4-T13")
}
