//! Windows 原生登錄讀取（P4-T2 紅燈骨架；只在 Windows 編譯）。
#![allow(unused_variables)]
use crate::device_source::windows_model::RegistryView;
use crate::traits::PalResult;

/// `RegOpenKeyExW`＋`RegEnumValueW` 的原生實作。
#[derive(Debug, Clone, Copy, Default)]
pub struct WinRegistryView;

impl RegistryView for WinRegistryView {
    fn enum_values(&self, key: &str) -> PalResult<Vec<(String, String)>> {
        unimplemented!("P4-T2")
    }
    fn enum_subkeys(&self, key: &str) -> PalResult<Vec<String>> {
        unimplemented!("P4-T2")
    }
    fn query_string(&self, key: &str, name: &str) -> PalResult<Option<String>> {
        unimplemented!("P4-T2")
    }
}
