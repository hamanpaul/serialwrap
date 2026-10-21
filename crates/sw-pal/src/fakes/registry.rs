//! 假 `RegistryView`（feature `test-fakes`）：`SERIALCOMM` 與 BTHENUM 藍牙埠登錄值可注入。

use std::sync::Mutex;
use std::collections::BTreeMap;

use crate::device_source::windows_model::{RegistryView, SERIALCOMM_KEY};
use crate::traits::{PalError, PalErrorKind, PalResult};

/// BTHENUM 根鍵（與 `windows_model::read_bt_ports` 的遞迴路徑一致）。
const BTHENUM_ROOT: &str = "HKLM\\SYSTEM\\CurrentControlSet\\Enum\\BTHENUM";

#[derive(Debug)]
pub struct FakeRegistry {
    serialcomm: Vec<(String, String)>,
    bt_ports: Vec<String>,
    com0com_ports: Mutex<BTreeMap<String, String>>,
    failing: Mutex<bool>,
}

impl FakeRegistry {
    pub fn new(serialcomm: Vec<(&str, &str)>, bt: &[&str]) -> std::sync::Arc<Self> {
        std::sync::Arc::new(Self {
            serialcomm: serialcomm
                .into_iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
            bt_ports: bt.iter().map(|s| s.to_string()).collect(),
            com0com_ports: Mutex::new(BTreeMap::new()),
            failing: Mutex::new(false),
        })
    }

    /// 注入 com0com 參數登錄列舉結果；僅供隔離測試驗證唯讀安裝狀態探測。
    pub fn set_com0com_pairs(&self, pairs: &[(&str, &str)]) {
        let mut ports = self
            .com0com_ports
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        ports.clear();
        for (index, (a, b)) in pairs.iter().enumerate() {
            ports.insert(format!("CNCA{index}"), (*a).to_string());
            ports.insert(format!("CNCB{index}"), (*b).to_string());
        }
    }

    /// 之後所有查詢回 `Err`（模擬登錄不可讀）。
    pub fn fail(&self) {
        *self.failing.lock().unwrap_or_else(|e| e.into_inner()) = true;
    }

    fn check(&self) -> PalResult<()> {
        if *self.failing.lock().unwrap_or_else(|e| e.into_inner()) {
            Err(PalError::plain(PalErrorKind::Io, "fake registry failure"))
        } else {
            Ok(())
        }
    }
}

impl RegistryView for FakeRegistry {
    fn enum_values(&self, key: &str) -> PalResult<Vec<(String, String)>> {
        self.check()?;
        if key == SERIALCOMM_KEY {
            Ok(self.serialcomm.clone())
        } else {
            Ok(Vec::new())
        }
    }

    fn enum_subkeys(&self, key: &str) -> PalResult<Vec<String>> {
        self.check()?;
        if key == BTHENUM_ROOT {
            Ok((0..self.bt_ports.len())
                .map(|i| format!("DEV{i}"))
                .collect())
        } else if key == "HKLM\\SYSTEM\\CurrentControlSet\\Services\\com0com\\Parameters" {
            Ok(self
                .com0com_ports
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .keys()
                .cloned()
                .collect())
        } else {
            Ok(Vec::new())
        }
    }

    fn query_string(&self, key: &str, name: &str) -> PalResult<Option<String>> {
        self.check()?;
        if name == "PortName" {
            let prefix = "HKLM\\SYSTEM\\CurrentControlSet\\Services\\com0com\\Parameters\\";
            if let Some(endpoint) = key.strip_prefix(prefix) {
                return Ok(self
                    .com0com_ports
                    .lock()
                    .unwrap_or_else(|error| error.into_inner())
                    .get(endpoint)
                    .cloned());
            }
        }
        let prefix = format!("{BTHENUM_ROOT}\\DEV");
        let Some(rest) = key.strip_prefix(&prefix) else {
            return Ok(None);
        };
        let index: usize = match rest.split('\\').next().and_then(|s| s.parse().ok()) {
            Some(i) => i,
            None => return Ok(None),
        };
        if name == "PortName" {
            Ok(self.bt_ports.get(index).cloned())
        } else {
            Ok(None)
        }
    }
}
