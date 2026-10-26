//! P7（08-slice23-login-events.md）測試共用輔助：讀 `contracts/golden/p7/*.ndjson`、規則 JSON 建構與規則檔 dump 選項。
#![allow(dead_code)]

use sw_proto::pyjson::{self, DumpOpts, PyDict, PyValue, Sep};

/// 一列 golden（`id` 欄位為 `<file>:<nnn>`）。
pub struct Row(pub PyDict);

impl Row {
    pub fn id(&self) -> &str {
        self.str("id")
    }
    pub fn get(&self, k: &str) -> &PyValue {
        self.0
            .get(k)
            .unwrap_or_else(|| panic!("golden 欄位 {k} 不存在"))
    }
    pub fn str(&self, k: &str) -> &str {
        self.get(k)
            .as_str()
            .unwrap_or_else(|| panic!("golden 欄位 {k} 不是字串"))
    }
    pub fn opt_str(&self, k: &str) -> Option<&str> {
        self.0.get(k).and_then(PyValue::as_str)
    }
    pub fn bool(&self, k: &str) -> bool {
        self.get(k)
            .as_bool()
            .unwrap_or_else(|| panic!("golden 欄位 {k} 不是 bool"))
    }
}

impl std::ops::Index<&str> for Row {
    type Output = PyValue;
    fn index(&self, k: &str) -> &PyValue {
        self.get(k)
    }
}

pub fn golden_rows(name: &str) -> Vec<Row> {
    let path = super::repo_path(&format!("contracts/golden/p7/{name}.ndjson"));
    let text =
        std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("讀取 {}：{e}", path.display()));
    text.lines()
        .map(|l| match pyjson::loads(l).expect("golden 行須為 JSON") {
            PyValue::Dict(d) => Row(d),
            other => panic!("golden 行須為 object：{other:?}"),
        })
        .collect()
}

/// `json.dump(raw, ensure_ascii=False, indent=2, sort_keys=True)`（registry 規則檔格式）。
pub const RULE_FILE: DumpOpts = DumpOpts {
    sep: Sep::Indent(2),
    ensure_ascii: false,
    sort_keys: true,
    allow_nan: true,
};

/// 以合法預設規則為底，覆寫 `fragment`（JSON object）中的頂層鍵。
pub fn rule_json(fragment: &str) -> PyValue {
    let mut base = match pyjson::loads(
        r#"{"schema_version":1,"owner":"o","name":"n","kind":"tool","selectors":["COM0"],
            "pattern":{"kind":"contains","value":"x"},"handler":{"exec":["/bin/true"]}}"#,
    )
    .unwrap()
    {
        PyValue::Dict(d) => d,
        _ => unreachable!(),
    };
    if let PyValue::Dict(over) = pyjson::loads(fragment).expect("fragment 須為 JSON") {
        base.update(&over);
    }
    PyValue::Dict(base)
}
