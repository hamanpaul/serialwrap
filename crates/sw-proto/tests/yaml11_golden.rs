//! `yaml11::safe_load` 對 PyYAML `safe_load` 的 golden 比對（P3-T6）。
//!
//! 期望值由 `scripts/rust/goldens/gen_yaml11.py` 產生（型別標記編碼見 `tag()`）。
//! `{"t":"error"}` 列只比對「是否錯誤」，錯誤訊息不屬於契約（plan G5）。
mod common;

use sw_proto::pyjson::{dumps, RPC_WIRE};
use sw_proto::yaml11::{safe_load, to_tagged_json};

const ERROR_ENC: &str = r#"{"t":"error"}"#;

#[test]
fn yaml11_golden() {
    let rows = common::golden("yaml11.tsv");
    let mut fails = Vec::new();
    for r in &rows {
        match safe_load(&r.input) {
            Ok(v) => {
                if r.expected == ERROR_ENC {
                    fails.push(format!("{}: 期望 YAML 錯誤，實際解析成功 input={:?}", r.id, r.input));
                    continue;
                }
                let got = dumps(&to_tagged_json(&v), &RPC_WIRE);
                if got != r.expected {
                    fails.push(format!(
                        "{}: got {got}\nwant {}\ninput {:?}",
                        r.id, r.expected, r.input
                    ));
                }
            }
            Err(e) => {
                if r.expected != ERROR_ENC {
                    fails.push(format!(
                        "{}: 非預期錯誤 {}（期望 {}）input={:?}",
                        r.id, e.message, r.expected, r.input
                    ));
                }
            }
        }
    }
    assert!(
        fails.is_empty(),
        "{} / {} 列不符，前 20 列：\n{}",
        fails.len(),
        rows.len(),
        fails[..fails.len().min(20)].join("\n\n")
    );
}
