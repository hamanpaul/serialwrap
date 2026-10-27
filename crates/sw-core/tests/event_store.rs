//! P7 Task 13（08-slice23-login-events.md）：counter／registry／events.ndjson 儲存（`event_engine/{counter,registry,event_log}.py`）。
//!
//! 斷言值以 Python 基線 golden `contracts/golden/p7/{counter_file,registry_load,event_log_tail}.ndjson`（CPython 3.12）
//! 與本機 CPython 3.12 實測為準。
//!
//! 偏離計畫原文（介面）：Python 這幾個方法會把例外穿出（counter 壞檔的 AttributeError／TypeError／UnicodeDecodeError、
//! `os.listdir` 非 FileNotFoundError、`delete`／`clear` 的非 FileNotFoundError、`write` 的 OSError），golden
//! `counter_file:007／012／013` 即為例證，故 `Counter::from_json`／`CounterStore::load` 回 `Result<_, PyErr>`、
//! `clear`／`RuleRegistry::delete`／`EventLogger::write` 回 `PalResult`、`load_all`／`diff_against` 回 `Result<_, PyErr>`。
#![cfg(feature = "test-fakes")]
mod common;

use std::path::Path;
use std::sync::Arc;

use common::p7::{b64, golden_rows};
use sw_core::event_engine::counter::{valid_rule_id, Counter, CounterStore};
use sw_core::event_engine::event_log::EventLogger;
use sw_core::event_engine::registry::{RuleRegistry, UpsertError};
use sw_pal::fakes::FakeClock;
use sw_pal::traits::{Clock, FsOps};
use sw_proto::pyjson::{self, PyDict, PyInt, PyValue};

fn native_fs() -> Arc<dyn FsOps> {
    sw_pal::native().fs
}

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
        other => panic!("not dict: {other:?}"),
    }
}

fn loads(s: &str) -> PyValue {
    pyjson::loads(s).unwrap()
}

fn rule_obj(owner: &str, name: &str) -> PyValue {
    loads(&format!(
        r#"{{"schema_version": 1, "owner": "{owner}", "name": "{name}", "kind": "tool", "selectors": ["COM0"],
            "pattern": {{"kind": "contains", "value": "x"}}, "handler": {{"exec": ["/bin/true"]}}}}"#
    ))
}

fn basename(p: &str) -> String {
    Path::new(p)
        .file_name()
        .unwrap()
        .to_string_lossy()
        .into_owned()
}

fn logger(dir: &Path, wall_ms: i64) -> (EventLogger, Arc<FakeClock>) {
    let clock = FakeClock::new();
    clock.set_wall_us(wall_ms * 1000);
    let c: Arc<dyn Clock> = clock.clone();
    let log = EventLogger::new(dir.join("events.ndjson"), native_fs(), c).unwrap();
    (log, clock)
}

// ── counter ────────────────────────────────────────────────────────────────

#[test]
fn counter_file_bytes_match_python() {
    let d = tempfile::tempdir().unwrap();
    let store = CounterStore::new(d.path().into(), native_fs()).unwrap();
    store
        .save(
            "o.n",
            &Counter {
                fires: PyInt::Small(3),
                last_fire_ts: Some(PyInt::Small(1_700_000_000_123)),
                exhausted: false,
            },
        )
        .unwrap();
    assert_eq!(
        std::fs::read_to_string(d.path().join("o.n.counter.json")).unwrap(),
        r#"{"fires": 3, "last_fire_ts": 1700000000123, "exhausted": false}"#
    );
    // 第二次覆寫（DF-18：Windows `os.rename` 目標已存在會失敗；一律 replace）。
    store
        .save(
            "o.n",
            &Counter {
                fires: PyInt::Small(4),
                ..Default::default()
            },
        )
        .unwrap();
    assert_eq!(store.load("o.n").unwrap().fires, PyInt::Small(4));
    assert!(!d.path().join("o.n.counter.json.tmp").exists());
}

#[test]
fn counter_file_matches_python_golden() {
    for row in golden_rows("counter_file") {
        let id = row.id().to_string();
        let d = tempfile::tempdir().unwrap();
        let store = CounterStore::new(d.path().into(), native_fs()).unwrap();
        let want = dict(row.get("counter"));
        match row.str("kind") {
            "save" => {
                let c = Counter::from_json(row.get("counter")).unwrap();
                store.save("o.n", &c).unwrap();
                let got = std::fs::read(d.path().join("o.n.counter.json")).unwrap();
                assert_eq!(got, b64(row.str("file_b64")), "case {id}");
            }
            "load" => {
                if let Some(b) = row.opt_str("file_b64") {
                    std::fs::write(d.path().join("o.n.counter.json"), b64(b)).unwrap();
                }
                let got = store.load("o.n");
                if want.get("raised").is_some() {
                    assert!(
                        got.is_err(),
                        "case {id}：Python 例外穿出（{:?}）",
                        want.get("raised")
                    );
                } else {
                    let got = got.unwrap_or_else(|e| panic!("case {id}: {e:?}"));
                    assert_eq!(
                        PyValue::Dict(got.to_json()),
                        PyValue::Dict(want.clone()),
                        "case {id}"
                    );
                }
            }
            k => panic!("{k}"),
        }
    }
}

#[test]
fn counter_load_raise_messages_match_python() {
    // CPython 3.12 實測 `str(exc)`（RPC framing EXCEPTION 的 message）。
    let d = tempfile::tempdir().unwrap();
    let store = CounterStore::new(d.path().into(), native_fs()).unwrap();
    let cases: &[(&[u8], &str)] = &[
        (b"[]", "'list' object has no attribute 'get'"),
        (b"\"x\"", "'str' object has no attribute 'get'"),
        (
            b"{\"fires\": null}",
            "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'",
        ),
        (
            b"\xff",
            "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
        ),
        (
            b"{\"fires\":\"abc\"}",
            "invalid literal for int() with base 10: 'abc'",
        ),
        (
            b"{\"fires\":1,\"last_fire_ts\":[1]}",
            "int() argument must be a string, a bytes-like object or a real number, not 'list'",
        ),
    ];
    for (content, msg) in cases {
        std::fs::write(d.path().join("o.n.counter.json"), content).unwrap();
        let err = store.load("o.n").expect_err("例外穿出");
        assert_eq!(err.message, *msg, "{content:?}");
    }
}

#[test]
fn counter_load_bad_file_is_default() {
    let d = tempfile::tempdir().unwrap();
    let store = CounterStore::new(d.path().into(), native_fs()).unwrap();
    assert_eq!(store.load("o.n").unwrap(), Counter::default(), "不存在");
    std::fs::write(d.path().join("o.n.counter.json"), b"{bad").unwrap();
    assert_eq!(store.load("o.n").unwrap(), Counter::default(), "壞 JSON");
}

#[test]
fn stale_tmp_is_overwritten() {
    let d = tempfile::tempdir().unwrap();
    std::fs::write(
        d.path().join("o.n.counter.json.tmp"),
        br#"{"fires": 99, "garbage"#,
    )
    .unwrap();
    let store = CounterStore::new(d.path().into(), native_fs()).unwrap();
    store
        .save(
            "o.n",
            &Counter {
                fires: PyInt::Small(1),
                ..Default::default()
            },
        )
        .unwrap();
    assert_eq!(
        std::fs::read_to_string(d.path().join("o.n.counter.json")).unwrap(),
        r#"{"fires": 1, "last_fire_ts": null, "exhausted": false}"#
    );
    let fresh = CounterStore::new(d.path().into(), native_fs()).unwrap();
    assert_eq!(fresh.load("o.n").unwrap().fires, PyInt::Small(1));
}

#[test]
fn counter_clear_and_known_rule_ids() {
    let d = tempfile::tempdir().unwrap();
    let store = CounterStore::new(d.path().into(), native_fs()).unwrap();
    store.save("o.a", &Counter::default()).unwrap();
    store
        .save(
            "o.b",
            &Counter {
                fires: PyInt::Small(2),
                ..Default::default()
            },
        )
        .unwrap();
    std::fs::write(d.path().join("notes.txt"), b"x").unwrap();
    let mut ids = store.known_rule_ids().unwrap();
    ids.sort();
    assert_eq!(ids, vec!["o.a".to_string(), "o.b".to_string()]);
    store.clear("o.b").unwrap();
    store.clear("o.b").unwrap(); // FileNotFoundError → pass
    assert_eq!(
        store.load("o.b").unwrap(),
        Counter::default(),
        "clear 後歸零（快取同步失效）"
    );
    assert!(!d.path().join("o.b.counter.json").exists());
}

#[test]
fn reset_with_invalid_rule_id_counts_zero_df01() {
    assert!(valid_rule_id("o.n"));
    assert!(valid_rule_id("a-1.b-2"));
    assert!(!valid_rule_id("a/b"));
    assert!(!valid_rule_id("o.n\n"), "`\\Z` 不接受行尾換行");
    assert!(!valid_rule_id("../victim"));
    assert!(!valid_rule_id("O.n"));
    assert!(!valid_rule_id(&format!("{}.n", "a".repeat(65))));
    assert!(valid_rule_id(&format!("{}.n", "a".repeat(64))));
}

// ── registry ───────────────────────────────────────────────────────────────

#[test]
fn rule_id_traversal_is_refused_df01() {
    let d = tempfile::tempdir().unwrap();
    let outside = d.path().join("victim.json");
    std::fs::write(&outside, "{}").unwrap();
    let reg = RuleRegistry::new(d.path().join("events.d"), native_fs()).unwrap();
    assert!(!reg.delete("../victim").unwrap());
    assert!(outside.exists());
}

#[test]
fn registry_load_matches_python_golden_reasons() {
    for row in golden_rows("registry_load") {
        let id = row.id().to_string();
        let d = tempfile::tempdir().unwrap();
        for (name, content) in dict(row.get("files")).0.iter() {
            std::fs::write(d.path().join(name), b64(content.as_str().unwrap())).unwrap();
        }
        let res = RuleRegistry::new(d.path().into(), native_fs())
            .unwrap()
            .load_all()
            .unwrap();
        assert_eq!(
            res.rules
                .iter()
                .map(|r| r.rule_id.clone())
                .collect::<Vec<_>>(),
            row.str_vec("rules"),
            "case {id}"
        );
        let failed: Vec<(String, String)> = res
            .failed
            .iter()
            .map(|f| (basename(&f.path), f.reason.clone()))
            .collect();
        assert_eq!(failed, row.pairs("failed"), "case {id}");
        for f in &res.failed {
            assert_eq!(
                Path::new(&f.path).parent().unwrap(),
                d.path(),
                "case {id}：path 為 root 下完整路徑"
            );
        }
    }
}

#[test]
fn registry_load_directory_entry_reason_has_filename() {
    let d = tempfile::tempdir().unwrap();
    std::fs::create_dir(d.path().join("x.json")).unwrap();
    let res = RuleRegistry::new(d.path().into(), native_fs())
        .unwrap()
        .load_all()
        .unwrap();
    let full = d.path().join("x.json").to_string_lossy().into_owned();
    assert_eq!(res.failed.len(), 1);
    assert_eq!(res.failed[0].path, full);
    // POSIX（CPython 3.12 實測）：`read/parse failed: [Errno 21] Is a directory: '<full>'`；Windows 為
    // `[Errno 13] Permission denied`（LOCAL-WINDOWS-TODO P7-LW-4 原生核對），兩者共同點為前綴與 `: repr(path)` 尾。
    let reason = &res.failed[0].reason;
    assert!(reason.starts_with("read/parse failed: [Errno "), "{reason}");
    assert!(
        reason.ends_with(&format!(": {}", sw_proto::pyfmt::py_repr_str(&full))),
        "{reason}"
    );
}

#[test]
fn registry_missing_root_is_empty() {
    let d = tempfile::tempdir().unwrap();
    let root = d.path().join("events.d");
    let reg = RuleRegistry::new(root.clone(), native_fs()).unwrap();
    std::fs::remove_dir(&root).unwrap();
    let res = reg.load_all().unwrap();
    assert!(res.rules.is_empty() && res.failed.is_empty());
}

#[test]
fn upsert_rule_file_indent2_sorted_no_newline() {
    let d = tempfile::tempdir().unwrap();
    let reg = RuleRegistry::new(d.path().into(), native_fs()).unwrap();
    let mut obj = rule_obj("o", "n");
    if let PyValue::Dict(m) = &mut obj {
        m.insert("debug", PyValue::from("中文"));
    }
    let rule = reg.upsert(&obj).unwrap();
    assert_eq!(rule.rule_id, "o.n");
    // CPython 3.12：json.dump(dict(rule.raw), f, ensure_ascii=False, indent=2, sort_keys=True)（文字模式）
    let expected = "{\n  \"debug\": \"中文\",\n  \"handler\": {\n    \"exec\": [\n      \"/bin/true\"\n    ]\n  },\n  \"kind\": \"tool\",\n  \"name\": \"n\",\n  \"owner\": \"o\",\n  \"pattern\": {\n    \"kind\": \"contains\",\n    \"value\": \"x\"\n  },\n  \"schema_version\": 1,\n  \"selectors\": [\n    \"COM0\"\n  ]\n}";
    // Python 文字模式寫入：Windows 每個 `\n` 寫成 `\r\n`（審查 76de0cf_96b28dd c58e8d4-2）。
    assert_eq!(
        std::fs::read_to_string(d.path().join("o.n.json")).unwrap(),
        expected.replace('\n', sw_pal::fs_ops::TEXT_NEWLINE)
    );
    assert!(!d.path().join("o.n.json.tmp").exists());
}

#[test]
fn upsert_overwrites_existing_df18() {
    let d = tempfile::tempdir().unwrap();
    let reg = RuleRegistry::new(d.path().into(), native_fs()).unwrap();
    reg.upsert(&rule_obj("o", "n")).unwrap();
    reg.upsert(&rule_obj("o", "n")).unwrap();
    assert_eq!(reg.load_all().unwrap().rules.len(), 1);
    assert!(reg.delete("o.n").unwrap());
    assert!(!reg.delete("o.n").unwrap(), "不存在 → false");
}

#[test]
fn upsert_schema_error_is_rule_error() {
    let d = tempfile::tempdir().unwrap();
    let reg = RuleRegistry::new(d.path().into(), native_fs()).unwrap();
    let err = reg.upsert(&loads("[]")).unwrap_err();
    assert!(matches!(err, UpsertError::Rule(_)), "{err:?}");
    assert!(
        std::fs::read_dir(d.path()).unwrap().next().is_none(),
        "驗證失敗不寫檔"
    );
}

#[test]
fn load_all_valueerror_typeerror_are_failed_df09() {
    let d = tempfile::tempdir().unwrap();
    let base = r#""schema_version": 1, "owner": "o", "name": "n", "kind": "tool", "selectors": ["COM0"], "pattern": {"kind": "contains", "value": "x"}, "handler": {"exec": ["/bin/true"]}"#;
    std::fs::write(
        d.path().join("f.json"),
        format!(r#"{{{base}, "cooldown_ms": "abc"}}"#),
    )
    .unwrap();
    std::fs::write(
        d.path().join("g.json"),
        format!(r#"{{{base}, "cooldown_ms": null}}"#),
    )
    .unwrap();
    let res = RuleRegistry::new(d.path().into(), native_fs())
        .unwrap()
        .load_all()
        .unwrap();
    assert!(res.rules.is_empty());
    assert_eq!(res.failed.len(), 2);
}

#[test]
fn diff_against_reports_sorted_ids() {
    let d = tempfile::tempdir().unwrap();
    let reg = RuleRegistry::new(d.path().into(), native_fs()).unwrap();
    for n in ["b", "c", "z"] {
        reg.upsert(&rule_obj("o", n)).unwrap();
    }
    let prev = reg.load_all().unwrap().rules;
    reg.delete("o.b").unwrap();
    reg.delete("o.z").unwrap();
    for n in ["y", "a"] {
        reg.upsert(&rule_obj("o", n)).unwrap();
    }
    let mut changed = rule_obj("o", "c");
    if let PyValue::Dict(m) = &mut changed {
        m.insert("cooldown_ms", PyValue::from(5));
    }
    reg.upsert(&changed).unwrap();
    let diff = reg.diff_against(&prev).unwrap();
    assert_eq!(diff.added, vec!["o.a".to_string(), "o.y".to_string()]);
    assert_eq!(diff.removed, vec!["o.b".to_string(), "o.z".to_string()]);
    assert_eq!(diff.changed, vec!["o.c".to_string()]);
}

// ── event_log ──────────────────────────────────────────────────────────────

#[test]
fn event_line_format_default_separators_ts_last() {
    let d = tempfile::tempdir().unwrap();
    let (log, _clock) = logger(d.path(), 1_700_000_000_123);
    let PyValue::Dict(ev) = loads(r#"{"type":"rule_loaded","rule_id":"o.n"}"#) else {
        unreachable!()
    };
    log.write(ev).unwrap();
    let PyValue::Dict(ev) = loads(r#"{"ts":7,"type":"中文"}"#) else {
        unreachable!()
    };
    log.write(ev).unwrap();
    assert_eq!(
        std::fs::read_to_string(d.path().join("events.ndjson")).unwrap(),
        "{\"type\": \"rule_loaded\", \"rule_id\": \"o.n\", \"ts\": 1700000000123}\n{\"ts\": 7, \"type\": \"中文\"}\n"
    );
}

#[test]
fn tail_n_semantics_match_python_slicing() {
    for row in golden_rows("event_log_tail") {
        let id = row.id().to_string();
        let d = tempfile::tempdir().unwrap();
        let ts0 = row.get("ts0").as_i64().unwrap();
        let (log, clock) = logger(d.path(), ts0);
        // Python golden 以 `iter(range(ts0, …))` mock `time.time`：每次補 ts 取下一毫秒。
        let mut next_ms = ts0;
        for ev in row.get("events").as_array().unwrap() {
            // golden 以 sort_keys 落檔；capture 腳本（`gen_event_log_tail`）的原始鍵序為 type、rule_id、selector、ts。
            let src = dict(ev);
            let mut ev = PyDict::new();
            for k in ["type", "rule_id", "selector", "ts"] {
                if let Some(v) = src.get(k) {
                    ev.insert(k, v.clone());
                }
            }
            assert_eq!(ev.0.len(), src.0.len(), "case {id}：未預期的鍵");
            if ev.get("ts").is_none() {
                clock.set_wall_us(next_ms * 1000);
                next_ms += 1;
            }
            log.write(ev).unwrap();
        }
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(d.path().join("events.ndjson"))
            .unwrap();
        std::io::Write::write_all(&mut f, b"{not json}\n").unwrap();
        drop(f);
        let opt = |k: &str| row.0.get(k).filter(|v| !v.is_null());
        let got = log.tail(opt("rule_id"), opt("selector"), opt("since_ts"), opt("n"));
        match got {
            Ok(v) => assert_eq!(
                dumps_default(&PyValue::List(v)),
                row.str("entries"),
                "case {id}"
            ),
            Err(e) => assert_eq!(row.str("entries"), "EXCEPTION", "case {id}: {e:?}"),
        }
    }
}

#[test]
fn tail_non_dict_lines_and_type_errors_match_python() {
    // CPython 3.12 實測（`\r\n` 行經 universal newlines 成空行、解析失敗略過）。
    let d = tempfile::tempdir().unwrap();
    let (log, _c) = logger(d.path(), 1);
    let PyValue::Dict(ev) = loads(r#"{"type":"x","ts":5,"rule_id":"o.a"}"#) else {
        unreachable!()
    };
    log.write(ev).unwrap();
    let mut f = std::fs::OpenOptions::new()
        .append(true)
        .open(d.path().join("events.ndjson"))
        .unwrap();
    std::io::Write::write_all(
        &mut f,
        b"[1]\n\"s\"\n{\"ts\":\"a\",\"rule_id\":\"o.a\"}\n\r\n",
    )
    .unwrap();
    drop(f);
    let all = log.tail(None, None, None, None).unwrap();
    assert_eq!(
        dumps_default(&PyValue::List(all)),
        r#"[{"type": "x", "ts": 5, "rule_id": "o.a"}, [1], "s", {"ts": "a", "rule_id": "o.a"}]"#
    );
    let err = |r: Result<Vec<PyValue>, sw_proto::pycompat::PyErr>| r.unwrap_err().message;
    let s = |x: &str| PyValue::from(x);
    assert_eq!(
        err(log.tail(Some(&s("o.a")), None, None, None)),
        "'list' object has no attribute 'get'"
    );
    assert_eq!(
        err(log.tail(None, None, Some(&s("z")), None)),
        "'<' not supported between instances of 'int' and 'str'"
    );
    assert_eq!(
        err(log.tail(None, None, None, Some(&PyValue::Float(1.5)))),
        "slice indices must be integers or None or have an __index__ method"
    );
    assert_eq!(
        err(log.tail(None, None, None, Some(&s("a")))),
        "bad operand type for unary -: 'str'"
    );
    assert_eq!(
        err(log.tail(None, None, None, Some(&loads("[1]")))),
        "bad operand type for unary -: 'list'"
    );
    let big = loads("1180591620717411303424");
    assert_eq!(log.tail(None, None, None, Some(&big)).unwrap().len(), 4);
    let neg = loads("-1180591620717411303424");
    assert!(log.tail(None, None, None, Some(&neg)).unwrap().is_empty());
}

#[test]
fn tail_filters_use_python_equality() {
    // Python `obj.get("rule_id") != rule_id`：1 == 1.0 == True。
    let d = tempfile::tempdir().unwrap();
    let (log, _c) = logger(d.path(), 1);
    for line in [
        r#"{"rule_id":1,"ts":1}"#,
        r#"{"rule_id":1.0,"ts":2}"#,
        r#"{"rule_id":"1","ts":3}"#,
    ] {
        let PyValue::Dict(ev) = loads(line) else {
            unreachable!()
        };
        log.write(ev).unwrap();
    }
    let got = log
        .tail(Some(&PyValue::Bool(true)), None, None, None)
        .unwrap();
    assert_eq!(got.len(), 2);
    let got = log
        .tail(None, None, Some(&PyValue::Float(1.5)), None)
        .unwrap();
    assert_eq!(got.len(), 2, "ts 2、3 ≥ 1.5");
}

#[test]
fn tail_missing_file_is_empty_and_reads_current_file_only() {
    let d = tempfile::tempdir().unwrap();
    let (log, _c) = logger(d.path(), 1);
    assert!(log.tail(None, None, None, None).unwrap().is_empty());
    std::fs::write(d.path().join("events.ndjson.1"), b"{\"old\": 1}\n").unwrap();
    let PyValue::Dict(ev) = loads(r#"{"new":1}"#) else {
        unreachable!()
    };
    log.write(ev).unwrap();
    let got = log.tail(None, None, None, None).unwrap();
    assert_eq!(
        dumps_default(&PyValue::List(got)),
        r#"[{"new": 1, "ts": 1}]"#
    );
}

#[test]
fn rotate_when_size_plus_incoming_exceeds() {
    const ROTATE: usize = 10 * 1024 * 1024;
    let d = tempfile::tempdir().unwrap();
    let path = d.path().join("events.ndjson");
    let (log, _c) = logger(d.path(), 1);
    let line = "{\"a\": 1, \"ts\": 1}\n";
    for i in 1..=3 {
        std::fs::write(
            d.path().join(format!("events.ndjson.{i}")),
            format!("old{i}"),
        )
        .unwrap();
    }
    std::fs::write(&path, vec![b'x'; ROTATE - line.len()]).unwrap();
    let ev = || match loads(r#"{"a":1}"#) {
        PyValue::Dict(d) => d,
        _ => unreachable!(),
    };
    log.write(ev()).unwrap();
    assert_eq!(
        std::fs::metadata(&path).unwrap().len() as usize,
        ROTATE,
        "恰等於上限不輪替"
    );
    log.write(ev()).unwrap();
    assert_eq!(
        std::fs::read_to_string(&path).unwrap(),
        line,
        "超過 → 輪替後寫新檔"
    );
    assert_eq!(
        std::fs::metadata(d.path().join("events.ndjson.1"))
            .unwrap()
            .len() as usize,
        ROTATE
    );
    assert_eq!(
        std::fs::read_to_string(d.path().join("events.ndjson.2")).unwrap(),
        "old1"
    );
    assert_eq!(
        std::fs::read_to_string(d.path().join("events.ndjson.3")).unwrap(),
        "old2"
    );
    assert!(
        !d.path().join("events.ndjson.4").exists(),
        "backup_count=3：最舊者被覆蓋"
    );
}

#[test]
fn logger_creates_parent_dir() {
    let d = tempfile::tempdir().unwrap();
    let clock = FakeClock::new();
    let c: Arc<dyn Clock> = clock;
    let p = d.path().join("a/b/events.ndjson");
    let log = EventLogger::new(p.clone(), native_fs(), c).unwrap();
    assert!(d.path().join("a/b").is_dir());
    assert_eq!(log.path(), p.as_path());
}

#[test]
fn diff_against_nan_field_is_unchanged() {
    // 審查 76de0cf_96b28dd c58e8d4-1：JSON 解出的 NaN 為單例，dict／list 比較走 identity 捷徑 → 相等（CPython 3.12 實測 `changed=[]`）。
    let d = tempfile::tempdir().unwrap();
    std::fs::write(
        d.path().join("o.n.json"),
        r#"{"schema_version": 1, "owner": "o", "name": "n", "kind": "tool", "selectors": ["COM0"], "pattern": {"kind": "contains", "value": "x"}, "handler": {"exec": ["/bin/true"]}, "note": NaN}"#,
    )
    .unwrap();
    let reg = RuleRegistry::new(d.path().into(), native_fs()).unwrap();
    let prev = reg.load_all().unwrap().rules;
    let diff = reg.diff_against(&prev).unwrap();
    assert!(diff.changed.is_empty(), "{diff:?}");
}

#[test]
fn tail_filter_list_with_nan_matches_python() {
    // CPython 3.12 實測：`tail(rule_id=[NaN])` 1 筆（容器 identity）、`tail(rule_id=NaN)` 0 筆（頂層純量 NaN != NaN）、
    // `tail(since_ts=[NaN, 2])` 對 `[NaN,1]`／`[NaN,3]` 留 1 筆（字典序跳過 identity 相等的 NaN）。
    let d = tempfile::tempdir().unwrap();
    let (log, _c) = logger(d.path(), 1);
    std::fs::write(
        d.path().join("events.ndjson"),
        b"{\"rule_id\": [NaN], \"ts\": 1}\n{\"rule_id\": NaN, \"ts\": 2}\n",
    )
    .unwrap();
    assert_eq!(
        log.tail(Some(&loads("[NaN]")), None, None, None)
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        log.tail(Some(&loads("NaN")), None, None, None)
            .unwrap()
            .len(),
        0
    );
    std::fs::write(
        d.path().join("events.ndjson"),
        b"{\"ts\": [NaN, 1]}\n{\"ts\": [NaN, 3]}\n",
    )
    .unwrap();
    assert_eq!(
        log.tail(None, None, Some(&loads("[NaN, 2]")), None)
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn tail_decode_error_position_is_chunk_relative() {
    // 審查 76de0cf_96b28dd c58e8d4-3：Python `readlines()` 經 TextIOWrapper 以 8192 bytes 分塊增量解碼，
    // UnicodeDecodeError 的 position 相對於「前塊殘留＋當前塊」（CPython 3.12 實測）。
    let d = tempfile::tempdir().unwrap();
    let (log, _c) = logger(d.path(), 1);
    let path = d.path().join("events.ndjson");
    let line = b"{\"a\": 1}\n";
    let mut base: Vec<u8> = line.repeat(15000 / line.len());
    base.resize(15000, b' ');
    let mut case1 = base.clone();
    case1.extend_from_slice(b"\xff\n");
    let mut case2 = vec![b'x'; 8191];
    case2.extend_from_slice("中".as_bytes());
    case2.push(b'\n');
    case2.extend_from_slice(&[b'y'; 100]);
    case2.extend_from_slice(b"\xff\n");
    let mut case5 = vec![b'x'; 8191];
    case5.extend_from_slice(b"\xe4\x41\n");
    let cases: Vec<(Vec<u8>, &str)> = vec![
        (
            case1,
            "'utf-8' codec can't decode byte 0xff in position 6808: invalid start byte",
        ),
        (
            case2,
            "'utf-8' codec can't decode byte 0xff in position 104: invalid start byte",
        ),
        (
            b"{\"a\":1}\n\xe4\xb8".to_vec(),
            "'utf-8' codec can't decode bytes in position 0-1: unexpected end of data",
        ),
        (
            b"ab\xe4\x41\n".to_vec(),
            "'utf-8' codec can't decode byte 0xe4 in position 2: invalid continuation byte",
        ),
        (
            case5,
            "'utf-8' codec can't decode byte 0xe4 in position 0: invalid continuation byte",
        ),
    ];
    // 審查 06c61d0_b65df2d aaf2c1d-1：CPython 非 final 解碼對塊尾「0xED＋0xA0..0xBF」（截斷代理對）延後到下一塊。
    let mut case6 = vec![b'x'; 8190];
    case6.extend_from_slice(b"\xed\xa0\x80\n");
    let mut case7 = vec![b'x'; 8190];
    case7.extend_from_slice(b"\xed\xa0");
    let mut case8 = vec![b'x'; 8189];
    case8.extend_from_slice(b"\xed\xa0\x80\n");
    let mut cases = cases;
    cases.push((
        case6,
        "'utf-8' codec can't decode byte 0xed in position 0: invalid continuation byte",
    ));
    cases.push((
        case7,
        "'utf-8' codec can't decode byte 0xed in position 0: invalid continuation byte",
    ));
    cases.push((
        case8,
        "'utf-8' codec can't decode byte 0xed in position 8189: invalid continuation byte",
    ));
    for (i, (data, msg)) in cases.into_iter().enumerate() {
        std::fs::write(&path, data).unwrap();
        let err = log
            .tail(None, None, None, None)
            .expect_err("UnicodeDecodeError 穿出");
        assert_eq!(err.message, msg, "case {i}");
    }
    // 多位元組字元跨塊但合法：照常解碼。
    let mut ok = vec![b' '; 8191];
    ok.extend_from_slice("{\"中\": 1}\n".as_bytes());
    std::fs::write(&path, ok).unwrap();
    assert_eq!(
        dumps_default(&PyValue::List(log.tail(None, None, None, None).unwrap())),
        r#"[{"中": 1}]"#
    );
}

#[test]
fn tail_reads_current_file_only() {
    // 計畫表名（08 Task 13）：`.1` 內容不出現在 tail。
    let d = tempfile::tempdir().unwrap();
    let (log, _c) = logger(d.path(), 1);
    std::fs::write(d.path().join("events.ndjson.1"), b"{\"old\": 1}\n").unwrap();
    std::fs::write(d.path().join("events.ndjson"), b"{\"cur\": 1}\n").unwrap();
    assert_eq!(
        dumps_default(&PyValue::List(log.tail(None, None, None, None).unwrap())),
        r#"[{"cur": 1}]"#
    );
}
