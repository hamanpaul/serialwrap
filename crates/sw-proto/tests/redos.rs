//! P7 Task 11（08-slice23-login-events.md）：無 re2 ReDoS 判定對 Python 基線（`contracts/golden/p7/redos.ndjson`）。
//!
//! 偏離計畫原文：08 §0.3 核對後 `pyre::parse` 為私有、無 `pyre::sre::SubPattern`，`ambiguity_score`／`has_backref`
//! 改以 `(pattern, flags)` 為輸入；本檔另逐列比對 score 與 has_backref（計畫原文只比 suspicious）。
mod common;

use sw_proto::pyjson::{self, PyInt, PyValue};
use sw_proto::pyre::{redos, CompileError, PyFlags};

fn golden_rows(name: &str) -> Vec<sw_proto::pyjson::PyDict> {
    let path = common::data_root().join(format!("contracts/golden/p7/{name}.ndjson"));
    let text =
        std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("讀取 {}：{e}", path.display()));
    text.lines()
        .map(|l| match pyjson::loads(l).unwrap() {
            PyValue::Dict(d) => d,
            _ => panic!("golden 行須為 object"),
        })
        .collect()
}

fn s<'a>(r: &'a sw_proto::pyjson::PyDict, k: &str) -> &'a str {
    r.get(k).and_then(PyValue::as_str).unwrap()
}

/// golden 的 Python 任意精度分數；超過 `u64::MAX` 者對應 Rust 的飽和值（sol 補審 #2）。
fn expected_score(row: &sw_proto::pyjson::PyDict) -> u64 {
    let text = match row.get("score") {
        Some(PyValue::Int(PyInt::Small(n))) => n.to_string(),
        Some(PyValue::Int(PyInt::Big(s))) => s.clone(),
        other => panic!("score 須為整數：{other:?}"),
    };
    let wide: u128 = text.parse().expect("score 須為非負整數");
    u64::try_from(wide).unwrap_or(u64::MAX)
}

#[test]
fn redos_golden() {
    let rows = golden_rows("redos");
    assert!(rows.len() >= 60);
    for row in rows {
        let id = s(&row, "id");
        let flags = PyFlags::from_rule_flags(s(&row, "flags")).unwrap();
        let compile_ok = row.get("compile_ok").and_then(PyValue::as_bool).unwrap();
        match redos::maybe_redos_suspicious(s(&row, "pattern"), flags) {
            Ok(sus) => {
                assert!(compile_ok, "case {id}：Python 編譯失敗但 Rust 成功");
                assert_eq!(
                    sus,
                    row.get("suspicious").and_then(PyValue::as_bool).unwrap(),
                    "case {id} suspicious"
                );
                let score = redos::ambiguity_score(s(&row, "pattern"), flags).unwrap();
                assert_eq!(score, expected_score(&row), "case {id} score");
                let br = redos::has_backref(s(&row, "pattern"), flags).unwrap();
                assert_eq!(
                    br,
                    row.get("has_backref").and_then(PyValue::as_bool).unwrap(),
                    "case {id} has_backref"
                );
            }
            Err(_) => assert!(!compile_ok, "case {id}：Python 編譯成功但 Rust 失敗"),
        }
    }
}

/// 審查 L2：編譯失敗列的 `str(re.error)`（含 position 與 line/column）須逐字一致。
#[test]
fn redos_golden_error_messages_match_python() {
    let mut checked = 0;
    for row in golden_rows("redos") {
        let Some(want) = row.get("error").and_then(PyValue::as_str) else {
            continue;
        };
        let id = s(&row, "id");
        let flags = PyFlags::from_rule_flags(s(&row, "flags")).unwrap();
        match redos::maybe_redos_suspicious(s(&row, "pattern"), flags) {
            Err(CompileError::Re(e)) => assert_eq!(e.to_string(), want, "case {id}"),
            other => panic!("case {id}：預期 re.error {want:?}，實得 {other:?}"),
        }
        checked += 1;
    }
    assert!(checked >= 14, "golden 至少 14 列錯誤訊息（實得 {checked}）");
}

#[test]
fn from_rule_flags_maps_ism_and_rejects_others() {
    assert_eq!(PyFlags::from_rule_flags("").unwrap(), PyFlags(0));
    assert_eq!(
        PyFlags::from_rule_flags("smi").unwrap(),
        PyFlags(PyFlags::I | PyFlags::S | PyFlags::M)
    );
    assert_eq!(PyFlags::from_rule_flags("ix"), Err('x'));
}

/// sol 補審 #2：巢狀固定 `{2}` 的分數逐層倍增，不得溢位（debug panic／release 回繞為 0 而放行）。
/// 以 `cargo test` 與 `cargo test --release` 各跑一次覆蓋兩種 overflow 設定。
#[test]
fn nested_fixed_repeat_score_saturates_and_stays_suspicious() {
    let none = PyFlags(0);
    for (depth, want) in [
        (31, 1u64 << 31),
        (32, 1u64 << 32),
        (63, 1u64 << 63),
        (64, u64::MAX),
        (80, u64::MAX),
    ] {
        let mut p = String::from("a?");
        for _ in 0..depth {
            p = format!("(?:{p}){{2}}");
        }
        assert_eq!(
            redos::ambiguity_score(&p, none).unwrap(),
            want,
            "depth {depth}"
        );
        assert!(
            redos::maybe_redos_suspicious(&p, none).unwrap(),
            "depth {depth}"
        );
    }
}
