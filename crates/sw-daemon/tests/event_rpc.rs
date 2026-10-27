//! P7 Task 16（08-slice23-login-events.md）：10 個 `event.*` RPC（`sw_core/service.py` `_dispatch` 的 event 分支）。
//!
//! 斷言值依 Python 基線 `service.py:1393-1432` 與 `engine.py`（CPython 3.12 實跑對照見 `sw-core/tests/event_engine.rs`）。
//! engine 以 `Service::attach_engine` 掛上（Task 16-wire 由 daemon 組裝點建立）；bridge 為 stub。
#![cfg(feature = "test-fakes")]
mod support;

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use support::ServiceRig;
use sw_core::event_engine::{BridgeQueries, EngineDeps, EventEngine};
use sw_pal::fakes::{FakeClock, FakeEnv, FakeHandlerSpawner};
use sw_pal::traits::{Clock, HandlerSpawner, SysEnv};
use sw_proto::pyjson::{dumps, PyDict, PyValue, RPC_WIRE};
use sw_proto::rpc_types::Method;

#[derive(Default)]
struct StubBridge(Mutex<Vec<String>>);

impl BridgeQueries for StubBridge {
    fn active_cmd_id_for(&self, _com: &str) -> Option<String> {
        None
    }
    fn profile_for(&self, _com: &str) -> Option<String> {
        None
    }
    fn known_coms(&self) -> Vec<String> {
        self.0.lock().unwrap().clone()
    }
}

struct Rig {
    rig: ServiceRig,
    dir: tempfile::TempDir,
    engine: Arc<EventEngine>,
}

fn j(d: &PyDict) -> String {
    dumps(&PyValue::Dict(d.clone()), &RPC_WIRE)
}

fn rig() -> Rig {
    let rig = ServiceRig::ready_shell();
    let dir = tempfile::tempdir().unwrap();
    let clock = FakeClock::new();
    clock.set_wall_us(1_700_000_000_000_000);
    let clock: Arc<dyn Clock> = clock;
    let env: Arc<dyn SysEnv> = Arc::new(FakeEnv::default());
    let handlers: Arc<dyn HandlerSpawner> = FakeHandlerSpawner::scripted(vec![]);
    let platform = sw_pal::fakes::platform()
        .fs(sw_pal::native().fs)
        .clock(clock)
        .env(env)
        .handlers(handlers)
        .build();
    let engine = Arc::new(
        EventEngine::new(EngineDeps {
            events_dir: dir.path().join("events.d"),
            runtime_dir: dir.path().join("rt"),
            log_path: dir.path().join("events.ndjson"),
            bridge: Arc::new(StubBridge(Mutex::new(vec!["COM0".into()]))),
            queue_max: 1024,
            per_daemon_max: 8,
            platform: Arc::new(platform),
            warn: Arc::new(|_m| {}),
        })
        .unwrap(),
    );
    engine.start().unwrap();
    assert!(rig.svc.attach_engine(engine.clone()));
    Rig { rig, dir, engine }
}

impl Drop for Rig {
    fn drop(&mut self) {
        self.engine.stop(Instant::now() + Duration::from_secs(2));
    }
}

const RULE: &str = r#"{"schema_version":1,"owner":"o","name":"n","kind":"tool","selectors":["COM0"],"pattern":{"kind":"contains","value":"x"},"handler":{"exec":["true"]}}"#;

#[test]
fn rule_set_response_spreads_raw_with_ok_first() {
    let r = rig();
    let v = r.rig.version.clone();
    let got = r.rig.call(
        "event.rule_set",
        r#"{"schema_version":1,"owner":"o","name":"n","kind":"tool","selectors":["COM0"],"pattern":{"kind":"contains","value":"x"},"handler":{"exec":["true"]},"ok":"spoof"}"#,
    );
    assert_eq!(
        j(&got),
        format!(
            r#"{{"ok":"spoof","schema_version":1,"owner":"o","name":"n","kind":"tool","selectors":["COM0"],"pattern":{{"kind":"contains","value":"x"}},"handler":{{"exec":["true"]}},"version":"{v}"}}"#
        ),
        "RPC-H-14 KEEP：raw 的 ok 覆寫值但位置第一"
    );
}

#[test]
fn rule_set_invalid_schema_and_type_error() {
    let r = rig();
    let v = r.rig.version.clone();
    let got = r.rig.call(
        "event.rule_set",
        &RULE.replace(r#""name":"n""#, r#""name":"N!""#),
    );
    assert_eq!(
        j(&got),
        format!(
            r#"{{"ok":false,"error_code":"INVALID_RULE_SCHEMA","error":"name must match [a-z0-9-]{{1,64}}","version":"{v}"}}"#
        )
    );
    let err = r.rig.call_err(
        "event.rule_set",
        &RULE.replace(r#""kind":"tool""#, r#""kind":"tool","cooldown_ms":null"#),
    );
    assert_eq!(
        err.message,
        "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'",
        "TypeError → framing EXCEPTION（無 version）"
    );
}

#[test]
fn rule_delete_get_list_and_not_found() {
    let r = rig();
    let v = r.rig.version.clone();
    r.rig.call("event.rule_set", RULE);
    assert_eq!(
        j(&r.rig.call("event.rule_delete", r#"{"rule_id":"../x"}"#)),
        format!(r#"{{"ok":true,"deleted":false,"version":"{v}"}}"#)
    );
    assert_eq!(
        j(&r.rig.call("event.rule_list", r#"{"selector":"COM1"}"#)),
        format!(r#"{{"ok":true,"rules":[],"version":"{v}"}}"#)
    );
    assert_eq!(
        j(&r.rig
            .call("event.rule_list", r#"{"selector":"COM0","owner":null}"#)),
        format!(
            r#"{{"ok":true,"rules":[{{"rule_id":"o.n","owner":"o","kind":"tool","selectors":["COM0"],"fires":0,"exhausted":false,"last_fire_ts":null}}],"version":"{v}"}}"#
        )
    );
    assert_eq!(
        j(&r.rig.call("event.rule_get", r#"{"rule_id":"o.q"}"#)),
        format!(r#"{{"ok":false,"error_code":"RULE_NOT_FOUND","version":"{v}"}}"#)
    );
    let got = r.rig.call("event.rule_get", r#"{"rule_id":"o.n"}"#);
    assert_eq!(
        j(&got),
        format!(
            r#"{{"ok":true,"rule":{{"schema_version":1,"owner":"o","name":"n","kind":"tool","selectors":["COM0"],"pattern":{{"kind":"contains","value":"x"}},"handler":{{"exec":["true"]}}}},"counter":{{"fires":0,"last_fire_ts":null,"exhausted":false}},"version":"{v}"}}"#
        )
    );
    assert_eq!(
        j(&r.rig.call("event.rule_delete", r#"{"rule_id":"o.n"}"#)),
        format!(r#"{{"ok":true,"deleted":true,"version":"{v}"}}"#)
    );
}

#[test]
fn com_toggle_status_reset_reload_tail() {
    let r = rig();
    let v = r.rig.version.clone();
    assert_eq!(
        j(&r.rig.call("event.com_enable", "{}")),
        format!(r#"{{"ok":true,"selector":"","enabled":true,"active_rules":[],"version":"{v}"}}"#),
        "str(params.get('selector') or '')：空字串也進集合"
    );
    assert_eq!(
        j(&r.rig.call("event.com_status", "{}")),
        format!(r#"{{"ok":true,"coms":[""],"version":"{v}"}}"#)
    );
    assert_eq!(
        r.rig
            .call_err("event.com_status", r#"{"selector":["x"]}"#)
            .message,
        "unhashable type: 'list'"
    );
    r.rig.call("event.rule_set", RULE);
    assert_eq!(
        j(&r.rig.call("event.com_disable", r#"{"selector":"COM0"}"#)),
        format!(
            r#"{{"ok":true,"selector":"COM0","enabled":false,"active_rules":["o.n"],"version":"{v}"}}"#
        )
    );
    assert_eq!(
        j(&r.rig
            .call("event.reset", r#"{"rule_id":"o.zz","selector":"COM0"}"#)),
        format!(r#"{{"ok":true,"cleared":1,"version":"{v}"}}"#)
    );
    std::fs::write(
        r.dir.path().join("events.d/o.a.json"),
        RULE.replace(r#""name":"n""#, r#""name":"a""#),
    )
    .unwrap();
    std::fs::write(
        r.dir.path().join("events.d/o.b.json"),
        RULE.replace(r#""name":"n""#, r#""name":"b""#),
    )
    .unwrap();
    assert_eq!(
        j(&r.rig.call("event.reload", "{}")),
        format!(r#"{{"ok":true,"added":["o.a","o.b"],"removed":[],"changed":[],"version":"{v}"}}"#)
    );
    // o.n 的事件：rule_loaded、counter_reset（com_disable）→ `out[-(-1):]` 跳過第 1 筆、留 1 筆。
    let tail = r.rig.call("event.tail", r#"{"n":-1,"rule_id":"o.n"}"#);
    let entries = tail.get("entries").unwrap().as_array().unwrap();
    assert_eq!(entries.len(), 1, "{entries:?}");
    assert_eq!(
        entries[0].get("type"),
        Some(&PyValue::from("counter_reset"))
    );
    assert_eq!(
        r.rig.call_err("event.tail", r#"{"n":"3"}"#).message,
        "bad operand type for unary -: 'str'"
    );
    assert_eq!(
        r.rig.call_err("event.tail", r#"{"since_ts":"x"}"#).message,
        "'<' not supported between instances of 'int' and 'str'"
    );
}

#[test]
fn event_methods_served_not_method_not_found_and_not_retryable() {
    let r = rig();
    for m in [
        "event.rule_set",
        "event.rule_delete",
        "event.rule_list",
        "event.rule_get",
        "event.com_enable",
        "event.com_disable",
        "event.com_status",
        "event.reset",
        "event.reload",
        "event.tail",
    ] {
        let got = r.rig.call_raw(m, "{}");
        if let Ok(d) = &got {
            assert_ne!(
                d.get("error_code"),
                Some(&PyValue::from("METHOD_NOT_FOUND")),
                "{m}"
            );
        }
        let method = Method::parse(m);
        assert!(!matches!(method, Method::Unknown(_)), "{m} 在方法目錄");
        assert!(!method.is_retryable_readonly(), "{m} 不可自動重試");
    }
    for m in ["file.push", "file.pull"] {
        assert_eq!(
            r.rig.call(m, "{}").get("error_code"),
            Some(&PyValue::from("METHOD_NOT_FOUND"))
        );
    }
}

#[test]
fn event_methods_without_engine_are_method_not_found() {
    // engine 未掛上（Task 16-wire 之前的組裝點）：維持 Slice 2.1 的 METHOD_NOT_FOUND、無副作用。
    let rig = ServiceRig::ready_shell();
    assert_eq!(
        rig.call("event.tail", "{}").get("error_code"),
        Some(&PyValue::from("METHOD_NOT_FOUND"))
    );
}

/// `read` 先睡 `delay` 的 FsOps（其餘委派原生）：模擬大 events.ndjson 的慢讀。
struct SlowFs {
    inner: Arc<dyn sw_pal::traits::FsOps>,
    delay: Duration,
}

impl sw_pal::traits::FsRead for SlowFs {
    fn read(&self, p: &std::path::Path) -> sw_pal::traits::PalResult<Vec<u8>> {
        std::thread::sleep(self.delay);
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

impl sw_pal::traits::FsOps for SlowFs {
    fn mkdirs(&self, p: &std::path::Path) -> sw_pal::traits::PalResult<()> {
        self.inner.mkdirs(p)
    }
    fn open_append(
        &self,
        p: &std::path::Path,
    ) -> sw_pal::traits::PalResult<Box<dyn sw_pal::traits::AppendFile>> {
        self.inner.open_append(p)
    }
    fn file_size(&self, p: &std::path::Path) -> sw_pal::traits::PalResult<Option<u64>> {
        self.inner.file_size(p)
    }
}

#[test]
fn event_rpc_does_not_block_other_sessions() {
    // event RPC 在連線 thread 上執行、不經 session actor：慢讀 events.ndjson 期間，另一 session 的
    // `session.get_state` 仍即時回覆（≤ 50 ms）。
    let rig = Arc::new(ServiceRig::ready_shell());
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(
        dir.path().join("events.ndjson"),
        b"{\"type\": \"x\", \"ts\": 1}\n",
    )
    .unwrap();
    let fs: Arc<dyn sw_pal::traits::FsOps> = Arc::new(SlowFs {
        inner: sw_pal::native().fs,
        delay: Duration::from_millis(500),
    });
    let clock: Arc<dyn Clock> = FakeClock::new();
    let env: Arc<dyn SysEnv> = Arc::new(FakeEnv::default());
    let handlers: Arc<dyn HandlerSpawner> = FakeHandlerSpawner::scripted(vec![]);
    let platform = sw_pal::fakes::platform()
        .fs(fs)
        .clock(clock)
        .env(env)
        .handlers(handlers)
        .build();
    let engine = Arc::new(
        EventEngine::new(EngineDeps {
            events_dir: dir.path().join("events.d"),
            runtime_dir: dir.path().join("rt"),
            log_path: dir.path().join("events.ndjson"),
            bridge: Arc::new(StubBridge::default()),
            queue_max: 1024,
            per_daemon_max: 8,
            platform: Arc::new(platform),
            warn: Arc::new(|_m| {}),
        })
        .unwrap(),
    );
    assert!(rig.svc.attach_engine(engine.clone()));
    let r2 = rig.clone();
    let tail = std::thread::spawn(move || r2.call("event.tail", "{}"));
    std::thread::sleep(Duration::from_millis(100));
    let t0 = Instant::now();
    let st = rig.call("session.get_state", r#"{"selector":"COM0"}"#);
    let took = t0.elapsed();
    assert_eq!(st.get("ok"), Some(&PyValue::Bool(true)));
    assert!(took <= Duration::from_millis(50), "{took:?}");
    let got = tail.join().unwrap();
    assert_eq!(got.get("entries").unwrap().as_array().unwrap().len(), 1);
    engine.stop(Instant::now() + Duration::from_secs(1));
}
