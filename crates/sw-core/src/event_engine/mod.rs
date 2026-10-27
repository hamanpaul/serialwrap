//! Event Engine（P7 Task 11～16；`sw_core/event_engine/*`，09 §3）。
//!
//! `EventEngine`（Task 16）組裝 registry／counter／event log／matcher／dispatcher，語意照 `engine.py`；
//! RPC 在連線 thread 上直接呼叫（不經 session actor），內部鎖只保護規則集合與 enabled set。

pub mod counter;
pub mod dispatcher;
pub mod event_log;
pub mod line_buffer;
pub mod matcher;
mod pyops;
pub mod registry;
pub mod schema;

use std::collections::{BTreeSet, HashMap};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use sw_pal::traits::PalError;
use sw_pal::Platform;
use sw_proto::pycompat::PyErr;
use sw_proto::pyjson::{PyDict, PyInt, PyValue};

use self::counter::{valid_rule_id, Counter, CounterStore};
use self::dispatcher::{Dispatcher, DispatcherStopReport};
use self::event_log::EventLogger;
use self::line_buffer::LineBuffer;
use self::matcher::{int_add, MatcherContext, MatcherFire, MatcherWorker};
use self::registry::{RuleRegistry, UpsertError};
use self::schema::Rule;

/// `LineBuffer()` 預設 `max_line_bytes`（`line_buffer.py`）。
const LINE_MAX_BYTES: usize = 16 * 1024;

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

fn pal_err(e: PalError) -> PyErr {
    PyErr {
        message: e.py_message,
    }
}

fn event(pairs: Vec<(&str, PyValue)>) -> PyDict {
    let mut d = PyDict::new();
    for (k, v) in pairs {
        d.insert(k, v);
    }
    d
}

/// Python `x in rule.selectors`（selectors 皆為 str；非 str 的 x 永不相等）。
fn selector_in(sel: &PyValue, selectors: &[String]) -> bool {
    matches!(sel, PyValue::Str(s) if selectors.iter().any(|x| x == s))
}

fn rule_targets(sel: &PyValue, r: &Rule) -> bool {
    selector_in(sel, &r.selectors) || r.selectors.iter().any(|x| x == "ALL")
}

#[derive(Default)]
struct Inner {
    rules: Vec<Arc<Rule>>,
    enabled: BTreeSet<String>,
    started: bool,
    intake_closed: bool,
}

/// matcher 執行緒與 engine 共用的元件（`MatcherContextImpl` 經此存取 log／counter／dispatcher）。
struct Core {
    bridge: Arc<dyn BridgeQueries>,
    counters: Arc<CounterStore>,
    log: Arc<EventLogger>,
    clock: Arc<dyn sw_pal::traits::Clock>,
    warn: Arc<dyn Fn(String) + Send + Sync>,
    dispatcher: Mutex<Option<Dispatcher>>,
}

impl Core {
    fn write(&self, ev: PyDict) -> Result<(), PyErr> {
        self.log.write(ev).map_err(pal_err)
    }
}

/// `MatcherContextImpl`：Python 例外在 matcher `_evaluate` 被攔下並丟棄該 item——Rust 以 panic 表達，由
/// `MatcherWorker` 的 `catch_unwind` 收斂並 WARNING（#79 STA-3）。
struct Ctx(Arc<Core>);

impl MatcherContext for Ctx {
    fn active_cmd_id(&self, com: &str) -> Option<String> {
        self.0.bridge.active_cmd_id_for(com)
    }
    fn com_profile(&self, com: &str) -> Option<String> {
        self.0.bridge.profile_for(com)
    }
    fn now_ms(&self) -> i64 {
        pyops::py_time_ms(&*self.0.clock)
    }
    fn emit_fire(&self, fire: MatcherFire) {
        let core = &self.0;
        let rule = Arc::clone(&fire.rule);
        if let Err(e) = core.write(event(vec![
            ("type", PyValue::from("match_recorded")),
            ("rule_id", PyValue::from(rule.rule_id.as_str())),
            ("selector", PyValue::from(fire.selector.as_str())),
            (
                "wal_seq",
                PyValue::Int(PyInt::Small(i128::from(fire.wal_seq))),
            ),
            ("matched_text", PyValue::from(fire.m.matched_text.as_str())),
        ])) {
            panic!("{}", e.message);
        }
        let mut counter = core
            .counters
            .load(&rule.rule_id)
            .unwrap_or_else(|e| panic!("{}", e.message));
        // EVT-D-E1 KEEP：dispatch 之前即設 exhausted（`counter.fires + 1 >= max_fires`）。
        if let Some(max) = &rule.max_fires {
            let next = PyValue::Int(int_add(&counter.fires, &PyInt::Small(1)));
            let below = pyops::py_lt(&next, max).unwrap_or_else(|e| panic!("{}", e.message));
            if !below {
                counter.exhausted = true;
                if let Err(e) = core.counters.save(&rule.rule_id, &counter) {
                    panic!("{}", e.py_message);
                }
            }
        }
        let line = fire.m.matched_text.clone();
        if let Some(d) = core
            .dispatcher
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
        {
            d.dispatch(fire, line);
        }
    }
    fn emit_event(&self, ev: PyDict) {
        let _ = self.0.log.write(ev);
    }
    fn counter_for(&self, rule_id: &str) -> Counter {
        self.0
            .counters
            .load(rule_id)
            .unwrap_or_else(|e| panic!("{}", e.message))
    }
    fn warn(&self, msg: &str) {
        (self.0.warn)(msg.to_string());
    }
}

pub struct EventEngine {
    core: Arc<Core>,
    registry: RuleRegistry,
    queue_max: usize,
    per_daemon_max: usize,
    platform: Arc<Platform>,
    inner: Mutex<Inner>,
    matcher: Mutex<Option<MatcherWorker>>,
    buffers: Mutex<HashMap<String, LineBuffer>>,
}

impl EventEngine {
    pub fn new(d: EngineDeps) -> sw_pal::PalResult<Self> {
        let fs = Arc::clone(&d.platform.fs);
        let registry = RuleRegistry::new(d.events_dir, Arc::clone(&fs))?;
        let counters = Arc::new(CounterStore::new(d.runtime_dir, Arc::clone(&fs))?);
        let log = Arc::new(EventLogger::new(
            d.log_path,
            fs,
            Arc::clone(&d.platform.clock),
        )?);
        Ok(Self {
            core: Arc::new(Core {
                bridge: d.bridge,
                counters,
                log,
                clock: Arc::clone(&d.platform.clock),
                warn: d.warn,
                dispatcher: Mutex::new(None),
            }),
            registry,
            queue_max: d.queue_max,
            per_daemon_max: d.per_daemon_max,
            platform: d.platform,
            inner: Mutex::new(Inner::default()),
            matcher: Mutex::new(None),
            buffers: Mutex::new(HashMap::new()),
        })
    }

    fn inner(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// `_mark_com_enabled`：已啟用不重複記錄。
    fn mark_enabled(&self, inner: &mut Inner, com: &str, by: &str) -> Result<(), PyErr> {
        if !inner.enabled.insert(com.to_string()) {
            return Ok(());
        }
        self.core.write(event(vec![
            ("type", PyValue::from("com_enabled")),
            ("selector", PyValue::from(com)),
            ("triggered_by", PyValue::from(by)),
        ]))
    }

    /// `auto_enable_com_on_load` 的 selector 展開；`ALL` 展開為**當下** `known_coms()`（EVT-D-D11 KEEP）。
    fn auto_enable(&self, inner: &mut Inner, rule: &Rule, by: &str) -> Result<(), PyErr> {
        if !rule.auto_enable_com_on_load {
            return Ok(());
        }
        for sel in &rule.selectors {
            if sel == "ALL" {
                for com in self.core.bridge.known_coms() {
                    self.mark_enabled(inner, &com, by)?;
                }
            } else {
                self.mark_enabled(inner, sel, by)?;
            }
        }
        Ok(())
    }

    fn replace_matcher_rules(&self, rules: &[Arc<Rule>]) {
        if let Some(m) = self
            .matcher
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
        {
            let _ = m.replace_rules(rules.to_vec());
        }
    }

    fn write_failed(&self, failed: &[registry::FailedLoad]) -> Result<(), PyErr> {
        for f in failed {
            self.core.write(event(vec![
                ("type", PyValue::from("rule_load_failed")),
                ("path", PyValue::from(f.path.as_str())),
                ("reason", PyValue::from(f.reason.as_str())),
            ]))?;
        }
        Ok(())
    }

    pub fn start(&self) -> Result<(), PyErr> {
        if self.inner().started {
            return Ok(());
        }
        let emit_log = Arc::clone(&self.core.log);
        let emit: Arc<dyn Fn(PyDict) + Send + Sync> = Arc::new(move |ev| {
            let _ = emit_log.write(ev);
        });
        *self
            .core
            .dispatcher
            .lock()
            .unwrap_or_else(|e| e.into_inner()) = Some(Dispatcher::start(
            self.per_daemon_max,
            Arc::clone(&self.platform.handlers),
            Arc::clone(&self.core.counters),
            emit,
            Arc::clone(&self.platform.env),
            Arc::clone(&self.platform.clock),
        ));
        let load = self.registry.load_all()?;
        self.write_failed(&load.failed)?;
        let rules = {
            let mut inner = self.inner();
            inner.rules = load.rules.clone();
            for rule in &load.rules {
                self.core.write(event(vec![
                    ("type", PyValue::from("rule_loaded")),
                    ("rule_id", PyValue::from(rule.rule_id.as_str())),
                ]))?;
                self.auto_enable(&mut inner, rule, "auto")?;
            }
            inner.rules.clone()
        };
        let ctx: Arc<dyn MatcherContext> = Arc::new(Ctx(Arc::clone(&self.core)));
        let worker = MatcherWorker::start(rules, ctx, self.queue_max).map_err(|e| PyErr {
            message: format!("{e:?}"),
        })?;
        *self.matcher.lock().unwrap_or_else(|e| e.into_inner()) = Some(worker);
        self.inner().started = true;
        Ok(())
    }

    /// 冪等：matcher.stop → dispatcher.close_intake；之後 `feed_rx` 為 no-op（R1-F2）。
    pub fn close_intake(&self) {
        {
            let mut inner = self.inner();
            if inner.intake_closed {
                return;
            }
            inner.intake_closed = true;
        }
        if let Some(m) = self
            .matcher
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take()
        {
            m.stop();
        }
        if let Some(d) = self
            .core
            .dispatcher
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
        {
            d.close_intake();
        }
    }

    /// close_intake（若未呼叫）→ dispatcher.stop(deadline)（SC-11 細化）；逾時終止者經 `deps.warn` WARNING。
    pub fn stop(&self, deadline: std::time::Instant) -> DispatcherStopReport {
        self.close_intake();
        let Some(d) = self
            .core
            .dispatcher
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take()
        else {
            return DispatcherStopReport::default();
        };
        let report = d.stop(deadline);
        if let Some(w) = report.warning() {
            (self.core.warn)(w);
        }
        self.inner().started = false;
        report
    }

    /// per-COM LineBuffer（永不重設，EVT-D-S5）→ enabled 才 feed_line（Python service `_engine_rx_observer`＋`feed_line`）。
    pub fn feed_rx(&self, com: &str, data: &[u8], wal_seq: u64) {
        let lines = {
            let mut b = self.buffers.lock().unwrap_or_else(|e| e.into_inner());
            b.entry(com.to_string())
                .or_insert_with(|| LineBuffer::new(LINE_MAX_BYTES))
                .feed(data)
        };
        for line in lines {
            {
                let inner = self.inner();
                if inner.intake_closed || !inner.enabled.contains(com) {
                    continue;
                }
            }
            if let Some(m) = self
                .matcher
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .as_ref()
            {
                m.feed_line(com, line, wal_seq);
            }
        }
    }

    pub fn rule_set(&self, obj: &PyValue) -> Result<Arc<Rule>, UpsertError> {
        let rule = self.registry.upsert(obj)?;
        let io = |e: PyErr| {
            UpsertError::Io(PalError::plain(sw_pal::traits::PalErrorKind::Io, e.message))
        };
        {
            let mut inner = self.inner();
            inner.rules.retain(|r| r.rule_id != rule.rule_id);
            inner.rules.push(Arc::clone(&rule));
            let rules = inner.rules.clone();
            self.replace_matcher_rules(&rules);
            self.auto_enable(&mut inner, &rule, "rule_set")
                .map_err(io)?;
        }
        self.core
            .write(event(vec![
                ("type", PyValue::from("rule_loaded")),
                ("rule_id", PyValue::from(rule.rule_id.as_str())),
            ]))
            .map_err(io)?;
        Ok(rule)
    }

    pub fn rule_delete(&self, rule_id: &str) -> Result<bool, PyErr> {
        if !valid_rule_id(rule_id) {
            return Ok(false);
        }
        let ok = self.registry.delete(rule_id).map_err(pal_err)?;
        if ok {
            self.core.counters.clear(rule_id).map_err(pal_err)?;
            {
                let mut inner = self.inner();
                inner.rules.retain(|r| r.rule_id != rule_id);
                let rules = inner.rules.clone();
                self.replace_matcher_rules(&rules);
            }
            self.core.write(event(vec![
                ("type", PyValue::from("rule_unloaded")),
                ("rule_id", PyValue::from(rule_id)),
            ]))?;
        }
        Ok(ok)
    }

    pub fn rule_list(
        &self,
        selector: Option<&PyValue>,
        owner: Option<&PyValue>,
    ) -> Result<Vec<PyDict>, PyErr> {
        let rules = self.inner().rules.clone();
        let mut out = Vec::new();
        for r in rules {
            if let Some(sel) = selector.filter(|v| !v.is_null()) {
                if !rule_targets(sel, &r) {
                    continue;
                }
            }
            if let Some(o) = owner.filter(|v| !v.is_null()) {
                if !pyops::py_eq(&PyValue::from(r.owner.as_str()), o) {
                    continue;
                }
            }
            let c = self.core.counters.load(&r.rule_id)?;
            out.push(event(vec![
                ("rule_id", PyValue::from(r.rule_id.as_str())),
                ("owner", PyValue::from(r.owner.as_str())),
                ("kind", PyValue::from(r.kind.as_str())),
                (
                    "selectors",
                    PyValue::List(
                        r.selectors
                            .iter()
                            .map(|s| PyValue::from(s.as_str()))
                            .collect(),
                    ),
                ),
                ("fires", PyValue::Int(c.fires.clone())),
                ("exhausted", PyValue::Bool(c.exhausted)),
                (
                    "last_fire_ts",
                    c.last_fire_ts.clone().map_or(PyValue::Null, PyValue::Int),
                ),
            ]));
        }
        Ok(out)
    }

    pub fn rule_get(&self, rule_id: &str) -> Result<Option<PyDict>, PyErr> {
        let rule = self
            .inner()
            .rules
            .iter()
            .find(|r| r.rule_id == rule_id)
            .cloned();
        let Some(rule) = rule else {
            return Ok(None);
        };
        let c = self.core.counters.load(rule_id)?;
        Ok(Some(event(vec![
            ("rule", PyValue::Dict(rule.raw.clone())),
            ("counter", PyValue::Dict(c.to_json())),
        ])))
    }

    /// `reload`：diff 後重新載入（失敗檔記 `rule_load_failed`）；移除者清 counter、新增者 auto enable（`reload`），
    /// 再依 added／removed／changed 記事件；不 disable COM（EVT-D-E10 KEEP）。清單升冪（RPC-H-13）。
    pub fn reload(&self) -> Result<PyDict, PyErr> {
        let previous = self.inner().rules.clone();
        let diff = self.registry.diff_against(&previous)?;
        let load = self.registry.load_all()?;
        self.write_failed(&load.failed)?;
        {
            let mut inner = self.inner();
            inner.rules = load.rules.clone();
            let rules = inner.rules.clone();
            self.replace_matcher_rules(&rules);
            for rid in &diff.removed {
                self.core.counters.clear(rid).map_err(pal_err)?;
            }
            for rid in &diff.added {
                if let Some(r) = load.rules.iter().find(|r| &r.rule_id == rid) {
                    self.auto_enable(&mut inner, r, "reload")?;
                }
            }
        }
        let note = |t: &str, rid: &str| {
            self.core.write(event(vec![
                ("type", PyValue::from(t)),
                ("rule_id", PyValue::from(rid)),
            ]))
        };
        for rid in &diff.added {
            note("rule_loaded", rid)?;
        }
        for rid in &diff.removed {
            note("rule_unloaded", rid)?;
        }
        for rid in &diff.changed {
            note("rule_loaded", rid)?;
        }
        let list =
            |v: &[String]| PyValue::List(v.iter().map(|s| PyValue::from(s.as_str())).collect());
        Ok(event(vec![
            ("added", list(&diff.added)),
            ("removed", list(&diff.removed)),
            ("changed", list(&diff.changed)),
        ]))
    }

    pub fn com_enable(&self, com: &str) -> Result<PyDict, PyErr> {
        {
            let mut inner = self.inner();
            self.mark_enabled(&mut inner, com, "manual")?;
        }
        self.com_status(Some(&PyValue::from(com)))
    }

    /// 先清受影響規則的 counter（`counter_reset`，`triggered_by: com_disable`）再記 `com_disabled`——即使原本未啟用。
    pub fn com_disable(&self, com: &str) -> Result<PyDict, PyErr> {
        let sel = PyValue::from(com);
        let affected: Vec<String> = {
            let mut inner = self.inner();
            inner.enabled.remove(com);
            inner
                .rules
                .iter()
                .filter(|r| rule_targets(&sel, r))
                .map(|r| r.rule_id.clone())
                .collect()
        };
        for rid in &affected {
            self.counter_reset(rid, "com_disable")?;
        }
        self.core.write(event(vec![
            ("type", PyValue::from("com_disabled")),
            ("selector", PyValue::from(com)),
            ("triggered_by", PyValue::from("manual")),
        ]))?;
        self.com_status(Some(&sel))
    }

    fn counter_reset(&self, rid: &str, by: &str) -> Result<(), PyErr> {
        let previous = self.core.counters.load(rid)?;
        self.core.counters.clear(rid).map_err(pal_err)?;
        self.core.write(event(vec![
            ("type", PyValue::from("counter_reset")),
            ("rule_id", PyValue::from(rid)),
            ("previous_fires", PyValue::Int(previous.fires)),
            ("triggered_by", PyValue::from(by)),
        ]))
    }

    /// `com_status(None)` → `{"coms": sorted}`；否則 `{"selector", "enabled", "active_rules"}`（set 成員檢查：
    /// list／dict 為 unhashable TypeError）。
    pub fn com_status(&self, com: Option<&PyValue>) -> Result<PyDict, PyErr> {
        let inner = self.inner();
        let Some(com) = com.filter(|v| !v.is_null()) else {
            return Ok(event(vec![(
                "coms",
                PyValue::List(
                    inner
                        .enabled
                        .iter()
                        .map(|s| PyValue::from(s.as_str()))
                        .collect(),
                ),
            )]));
        };
        if let PyValue::List(_) | PyValue::Dict(_) = com {
            return Err(PyErr {
                message: format!(
                    "unhashable type: '{}'",
                    if matches!(com, PyValue::List(_)) {
                        "list"
                    } else {
                        "dict"
                    }
                ),
            });
        }
        let enabled = matches!(com, PyValue::Str(s) if inner.enabled.contains(s));
        let active: Vec<PyValue> = inner
            .rules
            .iter()
            .filter(|r| rule_targets(com, r))
            .map(|r| PyValue::from(r.rule_id.as_str()))
            .collect();
        Ok(event(vec![
            ("selector", com.clone()),
            ("enabled", PyValue::Bool(enabled)),
            ("active_rules", PyValue::List(active)),
        ]))
    }

    /// `reset`：`rule_id` 優先（不合法 → 0、不檢查存在）；否則 selector 命中的規則；回清除數。
    pub fn reset(
        &self,
        rule_id: Option<&PyValue>,
        selector: Option<&PyValue>,
    ) -> Result<i128, PyErr> {
        let rule_id = rule_id.filter(|v| !v.is_null());
        let selector = selector.filter(|v| !v.is_null());
        let targets: Vec<String> = match (rule_id, selector) {
            (Some(rid), _) => match rid {
                PyValue::Str(s) if valid_rule_id(s) => vec![s.clone()],
                _ => return Ok(0),
            },
            (None, Some(sel)) => self
                .inner()
                .rules
                .iter()
                .filter(|r| rule_targets(sel, r))
                .map(|r| r.rule_id.clone())
                .collect(),
            (None, None) => Vec::new(),
        };
        for rid in &targets {
            self.counter_reset(rid, "reset")?;
        }
        Ok(targets.len() as i128)
    }

    pub fn tail(
        &self,
        rule_id: Option<&PyValue>,
        selector: Option<&PyValue>,
        since_ts: Option<&PyValue>,
        n: Option<&PyValue>,
    ) -> Result<Vec<PyValue>, PyErr> {
        self.core.log.tail(rule_id, selector, since_ts, n)
    }
}
