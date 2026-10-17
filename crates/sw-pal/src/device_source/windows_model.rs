//! Windows 裝置列舉的平台無關模型。
//!
//! 登錄存取抽象 `RegistryView` 與藍牙排除純函式放在此檔，不依賴 `windows-sys`，Linux 上也能單元測試。
use std::collections::HashSet;

use crate::traits::PalResult;

/// SERIALCOMM 登錄鍵（07 §1.6）。
pub const SERIALCOMM_KEY: &str = "HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM";

/// 登錄讀取抽象（P5 共用）。
pub trait RegistryView: Send + Sync {
    /// 依序回傳 `(值名稱, REG_SZ 資料)`；登錄鍵不存在時回 `Ok(vec![])`。
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
    crate::traits::PalError::plain(
        crate::traits::PalErrorKind::Unsupported,
        "POSIX 無 Windows 登錄",
    )
}

/// 遞迴讀取 `HKLM\SYSTEM\CurrentControlSet\Enum\BTHENUM` 下各 `Device Parameters\PortName`（REG_SZ）；吞掉所有錯誤。
pub fn read_bt_ports(view: &dyn RegistryView) -> HashSet<String> {
    fn visit(view: &dyn RegistryView, key: &str, ports: &mut HashSet<String>) {
        let params = format!("{key}\\Device Parameters");
        if let Ok(Some(port)) = view.query_string(&params, "PortName") {
            ports.insert(port);
        }
        if let Ok(children) = view.enum_subkeys(key) {
            for child in children {
                visit(view, &format!("{key}\\{child}"), ports);
            }
        }
    }

    let mut ports = HashSet::new();
    visit(
        view,
        "HKLM\\SYSTEM\\CurrentControlSet\\Enum\\BTHENUM",
        &mut ports,
    );
    ports
}

/// `com ∈ bt || com ∈ exclude || device_path.to_lowercase().contains("bthmodem")` 剔除；其餘依輸入順序收 `com`。
/// 輸入每列為 `(device_path, com)`，即 SERIALCOMM 的 `(value-name, data)`。
pub fn exclude_bluetooth(
    serialcomm: &[(String, String)],
    bt: &HashSet<String>,
    exclude: &HashSet<String>,
) -> Vec<String> {
    let mut seen = HashSet::new();
    serialcomm
        .iter()
        .filter_map(|(device_path, com)| {
            if bt.contains(com)
                || exclude.contains(com)
                || device_path.to_lowercase().contains("bthmodem")
                || !seen.insert(com.as_str())
            {
                None
            } else {
                Some(com.clone())
            }
        })
        .collect()
}
