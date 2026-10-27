//! P7 Task 12（08-slice23-login-events.md）：`PatternMatcher`、四道 gate 與有界 `MatcherWorker`（`event_engine/matcher.py`）。
//!
//! 偏離計畫原文：計畫的 `support::RecordingMatcherCtx` 放 `tests/common/matcher_ctx.rs`（P7 共用輔助）；
//! WARNING 經 `MatcherContext::warn`（新增、預設寫 stderr）擷取。
mod common;
#[path = "common/matcher_ctx.rs"]
mod matcher_ctx;

use std::sync::Arc;
use std::time::{Duration, Instant};

use matcher_ctx::{dumps_default, rule, rule_contains, RecordingMatcherCtx};
use sw_core::event_engine::counter::Counter;
use sw_core::event_engine::matcher::*;
use sw_core::event_engine::schema::Pattern;
use sw_proto::pyjson::PyInt;

fn counter(fires: i128, last: Option<i128>, exhausted: bool) -> Counter {
    Counter {
        fires: PyInt::Small(fires),
        last_fire_ts: last.map(PyInt::Small),
        exhausted,
    }
}

fn pm(kind: &str, value: &str, flags: &str) -> PatternMatcher {
    PatternMatcher::new(&Pattern {
        kind: kind.into(),
        value: value.into(),
        flags: flags.into(),
    })
    .expect("pattern")
}

#[test]
fn queue_overflow_drops_oldest_and_emits_event_dropped() {
    let ctx = RecordingMatcherCtx::blocking();
    let w = MatcherWorker::start(vec![rule_contains("x", &["ALL"])], ctx.clone(), 4).unwrap();
    w.feed_line("COM0", "x0".into(), 0);
    ctx.wait_blocked();
    for i in 1..=5 {
        w.feed_line("COM0", format!("x{i}"), i);
    }
    let dropped = ctx.events_of_type("event_dropped");
    assert_eq!(dropped.len(), 1);
    assert_eq!(
        dumps_default(&dropped[0]),
        r#"{"type": "event_dropped", "reason": "matcher_queue_overflow", "selector": "COM0", "wal_seq": 1}"#
    );
    ctx.release();
    ctx.wait_fires(5);
    let seqs: Vec<u64> = ctx.fires().iter().map(|f| f.2).collect();
    assert_eq!(seqs, [0, 2, 3, 4, 5]);
    w.stop();
}

#[test]
fn regex_input_capped_4096_chars() {
    let line = format!("{}TARGET", "a".repeat(4096));
    assert!(pm("regex", "TARGET", "").eval(&line).is_none(), "regex 只看前 4096 字元");
    assert!(pm("contains", "TARGET", "").eval(&line).is_some(), "contains 不受限");
    let line = format!("{}TARGET", "a".repeat(4091));
    assert!(pm("regex", "TARGET", "").eval(&line).is_none(), "跨越 4096 邊界亦不命中");
    let line = format!("{}TARGET", "a".repeat(4090));
    assert_eq!(pm("regex", "TARGET", "").eval(&line).unwrap().matched_text, "TARGET");
}

#[test]
fn contains_flags_i() {
    assert!(pm("contains", "PaNiC", "i").eval("kernel panic").is_some());
    assert!(pm("contains", "PaNiC", "").eval("kernel panic").is_none());
    assert_eq!(pm("contains", "a.b", "").eval("axb a.b").unwrap().matched_text, "a.b", "contains 為字面量");
}

#[test]
fn groups_none_for_unparticipating() {
    let m = pm("regex", "(a)|(b)", "").eval("b").unwrap();
    assert_eq!(m.matched_text, "b");
    assert_eq!(m.groups, vec![None, Some("b".to_string())]);
    let m = pm("regex", "x(?P<n>\\d+)", "").eval("x42").unwrap();
    assert_eq!(m.groups, vec![Some("42".to_string())]);
    assert!(pm("regex", "nope", "").eval("x").is_none());
}

#[test]
fn unknown_pattern_kind_is_value_error() {
    let r = PatternMatcher::new(&Pattern {
        kind: "glob".into(),
        value: "x".into(),
        flags: "".into(),
    });
    assert!(matches!(r, Err(sw_core::event_engine::schema::RuleError::Value(m)) if m == "unknown pattern kind: glob"));
}

#[test]
fn selector_filter_com_name_only() {
    let ctx = RecordingMatcherCtx::new();
    let w = MatcherWorker::start(
        vec![rule_contains("x", &["COM1"]), rule(r#"{"name":"m","selectors":["ALL"],"pattern":{"kind":"contains","value":"x"}}"#)],
        ctx.clone(),
        16,
    )
    .unwrap();
    w.feed_line("COM0", "x".into(), 1);
    w.feed_line("COM1", "x".into(), 2);
    ctx.wait_fires(3);
    std::thread::sleep(Duration::from_millis(50));
    let got: Vec<(String, String)> = ctx.fires().into_iter().map(|f| (f.0, f.1)).collect();
    assert_eq!(
        got,
        [
            ("o.m".to_string(), "COM0".to_string()),
            ("o.n".to_string(), "COM1".to_string()),
            ("o.m".to_string(), "COM1".to_string())
        ]
    );
    w.stop();
}

#[test]
fn gate_order_and_debug_skipped() {
    let r = rule(r#"{"debug":true,"scope":"spontaneous","profile":"prpl-template","max_fires":1,"cooldown_ms":1000}"#);
    let cases: [(Option<&str>, Option<&str>, Counter, i64, &str); 4] = [
        (Some("c1"), Some("other"), counter(5, Some(0), false), 0, "scope_mismatch"),
        (None, Some("other"), counter(5, Some(0), false), 0, "profile_mismatch"),
        (None, Some("prpl-template"), counter(1, Some(0), false), 0, "exhausted"),
        (None, Some("prpl-template"), counter(0, Some(0), false), 999, "cooldown"),
    ];
    for (active, profile, c, now, reason) in cases {
        let ctx = RecordingMatcherCtx::new();
        *ctx.active.lock().unwrap() = active.map(str::to_string);
        *ctx.profile.lock().unwrap() = profile.map(str::to_string);
        *ctx.now.lock().unwrap() = now;
        ctx.set_counter(c);
        let w = MatcherWorker::start(vec![Arc::clone(&r)], ctx.clone(), 16).unwrap();
        w.feed_line("COM0", "x".into(), 7);
        let deadline = Instant::now() + Duration::from_secs(5);
        while ctx.events_of_type("match_skipped").is_empty() {
            assert!(Instant::now() < deadline, "{reason}");
            std::thread::sleep(Duration::from_millis(5));
        }
        w.stop();
        let ev = ctx.events_of_type("match_skipped");
        assert_eq!(
            dumps_default(&ev[0]),
            format!(r#"{{"type": "match_skipped", "rule_id": "o.n", "selector": "COM0", "reason": "{reason}"}}"#)
        );
        assert!(ctx.fires().is_empty());
    }
    // debug:false 不發 match_skipped。
    let quiet = rule(r#"{"scope":"spontaneous"}"#);
    let ctx = RecordingMatcherCtx::new();
    *ctx.active.lock().unwrap() = Some("c".into());
    let w = MatcherWorker::start(vec![quiet], ctx.clone(), 16).unwrap();
    w.feed_line("COM0", "x".into(), 1);
    std::thread::sleep(Duration::from_millis(300));
    w.stop();
    assert!(ctx.events_of_type("match_skipped").is_empty());
}

#[test]
fn cooldown_from_last_fire() {
    let r = rule(r#"{"cooldown_ms":1000}"#);
    let c = counter(1, Some(5000), false);
    assert!(!apply_cooldown(&r, &c, 5999));
    assert!(apply_cooldown(&r, &c, 6000));
    assert!(apply_cooldown(&r, &counter(1, None, false), 0), "無 last_fire_ts 即通過");
    let zero = rule(r#"{"cooldown_ms":0}"#);
    assert!(apply_cooldown(&zero, &c, 5000));
}

#[test]
fn max_fires_zero_never() {
    let r = rule(r#"{"max_fires":0}"#);
    assert!(!apply_max_fires(&r, &counter(0, None, false)));
    let r = rule(r#"{"max_fires":2}"#);
    assert!(apply_max_fires(&r, &counter(1, None, false)));
    assert!(!apply_max_fires(&r, &counter(2, None, false)));
    assert!(!apply_max_fires(&r, &counter(0, None, true)), "exhausted → false");
    let unlimited = rule(r#"{"max_fires":null}"#);
    assert!(apply_max_fires(&unlimited, &counter(1_000_000, None, false)));
}

#[test]
fn scope_active_foreground_string() {
    let sp = rule(r#"{"scope":"spontaneous"}"#);
    let co = rule(r#"{"scope":"command_output"}"#);
    let any = rule(r#"{"scope":"any"}"#);
    assert!(!apply_scope(&sp, Some("foreground")));
    assert!(apply_scope(&co, Some("foreground")));
    assert!(apply_scope(&sp, None));
    assert!(!apply_scope(&co, None));
    assert!(apply_scope(&any, Some("x")) && apply_scope(&any, None));
    let all = rule(r#"{"profile":"ALL"}"#);
    let one = rule(r#"{"profile":"prpl-template"}"#);
    assert!(apply_profile(&all, None));
    assert!(apply_profile(&one, Some("prpl-template")));
    assert!(!apply_profile(&one, None));
}

#[test]
fn multiple_rules_fire_same_line() {
    let ctx = RecordingMatcherCtx::new();
    let w = MatcherWorker::start(
        vec![rule_contains("x", &["COM0"]), rule(r#"{"name":"m","pattern":{"kind":"regex","value":"x"}}"#)],
        ctx.clone(),
        16,
    )
    .unwrap();
    w.feed_line("COM0", "x".into(), 3);
    ctx.wait_fires(2);
    w.stop();
    assert_eq!(ctx.fire_rule_ids(), ["o.n", "o.m"]);
}

#[test] // Python：`_rules` 先換、`_rebuild_compiled` 拋錯 → `_compiled` 保留舊值（同 rule_id 仍以舊 pattern 求值）
fn replace_rules_failure_keeps_old_compiled() {
    let ctx = RecordingMatcherCtx::new();
    let w = MatcherWorker::start(vec![rule_contains("old", &["COM0"])], ctx.clone(), 16).unwrap();
    let mut bad = (*rule_contains("new", &["COM0"])).clone();
    bad.pattern.kind = "glob".into();
    assert!(w.replace_rules(vec![Arc::new(bad)]).is_err());
    w.feed_line("COM0", "new".into(), 1);
    w.feed_line("COM0", "old".into(), 2);
    ctx.wait_fires(1);
    std::thread::sleep(Duration::from_millis(100));
    w.stop();
    let fires = ctx.fires();
    assert_eq!(fires.len(), 1);
    assert_eq!((fires[0].0.as_str(), fires[0].2), ("o.n", 2));
}

#[test]
fn evaluate_panic_is_contained() {
    let ctx = RecordingMatcherCtx::new();
    ctx.panic_counter_once();
    let w = MatcherWorker::start(vec![rule_contains("x", &["COM0"])], ctx.clone(), 16).unwrap();
    w.feed_line("COM0", "x".into(), 1);
    w.feed_line("COM0", "x".into(), 2);
    ctx.wait_fires(1);
    w.stop();
    assert_eq!(ctx.fires().iter().map(|f| f.2).collect::<Vec<_>>(), [2]);
    assert!(
        ctx.warnings()
            .contains(&"event matcher 求值失敗，丟棄此 item（selector=COM0）".to_string()),
        "{:?}",
        ctx.warnings()
    );
}

#[test]
fn stop_join_2s_warning() {
    let ctx = RecordingMatcherCtx::blocking();
    let w = MatcherWorker::start(vec![rule_contains("x", &["COM0"])], ctx.clone(), 16).unwrap();
    w.feed_line("COM0", "x".into(), 1);
    ctx.wait_blocked();
    let t0 = Instant::now();
    w.stop();
    let elapsed = t0.elapsed();
    assert!(elapsed >= Duration::from_millis(1900) && elapsed < Duration::from_millis(3500), "{elapsed:?}");
    assert!(ctx.warnings().contains(
        &"event-matcher thread did not exit within 2s; context may receive late callbacks".to_string()
    ));
    ctx.release();
}

#[test] // parity #118（B:F-E05）：求值例外收斂、engine 不終止；stop 等待逾 2 s 寫 WARNING
fn matcher_exception_converges_and_stop_warns_after_2s() {
    let ctx = RecordingMatcherCtx::new();
    ctx.panic_counter_once();
    let w = MatcherWorker::start(vec![rule_contains("x", &["COM0"])], ctx.clone(), 4).unwrap();
    w.feed_line("COM0", "x".into(), 1);
    w.feed_line("COM0", "x".into(), 2);
    ctx.wait_fires(1);
    let t0 = Instant::now();
    w.stop();
    assert!(t0.elapsed() < Duration::from_millis(1500), "正常 stop 不等滿 2 s");
    assert_eq!(ctx.fires().len(), 1);
    assert_eq!(ctx.warnings().len(), 1, "{:?}", ctx.warnings());
}

#[test] // 審查 de24500：零寬可選擷取群組（`()?`、`(\b)?`…）不得在轉譯時被省略，使後續群組序號錯位；值以 CPython 3.12 實測
fn groups_zero_width_optional_capture_keeps_index() {
    for (pattern, first) in [
        ("()?(a)", Some("")),
        ("()*(a)", Some("")),
        ("()??(a)", None),
        ("(\\b)?(a)", Some("")),
        ("()*?(a)", None),
        ("()?+(a)", Some("")),
    ] {
        let m = pm("regex", pattern, "").eval("a").unwrap();
        assert_eq!(m.matched_text, "a", "{pattern}");
        assert_eq!(
            m.groups,
            vec![first.map(str::to_string), Some("a".to_string())],
            "{pattern}"
        );
    }
}

#[test] // 審查 de24500：cooldown 以任意精度整數比較（Python int），不得因 i128 飽和改變判定；值以 CPython 3.12 實測
fn cooldown_big_int_exact_not_saturated() {
    let big = |s: &str| PyInt::Big(s.to_string());
    let c = |last: PyInt| Counter {
        fires: PyInt::Small(1),
        last_fire_ts: Some(last),
        exhausted: false,
    };
    let r = rule(r#"{"cooldown_ms":2000000000000000000000000000000000000000}"#);
    assert!(!apply_cooldown(
        &r,
        &c(big("-1000000000000000000000000000000000000000")),
        0
    ));
    assert!(apply_cooldown(
        &r,
        &c(big("-3000000000000000000000000000000000000000")),
        0
    ));
    let r = rule(r#"{"cooldown_ms":10000000000000000000000000000000000000000}"#);
    assert!(!apply_cooldown(
        &r,
        &c(big("10000000000000000000000000000000000000000")),
        5
    ));
    let r = rule(r#"{"cooldown_ms":1000}"#);
    assert!(apply_cooldown(
        &r,
        &c(big("-170141183460469231731687303715884105728")),
        0
    ));
    assert!(!apply_cooldown(
        &r,
        &c(big("170141183460469231731687303715884105728")),
        0
    ));
    let r = rule(r#"{"cooldown_ms":170141183460469231731687303715884105728}"#);
    assert!(!apply_cooldown(&r, &c(PyInt::Small(i128::MIN + 1)), 0));
}
