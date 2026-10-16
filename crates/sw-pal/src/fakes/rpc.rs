//! 假 RPC 連線（P3-T9 骨架）。
#![allow(unused_variables)]

use crate::traits::PalError;
use std::time::Duration;

#[derive(Clone, Debug)]
pub enum StreamStep {
    /// 一次 recv 回傳整段。
    Reply(Vec<u8>),
    Chunk(Vec<u8>),
    Eof,
    RecvErr(PalError),
    /// 推進 FakeClock。
    Delay(Duration),
}

#[derive(Clone, Debug)]
pub enum FakeConn {
    Refuse(PalError),
    /// connect 回 kind=TimedOut。
    Timeout,
    Stream(Vec<StreamStep>),
}

impl FakeConn {
    /// kind 依 code 映射（111→ConnectionRefused、110→TimedOut…）。
    pub fn refuse_errno(code: i32, py_message: &str) -> FakeConn {
        todo!("P3-T9: FakeConn::refuse_errno")
    }
    pub fn stream(steps: Vec<StreamStep>) -> FakeConn {
        todo!("P3-T9: FakeConn::stream")
    }
    /// kind=Io、os_code=Some(code)、py_message 原樣。
    pub fn gai_error(code: i32, py_message: &str) -> PalError {
        todo!("P3-T9: FakeConn::gai_error")
    }
}

/// 假連線器（P3-T9 骨架：欄位與 trait 實作於 P3-T9 補上）。
pub struct ScriptedConnector;

impl ScriptedConnector {
    pub fn sent(&self) -> Vec<Vec<u8>> {
        todo!("P3-T9: ScriptedConnector::sent")
    }
    pub fn connect_count(&self) -> usize {
        todo!("P3-T9: ScriptedConnector::connect_count")
    }
    pub fn timeouts_s(&self) -> Vec<f64> {
        todo!("P3-T9: ScriptedConnector::timeouts_s")
    }
    pub fn resolve_count(&self) -> usize {
        todo!("P3-T9: ScriptedConnector::resolve_count")
    }
    pub fn fail_resolve(&self, host: &str, e: PalError) {
        todo!("P3-T9: ScriptedConnector::fail_resolve")
    }
}
