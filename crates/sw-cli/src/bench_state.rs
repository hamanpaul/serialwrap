//! `benches.state.json` 唯讀存取（`cli.py:1115-1188`）：P3-T12 紅燈 stub。
#![allow(unused_variables)]

use crate::ctx::CliCtx;
use sw_proto::pyjson::PyDict;

#[derive(Clone, Debug, PartialEq)]
pub enum BenchStateError {
    Value(String),
    Os(String),
}

/// 1A 只讀，不加鎖。
pub fn load_bench_state_doc(ctx: &CliCtx, path: &str) -> Result<PyDict, BenchStateError> {
    todo!("P3-T12: load_bench_state_doc")
}

pub fn remembered_bench_endpoint(benches: &PyDict, code: &str) -> Result<Option<String>, String> {
    todo!("P3-T12: remembered_bench_endpoint")
}
