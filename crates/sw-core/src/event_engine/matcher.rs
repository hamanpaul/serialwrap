//! 規則比對與 matcher worker（P7 Task 12；`event_engine/matcher.py`）。P7 Task 0 骨架。

use std::sync::Arc;

use sw_proto::pyjson::PyDict;

use super::counter::Counter;
use super::schema::{Pattern, Rule, RuleError};

#[derive(Clone, Debug, PartialEq)]
pub struct MatchResult {
    pub matched_text: String,
    pub groups: Vec<Option<String>>,
}

pub struct PatternMatcher {
    _todo: (),
}

impl PatternMatcher {
    pub fn new(_p: &Pattern) -> Result<Self, RuleError> {
        todo!("P7 Task 12")
    }
    /// regex：只看前 4096 字元。
    pub fn eval(&self, _line: &str) -> Option<MatchResult> {
        todo!("P7 Task 12")
    }
}

pub fn apply_cooldown(_rule: &Rule, _c: &Counter, _now_ms: i64) -> bool {
    todo!("P7 Task 12")
}

pub fn apply_max_fires(_rule: &Rule, _c: &Counter) -> bool {
    todo!("P7 Task 12")
}

pub fn apply_scope(_rule: &Rule, _active_cmd_id: Option<&str>) -> bool {
    todo!("P7 Task 12")
}

pub fn apply_profile(_rule: &Rule, _com_profile: Option<&str>) -> bool {
    todo!("P7 Task 12")
}

pub trait MatcherContext: Send + Sync {
    fn active_cmd_id(&self, com: &str) -> Option<String>;
    fn com_profile(&self, com: &str) -> Option<String>;
    fn now_ms(&self) -> i64;
    fn emit_fire(&self, fire: MatcherFire);
    fn emit_event(&self, ev: PyDict);
    fn counter_for(&self, rule_id: &str) -> Counter;
}

pub struct MatcherFire {
    pub rule: Arc<Rule>,
    pub selector: String,
    pub m: MatchResult,
    pub wal_seq: u64,
    pub matched_at: i64,
    pub active_cmd_id: Option<String>,
}

/// thread `event-matcher`；bounded(1024)，drop-oldest 由 `feed_line` 實作。
pub struct MatcherWorker {
    _todo: (),
}

impl MatcherWorker {
    pub fn start(_rules: Vec<Arc<Rule>>, _ctx: Arc<dyn MatcherContext>, _queue_max: usize) -> Result<Self, RuleError> {
        todo!("P7 Task 12")
    }
    pub fn feed_line(&self, _selector: &str, _line: String, _wal_seq: u64) {
        todo!("P7 Task 12")
    }
    pub fn replace_rules(&self, _rules: Vec<Arc<Rule>>) -> Result<(), RuleError> {
        todo!("P7 Task 12")
    }
    pub fn stop(self) {
        todo!("P7 Task 12")
    }
}
