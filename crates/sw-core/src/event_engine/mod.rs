//! Event Engine（P7 Task 11～16；`sw_core/event_engine/*`，09 §3）。
//!
//! P7 Task 0 骨架：型別與簽章凍結，本體為 `todo!("P7 Task N")`。

pub mod counter;
pub mod dispatcher;
pub mod event_log;
pub mod line_buffer;
pub mod matcher;
pub mod registry;
pub mod schema;

use std::path::PathBuf;
use std::sync::Arc;

use sw_pal::Platform;
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::{PyDict, PyValue};

use self::dispatcher::DispatcherStopReport;
use self::registry::{DiffResult, UpsertError};
use self::schema::Rule;

/// matcher／engine 查詢 session 現況的介面（Task 16-wire 由 `SessionManager` 實作）。
pub trait BridgeQueries: Send + Sync {
    fn active_cmd_id_for(&self, com: &str) -> Option<String>;
    fn profile_for(&self, com: &str) -> Option<String>;
    fn known_coms(&self) -> Vec<String>;
}

pub struct EngineDeps {
    pub events_dir: PathBuf,
    pub runtime_dir: PathBuf,
    pub log_path: PathBuf,
    pub bridge: Arc<dyn BridgeQueries>,
    pub queue_max: usize,
    pub per_daemon_max: usize,
    pub platform: Arc<Platform>,
    /// 與 06 `ManagerDeps.warn` 同型。
    pub warn: Arc<dyn Fn(String) + Send + Sync>,
}

pub struct EventEngine {
    _todo: (),
}

impl EventEngine {
    pub fn new(_d: EngineDeps) -> sw_pal::PalResult<Self> {
        todo!("P7 Task 16")
    }
    pub fn start(&self) -> Result<(), PyErr> {
        todo!("P7 Task 16")
    }
    /// 冪等：matcher.stop → dispatcher.close_intake；之後 `feed_rx` 為 no-op（R1-F2）。
    pub fn close_intake(&self) {
        todo!("P7 Task 16")
    }
    /// close_intake（若未呼叫）→ dispatcher.stop(deadline)（SC-11 細化）。
    pub fn stop(&self, _deadline: std::time::Instant) -> DispatcherStopReport {
        todo!("P7 Task 16")
    }
    /// per-COM LineBuffer（永不重設，EVT-D-S5）→ enabled 才 feed_line。
    pub fn feed_rx(&self, _com: &str, _data: &[u8], _wal_seq: u64) {
        todo!("P7 Task 16")
    }
    pub fn rule_set(&self, _obj: &PyValue) -> Result<Arc<Rule>, UpsertError> {
        todo!("P7 Task 16")
    }
    pub fn rule_delete(&self, _rule_id: &str) -> bool {
        todo!("P7 Task 16")
    }
    pub fn rule_list(&self, _selector: Option<&PyValue>, _owner: Option<&PyValue>) -> Vec<PyDict> {
        todo!("P7 Task 16")
    }
    pub fn rule_get(&self, _rule_id: &str) -> Option<PyDict> {
        todo!("P7 Task 16")
    }
    pub fn reload(&self) -> Result<DiffResult, PyErr> {
        todo!("P7 Task 16")
    }
    pub fn com_enable(&self, _com: &str) -> PyDict {
        todo!("P7 Task 16")
    }
    pub fn com_disable(&self, _com: &str) -> PyDict {
        todo!("P7 Task 16")
    }
    pub fn com_status(&self, _com: Option<&PyValue>) -> PyDict {
        todo!("P7 Task 16")
    }
    pub fn reset(&self, _rule_id: Option<&PyValue>, _selector: Option<&PyValue>) -> i128 {
        todo!("P7 Task 16")
    }
    pub fn tail(
        &self,
        _rule_id: Option<&PyValue>,
        _selector: Option<&PyValue>,
        _since_ts: Option<&PyValue>,
        _n: Option<&PyValue>,
    ) -> Result<Vec<PyValue>, PyErr> {
        todo!("P7 Task 16")
    }
}
