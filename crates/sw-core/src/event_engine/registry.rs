//! 規則檔註冊表（P7 Task 13；`event_engine/registry.py`，DF-09、DF-18、RPC-H-13）。P7 Task 0 骨架。

use std::path::PathBuf;
use std::sync::Arc;

use sw_pal::traits::{FsOps, PalError, PalResult};
use sw_proto::pyjson::PyValue;

use super::schema::{Rule, RuleError};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FailedLoad {
    pub path: String,
    pub reason: String,
}

pub struct LoadResult {
    pub rules: Vec<Arc<Rule>>,
    pub failed: Vec<FailedLoad>,
}

/// 升冪（RPC-H-13）。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DiffResult {
    pub added: Vec<String>,
    pub removed: Vec<String>,
    pub changed: Vec<String>,
}

pub struct RuleRegistry {
    _todo: (),
}

/// `Rule` → `INVALID_RULE_SCHEMA`（Schema 與 Value）；`Type`／`Io` → `EXCEPTION`。
#[derive(Debug)]
pub enum UpsertError {
    Rule(RuleError),
    Io(PalError),
}

impl RuleRegistry {
    pub fn new(_root: PathBuf, _fs: Arc<dyn FsOps>) -> PalResult<Self> {
        todo!("P7 Task 13")
    }
    pub fn load_all(&self) -> LoadResult {
        todo!("P7 Task 13")
    }
    pub fn upsert(&self, _obj: &PyValue) -> Result<Arc<Rule>, UpsertError> {
        todo!("P7 Task 13")
    }
    pub fn delete(&self, _rule_id: &str) -> bool {
        todo!("P7 Task 13")
    }
    pub fn diff_against(&self, _prev: &[Arc<Rule>]) -> DiffResult {
        todo!("P7 Task 13")
    }
}
