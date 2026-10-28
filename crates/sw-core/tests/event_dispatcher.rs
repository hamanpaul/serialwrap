//! P7 Task 15（08-slice23-login-events.md）：event handler dispatcher（`event_engine/dispatcher.py`，SC-11 細化、R1-F2）。
//!
//! 斷言以 Python 基線 golden `contracts/golden/p7/event_payload.ndjson` 與 `dispatcher.py:116-246` 為準。
//! 偏離計畫原文：`build_env` 的 `inherited` 為呼叫端依 `HandlerSpawner::inherited_env_keys()` 順序從 SysEnv 取得的鍵值
//! （Python `for key in _INHERITED_ENV_KEYS: if key in os.environ`）；sw-core 不得以 cfg 分辨平台鍵集合，故過濾在呼叫端。
//! shutdown WARNING 由 `DispatcherStopReport::warning()` 提供字串，Task 16 經 `EngineDeps.warn` 寫出。
#![cfg(feature = "test-fakes")]
mod common;

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use common::p7::{b64, golden_rows, rule_json};
use sw_core::event_engine::counter::{Counter, CounterStore};
use sw_core::event_engine::dispatcher::{
    build_env, build_payload, stdin_bytes, Dispatcher, DispatcherStopReport,
};
use sw_core::event_engine::matcher::{MatchResult, MatcherFire};
use sw_core::event_engine::schema::validate_rule_dict;
use sw_pal::fakes::{FakeClock, FakeEnv, FakeHandlerSpawner};
use sw_pal::traits::{Clock, HandlerOutcome, HandlerSpawner, PalError, PalErrorKind, SysEnv};
use sw_proto::pyjson::{self, PyDict, PyInt, PyValue};

const KEYS: [&str; 5] = ["PATH", "HOME", "USER", "LANG", "LC_ALL"];

fn dumps_default(v: &PyValue) -> String {
    pyjson::dumps(
        v,
        &pyjson::DumpOpts {
            sep: pyjson::Sep::Default,
            ensure_ascii: false,
            sort_keys: false,
            allow_nan: true,
        },
    )
}

fn dict(v: &PyValue) -> &PyDict {
    match v {
        PyValue::Dict(d) => d,
        o => panic!("not dict {o:?}"),
    }
}

fn py_int(v: &PyValue) -> PyInt {
    match v {
        PyValue::Int(i) => i.clone(),
        o => panic!("not int {o:?}"),
    }
}

fn fire_from_json(v: &PyValue) -> MatcherFire {
    let d = dict(v);
    let rule = validate_rule_dict(d.get("rule").unwrap()).unwrap();
    MatcherFire {
        rule: Arc::new(rule),
        selector: d.get("selector").unwrap().as_str().unwrap().into(),
        m: MatchResult {
            matched_text: d.get("matched_text").unwrap().as_str().unwrap().into(),
            groups: d
                .get("groups")
                .unwrap()
                .as_array()
                .unwrap()
                .iter()
                .map(|g| g.as_str().map(str::to_string))
                .collect(),
        },
        wal_seq: d.get("wal_seq").unwrap().as_i64().unwrap() as u64,
        matched_at: d.get("matched_at").unwrap().as_i64().unwrap(),
        active_cmd_id: d.get("active_cmd_id").unwrap().as_str().map(str::to_string),
    }
}

fn fire_with(fragment: &str, selector: &str, seq: u64, text: &str) -> MatcherFire {
    let rule = validate_rule_dict(&rule_json(fragment)).unwrap();
    MatcherFire {
        rule: Arc::new(rule),
        selector: selector.into(),
        m: MatchResult {
            matched_text: text.into(),
            groups: vec![],
        },
        wal_seq: seq,
        matched_at: 1_700_000_000_000,
        active_cmd_id: None,
    }
}

fn fire(rule: &str, selector: &str, seq: u64) -> MatcherFire {
    let (owner, name) = rule.split_once('.').unwrap();
    fire_with(
        &format!(r#"{{"owner":"{owner}","name":"{name}"}}"#),
        selector,
        seq,
        "x",
    )
}

#[derive(Clone, Default)]
struct RecordingEmit(Arc<Mutex<Vec<PyDict>>>);

impl RecordingEmit {
    fn as_fn(&self) -> Arc<dyn Fn(PyDict) + Send + Sync> {
        let me = self.clone();
        Arc::new(move |ev| me.0.lock().unwrap().push(ev))
    }
    fn all(&self) -> Vec<PyDict> {
        self.0.lock().unwrap().clone()
    }
    fn types(&self) -> Vec<String> {
        self.all()
            .iter()
            .map(|e| e.get("type").unwrap().as_str().unwrap().to_string())
            .collect()
    }
    fn of_type(&self, t: &str) -> Vec<PyDict> {
        self.all()
            .into_iter()
            .filter(|e| e.get("type").and_then(PyValue::as_str) == Some(t))
            .collect()
    }
    fn wait_for(&self, t: &str, n: usize) {
        let end = Instant::now() + Duration::from_secs(5);
        while self.of_type(t).len() < n {
            assert!(
                Instant::now() < end,
                "逾時等待 {n} 筆 {t}：{:?}",
                self.types()
            );
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}

struct Rig {
    _dir: tempfile::TempDir,
    counters: Arc<CounterStore>,
    clock: Arc<FakeClock>,
    env: Arc<FakeEnv>,
    emit: RecordingEmit,
}

fn rig() -> Rig {
    let dir = tempfile::tempdir().unwrap();
    let counters = Arc::new(CounterStore::new(dir.path().into(), sw_pal::native().fs).unwrap());
    let clock = FakeClock::new();
    clock.set_wall_us(1_000_000_000);
    let env = Arc::new(FakeEnv::default());
    env.vars
        .lock()
        .unwrap()
        .insert("PATH".into(), "/usr/bin:/bin".into());
    Rig {
        _dir: dir,
        counters,
        clock,
        env,
        emit: RecordingEmit::default(),
    }
}

impl Rig {
    fn start(&self, max: usize, sp: Arc<dyn HandlerSpawner>) -> Dispatcher {
        let clock: Arc<dyn Clock> = self.clock.clone();
        let env: Arc<dyn SysEnv> = self.env.clone();
        Dispatcher::start(
            max,
            sp,
            self.counters.clone(),
            self.emit.as_fn(),
            env,
            clock,
        )
    }
}

fn keys(d: &PyDict) -> Vec<&str> {
    d.0.keys().map(String::as_str).collect()
}

fn ok(stdout: &[u8]) -> HandlerOutcome {
    HandlerOutcome::Completed {
        exit_code: 0,
        stdout_tail: stdout.to_vec(),
        stderr_tail: vec![],
    }
}

fn soon() -> Instant {
    Instant::now() + Duration::from_secs(5)
}

#[test]
fn payload_and_env_bytes_match_python() {
    for row in golden_rows("event_payload") {
        let id = row.id().to_string();
        let fire = fire_from_json(row.get("fire"));
        let count = py_int(row.get("fire_count"));
        let payload = build_payload(&fire, &count, row.str("matched_line"));
        assert_eq!(
            stdin_bytes(&payload),
            b64(row.str("stdin_b64")),
            "case {id}"
        );
        // Python `for key in _INHERITED_ENV_KEYS: if key in os.environ`。
        let environ = row.pairs("inherited");
        let inherited: Vec<(String, String)> = KEYS
            .iter()
            .filter_map(|k| environ.iter().find(|(ek, _)| ek == k).cloned())
            .collect();
        assert_eq!(
            build_env(&fire, &count, &inherited),
            row.pairs("env"),
            "case {id}"
        );
    }
}

#[test]
fn per_rule_busy_and_per_daemon_saturated() {
    let r = rig();
    let sp = FakeHandlerSpawner::blocking();
    let d = r.start(2, sp.clone());
    d.dispatch(fire("o.a", "COM0", 1), "x".into());
    d.dispatch(fire("o.a", "COM0", 2), "x".into()); // 同 rule 忙
    d.dispatch(fire("o.b", "COM0", 3), "x".into());
    d.dispatch(fire("o.c", "COM1", 4), "x".into()); // inflight==2 → 飽和丟新
    assert_eq!(r.emit.types(), ["event_dropped", "event_dropped"]);
    let dropped = r.emit.all();
    assert_eq!(
        dumps_default(&PyValue::Dict(dropped[0].clone())),
        r#"{"type": "event_dropped", "reason": "per_rule_busy", "rule_id": "o.a", "selector": "COM0"}"#
    );
    assert_eq!(
        dumps_default(&PyValue::Dict(dropped[1].clone())),
        r#"{"type": "event_dropped", "reason": "per_daemon_saturated", "rule_id": "o.c", "selector": "COM1"}"#
    );
    sp.release_all(ok(b"ok"));
    r.emit.wait_for("fire_completed", 2);
    let rep = d.stop(soon());
    assert_eq!(rep, DispatcherStopReport::default());
    assert_eq!(r.emit.of_type("fire_completed").len(), 2);
}

#[test]
fn fire_completed_shape() {
    let r = rig();
    let sp = FakeHandlerSpawner::scripted(vec![HandlerOutcome::Completed {
        exit_code: 7,
        stdout_tail: b"out".to_vec(),
        stderr_tail: b"err".to_vec(),
    }]);
    let d = r.start(8, sp.clone());
    d.dispatch(fire("o.a", "COM0", 42), "line".into());
    r.emit.wait_for("fire_completed", 1);
    d.stop(soon());
    let ev = &r.emit.of_type("fire_completed")[0];
    assert_eq!(
        keys(ev),
        [
            "type",
            "rule_id",
            "selector",
            "exit_code",
            "duration_ms",
            "stdout_tail",
            "stderr_tail",
            "fire_count",
            "wal_seq"
        ]
    );
    assert_eq!(
        dumps_default(&PyValue::Dict(ev.clone())),
        r#"{"type": "fire_completed", "rule_id": "o.a", "selector": "COM0", "exit_code": 7, "duration_ms": 0, "stdout_tail": "out", "stderr_tail": "err", "fire_count": 1, "wal_seq": 42}"#,
        "非 0 exit 仍為 fire_completed"
    );
    // 規格：stdin＝payload、timeout＝rule.timeout_ms、tail＝4096。
    let spec = sp.calls.lock().unwrap()[0].clone();
    assert_eq!(spec.argv, ["/bin/true"]);
    assert_eq!(spec.timeout, Duration::from_secs(10));
    assert_eq!(spec.tail_bytes, 4096);
    assert!(spec.stdin.ends_with(b"\n"));
    assert_eq!(r.counters.load("o.a").unwrap().fires, PyInt::Small(1));
}

/// 審查 01c474c_a0cad06（3030adc-2）：`fire_completed.exit_code` 必須經 `sw_pal::process::py_returncode`
/// （Windows 為 `GetExitCodeProcess` 的不帶號 DWORD，`-1073741819` → `3221225477`；POSIX 原值，訊號終止為
/// `-signum`）。sw-core 不得以 cfg 分辨平台（OD），故以 helper 本身為期望值綁定 dispatcher 的呈現；
/// helper 的平台值由 sw-pal `handler_spawner::py_returncode_matches_python_popen` 鎖定（Windows 原生見 P7-LW-6）。
#[test]
fn fire_completed_exit_code_follows_py_returncode() {
    for (rule, raw) in [("o.a", -1_073_741_819_i32), ("o.b", -9)] {
        let r = rig();
        let sp = FakeHandlerSpawner::scripted(vec![HandlerOutcome::Completed {
            exit_code: raw,
            stdout_tail: vec![],
            stderr_tail: vec![],
        }]);
        let d = r.start(8, sp);
        d.dispatch(fire(rule, "COM0", 1), "line".into());
        r.emit.wait_for("fire_completed", 1);
        d.stop(soon());
        let ev = &r.emit.of_type("fire_completed")[0];
        assert_eq!(
            ev.get("exit_code"),
            Some(&PyValue::from(sw_pal::process::py_returncode(raw))),
            "raw={raw}"
        );
    }
}

#[test]
fn fire_timeout_shape() {
    let r = rig();
    let sp = FakeHandlerSpawner::scripted(vec![HandlerOutcome::TimedOut]);
    let d = r.start(8, sp);
    d.dispatch(fire("o.a", "COM0", 5), "line".into());
    r.emit.wait_for("fire_timeout", 1);
    d.stop(soon());
    let ev = &r.emit.of_type("fire_timeout")[0];
    assert_eq!(
        dumps_default(&PyValue::Dict(ev.clone())),
        r#"{"type": "fire_timeout", "rule_id": "o.a", "selector": "COM0", "duration_ms": 0, "fire_count": 1, "wal_seq": 5}"#
    );
    assert_eq!(r.counters.load("o.a").unwrap().fires, PyInt::Small(1));
}

#[test]
fn spawn_failure_reports_fire_failed() {
    let r = rig();
    let err = PalError::new(
        PalErrorKind::NotFound,
        Some(2),
        None,
        "No such file or directory",
        "[Errno 2] No such file or directory: 'nope'".to_string(),
    );
    let sp = FakeHandlerSpawner::scripted(vec![HandlerOutcome::SpawnFailed(err)]);
    let hook_clock = r.clock.clone();
    sp.set_hook(Arc::new(move |_| hook_clock.set_wall_us(1_000_500_000)));
    let d = r.start(8, sp);
    d.dispatch(fire("o.a", "COM0", 5), "line".into());
    r.emit.wait_for("fire_failed", 1);
    d.stop(soon());
    assert_eq!(
        dumps_default(&PyValue::Dict(r.emit.of_type("fire_failed")[0].clone())),
        r#"{"type": "fire_failed", "rule_id": "o.a", "selector": "COM0", "reason": "[Errno 2] No such file or directory: 'nope'"}"#
    );
    // `_post_fire_counter_update`：fires+1、last_fire_ts 為完成時刻。
    assert_eq!(
        r.counters.load("o.a").unwrap(),
        Counter {
            fires: PyInt::Small(1),
            last_fire_ts: Some(PyInt::Small(1_000_500)),
            exhausted: false
        }
    );
}

#[test]
fn spawn_value_error_emits_nothing_and_releases_rule() {
    // Python：Popen 的 ValueError（嵌入 NUL）不在 `except OSError` 內 → 穿出 worker、被 executor 吞掉：不 emit、不更新 counter。
    let r = rig();
    let sp = FakeHandlerSpawner::scripted(vec![
        HandlerOutcome::SpawnFailed(PalError::plain(
            PalErrorKind::InvalidInput,
            "embedded null byte",
        )),
        ok(b""),
    ]);
    let d = r.start(8, sp.clone());
    d.dispatch(fire("o.a", "COM0", 1), "x".into());
    let end = Instant::now() + Duration::from_secs(5);
    while sp.spawn_count() < 1 {
        assert!(Instant::now() < end);
        std::thread::sleep(Duration::from_millis(5));
    }
    std::thread::sleep(Duration::from_millis(50));
    assert!(r.emit.all().is_empty(), "{:?}", r.emit.types());
    assert_eq!(r.counters.load("o.a").unwrap().fires, PyInt::Small(0));
    d.dispatch(fire("o.a", "COM0", 2), "x".into());
    r.emit.wait_for("fire_completed", 1);
    d.stop(soon());
}

#[test]
fn counter_load_error_skips_fire_and_releases_rule() {
    // Python `_run_handler`：`self._counters.load` 的例外穿出 try、finally 釋放 busy，不 emit。
    let r = rig();
    std::fs::write(r.counters.path_for("o.a"), b"[]").unwrap();
    let sp = FakeHandlerSpawner::scripted(vec![]);
    let d = r.start(8, sp.clone());
    d.dispatch(fire("o.a", "COM0", 1), "x".into());
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(sp.spawn_count(), 0);
    assert!(r.emit.all().is_empty());
    std::fs::remove_file(r.counters.path_for("o.a")).unwrap();
    d.dispatch(fire("o.a", "COM0", 2), "x".into());
    r.emit.wait_for("fire_completed", 1);
    d.stop(soon());
}

#[test]
fn last_fire_ts_is_end_time() {
    let r = rig();
    let sp = FakeHandlerSpawner::scripted(vec![ok(b"")]);
    let hook_clock = r.clock.clone();
    sp.set_hook(Arc::new(move |_| hook_clock.set_wall_us(1_002_000_000)));
    let d = r.start(8, sp);
    d.dispatch(fire("o.a", "COM0", 1), "x".into());
    r.emit.wait_for("fire_completed", 1);
    d.stop(soon());
    let ev = &r.emit.of_type("fire_completed")[0];
    assert_eq!(ev.get("duration_ms"), Some(&PyValue::from(2000)));
    assert_eq!(
        r.counters.load("o.a").unwrap().last_fire_ts,
        Some(PyInt::Small(1_002_000)),
        "EVT-D-E4 KEEP：last_fire_ts 為完成時刻"
    );
}

#[test]
fn fire_count_continues_from_counter() {
    let r = rig();
    r.counters
        .save(
            "o.a",
            &Counter {
                fires: PyInt::Small(4),
                ..Default::default()
            },
        )
        .unwrap();
    let sp = FakeHandlerSpawner::scripted(vec![ok(b"")]);
    let d = r.start(8, sp.clone());
    d.dispatch(fire("o.a", "COM0", 1), "x".into());
    r.emit.wait_for("fire_completed", 1);
    d.stop(soon());
    assert_eq!(
        r.emit.of_type("fire_completed")[0].get("fire_count"),
        Some(&PyValue::from(5))
    );
    let spec = sp.calls.lock().unwrap()[0].clone();
    assert!(spec
        .env
        .contains(&("SERIALWRAP_EVENT_FIRE_COUNT".into(), "5".into())));
}

#[test]
fn matched_text_env_truncation_chars() {
    let text: String = "中".repeat(4097);
    let f = fire_with("{}", "COM0", 1, &text);
    let env = build_env(&f, &PyInt::Small(1), &[]);
    let got = &env
        .iter()
        .find(|(k, _)| k == "SERIALWRAP_EVENT_MATCHED_TEXT")
        .unwrap()
        .1;
    assert_eq!(*got, format!("{} ...truncated", "中".repeat(4096)));
    let exact: String = "中".repeat(4096);
    let env = build_env(&fire_with("{}", "COM0", 1, &exact), &PyInt::Small(1), &[]);
    assert!(env.contains(&("SERIALWRAP_EVENT_MATCHED_TEXT".into(), exact)));
}

#[test]
fn stdout_tail_decoded_replace() {
    // CPython 3.12：b"\xe4\xb8\xadab\xe4"[-4:].decode("utf-8", "replace") == "�ab�"。
    let r = rig();
    let sp = FakeHandlerSpawner::scripted(vec![ok(b"\xb8\xadab\xe4")]);
    let d = r.start(8, sp);
    d.dispatch(fire("o.a", "COM0", 1), "x".into());
    r.emit.wait_for("fire_completed", 1);
    d.stop(soon());
    assert_eq!(
        r.emit.of_type("fire_completed")[0].get("stdout_tail"),
        Some(&PyValue::from("\u{fffd}\u{fffd}ab\u{fffd}"))
    );
}

#[test]
fn shell_handler_uses_platform_argv() {
    let r = rig();
    let sp = FakeHandlerSpawner::scripted(vec![ok(b"")]);
    let d = r.start(8, sp.clone());
    d.dispatch(
        fire_with(r#"{"handler":{"shell":"echo hi | cat"}}"#, "COM0", 1, "x"),
        "x".into(),
    );
    r.emit.wait_for("fire_completed", 1);
    d.stop(soon());
    assert_eq!(
        sp.calls.lock().unwrap()[0].argv,
        ["fake-sh", "-c", "echo hi | cat"]
    );
}

#[test]
fn inherited_env_only_whitelisted_present() {
    let r = rig();
    r.env
        .vars
        .lock()
        .unwrap()
        .insert("SECRET".into(), "s".into());
    let sp = FakeHandlerSpawner::scripted(vec![ok(b"")]);
    let d = r.start(8, sp.clone());
    d.dispatch(fire("o.a", "COM0", 1), "x".into());
    r.emit.wait_for("fire_completed", 1);
    d.stop(soon());
    let env = sp.calls.lock().unwrap()[0].env.clone();
    let names: Vec<&str> = env.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(names[0], "PATH");
    assert_eq!(names.len(), 12, "{names:?}");
    assert!(names[1..]
        .iter()
        .all(|k| k.starts_with("SERIALWRAP_EVENT_")));
    assert!(!names.contains(&"SECRET"));
}

#[test]
fn stop_deadline_aborts_running_handlers() {
    let r = rig();
    let sp = FakeHandlerSpawner::blocking();
    let d = r.start(8, sp.clone());
    d.dispatch(fire("o.a", "COM0", 1), "x".into());
    while sp.spawn_count() < 1 {
        std::thread::sleep(Duration::from_millis(2));
    }
    let t0 = Instant::now();
    let rep = d.stop(Instant::now() + Duration::from_millis(200));
    assert!(
        t0.elapsed() <= Duration::from_millis(300),
        "{:?}",
        t0.elapsed()
    );
    assert_eq!(
        rep,
        DispatcherStopReport {
            aborted: 1,
            unreaped: vec![]
        }
    );
    assert_eq!(
        sp.aborts_seen(),
        [true],
        "該 handler 的 HandlerAbort 已 trigger"
    );
    assert_eq!(
        rep.warning().as_deref(),
        Some("event dispatcher shutdown 逾時，終止 1 個 handler")
    );
    assert_eq!(DispatcherStopReport::default().warning(), None);
    // abort 視同逾時：fire_timeout。
    r.emit.wait_for("fire_timeout", 1);
}

#[test]
fn close_intake_rejects_new_dispatch() {
    let r = rig();
    let sp = FakeHandlerSpawner::blocking();
    let d = r.start(8, sp.clone());
    d.dispatch(fire("o.a", "COM0", 1), "x".into());
    while sp.spawn_count() < 1 {
        std::thread::sleep(Duration::from_millis(2));
    }
    d.close_intake();
    d.close_intake(); // 冪等
    d.dispatch(fire("o.a", "COM0", 2), "x".into());
    d.dispatch(fire("o.b", "COM0", 3), "x".into());
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(sp.spawn_count(), 1);
    assert!(
        r.emit.all().is_empty(),
        "關閉後直接丟棄（不 emit event_dropped）"
    );
    sp.release_all(ok(b""));
    r.emit.wait_for("fire_completed", 1);
    assert_eq!(sp.aborts_seen(), [false], "原 handler 不被 abort");
    assert_eq!(d.stop(soon()), DispatcherStopReport::default());
    assert_eq!(r.counters.load("o.a").unwrap().fires, PyInt::Small(1));
    assert_eq!(r.counters.load("o.b").unwrap().fires, PyInt::Small(0));
}

#[test]
fn stop_reports_unreaped_when_run_ignores_abort() {
    let r = rig();
    let sp = FakeHandlerSpawner::ignore_abort();
    let d = r.start(8, sp.clone());
    d.dispatch(fire("o.r1", "COM0", 1), "x".into());
    while sp.spawn_count() < 1 {
        std::thread::sleep(Duration::from_millis(2));
    }
    let t0 = Instant::now();
    let rep = d.stop(Instant::now() + Duration::from_millis(200));
    // ignore_abort：deadline 200 ms＋abort 寬限 100 ms 本身即 300 ms，再加 100 ms 排程餘裕（審查 96b28dd_06c61d0 91109e7-3）。
    assert!(
        t0.elapsed() <= Duration::from_millis(300) + Duration::from_millis(100),
        "{:?}",
        t0.elapsed()
    );
    assert_eq!(
        rep,
        DispatcherStopReport {
            aborted: 1,
            unreaped: vec!["o.r1".into()]
        }
    );
    sp.release_all(ok(b""));
}

#[test]
fn timeout_kills_process_group() {
    // 真 spawner：rule timeout 300 ms、shell 起孫行程長睡 → fire_timeout（PAL 終止整個 group／Job，Task 14 驗孫行程）。
    let r = rig();
    let native = sw_pal::native().handlers;
    let cmd = if native.shell_argv("x")[0] == "cmd.exe" {
        "ping -n 30 127.0.0.1 >nul"
    } else {
        "sleep 30 & wait"
    };
    let real_clock = sw_pal::native().clock;
    let env: Arc<dyn SysEnv> = r.env.clone();
    let d = Dispatcher::start(
        8,
        native,
        r.counters.clone(),
        r.emit.as_fn(),
        env,
        real_clock,
    );
    let f = fire_with(
        &format!(
            r#"{{"handler":{{"shell":{}}},"timeout_ms":300}}"#,
            dumps_default(&PyValue::from(cmd))
        ),
        "COM0",
        1,
        "x",
    );
    let t0 = Instant::now();
    d.dispatch(f, "x".into());
    r.emit.wait_for("fire_timeout", 1);
    assert!(t0.elapsed() < Duration::from_secs(5));
    let ev = &r.emit.of_type("fire_timeout")[0];
    let ms = ev.get("duration_ms").unwrap().as_i64().unwrap();
    assert!((300..5000).contains(&ms), "{ms}");
    d.stop(soon());
}

/// `read` 在 gate 開啟前阻塞的 FsOps（其餘委派原生）：讓 worker 停在 `counters.load`（登記 `running` 之前）。
struct GatedFs {
    inner: Arc<dyn sw_pal::traits::FsOps>,
    open: Mutex<bool>,
    cv: std::sync::Condvar,
    entered: std::sync::atomic::AtomicBool,
}

impl sw_pal::traits::FsRead for GatedFs {
    fn read(&self, p: &std::path::Path) -> sw_pal::traits::PalResult<Vec<u8>> {
        self.entered
            .store(true, std::sync::atomic::Ordering::SeqCst);
        let mut g = self.open.lock().unwrap();
        while !*g {
            g = self.cv.wait(g).unwrap();
        }
        self.inner.read(p)
    }
    fn exists(&self, p: &std::path::Path) -> bool {
        self.inner.exists(p)
    }
    fn is_dir(&self, p: &std::path::Path) -> bool {
        self.inner.is_dir(p)
    }
    fn is_file(&self, p: &std::path::Path) -> bool {
        self.inner.is_file(p)
    }
    fn list_dir(&self, p: &std::path::Path) -> sw_pal::traits::PalResult<Vec<String>> {
        self.inner.list_dir(p)
    }
}

impl sw_pal::traits::FsOps for GatedFs {
    fn mkdirs(&self, p: &std::path::Path) -> sw_pal::traits::PalResult<()> {
        self.inner.mkdirs(p)
    }
    fn replace(&self, a: &std::path::Path, b: &std::path::Path) -> sw_pal::traits::PalResult<()> {
        self.inner.replace(a, b)
    }
    fn sync_file(&self, p: &std::path::Path) -> sw_pal::traits::PalResult<()> {
        self.inner.sync_file(p)
    }
    fn create_truncate(&self, p: &std::path::Path, b: &[u8]) -> sw_pal::traits::PalResult<()> {
        self.inner.create_truncate(p, b)
    }
}

#[test]
fn stop_aborts_handler_registered_after_deadline() {
    // 審查 96b28dd_06c61d0 91109e7-1：worker 已計入 inflight、尚未登記 running 時 stop 逾時——之後登記的 handler
    // 必須立即 abort（不得 spawn 一個不受控的 handler）。
    let r = rig();
    let gated = Arc::new(GatedFs {
        inner: sw_pal::native().fs,
        open: Mutex::new(false),
        cv: std::sync::Condvar::new(),
        entered: std::sync::atomic::AtomicBool::new(false),
    });
    let dir = tempfile::tempdir().unwrap();
    let counters = Arc::new(CounterStore::new(dir.path().into(), gated.clone()).unwrap());
    let sp = FakeHandlerSpawner::blocking();
    let clock: Arc<dyn Clock> = r.clock.clone();
    let env: Arc<dyn SysEnv> = r.env.clone();
    let d = Dispatcher::start(8, sp.clone(), counters, r.emit.as_fn(), env, clock);
    d.dispatch(fire("o.a", "COM0", 1), "x".into());
    while !gated.entered.load(std::sync::atomic::Ordering::SeqCst) {
        std::thread::sleep(Duration::from_millis(2));
    }
    let rep = d.stop(Instant::now() + Duration::from_millis(50));
    assert_eq!(rep.unreaped, ["o.a"], "仍卡在 counters.load");
    *gated.open.lock().unwrap() = true;
    gated.cv.notify_all();
    r.emit.wait_for("fire_timeout", 1);
    assert_eq!(
        sp.aborts_seen(),
        [true],
        "登記時即已 abort：run 立即回 TimedOut"
    );
}
