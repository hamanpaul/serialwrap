//! Stage 1A 委派路由（P3-T10：`DELEGATED_GROUPS`、`Route`、`classify`）已實作；委派執行由 P3-T16 實作。
#![allow(unused_variables)]

use crate::ctx::CliCtx;
use std::ffi::OsString;
use std::path::PathBuf;

/// 委派集合（arch §7.4、計畫 §0.3）；`cli_spec` 與 `run` 共用。
pub const DELEGATED_GROUPS: [&str; 8] = [
    "daemon",
    "remote",
    "connect",
    "benches",
    "supervision-mode",
    "service",
    "setup",
    "doctor",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Route {
    Native,
    Delegate { group: &'static str },
}

/// 在任何有損 UTF-8 轉換之前執行：逐 token 以 `to_str()` 比對選項與群組名。
pub fn classify(argv_tail: &[OsString]) -> Route {
    const VALUE_OPTIONS: [&str; 5] = [
        "--socket",
        "--endpoint",
        "--bench",
        "--timeout",
        "--retries",
    ];
    let mut index = 0;
    while index < argv_tail.len() {
        let Some(token) = argv_tail[index].to_str() else {
            return Route::Native;
        };
        if token == "--" {
            let Some(group) = argv_tail.get(index + 1).and_then(|value| value.to_str()) else {
                return Route::Native;
            };
            return if DELEGATED_GROUPS.contains(&group) {
                Route::Delegate {
                    group: DELEGATED_GROUPS
                        .iter()
                        .copied()
                        .find(|item| *item == group)
                        .unwrap(),
                }
            } else {
                Route::Native
            };
        }
        if token.starts_with('-') && token != "-" {
            let option = token.split_once('=').map_or(token, |(option, _)| option);
            let takes_value = option.starts_with("--")
                && VALUE_OPTIONS
                    .iter()
                    .filter(|candidate| candidate.starts_with(option))
                    .count()
                    == 1;
            index += if takes_value && !token.contains('=') {
                2
            } else {
                1
            };
            continue;
        }
        return if DELEGATED_GROUPS.contains(&token) {
            Route::Delegate {
                group: DELEGATED_GROUPS
                    .iter()
                    .copied()
                    .find(|item| *item == token)
                    .unwrap(),
            }
        } else {
            Route::Native
        };
    }
    Route::Native
}

#[derive(Clone, Debug, PartialEq)]
pub enum DelegateError {
    Unavailable(String),
    Recursion,
}

/// sha256(各元素 `OsStr::as_encoded_bytes()` 以 `"\0"` 連接)。
pub fn argv_sha256(argv_tail: &[OsString]) -> String {
    todo!("P3-T16: argv_sha256")
}

/// `SERIALWRAP_DELEGATE_DEPTH`／`SERIALWRAP_DELEGATE_ARGV_SHA` 遞迴防護；回傳 incoming depth。
pub fn check_recursion(
    depth_raw: Option<&str>,
    sha_raw: Option<&str>,
    my_sha: &str,
) -> Result<u32, DelegateError> {
    todo!("P3-T16: check_recursion")
}

pub struct DelegateTarget {
    pub argv: Vec<OsString>,
    pub manifest_path: PathBuf,
}

pub fn resolve_target(
    p: &sw_pal::ClientPlatform,
    argv_tail: &[OsString],
) -> Result<DelegateTarget, DelegateError> {
    todo!("P3-T16: resolve_target")
}

/// 只在錯誤時返回 1。
pub fn run_delegate(ctx: &mut CliCtx, argv_tail: &[OsString], group: &str) -> i32 {
    todo!("P3-T16: run_delegate")
}
