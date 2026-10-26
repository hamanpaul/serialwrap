//! P7 Task 11（08-slice23-login-events.md）：事件規則 schema 驗證對 Python 基線（`contracts/golden/p7/event_schema.ndjson`）。
//!
//! 偏離計畫原文：計畫寫 `mod support; use support::golden_rows`；06 的 `tests/support/mod.rs` 由 P5 實作中，
//! golden 工具改放 `tests/common/p7.rs`（`CARGO_MANIFEST_DIR` 只允許出現在 `tests/common/mod.rs`）。
mod common;

use common::p7::{golden_rows, rule_json, RULE_FILE};
use sw_core::event_engine::schema::*;
use sw_proto::pyjson::{self, PyInt, PyValue};

#[test]
fn every_python_validation_outcome_matches() {
    let rows = golden_rows("event_schema");
    assert!(rows.len() >= 60, "golden 至少 60 列");
    for row in rows {
        match (validate_rule_dict(&row["input"]), row.bool("ok")) {
            (Ok(rule), true) => {
                assert_eq!(
                    pyjson::dumps(&rule.raw.clone().into(), &RULE_FILE),
                    row.str("raw_dump"),
                    "case {}",
                    row.id()
                );
                assert_eq!(rule.rule_id, row.str("rule_id"), "case {}", row.id());
            }
            (Err(RuleError::Schema(m)), false) => {
                assert_eq!(row.str("exc_type"), "RuleSchemaError", "case {}", row.id());
                assert_eq!(m, row.str("message"), "case {}", row.id());
            }
            (Err(RuleError::Value(m)), false) => {
                assert_eq!(row.str("exc_type"), "ValueError", "case {}", row.id());
                assert_eq!(m, row.str("message"), "case {}", row.id());
            }
            (Err(RuleError::Type(m)), false) => {
                assert_eq!(row.str("exc_type"), "TypeError", "case {}", row.id());
                assert_eq!(m, row.str("message"), "case {}", row.id());
            }
            (got, want) => panic!(
                "case {} 期望 ok={want} 實得 {:?}",
                row.id(),
                got.map(|r| r.rule_id)
            ),
        }
    }
}

#[test]
fn contains_with_bad_flag_is_rejected_df08() {
    let v = rule_json(r#"{"pattern":{"kind":"contains","value":"x","flags":"q"}}"#);
    assert!(
        matches!(validate_rule_dict(&v), Err(RuleError::Schema(m)) if m == "unsupported regex flag: q")
    );
}

fn schema_err(v: &PyValue) -> String {
    match validate_rule_dict(v) {
        Err(RuleError::Schema(m)) => m,
        other => panic!("預期 RuleSchemaError，實得 {:?}", other.map(|r| r.rule_id)),
    }
}

#[test]
fn schema_version_python_eq() {
    for ok in [
        r#"{"schema_version":1}"#,
        r#"{"schema_version":1.0}"#,
        r#"{"schema_version":true}"#,
    ] {
        assert!(validate_rule_dict(&rule_json(ok)).is_ok(), "{ok}");
    }
    for bad in [
        r#"{"schema_version":"1"}"#,
        r#"{"schema_version":2}"#,
        r#"{"schema_version":null}"#,
    ] {
        assert_eq!(
            schema_err(&rule_json(bad)),
            "schema_version must be 1",
            "{bad}"
        );
    }
}

#[test]
fn name_re_allows_trailing_newline() {
    let r = validate_rule_dict(&rule_json(r#"{"name":"abc\n"}"#)).unwrap();
    assert_eq!(r.rule_id, "o.abc\n");
    assert_eq!(
        schema_err(&rule_json(r#"{"name":"abc\n\n"}"#)),
        "name must match [a-z0-9-]{1,64}"
    );
    assert_eq!(
        schema_err(&rule_json(r#"{"owner":"Bad"}"#)),
        "owner must match [a-z0-9-]{1,64}"
    );
}

#[test]
fn rule_id_must_equal_derived() {
    assert_eq!(
        schema_err(&rule_json(r#"{"rule_id":"x.y"}"#)),
        "rule_id must equal 'o.n'"
    );
    assert_eq!(
        schema_err(&rule_json(r#"{"rule_id":null}"#)),
        "rule_id must equal 'o.n'"
    );
    assert!(validate_rule_dict(&rule_json(r#"{"rule_id":"o.n"}"#)).is_ok());
}

#[test]
fn regex_len_512_chars() {
    let ok = format!(
        r#"{{"pattern":{{"kind":"regex","value":"{}"}}}}"#,
        "中".repeat(512)
    );
    assert!(validate_rule_dict(&rule_json(&ok)).is_ok(), "長度以字元計");
    let bad = format!(
        r#"{{"pattern":{{"kind":"regex","value":"{}"}}}}"#,
        "a".repeat(513)
    );
    assert_eq!(
        schema_err(&rule_json(&bad)),
        "pattern.value regex 過長（上限 512 字元，避免 ReDoS）"
    );
}

#[test]
fn redos_rejects_a_plus_b_plus() {
    let msg = schema_err(&rule_json(r#"{"pattern":{"kind":"regex","value":"a+b+"}}"#));
    assert!(
        msg.starts_with("pattern.value 結構上可能 catastrophic backtracking"),
        "{msg}"
    );
    assert!(msg.ends_with("以線性引擎免疫此限制"), "{msg}");
    for ok in [r"temp=(\\d+)C", "Kernel panic", "(?=x)abc"] {
        let v = rule_json(&format!(
            r#"{{"pattern":{{"kind":"regex","value":"{ok}"}}}}"#
        ));
        assert!(validate_rule_dict(&v).is_ok(), "{ok}");
    }
    let invalid = schema_err(&rule_json(r#"{"pattern":{"kind":"regex","value":"("}}"#));
    assert_eq!(
        invalid,
        "pattern.value is not a valid regex: missing ), unterminated subpattern at position 0"
    );
}

#[test]
fn cooldown_python_int() {
    let get = |frag: &str| validate_rule_dict(&rule_json(frag)).map(|r| r.cooldown_ms);
    assert_eq!(
        get(r#"{"cooldown_ms":"5000"}"#).unwrap(),
        PyInt::Small(5000)
    );
    assert_eq!(get(r#"{"cooldown_ms":1.5}"#).unwrap(), PyInt::Small(1));
    assert_eq!(get(r#"{"cooldown_ms":true}"#).unwrap(), PyInt::Small(1));
    assert_eq!(get(r#"{"cooldown_ms":" 7 "}"#).unwrap(), PyInt::Small(7));
    assert!(
        matches!(get(r#"{"cooldown_ms":"abc"}"#), Err(RuleError::Value(m)) if m == "invalid literal for int() with base 10: 'abc'")
    );
    assert!(matches!(
        get(r#"{"cooldown_ms":null}"#),
        Err(RuleError::Type(_))
    ));
    assert!(
        matches!(get(r#"{"cooldown_ms":-1}"#), Err(RuleError::Schema(m)) if m == "cooldown_ms must be >= 0")
    );
    assert!(
        matches!(validate_rule_dict(&rule_json(r#"{"timeout_ms":0}"#)), Err(RuleError::Schema(m)) if m == "timeout_ms must be > 0")
    );
}

#[test]
fn big_ints_beyond_i128_accepted() {
    let big = "340282366920938463463374607431768211456"; // 2**128
    let v = rule_json(&format!(
        r#"{{"cooldown_ms":{big},"timeout_ms":{big},"max_fires":{big}}}"#
    ));
    let r = validate_rule_dict(&v).unwrap();
    assert_eq!(r.cooldown_ms, PyInt::Big(big.into()));
    assert_eq!(r.cooldown_ms_sat(), i128::MAX);
    assert_eq!(r.timeout(), std::time::Duration::MAX);
    assert_eq!(
        pyjson::dumps(r.raw.get("max_fires").unwrap(), &pyjson::RPC_WIRE),
        big
    );
    let small = validate_rule_dict(&rule_json(r#"{"timeout_ms":1500}"#)).unwrap();
    assert_eq!(small.timeout(), std::time::Duration::from_millis(1500));
    assert_eq!(small.cooldown_ms_sat(), 0);
}

#[test]
fn max_fires_bool_passes() {
    let r = validate_rule_dict(&rule_json(r#"{"max_fires":true}"#)).unwrap();
    assert_eq!(r.max_fires, Some(PyValue::Bool(true)));
    assert_eq!(
        validate_rule_dict(&rule_json(r#"{"max_fires":null}"#))
            .unwrap()
            .max_fires,
        None
    );
    for bad in [
        r#"{"max_fires":-1}"#,
        r#"{"max_fires":"3"}"#,
        r#"{"max_fires":1.5}"#,
    ] {
        assert_eq!(
            schema_err(&rule_json(bad)),
            "max_fires must be null or non-negative int",
            "{bad}"
        );
    }
}

#[test]
fn handler_exactly_one() {
    let msg = "handler must have exactly one of 'exec' (list[str]) or 'shell' (str)";
    assert_eq!(
        schema_err(&rule_json(r#"{"handler":{"exec":["a"],"shell":"b"}}"#)),
        msg
    );
    assert_eq!(schema_err(&rule_json(r#"{"handler":{}}"#)), msg);
    assert_eq!(schema_err(&rule_json(r#"{"handler":{"exec":[]}}"#)), msg);
    assert_eq!(
        schema_err(&rule_json(r#"{"handler":{"exec":"a"}}"#)),
        "handler.exec must be a non-empty list of non-empty strings"
    );
    assert_eq!(
        schema_err(&rule_json(r#"{"handler":{"shell":3}}"#)),
        "handler.shell must be non-empty string"
    );
    let r = validate_rule_dict(&rule_json(r#"{"handler":{"shell":"echo hi"}}"#)).unwrap();
    assert_eq!(
        r.handler,
        Handler {
            exec: None,
            shell: Some("echo hi".into())
        }
    );
}

#[test]
fn truthiness_flags() {
    let r = validate_rule_dict(&rule_json(
        r#"{"auto_enable_com_on_load":"false","debug":"yes"}"#,
    ))
    .unwrap();
    assert!(
        r.auto_enable_com_on_load,
        "EVT-D-C3 KEEP：字串 \"false\" 為真"
    );
    assert!(r.debug);
    let r = validate_rule_dict(&rule_json(r#"{"auto_enable_com_on_load":0}"#)).unwrap();
    assert!(!r.auto_enable_com_on_load);
    assert!(
        validate_rule_dict(&rule_json("{}"))
            .unwrap()
            .auto_enable_com_on_load,
        "預設 True"
    );
}

#[test]
fn raw_preserves_unknown_keys() {
    let r = validate_rule_dict(&rule_json(r#"{"x_note":{"k":[1,"二"]}}"#)).unwrap();
    assert_eq!(
        pyjson::dumps(r.raw.get("x_note").unwrap(), &pyjson::RPC_WIRE),
        r#"{"k":[1,"二"]}"#
    );
    assert_eq!(r.profile, "ALL");
    assert_eq!(r.level, "INFO");
    assert_eq!(r.scope, "spontaneous");
    assert_eq!(r.selectors, vec!["COM0".to_string()]);
}

#[test]
fn flags_from_string_ism_only() {
    use sw_proto::pyre::PyFlags;
    assert_eq!(flags_from_string("").unwrap(), PyFlags(0));
    assert_eq!(
        flags_from_string("ism").unwrap(),
        PyFlags(PyFlags::I | PyFlags::S | PyFlags::M)
    );
    assert!(
        matches!(flags_from_string("ix"), Err(RuleError::Schema(m)) if m == "unsupported regex flag: x")
    );
}

#[test]
fn name_re_constant_matches_python() {
    assert_eq!(NAME_RE, r"^[a-z0-9][a-z0-9-]{0,63}$");
}
