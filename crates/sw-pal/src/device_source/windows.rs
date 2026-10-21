//! Windows 裝置來源（arch §4.3，P5-T5）。
//!
//! 平台無關：登錄存取經 `RegistryView`、時間經 `Clock`，故 Linux 上也能以替身單元測試。
//!
//! 【TDD 紅燈 stub】本檔目前只有介面骨架，實作留給 Task 5 Step 3。
use std::collections::HashSet;
use std::sync::Arc;

use crate::device_source::windows_model::RegistryView;
use crate::traits::{Clock, DeviceInfo, DeviceSource, PalResult};

/// SERIALCOMM＋藍牙雙重排除的裝置來源（stub）。
#[allow(dead_code)]
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
        _view: Box<dyn RegistryView>,
        _clock: Arc<dyn Clock>,
        _exclude: HashSet<String>,
        _allowlist: Option<Vec<String>>,
    ) -> Self {
        todo!("slice21 T5 WindowsDeviceSource::new 尚未實作")
    }
}

impl DeviceSource for WindowsDeviceSource {
    fn scan(&mut self) -> PalResult<Vec<(String, DeviceInfo)>> {
        todo!("slice21 T5 WindowsDeviceSource::scan 尚未實作")
    }
}
