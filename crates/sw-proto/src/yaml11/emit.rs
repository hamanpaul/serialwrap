//! YAML 輸出（P4-T17；移植 PyYAML `Emitter`／`Representer` 的 `safe_dump` 路徑）。
//!
//! 紅燈 stub：行為尚未實作，語料與期望值見 `tests/golden/yaml_emit.ndjson`。

use super::YValue;

/// 等同 PyYAML `yaml.safe_dump(v, allow_unicode=..., sort_keys=..., default_flow_style=False)`。
pub fn safe_dump(_v: &YValue, _allow_unicode: bool, _sort_keys: bool) -> String {
    unimplemented!("P4-T17")
}
