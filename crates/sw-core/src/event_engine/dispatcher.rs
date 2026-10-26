//! handler 派送（P7 Task 15；`event_engine/dispatcher.py`，SC-11 細化、R1-F2）。P7 Task 0 骨架。

use std::sync::Arc;

use sw_pal::traits::{Clock, HandlerSpawner, SysEnv};
use sw_proto::pyjson::{PyDict, PyInt};

use super::counter::CounterStore;
use super::matcher::MatcherFire;

/// `bridge_generation` 恆 0（EVT-D-E2 KEEP）。
pub fn build_payload(_fire: &MatcherFire, _fire_count: &PyInt, _matched_line: &str) -> PyDict {
    todo!("P7 Task 15")
}

pub fn build_env(_fire: &MatcherFire, _fire_count: &PyInt, _inherited: &[(String, String)]) -> Vec<(String, String)> {
    todo!("P7 Task 15")
}

/// Default 分隔符、`ensure_ascii=false`、`+"\n"`。
pub fn stdin_bytes(_payload: &PyDict) -> Vec<u8> {
    todo!("P7 Task 15")
}

/// 8 條 worker thread `event-dispatch-N`。
pub struct Dispatcher {
    _todo: (),
}

impl Dispatcher {
    pub fn start(
        _per_daemon_max: usize,
        _spawner: Arc<dyn HandlerSpawner>,
        _counters: Arc<CounterStore>,
        _emit: Arc<dyn Fn(PyDict) + Send + Sync>,
        _env: Arc<dyn SysEnv>,
        _clock: Arc<dyn Clock>,
    ) -> Self {
        todo!("P7 Task 15")
    }
    pub fn dispatch(&self, _fire: MatcherFire, _matched_line: String) {
        todo!("P7 Task 15")
    }
    /// 冪等；之後 `dispatch()` 直接丟棄（不 spawn、不計 fire_count），已在跑的 handler 不受影響（R1-F2）。
    pub fn close_intake(&self) {
        todo!("P7 Task 15")
    }
    /// 先 close_intake；等到 deadline；逾時 `HandlerAbort::trigger()`，之後最多再等 100 ms。
    pub fn stop(self, _deadline: std::time::Instant) -> DispatcherStopReport {
        todo!("P7 Task 15")
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DispatcherStopReport {
    pub aborted: usize,
    /// 100 ms 寬限後 run 仍未返回的 rule_id，依 rule_id 排序。
    pub unreaped: Vec<String>,
}
