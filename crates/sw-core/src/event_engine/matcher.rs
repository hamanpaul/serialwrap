//! 規則比對與 matcher worker（P7 Task 12；`event_engine/matcher.py`）。
//!
//! - `PatternMatcher`：`regex` 直接編譯、`contains` 以 `re.escape` 後編譯；`regex` 只看前
//!   `REGEX_MATCH_INPUT_MAX`（4096）字元（Python 未安裝 google-re2 時的 `re` 回退路徑）。
//! - gate 順序：selector → scope → profile → max_fires → cooldown；`debug:true` 時被擋者發 `match_skipped`。
//! - `MatcherWorker`：thread `event-matcher`；`Mutex<VecDeque>`＋`Condvar` 有界佇列，滿載時 drop-oldest 並發
//!   `event_dropped`；單一 item 求值 panic 收斂為 WARNING；`stop` 等待 2 s 後 WARNING 並分離 thread。

use std::cmp::Ordering as CmpOrdering;
use std::collections::{HashMap, VecDeque};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use sw_proto::pyjson::{PyDict, PyInt, PyValue};
use sw_proto::pyre::{compile_capturing, escape, PyRegex};

use super::counter::Counter;
use super::schema::{flags_from_string, Pattern, Rule, RuleError};

/// `schema.REGEX_MATCH_INPUT_MAX`。
const REGEX_MATCH_INPUT_MAX: usize = 4096;
/// worker `queue.get(timeout=0.1)`。
const WORKER_POLL: Duration = Duration::from_millis(100);
/// `stop()` 的 `join(timeout=2.0)`。
const STOP_JOIN: Duration = Duration::from_secs(2);

#[derive(Clone, Debug, PartialEq)]
pub struct MatchResult {
    pub matched_text: String,
    pub groups: Vec<Option<String>>,
}

pub struct PatternMatcher {
    re: PyRegex,
    is_regex: bool,
}

impl PatternMatcher {
    pub fn new(p: &Pattern) -> Result<Self, RuleError> {
        let raw = match p.kind.as_str() {
            "regex" => p.value.clone(),
            "contains" => escape(&p.value),
            other => return Err(RuleError::Value(format!("unknown pattern kind: {other}"))),
        };
        let flags = flags_from_string(&p.flags)?;
        let re = compile_capturing(&raw, flags)
            .map_err(|error| RuleError::Value(format!("{error:?}")))?;
        Ok(PatternMatcher {
            re,
            is_regex: p.kind == "regex",
        })
    }

    /// regex：只看前 4096 字元。
    pub fn eval(&self, line: &str) -> Option<MatchResult> {
        let capped;
        let target = if self.is_regex {
            capped = line.chars().take(REGEX_MATCH_INPUT_MAX).collect::<String>();
            capped.as_str()
        } else {
            line
        };
        let (matched_text, groups) = self.re.search_groups(target)?;
        Some(MatchResult {
            matched_text,
            groups,
        })
    }
}

/// Python 任意精度 int 的比較（`PyInt::Big` 為十進位字串）。
fn int_cmp(a: &PyInt, b: &PyInt) -> CmpOrdering {
    fn parts(v: &PyInt) -> (bool, String) {
        let s = match v {
            PyInt::Small(n) => n.to_string(),
            PyInt::Big(s) => s.clone(),
        };
        match s.strip_prefix('-') {
            Some(rest) => (true, rest.trim_start_matches('0').to_string()),
            None => (false, s.trim_start_matches('0').to_string()),
        }
    }
    let (an, ad) = parts(a);
    let (bn, bd) = parts(b);
    let mag = ad.len().cmp(&bd.len()).then_with(|| ad.cmp(&bd));
    match (an && !ad.is_empty(), bn && !bd.is_empty()) {
        (false, false) => mag,
        (true, true) => mag.reverse(),
        (true, false) => CmpOrdering::Less,
        (false, true) => CmpOrdering::Greater,
    }
}

fn int_sat(v: &PyInt) -> i128 {
    match v {
        PyInt::Small(n) => *n,
        PyInt::Big(s) if s.starts_with('-') => i128::MIN,
        PyInt::Big(_) => i128::MAX,
    }
}

pub fn apply_cooldown(rule: &Rule, c: &Counter, now_ms: i64) -> bool {
    let cooldown = rule.cooldown_ms_sat();
    let Some(last) = c.last_fire_ts.as_ref() else {
        return true;
    };
    if cooldown <= 0 {
        return true;
    }
    (now_ms as i128).saturating_sub(int_sat(last)) >= cooldown
}

pub fn apply_max_fires(rule: &Rule, c: &Counter) -> bool {
    let Some(max) = rule.max_fires.as_ref().filter(|v| !v.is_null()) else {
        return true;
    };
    if c.exhausted {
        return false;
    }
    let max = match max {
        PyValue::Bool(b) => PyInt::Small(i128::from(*b)),
        PyValue::Int(i) => i.clone(),
        _ => return true,
    };
    int_cmp(&c.fires, &max) == CmpOrdering::Less
}

pub fn apply_scope(rule: &Rule, active_cmd_id: Option<&str>) -> bool {
    match rule.scope.as_str() {
        "any" => true,
        "spontaneous" => active_cmd_id.is_none(),
        "command_output" => active_cmd_id.is_some(),
        _ => false,
    }
}

pub fn apply_profile(rule: &Rule, com_profile: Option<&str>) -> bool {
    rule.profile == "ALL" || com_profile == Some(rule.profile.as_str())
}

pub trait MatcherContext: Send + Sync {
    fn active_cmd_id(&self, com: &str) -> Option<String>;
    fn com_profile(&self, com: &str) -> Option<String>;
    fn now_ms(&self) -> i64;
    fn emit_fire(&self, fire: MatcherFire);
    fn emit_event(&self, ev: PyDict);
    fn counter_for(&self, rule_id: &str) -> Counter;
    /// P7 Task 12：WARNING（Python `logging.warning` 的訊息本文）；預設寫 daemon stderr。
    fn warn(&self, msg: &str) {
        eprintln!("WARNING {msg}");
    }
}

pub struct MatcherFire {
    pub rule: Arc<Rule>,
    pub selector: String,
    pub m: MatchResult,
    pub wal_seq: u64,
    pub matched_at: i64,
    pub active_cmd_id: Option<String>,
}

struct Item {
    selector: String,
    line: String,
    wal_seq: u64,
}

struct Compiled {
    rules: Vec<Arc<Rule>>,
    compiled: HashMap<String, Arc<PatternMatcher>>,
}

struct Shared {
    queue: Mutex<VecDeque<Item>>,
    ready: Condvar,
    queue_max: usize,
    stop: AtomicBool,
    compiled: Mutex<Compiled>,
    ctx: Arc<dyn MatcherContext>,
}

fn dropped_event(selector: &str, wal_seq: u64) -> PyDict {
    let mut ev = PyDict::new();
    ev.insert("type", PyValue::from("event_dropped"));
    ev.insert("reason", PyValue::from("matcher_queue_overflow"));
    ev.insert("selector", PyValue::from(selector));
    ev.insert("wal_seq", PyValue::Int(PyInt::Small(i128::from(wal_seq))));
    ev
}

fn skipped_event(rule_id: &str, selector: &str, reason: &str) -> PyDict {
    let mut ev = PyDict::new();
    ev.insert("type", PyValue::from("match_skipped"));
    ev.insert("rule_id", PyValue::from(rule_id));
    ev.insert("selector", PyValue::from(selector));
    ev.insert("reason", PyValue::from(reason));
    ev
}

fn compile_all(rules: &[Arc<Rule>]) -> Result<HashMap<String, Arc<PatternMatcher>>, RuleError> {
    let mut out = HashMap::new();
    for rule in rules {
        out.insert(rule.rule_id.clone(), Arc::new(PatternMatcher::new(&rule.pattern)?));
    }
    Ok(out)
}

impl Shared {
    fn run(&self) {
        while !self.stop.load(Ordering::Acquire) {
            let item = {
                let mut q = self.queue.lock().unwrap_or_else(|e| e.into_inner());
                if q.is_empty() {
                    q = self
                        .ready
                        .wait_timeout(q, WORKER_POLL)
                        .unwrap_or_else(|e| e.into_inner())
                        .0;
                }
                q.pop_front()
            };
            let Some(item) = item else {
                continue;
            };
            if self.stop.load(Ordering::Acquire) {
                return;
            }
            if catch_unwind(AssertUnwindSafe(|| self.evaluate(&item))).is_err() {
                self.ctx.warn(&format!(
                    "event matcher 求值失敗，丟棄此 item（selector={}）",
                    item.selector
                ));
            }
        }
    }

    fn evaluate(&self, item: &Item) {
        let (rules, compiled) = {
            let c = self.compiled.lock().unwrap_or_else(|e| e.into_inner());
            (c.rules.clone(), c.compiled.clone())
        };
        let ctx = &self.ctx;
        for rule in rules {
            if !rule.selectors.iter().any(|s| s == "ALL" || *s == item.selector) {
                continue;
            }
            let counter = ctx.counter_for(&rule.rule_id);
            let now = ctx.now_ms();
            let active = ctx.active_cmd_id(&item.selector);
            let profile = ctx.com_profile(&item.selector);
            let Some(pm) = compiled.get(&rule.rule_id) else {
                continue;
            };
            let Some(m) = pm.eval(&item.line) else {
                continue;
            };
            let reject = if !apply_scope(&rule, active.as_deref()) {
                Some("scope_mismatch")
            } else if !apply_profile(&rule, profile.as_deref()) {
                Some("profile_mismatch")
            } else if !apply_max_fires(&rule, &counter) {
                Some("exhausted")
            } else if !apply_cooldown(&rule, &counter, now) {
                Some("cooldown")
            } else {
                None
            };
            if let Some(reason) = reject {
                if rule.debug {
                    ctx.emit_event(skipped_event(&rule.rule_id, &item.selector, reason));
                }
                continue;
            }
            ctx.emit_fire(MatcherFire {
                rule: Arc::clone(&rule),
                selector: item.selector.clone(),
                m,
                wal_seq: item.wal_seq,
                matched_at: now,
                active_cmd_id: active,
            });
        }
    }
}

/// thread `event-matcher`；bounded(1024)，drop-oldest 由 `feed_line` 實作。
pub struct MatcherWorker {
    shared: Arc<Shared>,
    done: crossbeam_channel::Receiver<()>,
}

impl MatcherWorker {
    pub fn start(rules: Vec<Arc<Rule>>, ctx: Arc<dyn MatcherContext>, queue_max: usize) -> Result<Self, RuleError> {
        let compiled = compile_all(&rules)?;
        let shared = Arc::new(Shared {
            queue: Mutex::new(VecDeque::new()),
            ready: Condvar::new(),
            queue_max,
            stop: AtomicBool::new(false),
            compiled: Mutex::new(Compiled { rules, compiled }),
            ctx,
        });
        let (done_tx, done) = crossbeam_channel::bounded(1);
        let worker = Arc::clone(&shared);
        std::thread::Builder::new()
            .name("event-matcher".into())
            .spawn(move || {
                worker.run();
                let _ = done_tx.send(());
            })
            .expect("無法建立 event-matcher thread");
        Ok(MatcherWorker { shared, done })
    }

    pub fn feed_line(&self, selector: &str, line: String, wal_seq: u64) {
        let item = Item {
            selector: selector.to_string(),
            line,
            wal_seq,
        };
        let dropped = {
            let mut q = self.shared.queue.lock().unwrap_or_else(|e| e.into_inner());
            if q.len() < self.shared.queue_max {
                q.push_back(item);
                None
            } else if let Some(oldest) = q.pop_front() {
                q.push_back(item);
                Some((oldest.selector, oldest.wal_seq))
            } else {
                // queue_max == 0：新 item 自身被丟棄（Python 第二次 put_nowait 仍 Full）。
                Some((item.selector, item.wal_seq))
            }
        };
        self.shared.ready.notify_one();
        if let Some((sel, seq)) = dropped {
            self.shared.ctx.emit_event(dropped_event(&sel, seq));
        }
    }

    /// Python：先換 `_rules` 再 `_rebuild_compiled`；重建失敗時回錯、`_compiled` 保留舊值。
    pub fn replace_rules(&self, rules: Vec<Arc<Rule>>) -> Result<(), RuleError> {
        let mut c = self.shared.compiled.lock().unwrap_or_else(|e| e.into_inner());
        c.rules = rules;
        c.compiled = compile_all(&c.rules)?;
        Ok(())
    }

    pub fn stop(self) {
        self.shared.stop.store(true, Ordering::Release);
        self.shared.ready.notify_all();
        if self.done.recv_timeout(STOP_JOIN).is_err() {
            self.shared.ctx.warn(
                "event-matcher thread did not exit within 2s; context may receive late callbacks",
            );
        }
    }
}
