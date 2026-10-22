//! `mcu.patterns` 靜態清單（Slice 2.1 Task 18；2.1 只列出、不做 flash probe，2.3／2.4 承接）。
//!
//! Task 0 骨架：型別與簽章凍結，清單本體與 RPC 列格式由 Task 18 交付。
//! 名稱依 06 計畫 Task 18 Consumes（`McuPatternRegistry::{default, to_rpc_rows}`）；
//! 07／09 計畫（P8）的 `default_registry`／`patterns_rpc` 須於該計畫對齊本名稱（跨計畫缺口，見 Task 0 回報）。

use sw_proto::pyjson::PyDict;

/// MCU 韌體升級線路的探測樣式。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct McuPattern {
    pub family: &'static str,
    pub probe: &'static str,
    pub expect: &'static str,
    pub baud: u32,
}

/// 目前支援的樣式清單（`mcu.patterns` 回應的來源）。
pub fn patterns() -> &'static [McuPattern] {
    crate::s21_todo!("T18", "patterns")
}

/// `mcu.patterns` 的登錄表（Task 18 交付；Slice 2.1 只提供預設清單）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct McuPatternRegistry {
    _priv: (),
}

impl Default for McuPatternRegistry {
    /// 預設清單（與 Python `_DEFAULTS` 一致）。
    fn default() -> Self {
        crate::s21_todo!("T18", "McuPatternRegistry::default")
    }
}

impl McuPatternRegistry {
    /// RPC 列：每列含 inventory §9.9 欄位 `family`、`probe`、`expect`、`baud`；`probe`／`expect` 為空白分隔的小寫十六進位（例如 `"55 55"`）。
    pub fn to_rpc_rows(&self) -> Vec<PyDict> {
        crate::s21_todo!("T18", "McuPatternRegistry::to_rpc_rows")
    }
}
