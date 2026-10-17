//! Windows 裝置列舉的平台無關模型（P4-T2 紅燈骨架）。
//!
//! 登錄存取抽象 `RegistryView` 與藍牙排除純函式放在此檔，不依賴 `windows-sys`，Linux 上也能單元測試。
#![allow(unused_variables)]
use std::collections::HashSet;

use crate::traits::PalResult;

/// SERIALCOMM 登錄鍵（07 §1.6）。
pub const SERIALCOMM_KEY: &str = "HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM";

/// 登錄讀取抽象（P5 共用）。
pub trait RegistryView: Send + Sync {
    /// 依序回傳 `(value-name, REG_SZ 資料)`；key 不存在回 `Ok(vec![])`。
    fn enum_values(&self, key: &str) -> PalResult<Vec<(String, String)>>;
    fn enum_subkeys(&self, key: &str) -> PalResult<Vec<String>>;
    fn query_string(&self, key: &str, name: &str) -> PalResult<Option<String>>;
}

/// POSIX 無登錄：一律 `Err(Unsupported)`。
#[derive(Debug, Clone, Copy, Default)]
pub struct NullRegistryView;

impl RegistryView for NullRegistryView {
    fn enum_values(&self, _key: &str) -> PalResult<Vec<(String, String)>> {
        Err(registry_unsupported())
    }
    fn enum_subkeys(&self, _key: &str) -> PalResult<Vec<String>> {
        Err(registry_unsupported())
    }
    fn query_string(&self, _key: &str, _name: &str) -> PalResult<Option<String>> {
        Err(registry_unsupported())
    }
}

fn registry_unsupported() -> crate::traits::PalError {
    crate::traits::PalError::plain(crate::traits::PalErrorKind::Unsupported, "POSIX 無 Windows 登錄")
}

/// 遞迴讀取 `HKLM\SYSTEM\CurrentControlSet\Enum\BTHENUM` 下各 `Device Parameters\PortName`（REG_SZ）；吞掉所有錯誤。
pub fn read_bt_ports(view: &dyn RegistryView) -> HashSet<String> {
    unimplemented!("P4-T2")
}

/// `com ∈ bt || com ∈ exclude || device_path.to_lowercase().contains("bthmodem")` 剔除；其餘依輸入順序收 `com`。
/// 輸入每列為 `(device_path, com)`，即 SERIALCOMM 的 `(value-name, data)`。
pub fn exclude_bluetooth(
    serialcomm: &[(String, String)],
    bt: &HashSet<String>,
    exclude: &HashSet<String>,
) -> Vec<String> {
    unimplemented!("P4-T2")
}
