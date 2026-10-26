//! fire 計數持久化（P7 Task 13；`event_engine/counter.py`，EVT-D-E11 write-through 快取，DF-01、DF-18）。P7 Task 0 骨架。

use std::path::PathBuf;
use std::sync::Arc;

use sw_pal::traits::{FsOps, PalResult};
use sw_proto::pyjson::{PyDict, PyInt, PyValue};

/// counter 檔可被手改成超出 i128 的值，Python 照收；遞增經 `PyInt::add_small(1)`。
#[derive(Clone, Debug, PartialEq)]
pub struct Counter {
    pub fires: PyInt,
    pub last_fire_ts: Option<PyInt>,
    pub exhausted: bool,
}

impl Counter {
    pub fn to_json(&self) -> PyDict {
        todo!("P7 Task 13")
    }
    pub fn from_json(_v: &PyValue) -> Counter {
        todo!("P7 Task 13")
    }
}

pub struct CounterStore {
    _todo: (),
}

impl CounterStore {
    pub fn new(_root: PathBuf, _fs: Arc<dyn FsOps>) -> PalResult<Self> {
        todo!("P7 Task 13")
    }
    pub fn load(&self, _rule_id: &str) -> Counter {
        todo!("P7 Task 13")
    }
    /// `FsOps::create_truncate(<path>.tmp)` → `FsOps::replace(tmp, path)`（DF-18）。
    pub fn save(&self, _rule_id: &str, _c: &Counter) -> PalResult<()> {
        todo!("P7 Task 13")
    }
    pub fn clear(&self, _rule_id: &str) {
        todo!("P7 Task 13")
    }
}

/// DF-01 正規式：`^[a-z0-9][a-z0-9-]{0,63}\.[a-z0-9][a-z0-9-]{0,63}\Z`。
pub fn valid_rule_id(_rule_id: &str) -> bool {
    todo!("P7 Task 13")
}
