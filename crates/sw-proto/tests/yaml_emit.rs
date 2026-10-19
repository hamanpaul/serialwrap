//! `yaml11::emit::safe_dump` 對 PyYAML `safe_dump` 的 golden 比對（P4-T17）。
//!
//! 語料 `golden/yaml_emit.ndjson` 由 `scripts/rust/goldens/gen_yaml_golden.py` 產生（PyYAML 6.0.3 為 oracle）。
//! 每列 `{"input": <JSON 值>, "allow_unicode": bool, "out": "<PyYAML 輸出>"}`；`sort_keys` 固定為 true。
//! 本檔不使用 `CARGO_MANIFEST_DIR`（`check_cfg_boundary` 的 test-data-path 規則），改以 `include_str!` 內嵌語料。

use sw_proto::pyjson::{self, PyValue};
use sw_proto::yaml11::emit::safe_dump;
use sw_proto::yaml11::{YMap, YValue};

const GOLDEN: &str = include_str!("golden/yaml_emit.ndjson");
const MIN_ROWS: usize = 150;

/// `pyjson` 的 JSON 值 → `YValue`（JSON 只有 null／bool／int／float／str／list／dict，與 PyYAML safe 型別對應）。
fn to_yvalue(v: &PyValue) -> YValue {
    match v {
        PyValue::Null => YValue::Null,
        PyValue::Bool(b) => YValue::Bool(*b),
        PyValue::Int(i) => YValue::Int(i.clone()),
        PyValue::Float(f) => YValue::Float(*f),
        PyValue::Str(s) => YValue::Str(s.clone()),
        PyValue::List(items) => YValue::Seq(items.iter().map(to_yvalue).collect()),
        PyValue::Dict(d) => YValue::Map(YMap(
            d.0.iter()
                .map(|(k, v)| (YValue::Str(k.clone()), to_yvalue(v)))
                .collect(),
        )),
    }
}

#[test]
fn yaml_emit_matches_pyyaml_golden() {
    let mut rows = 0usize;
    let mut fails = Vec::new();
    for (idx, line) in GOLDEN.split('\n').filter(|l| !l.is_empty()).enumerate() {
        let row = pyjson::loads(line).unwrap_or_else(|e| panic!("golden 第 {idx} 列非合法 JSON：{e}"));
        let PyValue::Dict(row) = row else {
            panic!("golden 第 {idx} 列不是 JSON 物件");
        };
        let input = row.get("input").unwrap_or_else(|| panic!("golden 第 {idx} 列缺 input"));
        let allow_unicode = match row.get("allow_unicode") {
            Some(PyValue::Bool(b)) => *b,
            _ => panic!("golden 第 {idx} 列 allow_unicode 必須為 bool"),
        };
        let expected = match row.get("out") {
            Some(PyValue::Str(s)) => s.clone(),
            _ => panic!("golden 第 {idx} 列 out 必須為字串"),
        };
        let got = safe_dump(&to_yvalue(input), allow_unicode, true);
        if got != expected {
            fails.push(format!(
                "第 {idx} 列 allow_unicode={allow_unicode}\n  want {expected:?}\n  got  {got:?}"
            ));
        }
        rows += 1;
    }
    assert!(
        rows >= MIN_ROWS,
        "golden 列數 {rows} 低於下限 {MIN_ROWS}（語料被截斷？）"
    );
    assert!(
        fails.is_empty(),
        "{} / {rows} 列與 PyYAML 不符，前 20 列：\n{}",
        fails.len(),
        fails[..fails.len().min(20)].join("\n\n")
    );
}
