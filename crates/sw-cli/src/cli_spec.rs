//! argparse 宣告式規格（`cli.py:1908-2452`）：P3-T10 紅燈 stub。
#![allow(unused_variables)]

use crate::argparse::ParserSpec;

pub struct SpecDefaults {
    pub version_line: String,
    pub profile_dir: String,
}

/// 20 個群組的 root 順序。
pub const GROUP_ORDER: [&str; 20] = [
    "daemon",
    "device",
    "session",
    "alias",
    "cmd",
    "stream",
    "log",
    "file",
    "wal",
    "mcu",
    "remote",
    "connect",
    "benches",
    "event",
    "supervision-mode",
    "service",
    "setup",
    "doctor",
    "profile",
    "skill",
];

/// 20 群組；`DELEGATED_GROUPS` 為 `SubTarget::Delegated`。
pub fn root_spec(d: &SpecDefaults) -> ParserSpec {
    todo!("P3-T10: root_spec")
}
