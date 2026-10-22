//! RPC 分派器（arch §6.3，P5-T18）：39 個 served method 的平面 `match`；15 個延後 method 落入 `METHOD_NOT_FOUND`。
//!
//! Task 0 骨架：型別與簽章凍結，分派本體由 Task 18 交付。

use std::sync::Arc;

use sw_core::arbiter::CommandRegistry;
use sw_core::session_manager::SessionManager;
use sw_core::wal::WalWriter;
use sw_pal::ProcScan;
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::PyDict;

use crate::framing::RpcHandler;

/// OD-11 測試 allowlist（Task 19 Produces；`resolve_test_allowlist` 的輸出，`enabled` 為 false 時不注入 service）。
/// `source`：`env`｜`config`｜`none`；`rejected_reason`：env／config 值非法時的拒絕原因（P1 T13 規格）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TestAllowlist {
    pub enabled: bool,
    pub coms: Vec<String>,
    pub source: &'static str,
    pub rejected_reason: Option<String>,
}

/// RPC 服務本體（分派與 health 的共用狀態）。
pub struct Service {
    _priv: (),
}

/// `Service::new` 的依賴（組裝點由 `daemon_main` 提供）。
pub struct ServiceDeps {
    pub manager: Arc<SessionManager>,
    pub wal: Arc<WalWriter>,
    pub registry: Arc<CommandRegistry>,
    pub procs: Arc<dyn ProcScan>,
    pub version: String,
    pub started_at: String,
    pub explicit_target_count: usize,
    /// Task 19：enabled 時才有值。
    pub test_device_allowlist: Option<TestAllowlist>,
    pub pid: u32,
}

impl Service {
    pub fn new(_deps: ServiceDeps) -> Service {
        sw_core::s21_todo!("T18", "Service::new")
    }
    /// dispatch＋`setdefault("version")`。
    pub fn rpc(&self, _method: &str, _params: &PyDict) -> Result<PyDict, PyErr> {
        sw_core::s21_todo!("T18", "Service::rpc")
    }
}

/// `RpcHandler` 實作：分派到 `Service::rpc`，`daemon.stop` 經 `stop` channel 通知 daemon main。
#[allow(dead_code)] // Task 18 實作前欄位尚未被讀取（分派與 daemon.stop 通道）。
pub struct ServiceHandler {
    svc: Arc<Service>,
    stop: crossbeam_channel::Sender<()>,
}

impl ServiceHandler {
    pub fn new(_svc: Arc<Service>, _stop: crossbeam_channel::Sender<()>) -> ServiceHandler {
        sw_core::s21_todo!("T18", "ServiceHandler::new")
    }
}

impl RpcHandler for ServiceHandler {
    fn call(&self, _method: &str, _params: &PyDict) -> Result<PyDict, PyErr> {
        sw_core::s21_todo!("T18", "ServiceHandler::call")
    }
    fn request_stop(&self) {
        sw_core::s21_todo!("T18", "ServiceHandler::request_stop")
    }
}
