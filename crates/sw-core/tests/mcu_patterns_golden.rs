//! P8-T2：Rust `mcu_patterns` 逐列對照 `contracts/golden/mcu_patterns.ndjson`（由 Python `sw_core` 產生）。
//! 本測試只讀 golden，不呼叫 Python。

use std::collections::BTreeMap;

use sw_core::mcu_patterns::{
    bytes_hex_spaced, hex_to_bytes, McuPattern, McuPatternError, McuPatternRegistry,
};
use sw_proto::pyjson::{self, PyDict, PyValue, RPC_WIRE};

const GOLDEN: &str = include_str!("../../../contracts/golden/mcu_patterns.ndjson");

/// Python 端 `_pattern_str` 的格式：`family|probe|expect|baud|timeout_ms`。
fn pattern_str(p: &McuPattern) -> String {
    format!(
        "{}|{}|{}|{}|{}",
        p.family,
        bytes_hex_spaced(&p.probe),
        bytes_hex_spaced(&p.expect),
        p.baud,
        p.timeout_ms
    )
}

/// 失敗列的 golden 形式：`{"error": "<Python 例外類別名>"}`。
fn err(e: &McuPatternError) -> PyValue {
    PyValue::Dict(PyDict::from_pairs(vec![(
        "error".to_string(),
        PyValue::Str(e.py_exc().to_string()),
    )]))
}

/// golden 的 `in` 為 `null`（None）或 dict 列表。
fn rows_of(v: &PyValue) -> Option<Vec<PyDict>> {
    match v {
        PyValue::Null => None,
        PyValue::List(items) => Some(
            items
                .iter()
                .map(|i| match i {
                    PyValue::Dict(d) => d.clone(),
                    other => panic!("rows 元素須為 dict，實際 {other:?}"),
                })
                .collect(),
        ),
        other => panic!("rows 須為 null 或 list，實際 {other:?}"),
    }
}

#[test]
fn golden_rows_match_python() {
    let mut seen: BTreeMap<String, usize> = BTreeMap::new();
    for (idx, line) in GOLDEN.lines().enumerate() {
        let lineno = idx + 1;
        let rec = match pyjson::loads(line)
            .unwrap_or_else(|e| panic!("line {lineno}: 非法 JSON: {e:?}"))
        {
            PyValue::Dict(d) => d,
            other => panic!("line {lineno}: 須為物件，實際 {other:?}"),
        };
        let f = match rec.get("fn") {
            Some(PyValue::Str(s)) => s.clone(),
            other => panic!("line {lineno}: fn 缺漏或型別錯誤: {other:?}"),
        };
        let input = rec
            .get("in")
            .unwrap_or_else(|| panic!("line {lineno}: 缺 in"))
            .clone();
        let want = rec
            .get("out")
            .unwrap_or_else(|| panic!("line {lineno}: 缺 out"))
            .clone();

        let got = match f.as_str() {
            "hex_to_bytes" => {
                let PyValue::Str(s) = &input else {
                    panic!("line {lineno}: hex_to_bytes 的 in 須為字串")
                };
                match hex_to_bytes(s) {
                    Ok(b) => PyValue::Str(bytes_hex_spaced(&b)),
                    Err(e) => err(&e),
                }
            }
            "from_dict" => {
                let PyValue::Dict(d) = &input else {
                    panic!("line {lineno}: from_dict 的 in 須為 dict")
                };
                match McuPattern::from_dict(d) {
                    Ok(p) => PyValue::Str(pattern_str(&p)),
                    Err(e) => err(&e),
                }
            }
            "load" => match McuPatternRegistry::load(rows_of(&input).as_deref()) {
                Ok(reg) => PyValue::Str(
                    reg.all()
                        .iter()
                        .map(|p| pattern_str(p))
                        .collect::<Vec<_>>()
                        .join(","),
                ),
                Err(e) => err(&e),
            },
            "rpc" => match McuPatternRegistry::load(rows_of(&input).as_deref()) {
                Ok(reg) => reg.patterns_rpc(),
                Err(e) => err(&e),
            },
            other => panic!("line {lineno}: 未知 fn {other}"),
        };
        assert_eq!(
            got,
            want,
            "line {lineno}: fn={f} in={}",
            pyjson::dumps(&input, &RPC_WIRE)
        );
        *seen.entry(f).or_default() += 1;
    }
    for k in ["hex_to_bytes", "from_dict", "load", "rpc"] {
        assert!(seen.get(k).copied().unwrap_or(0) > 0, "golden 缺少 {k} 列");
    }
}

#[test]
fn default_registry_matches_python_default() {
    let reg = McuPatternRegistry::default_registry();
    let rows: Vec<String> = reg.all().iter().map(|p| pattern_str(p)).collect();
    assert_eq!(rows, ["ti-cc26xx|55 55|00 cc|115200|500"]);
}
