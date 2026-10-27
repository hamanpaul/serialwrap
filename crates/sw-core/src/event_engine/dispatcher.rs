//! handler 派送（P7 Task 15；`event_engine/dispatcher.py`，SC-11 細化、R1-F2）。
//!
//! per-rule 併發 1、per-daemon 上限 `per_daemon_max`（`event_dropped` 的 `per_rule_busy`／`per_daemon_saturated`）；
//! worker 執行 `_run_handler`：讀 counter → payload／env → spawn → `fire_completed`／`fire_timeout`／`fire_failed`
//! → `_post_fire_counter_update`（fires＝新值、`last_fire_ts`＝完成時刻）。關機：`close_intake` 後等到期限，
//! 逾時 abort 仍在跑者並再等 100 ms（Rust 細化；Python `shutdown(wait=True)` 無期限）。

use std::collections::{HashMap, HashSet};
use std::sync::{mpsc, Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use sw_pal::traits::{
    Clock, HandlerAbort, HandlerOutcome, HandlerSpawner, HandlerSpec, PalErrorKind, SysEnv,
};
use sw_proto::pycompat::decode_utf8_replace;
use sw_proto::pyjson::{self, PyDict, PyInt, PyValue};

use super::counter::CounterStore;
use super::matcher::{int_add, MatcherFire};
use super::pyops::{py_time_ms, py_time_s};

/// dispatcher `_STDIO_TAIL_BYTES`（stdout／stderr tail 與 `SERIALWRAP_EVENT_MATCHED_TEXT` 字元上限）。
const STDIO_TAIL_BYTES: usize = 4096;
/// abort 後等待 worker 的 `run` 回收子行程樹返回的寬限。
const ABORT_GRACE: Duration = Duration::from_millis(100);

fn int_str(i: &PyInt) -> String {
    match i {
        PyInt::Small(n) => n.to_string(),
        PyInt::Big(s) => s.clone(),
    }
}

/// `counter.fires + 1`（結果可回到 i128 時以 `Small` 表示）。
fn int_inc(i: &PyInt) -> PyInt {
    if let PyInt::Small(n) = i {
        if let Some(m) = n.checked_add(1) {
            return PyInt::Small(m);
        }
    }
    match int_add(i, &PyInt::Small(1)) {
        PyInt::Big(s) => s.parse::<i128>().map_or(PyInt::Big(s), PyInt::Small),
        small => small,
    }
}

/// `build_payload`；`bridge_generation` 恆 0（EVT-D-E2 KEEP）。
pub fn build_payload(fire: &MatcherFire, fire_count: &PyInt, matched_line: &str) -> PyDict {
    let rule = &fire.rule;
    let mut d = PyDict::new();
    d.insert("schema_version", PyValue::from(1));
    d.insert("rule_id", PyValue::from(rule.rule_id.as_str()));
    d.insert("rule_name", PyValue::from(rule.name.as_str()));
    d.insert("owner", PyValue::from(rule.owner.as_str()));
    d.insert("kind", PyValue::from(rule.kind.as_str()));
    d.insert("selector", PyValue::from(fire.selector.as_str()));
    d.insert("matched_at", PyValue::from(fire.matched_at));
    d.insert("matched_line", PyValue::from(matched_line));
    d.insert("matched_text", PyValue::from(fire.m.matched_text.as_str()));
    d.insert(
        "match_groups",
        PyValue::List(
            fire.m
                .groups
                .iter()
                .map(|g| g.as_deref().map_or(PyValue::Null, PyValue::from))
                .collect(),
        ),
    );
    d.insert("scope", PyValue::from(rule.scope.as_str()));
    d.insert(
        "active_cmd_id",
        fire.active_cmd_id
            .as_deref()
            .map_or(PyValue::Null, PyValue::from),
    );
    d.insert(
        "wal_seq",
        PyValue::Int(PyInt::Small(i128::from(fire.wal_seq))),
    );
    d.insert("bridge_generation", PyValue::from(0));
    d.insert("fire_count", PyValue::Int(fire_count.clone()));
    d.insert("level", PyValue::from(rule.level.as_str()));
    d.insert("profile", PyValue::from(rule.profile.as_str()));
    d
}

/// `build_env`：`inherited`（呼叫端依白名單順序取自環境）後接 11 個 `SERIALWRAP_EVENT_*`；
/// `matched_text` 超過 4096 字元時取前 4096 字元＋`" ...truncated"`。
pub fn build_env(
    fire: &MatcherFire,
    fire_count: &PyInt,
    inherited: &[(String, String)],
) -> Vec<(String, String)> {
    let rule = &fire.rule;
    let text = &fire.m.matched_text;
    let matched_text = if text.chars().count() > STDIO_TAIL_BYTES {
        let head: String = text.chars().take(STDIO_TAIL_BYTES).collect();
        format!("{head} ...truncated")
    } else {
        text.clone()
    };
    let mut env: Vec<(String, String)> = inherited.to_vec();
    let mut set = |k: &str, v: String| match env.iter_mut().find(|(ek, _)| ek == k) {
        Some(slot) => slot.1 = v,
        None => env.push((k.to_string(), v)),
    };
    set("SERIALWRAP_EVENT_SCHEMA_VERSION", "1".into());
    set("SERIALWRAP_EVENT_RULE_ID", rule.rule_id.clone());
    set("SERIALWRAP_EVENT_OWNER", rule.owner.clone());
    set("SERIALWRAP_EVENT_KIND", rule.kind.clone());
    set("SERIALWRAP_EVENT_SELECTOR", fire.selector.clone());
    set("SERIALWRAP_EVENT_MATCHED_AT", fire.matched_at.to_string());
    set("SERIALWRAP_EVENT_MATCHED_TEXT", matched_text);
    set("SERIALWRAP_EVENT_FIRE_COUNT", int_str(fire_count));
    set("SERIALWRAP_EVENT_WAL_SEQ", fire.wal_seq.to_string());
    set("SERIALWRAP_EVENT_LEVEL", rule.level.clone());
    set("SERIALWRAP_EVENT_SCOPE", rule.scope.clone());
    env
}

/// `(json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")`。
pub fn stdin_bytes(payload: &PyDict) -> Vec<u8> {
    let mut s = pyjson::dumps_dict(
        payload,
        &pyjson::DumpOpts {
            sep: pyjson::Sep::Default,
            ensure_ascii: false,
            sort_keys: false,
            allow_nan: true,
        },
    );
    s.push('\n');
    s.into_bytes()
}

#[derive(Default)]
struct State {
    closed: bool,
    /// stop 已逾時：之後才登記 `running` 的 handler 立即 abort（審查 96b28dd_06c61d0 91109e7-1）。
    aborting: bool,
    busy: HashSet<String>,
    inflight: usize,
    running: HashMap<String, HandlerAbort>,
}

struct Shared {
    state: Mutex<State>,
    idle: Condvar,
    spawner: Arc<dyn HandlerSpawner>,
    counters: Arc<CounterStore>,
    emit: Arc<dyn Fn(PyDict) + Send + Sync>,
    env: Arc<dyn SysEnv>,
    clock: Arc<dyn Clock>,
    max: usize,
}

struct Job {
    fire: MatcherFire,
    line: String,
}

/// `per_daemon_max` 條 worker thread `event-dispatch-N`＋有界 job channel（`dispatch` 先以 inflight 檢查，送出不阻塞）。
pub struct Dispatcher {
    shared: Arc<Shared>,
    jobs: Mutex<Option<mpsc::SyncSender<Job>>>,
}

fn event(pairs: Vec<(&str, PyValue)>) -> PyDict {
    let mut d = PyDict::new();
    for (k, v) in pairs {
        d.insert(k, v);
    }
    d
}

impl Shared {
    fn release(&self, rule_id: &str) {
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        st.busy.remove(rule_id);
        st.running.remove(rule_id);
        st.inflight = st.inflight.saturating_sub(1);
        if st.inflight == 0 {
            self.idle.notify_all();
        }
    }

    fn run_handler(&self, fire: MatcherFire, line: String) {
        let rule_id = fire.rule.rule_id.clone();
        self.run_inner(&fire, &line);
        self.release(&rule_id);
    }

    fn run_inner(&self, fire: &MatcherFire, line: &str) {
        let rule = &fire.rule;
        // 例外（壞 counter 檔）在 Python 穿出 try、只由 finally 釋放：不 emit。
        let Ok(counter) = self.counters.load(&rule.rule_id) else {
            return;
        };
        let new_fires = int_inc(&counter.fires);
        let payload = build_payload(fire, &new_fires, line);
        let inherited: Vec<(String, String)> = self
            .spawner
            .inherited_env_keys()
            .iter()
            .filter_map(|k| self.env.var(k).map(|v| (k.to_string(), v)))
            .collect();
        let env = build_env(fire, &new_fires, &inherited);
        let argv = match &rule.handler.exec {
            Some(exec) => exec.clone(),
            None => self
                .spawner
                .shell_argv(rule.handler.shell.as_deref().unwrap_or("")),
        };
        let spec = HandlerSpec {
            argv,
            env,
            stdin: stdin_bytes(&payload),
            timeout: rule.timeout(),
            tail_bytes: STDIO_TAIL_BYTES,
        };
        let abort = HandlerAbort::new();
        {
            let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if st.aborting {
                abort.trigger();
            }
            st.running.insert(rule.rule_id.clone(), abort.clone());
        }
        let start = py_time_s(&*self.clock);
        let outcome = self.spawner.run(&spec, &abort);
        let duration = || PyValue::from(((py_time_s(&*self.clock) - start) * 1000.0) as i64);
        let wal_seq = PyValue::Int(PyInt::Small(i128::from(fire.wal_seq)));
        match outcome {
            HandlerOutcome::SpawnFailed(e) => {
                // Popen 的 ValueError（嵌入 NUL）不屬 `except OSError`：穿出、不 emit、不更新 counter。
                if e.os_code.is_none()
                    && e.winerror.is_none()
                    && e.kind == PalErrorKind::InvalidInput
                {
                    return;
                }
                (self.emit)(event(vec![
                    ("type", PyValue::from("fire_failed")),
                    ("rule_id", PyValue::from(rule.rule_id.as_str())),
                    ("selector", PyValue::from(fire.selector.as_str())),
                    ("reason", PyValue::from(e.py_message.as_str())),
                ]));
            }
            HandlerOutcome::Completed {
                exit_code,
                stdout_tail,
                stderr_tail,
            } => {
                let tail = |b: &[u8]| {
                    let cut = b.len().saturating_sub(STDIO_TAIL_BYTES);
                    PyValue::from(decode_utf8_replace(&b[cut..]))
                };
                (self.emit)(event(vec![
                    ("type", PyValue::from("fire_completed")),
                    ("rule_id", PyValue::from(rule.rule_id.as_str())),
                    ("selector", PyValue::from(fire.selector.as_str())),
                    (
                        "exit_code",
                        PyValue::from(sw_pal::process::py_returncode(exit_code)),
                    ),
                    ("duration_ms", duration()),
                    ("stdout_tail", tail(&stdout_tail)),
                    ("stderr_tail", tail(&stderr_tail)),
                    ("fire_count", PyValue::Int(new_fires.clone())),
                    ("wal_seq", wal_seq),
                ]));
            }
            HandlerOutcome::TimedOut => {
                (self.emit)(event(vec![
                    ("type", PyValue::from("fire_timeout")),
                    ("rule_id", PyValue::from(rule.rule_id.as_str())),
                    ("selector", PyValue::from(fire.selector.as_str())),
                    ("duration_ms", duration()),
                    ("fire_count", PyValue::Int(new_fires.clone())),
                    ("wal_seq", wal_seq),
                ]));
            }
        }
        self.post_fire_counter_update(&rule.rule_id, new_fires);
    }

    /// `_post_fire_counter_update`：重讀 counter、寫回 fires 與完成時刻（例外在 Python 穿出並被 executor 吞掉）。
    fn post_fire_counter_update(&self, rule_id: &str, new_fires: PyInt) {
        let Ok(mut counter) = self.counters.load(rule_id) else {
            return;
        };
        counter.fires = new_fires;
        counter.last_fire_ts = Some(PyInt::Small(i128::from(py_time_ms(&*self.clock))));
        let _ = self.counters.save(rule_id, &counter);
    }
}

impl Dispatcher {
    pub fn start(
        per_daemon_max: usize,
        spawner: Arc<dyn HandlerSpawner>,
        counters: Arc<CounterStore>,
        emit: Arc<dyn Fn(PyDict) + Send + Sync>,
        env: Arc<dyn SysEnv>,
        clock: Arc<dyn Clock>,
    ) -> Self {
        let max = per_daemon_max.max(1);
        let shared = Arc::new(Shared {
            state: Mutex::new(State::default()),
            idle: Condvar::new(),
            spawner,
            counters,
            emit,
            env,
            clock,
            max: per_daemon_max,
        });
        let (tx, rx) = mpsc::sync_channel::<Job>(max);
        let rx = Arc::new(Mutex::new(rx));
        for n in 0..max {
            let rx = Arc::clone(&rx);
            let shared = Arc::clone(&shared);
            std::thread::Builder::new()
                .name(format!("event-dispatch-{n}"))
                .spawn(move || loop {
                    let job = rx.lock().unwrap_or_else(|e| e.into_inner()).recv();
                    match job {
                        Ok(job) => shared.run_handler(job.fire, job.line),
                        Err(_) => break,
                    }
                })
                .expect("spawn event-dispatch worker");
        }
        Self {
            shared,
            jobs: Mutex::new(Some(tx)),
        }
    }

    pub fn dispatch(&self, fire: MatcherFire, matched_line: String) {
        let rule_id = fire.rule.rule_id.clone();
        {
            let mut st = self.shared.state.lock().unwrap_or_else(|e| e.into_inner());
            if st.closed {
                return;
            }
            let dropped = |reason: &str| {
                event(vec![
                    ("type", PyValue::from("event_dropped")),
                    ("reason", PyValue::from(reason)),
                    ("rule_id", PyValue::from(rule_id.as_str())),
                    ("selector", PyValue::from(fire.selector.as_str())),
                ])
            };
            if st.busy.contains(&rule_id) {
                (self.shared.emit)(dropped("per_rule_busy"));
                return;
            }
            if st.inflight >= self.shared.max {
                (self.shared.emit)(dropped("per_daemon_saturated"));
                return;
            }
            st.busy.insert(rule_id.clone());
            st.inflight += 1;
        }
        let sent = self
            .jobs
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
            .map(|tx| {
                tx.try_send(Job {
                    fire,
                    line: matched_line,
                })
            });
        if !matches!(sent, Some(Ok(()))) {
            self.shared.release(&rule_id);
        }
    }

    /// 冪等；之後 `dispatch()` 直接丟棄（不 spawn、不計 fire_count），已在跑的 handler 不受影響（R1-F2）。
    pub fn close_intake(&self) {
        self.shared
            .state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .closed = true;
    }

    /// 先 close_intake；等到 deadline；逾時 `HandlerAbort::trigger()`，之後最多再等 100 ms。
    pub fn stop(self, deadline: Instant) -> DispatcherStopReport {
        self.close_intake();
        // 關閉 job channel：閒置 worker 收到 Err 後結束；忙碌 worker 跑完當前 handler 後結束。
        self.jobs.lock().unwrap_or_else(|e| e.into_inner()).take();
        let shared = &self.shared;
        let wait_idle = |until: Instant| {
            let mut st = shared.state.lock().unwrap_or_else(|e| e.into_inner());
            while st.inflight > 0 {
                let now = Instant::now();
                if now >= until {
                    break;
                }
                st = shared
                    .idle
                    .wait_timeout(st, until - now)
                    .unwrap_or_else(|e| e.into_inner())
                    .0;
            }
            st.inflight
        };
        if wait_idle(deadline) == 0 {
            return DispatcherStopReport::default();
        }
        let aborted = {
            let mut st = shared.state.lock().unwrap_or_else(|e| e.into_inner());
            st.aborting = true;
            for abort in st.running.values() {
                abort.trigger();
            }
            st.inflight
        };
        wait_idle(Instant::now() + ABORT_GRACE);
        let mut unreaped: Vec<String> = shared
            .state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .busy
            .iter()
            .cloned()
            .collect();
        unreaped.sort();
        DispatcherStopReport { aborted, unreaped }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DispatcherStopReport {
    pub aborted: usize,
    /// 100 ms 寬限後 run 仍未返回的 rule_id，依 rule_id 排序。
    pub unreaped: Vec<String>,
}

impl DispatcherStopReport {
    /// SC-11 細化的 WARNING 全文（`event dispatcher shutdown 逾時，終止 %d 個 handler`）；無 abort 時 `None`。
    pub fn warning(&self) -> Option<String> {
        (self.aborted > 0).then(|| {
            format!(
                "event dispatcher shutdown 逾時，終止 {} 個 handler",
                self.aborted
            )
        })
    }
}
