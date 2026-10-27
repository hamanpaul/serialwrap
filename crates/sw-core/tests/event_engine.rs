//! P7 Task 16（08-slice23-login-events.md）：`EventEngine` 組裝（`event_engine/engine.py`）。
//!
//! 期望值以 Python 基線 golden `contracts/golden/p7/event_engine_order.ndjson` 與 CPython 3.12 實跑
//! （`EventEngine(EngineDeps(...))`＋假 bridge，逐步驟記錄回傳值與 events.ndjson）為準。
//! 偏離計畫原文（介面）：Python 這些方法的例外（counter／registry／log I/O、`com in set` 的 unhashable TypeError）
//! 穿出為 RPC EXCEPTION，故 `rule_delete`／`rule_list`／`rule_get`／`com_enable`／`com_disable`／`com_status`／
//! `reset` 回 `Result<_, PyErr>`；`reload` 回 `Result<PyDict, PyErr>`（`added`／`removed`／`changed` 升冪）。
#![cfg(feature = "test-fakes")]
mod common;

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use common::p7::{golden_rows, rule_json};
use sw_core::event_engine::{BridgeQueries, EngineDeps, EventEngine};
use sw_pal::fakes::{FakeClock, FakeEnv, FakeHandlerSpawner};
use sw_pal::traits::{Clock, HandlerOutcome, HandlerSpawner, SysEnv};
use sw_proto::pyjson::{self, PyDict, PyValue};

#[derive(Default)]
struct StubBridge {
    coms: Mutex<Vec<String>>,
    active: Mutex<BTreeMap<String, String>>,
}

impl BridgeQueries for StubBridge {
    fn active_cmd_id_for(&self, com: &str) -> Option<String> {
        self.active.lock().unwrap().get(com).cloned()
    }
    fn profile_for(&self, _com: &str) -> Option<String> {
        None
    }
    fn known_coms(&self) -> Vec<String> {
        self.coms.lock().unwrap().clone()
    }
}

struct H {
    dir: tempfile::TempDir,
    engine: EventEngine,
    sp: Arc<FakeHandlerSpawner>,
    bridge: Arc<StubBridge>,
    warns: Arc<Mutex<Vec<String>>>,
}

fn loads(s: &str) -> PyValue {
    pyjson::loads(s).unwrap()
}

fn dumps_default(v: &PyValue) -> String {
    pyjson::dumps(
        v,
        &pyjson::DumpOpts {
            sep: pyjson::Sep::Default,
            ensure_ascii: true,
            sort_keys: false,
            allow_nan: true,
        },
    )
}

fn harness_with(
    known: &[&str],
    files: &[(&str, String)],
    per_daemon_max: usize,
    sp: Arc<FakeHandlerSpawner>,
) -> H {
    let dir = tempfile::tempdir().unwrap();
    let events_dir = dir.path().join("events.d");
    std::fs::create_dir_all(&events_dir).unwrap();
    for (name, body) in files {
        std::fs::write(events_dir.join(name), body).unwrap();
    }
    let bridge = Arc::new(StubBridge::default());
    *bridge.coms.lock().unwrap() = known.iter().map(|s| s.to_string()).collect();
    let clock = FakeClock::new();
    clock.set_wall_us(1_700_000_000_000_000);
    let env = Arc::new(FakeEnv::default());
    env.vars
        .lock()
        .unwrap()
        .insert("PATH".into(), "/usr/bin:/bin".into());
    let clock: Arc<dyn Clock> = clock;
    let env: Arc<dyn SysEnv> = env;
    let handlers: Arc<dyn HandlerSpawner> = sp.clone();
    let platform = sw_pal::fakes::platform()
        .fs(sw_pal::native().fs)
        .clock(clock)
        .env(env)
        .handlers(handlers)
        .build();
    let warns = Arc::new(Mutex::new(Vec::new()));
    let w = warns.clone();
    let engine = EventEngine::new(EngineDeps {
        events_dir,
        runtime_dir: dir.path().join("rt"),
        log_path: dir.path().join("events.ndjson"),
        bridge: bridge.clone(),
        queue_max: 1024,
        per_daemon_max,
        platform: Arc::new(platform),
        warn: Arc::new(move |m| w.lock().unwrap().push(m)),
    })
    .unwrap();
    H {
        dir,
        engine,
        sp,
        bridge,
        warns,
    }
}

fn harness(known: &[&str]) -> H {
    let h = harness_with(known, &[], 8, FakeHandlerSpawner::scripted(vec![]));
    h.engine.start().unwrap();
    h
}

impl H {
    fn log(&self) -> Vec<PyDict> {
        let p = self.dir.path().join("events.ndjson");
        let Ok(text) = std::fs::read_to_string(p) else {
            return Vec::new();
        };
        text.lines()
            .map(|l| match loads(l) {
                PyValue::Dict(mut d) => {
                    d.0.shift_remove("ts");
                    d
                }
                o => panic!("{o:?}"),
            })
            .collect()
    }
    fn types(&self) -> Vec<String> {
        self.log()
            .iter()
            .map(|e| e.get("type").unwrap().as_str().unwrap().to_string())
            .collect()
    }
    fn log_from(&self, n: usize) -> String {
        dumps_default(&PyValue::List(
            self.log().into_iter().skip(n).map(PyValue::Dict).collect(),
        ))
    }
    fn wait_type(&self, t: &str, n: usize) {
        let end = Instant::now() + Duration::from_secs(5);
        while self.types().iter().filter(|x| *x == t).count() < n {
            assert!(Instant::now() < end, "逾時等待 {t}：{:?}", self.types());
            std::thread::sleep(Duration::from_millis(5));
        }
    }
    fn events_dir(&self) -> PathBuf {
        self.dir.path().join("events.d")
    }
}

fn rule(fragment: &str) -> PyValue {
    rule_json(fragment)
}

fn rule_text(fragment: &str) -> String {
    dumps_default(&rule(fragment))
}

fn d(v: PyDict) -> String {
    dumps_default(&PyValue::Dict(v))
}

#[test]
fn event_order_matches_python_golden_for_start_rule_set_reload() {
    for row in golden_rows("event_engine_order") {
        let id = row.id().to_string();
        let setup = match row.get("setup") {
            PyValue::Dict(d) => d.clone(),
            _ => unreachable!(),
        };
        let known: Vec<String> = setup
            .get("known_coms")
            .and_then(PyValue::as_array)
            .map(|a| a.iter().map(|v| v.as_str().unwrap().to_string()).collect())
            .unwrap_or_default();
        let known_ref: Vec<&str> = known.iter().map(String::as_str).collect();
        let files: Vec<(String, String)> = match setup.get("files") {
            Some(PyValue::Dict(f)) => {
                f.0.iter()
                    .map(|(k, v)| (k.clone(), dumps_default(v)))
                    .collect()
            }
            _ => vec![],
        };
        let files_ref: Vec<(&str, String)> =
            files.iter().map(|(k, v)| (k.as_str(), v.clone())).collect();
        let h = harness_with(
            &known_ref,
            &files_ref,
            8,
            FakeHandlerSpawner::scripted(vec![]),
        );
        h.engine.start().unwrap();
        for op in setup
            .get("ops")
            .and_then(PyValue::as_array)
            .cloned()
            .unwrap_or_default()
        {
            let PyValue::Dict(op) = op else {
                unreachable!()
            };
            match op.get("op").unwrap().as_str().unwrap() {
                "rule_set" => {
                    h.engine.rule_set(op.get("rule").unwrap()).unwrap();
                }
                "reload" => {
                    std::fs::write(
                        h.events_dir()
                            .join(op.get("file").unwrap().as_str().unwrap()),
                        dumps_default(op.get("rule").unwrap()),
                    )
                    .unwrap();
                    h.engine.reload().unwrap();
                }
                o => panic!("{o}"),
            }
        }
        h.engine.stop(Instant::now() + Duration::from_secs(5));
        assert_eq!(h.types(), row.str_vec("types"), "case {id}");
    }
}

#[test]
fn end_to_end_fire_uses_exact_wal_seq_and_writes_ndjson() {
    let h = harness(&["COM0"]);
    h.engine
        .rule_set(&rule(
            r#"{"selectors":["COM0"],"pattern":{"kind":"contains","value":"Kernel panic"}}"#,
        ))
        .unwrap();
    h.engine.feed_rx("COM0", b"noise\nKernel pa", 41);
    h.engine.feed_rx("COM0", b"nic - not syncing\n", 42);
    h.wait_type("fire_completed", 1);
    assert_eq!(
        h.types(),
        [
            "com_enabled",
            "rule_loaded",
            "match_recorded",
            "fire_completed"
        ]
    );
    let mr = &h.log()[2];
    assert_eq!(
        d(mr.clone()),
        r#"{"type": "match_recorded", "rule_id": "o.n", "selector": "COM0", "wal_seq": 42, "matched_text": "Kernel panic"}"#,
        "DF-15：取完成該行之 chunk 的精確 seq"
    );
    let spec = h.sp.calls.lock().unwrap()[0].clone();
    assert!(spec
        .env
        .contains(&("SERIALWRAP_EVENT_WAL_SEQ".into(), "42".into())));
    h.engine.stop(Instant::now() + Duration::from_secs(5));
}

#[test]
fn line_buffer_persists_while_com_disabled() {
    // EVT-D-S5：per-COM LineBuffer 永不重設；啟用與否在「行完成時」判定（Python `feed_line` 檢查 enabled）。
    let h = harness(&["COM0"]);
    h.engine
        .rule_set(&rule(
            r#"{"auto_enable_com_on_load":false,"pattern":{"kind":"contains","value":"x"}}"#,
        ))
        .unwrap();
    h.engine.feed_rx("COM0", b"a x a\npart", 1);
    assert!(
        !h.types().contains(&"match_recorded".to_string()),
        "未啟用不送 matcher"
    );
    h.engine.com_enable("COM0").unwrap();
    h.engine.feed_rx("COM0", b"ial x\n", 2);
    h.wait_type("match_recorded", 1);
    let mr = h
        .log()
        .into_iter()
        .find(|e| e.get("type") == Some(&PyValue::from("match_recorded")))
        .unwrap();
    assert_eq!(mr.get("wal_seq"), Some(&PyValue::from(2)));
    h.engine.stop(Instant::now() + Duration::from_secs(5));
}

#[test]
fn exhausted_set_before_dispatch_keep_e1() {
    let h = harness_with(&["COM0"], &[], 0, FakeHandlerSpawner::scripted(vec![]));
    h.engine.start().unwrap();
    h.engine
        .rule_set(&rule(
            r#"{"max_fires":1,"selectors":["COM0"],"pattern":{"kind":"contains","value":"x"}}"#,
        ))
        .unwrap();
    h.engine.feed_rx("COM0", b"x\n", 1);
    h.wait_type("event_dropped", 1);
    let got = h.engine.rule_get("o.n").unwrap().unwrap();
    assert_eq!(
        dumps_default(got.get("counter").unwrap()),
        r#"{"fires": 0, "last_fire_ts": null, "exhausted": true}"#,
        "EVT-D-E1 KEEP：emit_fire 在 dispatch 之前設 exhausted；dispatcher 飽和丟棄不計 fires"
    );
    h.engine.stop(Instant::now() + Duration::from_secs(5));
}

#[test]
fn com_disable_clears_counter_then_emits_in_order() {
    let h = harness(&["COM0"]);
    h.engine.rule_set(&rule(r#"{"name":"a"}"#)).unwrap();
    h.engine
        .rule_set(&rule(r#"{"name":"z","selectors":["ALL"]}"#))
        .unwrap();
    h.engine
        .rule_set(&rule(
            r#"{"name":"c","selectors":["COM1"],"auto_enable_com_on_load":false}"#,
        ))
        .unwrap();
    let n0 = h.log().len();
    assert_eq!(
        d(h.engine.com_disable("COM9").unwrap()),
        r#"{"selector": "COM9", "enabled": false, "active_rules": ["o.z"]}"#
    );
    assert_eq!(
        h.log_from(n0),
        r#"[{"type": "counter_reset", "rule_id": "o.z", "previous_fires": 0, "triggered_by": "com_disable"}, {"type": "com_disabled", "selector": "COM9", "triggered_by": "manual"}]"#
    );
    let n0 = h.log().len();
    assert_eq!(
        d(h.engine.com_disable("COM0").unwrap()),
        r#"{"selector": "COM0", "enabled": false, "active_rules": ["o.a", "o.z"]}"#
    );
    assert_eq!(
        h.log_from(n0),
        r#"[{"type": "counter_reset", "rule_id": "o.a", "previous_fires": 0, "triggered_by": "com_disable"}, {"type": "counter_reset", "rule_id": "o.z", "previous_fires": 0, "triggered_by": "com_disable"}, {"type": "com_disabled", "selector": "COM0", "triggered_by": "manual"}]"#
    );
    // rule_list（CPython 3.12 實測）
    let list = |sel: Option<&PyValue>, owner: Option<&PyValue>| {
        dumps_default(&PyValue::List(
            h.engine
                .rule_list(sel, owner)
                .unwrap()
                .into_iter()
                .map(PyValue::Dict)
                .collect(),
        ))
    };
    assert_eq!(
        list(Some(&PyValue::from("COM1")), None),
        r#"[{"rule_id": "o.z", "owner": "o", "kind": "tool", "selectors": ["ALL"], "fires": 0, "exhausted": false, "last_fire_ts": null}, {"rule_id": "o.c", "owner": "o", "kind": "tool", "selectors": ["COM1"], "fires": 0, "exhausted": false, "last_fire_ts": null}]"#
    );
    assert_eq!(list(None, Some(&PyValue::from("x"))), "[]");
    assert_eq!(
        list(Some(&PyValue::from(5)), None),
        r#"[{"rule_id": "o.z", "owner": "o", "kind": "tool", "selectors": ["ALL"], "fires": 0, "exhausted": false, "last_fire_ts": null}]"#
    );
    // com_status
    assert_eq!(
        d(h.engine.com_enable("").unwrap()),
        r#"{"selector": "", "enabled": true, "active_rules": ["o.z"]}"#
    );
    assert_eq!(d(h.engine.com_status(None).unwrap()), r#"{"coms": [""]}"#);
    assert_eq!(
        h.engine
            .com_status(Some(&loads(r#"["x"]"#)))
            .unwrap_err()
            .message,
        "unhashable type: 'list'"
    );
    assert_eq!(
        d(h.engine.com_status(Some(&PyValue::from(5))).unwrap()),
        r#"{"selector": 5, "enabled": false, "active_rules": ["o.z"]}"#
    );
    // reset
    let n0 = h.log().len();
    let s = |x: &str| PyValue::from(x);
    assert_eq!(
        h.engine
            .reset(Some(&s("o.nonexist")), Some(&s("COM0")))
            .unwrap(),
        1,
        "rule_id 優先、不檢查存在"
    );
    assert_eq!(h.engine.reset(Some(&s("bad/id")), None).unwrap(), 0);
    assert_eq!(h.engine.reset(Some(&PyValue::from(5)), None).unwrap(), 0);
    assert_eq!(h.engine.reset(None, Some(&s("COM1"))).unwrap(), 2);
    assert_eq!(h.engine.reset(None, None).unwrap(), 0);
    assert_eq!(
        h.log_from(n0),
        r#"[{"type": "counter_reset", "rule_id": "o.nonexist", "previous_fires": 0, "triggered_by": "reset"}, {"type": "counter_reset", "rule_id": "o.z", "previous_fires": 0, "triggered_by": "reset"}, {"type": "counter_reset", "rule_id": "o.c", "previous_fires": 0, "triggered_by": "reset"}]"#
    );
    // rule_delete
    let n0 = h.log().len();
    assert!(!h.engine.rule_delete("../x").unwrap());
    assert!(h.engine.rule_delete("o.a").unwrap());
    assert!(!h.engine.rule_delete("o.a").unwrap());
    assert_eq!(
        h.log_from(n0),
        r#"[{"type": "rule_unloaded", "rule_id": "o.a"}]"#
    );
    // reload：移除 z、新增 y、改 c、壞檔
    let ev = h.events_dir();
    std::fs::remove_file(ev.join("o.z.json")).unwrap();
    std::fs::write(
        ev.join("o.y.json"),
        rule_text(r#"{"name":"y","selectors":["COM5"]}"#),
    )
    .unwrap();
    std::fs::write(
        ev.join("o.c.json"),
        rule_text(
            r#"{"name":"c","selectors":["COM1"],"cooldown_ms":5,"auto_enable_com_on_load":false}"#,
        ),
    )
    .unwrap();
    std::fs::write(ev.join("bad.json"), "{").unwrap();
    let n0 = h.log().len();
    assert_eq!(
        d(h.engine.reload().unwrap()),
        r#"{"added": ["o.y"], "removed": ["o.z"], "changed": ["o.c"]}"#
    );
    let bad = ev.join("bad.json").to_string_lossy().into_owned();
    assert_eq!(
        h.log_from(n0),
        format!(
            r#"[{{"type": "rule_load_failed", "path": "{bad}", "reason": "read/parse failed: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"}}, {{"type": "com_enabled", "selector": "COM5", "triggered_by": "reload"}}, {{"type": "rule_loaded", "rule_id": "o.y"}}, {{"type": "rule_unloaded", "rule_id": "o.z"}}, {{"type": "rule_loaded", "rule_id": "o.c"}}]"#
        )
    );
    assert_eq!(
        d(h.engine.com_status(None).unwrap()),
        r#"{"coms": ["", "COM5"]}"#,
        "reload 不 disable COM（EVT-D-E10）"
    );
    // rule_get
    let got = h.engine.rule_get("o.c").unwrap().unwrap();
    assert_eq!(
        dumps_default(got.get("counter").unwrap()),
        r#"{"fires": 0, "last_fire_ts": null, "exhausted": false}"#
    );
    assert_eq!(
        got.get("rule").unwrap().get("cooldown_ms"),
        Some(&PyValue::from(5))
    );
    assert!(h.engine.rule_get("o.q").unwrap().is_none());
    h.engine.stop(Instant::now() + Duration::from_secs(5));
}

#[test]
fn start_failed_load_written_and_all_selector_expands_on_start_rule_set_reload() {
    let h = harness_with(
        &["COM0"],
        &[
            ("bad.json", "[".into()),
            ("o.n.json", rule_text(r#"{"selectors":["ALL"]}"#)),
        ],
        8,
        FakeHandlerSpawner::scripted(vec![]),
    );
    h.engine.start().unwrap();
    let bad = h
        .events_dir()
        .join("bad.json")
        .to_string_lossy()
        .into_owned();
    assert_eq!(
        h.log_from(0),
        format!(
            r#"[{{"type": "rule_load_failed", "path": "{bad}", "reason": "read/parse failed: Expecting value: line 1 column 2 (char 1)"}}, {{"type": "rule_loaded", "rule_id": "o.n"}}, {{"type": "com_enabled", "selector": "COM0", "triggered_by": "auto"}}]"#
        ),
        "DF-09：壞檔記 rule_load_failed、engine 正常啟動；start 時 rule_loaded 先於 com_enabled"
    );
    h.bridge.coms.lock().unwrap().push("COM1".into());
    let n0 = h.log().len();
    h.engine
        .rule_set(&rule(r#"{"selectors":["ALL"]}"#))
        .unwrap();
    assert_eq!(
        h.log_from(n0),
        r#"[{"type": "com_enabled", "selector": "COM1", "triggered_by": "rule_set"}, {"type": "rule_loaded", "rule_id": "o.n"}]"#
    );
    h.bridge.coms.lock().unwrap().push("COM2".into());
    std::fs::write(
        h.events_dir().join("o.m.json"),
        rule_text(r#"{"name":"m","selectors":["ALL"]}"#),
    )
    .unwrap();
    let n0 = h.log().len();
    assert_eq!(
        d(h.engine.reload().unwrap()),
        r#"{"added": ["o.m"], "removed": [], "changed": []}"#
    );
    assert_eq!(
        h.log_from(n0),
        format!(
            r#"[{{"type": "rule_load_failed", "path": "{bad}", "reason": "read/parse failed: Expecting value: line 1 column 2 (char 1)"}}, {{"type": "com_enabled", "selector": "COM2", "triggered_by": "reload"}}, {{"type": "rule_loaded", "rule_id": "o.m"}}]"#
        )
    );
    h.engine.stop(Instant::now() + Duration::from_secs(5));
}

#[test]
fn close_intake_stops_feed_and_stop_reports() {
    let h = harness_with(&["COM0"], &[], 8, FakeHandlerSpawner::blocking());
    h.engine.start().unwrap();
    h.engine.rule_set(&rule("{}")).unwrap();
    h.engine.feed_rx("COM0", b"x\n", 1);
    let end = Instant::now() + Duration::from_secs(5);
    while h.sp.spawn_count() < 1 {
        assert!(Instant::now() < end);
        std::thread::sleep(Duration::from_millis(2));
    }
    h.engine.close_intake();
    h.engine.close_intake();
    let n0 = h.log().len();
    h.engine.feed_rx("COM0", b"x\n", 2);
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(h.log().len(), n0, "close_intake 後 feed_rx 為 no-op");
    let rep = h.engine.stop(Instant::now() + Duration::from_millis(100));
    assert_eq!(rep.aborted, 1);
    assert_eq!(
        h.warns.lock().unwrap().as_slice(),
        ["event dispatcher shutdown 逾時，終止 1 個 handler"]
    );
    assert_eq!(
        h.engine.stop(Instant::now()).aborted,
        0,
        "再次 stop 為 no-op"
    );
    let _: &Path = h.dir.path();
    let _ = HandlerOutcome::TimedOut;
}
