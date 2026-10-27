//! `mcu.patterns` 靜態清單（Slice 2.1 Task 18；2.1 只列出、不做 flash probe，2.3／2.4 承接）。
//!
//! 名稱依 06 計畫 Task 18 Consumes（`McuPatternRegistry::{default, to_rpc_rows}`）；
//! 07／09 計畫（P8）的 `default_registry`／`patterns_rpc` 須於該計畫對齊本名稱（跨計畫缺口，見 Task 0 回報）。
//! 內容對照 Python `sw_core/mcu_patterns.py` 的 `_DEFAULTS`；RPC 列格式對照 `service.py` 的
//! `{"family", "probe": p.probe.hex(" "), "expect": p.expect.hex(" "), "baud"}`。

use sw_proto::pydict;
use sw_proto::pyjson::PyDict;

/// MCU 韌體升級線路的探測樣式。
///
/// `probe`／`expect` 為 Python `bytes.hex(" ")` 形式（空白分隔的小寫十六進位）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct McuPattern {
    pub family: &'static str,
    pub probe: &'static str,
    pub expect: &'static str,
    pub baud: u32,
}

/// Python `_DEFAULTS`：僅含已審核的非破壞性 pattern（TI CC26xx UART bootloader sync／ACK）。
const DEFAULTS: [McuPattern; 1] = [McuPattern {
    family: "ti-cc26xx",
    probe: "55 55",
    expect: "00 cc",
    baud: 115_200,
}];

/// 目前支援的樣式清單（`mcu.patterns` 回應的來源）。
pub fn patterns() -> &'static [McuPattern] {
    &DEFAULTS
}

/// `mcu.patterns` 的登錄表（Slice 2.1 只提供預設清單）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct McuPatternRegistry {
    patterns: Vec<McuPattern>,
}

impl Default for McuPatternRegistry {
    /// 預設清單（與 Python `_DEFAULTS` 一致）。
    fn default() -> Self {
        McuPatternRegistry {
            patterns: patterns().to_vec(),
        }
    }
}

impl McuPatternRegistry {
    /// RPC 列：每列含 inventory §9.9 欄位 `family`、`probe`、`expect`、`baud`；`probe`／`expect` 為空白分隔的小寫十六進位（例如 `"55 55"`）。
    pub fn to_rpc_rows(&self) -> Vec<PyDict> {
        self.patterns
            .iter()
            .map(|p| {
                pydict! {
                    "family" => p.family,
                    "probe" => p.probe,
                    "expect" => p.expect,
                    "baud" => i64::from(p.baud),
                }
            })
            .collect()
    }
}
