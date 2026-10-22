//! RPC 連線 framing 與 acceptor／連線 thread（arch §6.2，對應 Python `serve_connection`；P5-T17）。
//!
//! Task 0 骨架：型別與簽章凍結，行為由 Task 17 交付。

use std::sync::Arc;
use std::time::Duration;

use sw_pal::traits::{RpcListener, RpcServerStream};
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::PyDict;

/// 分派介面（由 `service.rs` 的 `ServiceHandler` 實作）。
pub trait RpcHandler: Send + Sync {
    fn call(&self, method: &str, params: &PyDict) -> Result<PyDict, PyErr>;
    fn request_stop(&self);
}

/// 請求行上限（內容不含 `\n`；超過斷線不回應，RPC-H-02）。
pub const MAX_LINE_BYTES: usize = 65_536;

/// 連線停止旗標與「本連線是否正在 handler 內」（stop 時的分級關閉依據）。
pub struct ConnStop {
    _priv: (),
}

impl ConnStop {
    /// 建立停止旗標（Task 17 交付；測試與 `RpcServer` 共用同一個實例）。
    pub fn new() -> ConnStop {
        sw_core::s21_todo!("T17", "ConnStop::new")
    }
}

/// 處理單一連線：逐 byte 累積到 `\n`（arch §6.2 九步）。
pub fn serve_connection(_stream: Box<dyn RpcServerStream>, _handler: &dyn RpcHandler, _stop: &ConnStop) {
    sw_core::s21_todo!("T17", "serve_connection")
}

/// RPC acceptor（連線上限 128；每連線 thread 與 shutdown handle）。
pub struct RpcServer {
    _priv: (),
}

impl RpcServer {
    pub fn start(_listener: Box<dyn RpcListener>, _handler: Arc<dyn RpcHandler>) -> RpcServer {
        sw_core::s21_todo!("T17", "RpcServer::start")
    }
    /// 回傳 handler 是否全部收斂（arch §6.2 停止流程 ①～④）。
    pub fn stop(self, _budget: Duration) -> bool {
        sw_core::s21_todo!("T17", "RpcServer::stop")
    }
}
