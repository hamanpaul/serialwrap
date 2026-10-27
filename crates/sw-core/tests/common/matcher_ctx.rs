//! P7 Task 12：`MatcherContext` 錄製替身（可阻塞 `emit_fire`、可注入 `counter_for` panic）。
#![allow(dead_code)]

use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use sw_core::event_engine::counter::Counter;
use sw_core::event_engine::matcher::{MatcherContext, MatcherFire};
use sw_core::event_engine::schema::{validate_rule_dict, Rule};
use sw_proto::pyjson::{self, PyDict, PyInt, PyValue, Sep};

#[derive(Default)]
struct St {
    fires: Vec<(String, String, u64, Vec<Option<String>>, String)>,
    events: Vec<PyDict>,
    warnings: Vec<String>,
    blocked: bool,
    released: bool,
    counter: Option<Counter>,
    panic_counter_once: bool,
}

pub struct RecordingMatcherCtx {
    st: Mutex<St>,
    cv: Condvar,
    blocking: bool,
    pub active: Mutex<Option<String>>,
    pub profile: Mutex<Option<String>>,
    pub now: Mutex<i64>,
}

impl RecordingMatcherCtx {
    pub fn new() -> Arc<Self> {
        Self::make(false)
    }
    /// `emit_fire` 會阻塞直到 `release()`。
    pub fn blocking() -> Arc<Self> {
        Self::make(true)
    }
    fn make(blocking: bool) -> Arc<Self> {
        Arc::new(RecordingMatcherCtx {
            st: Mutex::new(St::default()),
            cv: Condvar::new(),
            blocking,
            active: Mutex::new(None),
            profile: Mutex::new(None),
            now: Mutex::new(0),
        })
    }
    pub fn set_counter(&self, c: Counter) {
        self.st.lock().unwrap().counter = Some(c);
    }
    pub fn panic_counter_once(&self) {
        self.st.lock().unwrap().panic_counter_once = true;
    }
    pub fn wait_blocked(&self) {
        let mut st = self.st.lock().unwrap();
        while !st.blocked {
            let (g, t) = self.cv.wait_timeout(st, Duration::from_secs(5)).unwrap();
            st = g;
            assert!(!t.timed_out() || st.blocked, "emit_fire 未被呼叫");
        }
    }
    pub fn release(&self) {
        self.st.lock().unwrap().released = true;
        self.cv.notify_all();
    }
    /// 等到 fire 數達 `n`（最多 5 s）。
    pub fn wait_fires(&self, n: usize) {
        let mut st = self.st.lock().unwrap();
        while st.fires.len() < n {
            let (g, t) = self.cv.wait_timeout(st, Duration::from_secs(5)).unwrap();
            st = g;
            assert!(!t.timed_out() || st.fires.len() >= n, "fire 數不足：{}", st.fires.len());
        }
    }
    /// `(rule_id, selector, wal_seq, groups, matched_text)`。
    pub fn fires(&self) -> Vec<(String, String, u64, Vec<Option<String>>, String)> {
        self.st.lock().unwrap().fires.clone()
    }
    pub fn fire_rule_ids(&self) -> Vec<String> {
        self.fires().into_iter().map(|f| f.0).collect()
    }
    pub fn events_of_type(&self, t: &str) -> Vec<PyDict> {
        self.st
            .lock()
            .unwrap()
            .events
            .iter()
            .filter(|e| e.get("type").and_then(PyValue::as_str) == Some(t))
            .cloned()
            .collect()
    }
    pub fn warnings(&self) -> Vec<String> {
        self.st.lock().unwrap().warnings.clone()
    }
}

impl MatcherContext for RecordingMatcherCtx {
    fn active_cmd_id(&self, _com: &str) -> Option<String> {
        self.active.lock().unwrap().clone()
    }
    fn com_profile(&self, _com: &str) -> Option<String> {
        self.profile.lock().unwrap().clone()
    }
    fn now_ms(&self) -> i64 {
        *self.now.lock().unwrap()
    }
    fn emit_fire(&self, fire: MatcherFire) {
        let mut st = self.st.lock().unwrap();
        st.fires.push((
            fire.rule.rule_id.clone(),
            fire.selector.clone(),
            fire.wal_seq,
            fire.m.groups.clone(),
            fire.m.matched_text.clone(),
        ));
        self.cv.notify_all();
        if self.blocking {
            st.blocked = true;
            self.cv.notify_all();
            while !st.released {
                st = self.cv.wait(st).unwrap();
            }
        }
    }
    fn emit_event(&self, ev: PyDict) {
        self.st.lock().unwrap().events.push(ev);
        self.cv.notify_all();
    }
    fn counter_for(&self, _rule_id: &str) -> Counter {
        let mut st = self.st.lock().unwrap();
        if st.panic_counter_once {
            st.panic_counter_once = false;
            drop(st);
            panic!("注入：counter store I/O 失敗");
        }
        st.counter.clone().unwrap_or(Counter {
            fires: PyInt::Small(0),
            last_fire_ts: None,
            exhausted: false,
        })
    }
    fn warn(&self, msg: &str) {
        self.st.lock().unwrap().warnings.push(msg.to_string());
        self.cv.notify_all();
    }
}

/// 以合法預設規則覆寫 `fragment` 後驗證。
pub fn rule(fragment: &str) -> Arc<Rule> {
    Arc::new(validate_rule_dict(&super::p7::rule_json(fragment)).expect("規則應合法"))
}

pub fn rule_contains(value: &str, selectors: &[&str]) -> Arc<Rule> {
    let sel = selectors
        .iter()
        .map(|s| format!("\"{s}\""))
        .collect::<Vec<_>>()
        .join(",");
    rule(&format!(
        r#"{{"selectors":[{sel}],"pattern":{{"kind":"contains","value":"{value}"}}}}"#
    ))
}

/// `json.dumps(d)`（預設分隔符、不排序）。
pub fn dumps_default(d: &PyDict) -> String {
    pyjson::dumps_dict(
        d,
        &pyjson::DumpOpts {
            sep: Sep::Default,
            ensure_ascii: true,
            sort_keys: false,
            allow_nan: true,
        },
    )
}
