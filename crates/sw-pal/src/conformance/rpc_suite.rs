//! RpcListener conformance（arch §4.6、計畫 Task 17）。Task 0 骨架：簽章凍結，斷言本體由 Task 17 測試作者補上。

use std::path::Path;
use std::sync::Arc;

use crate::traits::{RpcConnector, RpcListenerFactory};

/// listener 共用斷言：bind／accept 有界等待、close 移除 socket 檔、TCP loopback 限制（Task 17）。
pub fn rpc_suite(_listeners: Arc<dyn RpcListenerFactory>, _connector: Arc<dyn RpcConnector>, _dir: &Path) {
    crate::s21_todo!("T17", "rpc_suite")
}
