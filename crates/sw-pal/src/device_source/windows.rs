//! Windows 裝置來源（arch §4.3，P5-T5）。
//!
//! 平台無關：登錄存取經 `RegistryView`、時間經 `Clock`，故 Linux 上也能以替身單元測試。
use std::collections::HashSet;
use std::sync::Arc;

use crate::device_source::windows_model::{
    exclude_bluetooth, read_bt_ports, RegistryView, SERIALCOMM_KEY,
};
use crate::traits::{Clock, DeviceInfo, DeviceSource, PalErrorKind, PalResult};

const BT_CACHE_TTL_NS: u64 = 5_000_000_000;

/// SERIALCOMM＋藍牙雙重排除的裝置來源。
pub struct WindowsDeviceSource {
    view: Box<dyn RegistryView>,
    clock: Arc<dyn Clock>,
    exclude: HashSet<String>,
    allowlist: Option<Vec<String>>,
    /// BTHENUM 藍牙埠快取（時間戳 mono_ns、埠集合），TTL 5 s。
    bt_cache: Option<(u64, HashSet<String>)>,
}

impl WindowsDeviceSource {
    pub fn new(
        view: Box<dyn RegistryView>,
        clock: Arc<dyn Clock>,
        exclude: HashSet<String>,
        allowlist: Option<Vec<String>>,
    ) -> Self {
        Self {
            view,
            clock,
            exclude,
            allowlist,
            bt_cache: None,
        }
    }
}

impl DeviceSource for WindowsDeviceSource {
    fn scan(&mut self) -> PalResult<Vec<(String, DeviceInfo)>> {
        let serialcomm = match self.view.enum_values(SERIALCOMM_KEY) {
            Ok(values) => values,
            Err(error) if error.kind == PalErrorKind::NotFound => Vec::new(),
            Err(error) => return Err(error),
        };

        let now = self.clock.mono_ns();
        let refresh = self
            .bt_cache
            .as_ref()
            .is_none_or(|(cached_at, _)| now.saturating_sub(*cached_at) >= BT_CACHE_TTL_NS);
        if refresh {
            self.bt_cache = Some((now, read_bt_ports(self.view.as_ref())));
        }

        let bt_ports = &self
            .bt_cache
            .as_ref()
            .expect("BTHENUM cache is initialized before use")
            .1;
        let kept = exclude_bluetooth(&serialcomm, bt_ports, &self.exclude);
        let mut devices = Vec::with_capacity(kept.len());
        for com in kept {
            if self
                .allowlist
                .as_ref()
                .is_some_and(|allowed| !allowed.iter().any(|item| item == &com))
            {
                continue;
            }

            devices.push((
                com.clone(),
                DeviceInfo {
                    by_id: com.clone(),
                    real_path: format!(r"\\.\{com}"),
                },
            ));
        }
        Ok(devices)
    }
}
