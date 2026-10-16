//! `profile test`（`cli.py:1804-1905`）：P3-T15 紅燈 stub。
#![allow(unused_variables)]

use crate::argparse::Namespace;
use crate::ctx::CliCtx;
use sw_proto::pyjson::PyDict;

pub fn profile_test_check(pattern: &str, sample: &str) -> PyDict {
    todo!("P3-T15: profile_test_check")
}

pub fn last_nonblank_line(text: &str) -> Option<&str> {
    todo!("P3-T15: last_nonblank_line")
}

pub fn bootloader_check(pattern: &str, last: Option<&str>) -> PyDict {
    todo!("P3-T15: bootloader_check")
}

pub fn run_profile_test(ctx: &mut CliCtx, ns: &Namespace) -> i32 {
    todo!("P3-T15: run_profile_test")
}
