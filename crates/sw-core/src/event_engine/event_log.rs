//! events.ndjson 事件日誌（P7 Task 13；`event_engine/event_log.py`，rotate 10 MiB×3）。P7 Task 0 骨架。

use std::path::PathBuf;
use std::sync::Arc;

use sw_pal::traits::{Clock, FsOps, PalResult};
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::{PyDict, PyValue};

pub struct EventLogger {
    _todo: (),
}

impl EventLogger {
    pub fn new(_path: PathBuf, _fs: Arc<dyn FsOps>, _clock: Arc<dyn Clock>) -> PalResult<Self> {
        todo!("P7 Task 13")
    }
    pub fn write(&self, _ev: PyDict) {
        todo!("P7 Task 13")
    }
    pub fn tail(
        &self,
        _rule_id: Option<&PyValue>,
        _selector: Option<&PyValue>,
        _since_ts: Option<&PyValue>,
        _n: Option<&PyValue>,
    ) -> Result<Vec<PyValue>, PyErr> {
        todo!("P7 Task 13")
    }
}
