//! P7 Task 11（08-slice23-login-events.md）：無 re2 ReDoS 判定對 Python 基線（`contracts/golden/p7/redos.ndjson`）。
//!
//! 偏離計畫原文：08 §0.3 核對後 `pyre::parse` 為私有、無 `pyre::sre::SubPattern`，`ambiguity_score`／`has_backref`
//! 改以 `(pattern, flags)` 為輸入；本檔另逐列比對 score 與 has_backref（計畫原文只比 suspicious）。
mod common;

use sw_proto::pyjson::{self, PyValue};
use sw_proto::pyre::{redos, PyFlags};

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
                assert_eq!(
                    Some(score as i64),
                    row.get("score").and_then(PyValue::as_i64),
                    "case {id} score"
                );
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

#[test]
fn from_rule_flags_maps_ism_and_rejects_others() {
    assert_eq!(PyFlags::from_rule_flags("").unwrap(), PyFlags(0));
    assert_eq!(
        PyFlags::from_rule_flags("smi").unwrap(),
        PyFlags(PyFlags::I | PyFlags::S | PyFlags::M)
    );
    assert_eq!(PyFlags::from_rule_flags("ix"), Err('x'));
}
