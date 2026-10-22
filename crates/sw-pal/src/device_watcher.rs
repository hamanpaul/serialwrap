//! DeviceWatcher（arch §4.3，P5-T5）。thread 名 `serialwrap-device-watcher`。
//!
//! 【TDD 紅燈 stub】本檔目前只有介面骨架，實作留給 Task 5 Step 3。
use std::sync::Arc;
use std::time::Duration;

use crossbeam_channel::Sender;

use crate::traits::{Clock, DeviceInfo, DeviceSource, PalResult, WatcherEvent};

/// 裝置輪詢 watcher（arch §4.3）。
pub struct DeviceWatcher;

/// watcher thread 的控制柄（stub）。
pub struct WatcherHandle;

impl DeviceWatcher {
    /// 呼叫端必須先做一次 `initial_scan()` 並完成 rank 後才 `start`（DF-14）。
    pub fn start(
        _src: Box<dyn DeviceSource>,
        _poll: Duration,
        _tx: Sender<WatcherEvent>,
        _clock: Arc<dyn Clock>,
    ) -> WatcherHandle {
        s21_todo!("T5", "DeviceWatcher::start")
    }

    pub fn initial_scan(_src: &mut dyn DeviceSource) -> PalResult<Vec<(String, DeviceInfo)>> {
        s21_todo!("T5", "DeviceWatcher::initial_scan")
    }

    /// 差異演算法照 07 §1.4：`added` 先 sorted(cur−prev)、`changed` 的 cur 接在後；`removed` 先 sorted(prev−cur)、`changed` 的 prev 接在後。
    pub fn diff(
        _prev: &[(String, DeviceInfo)],
        _cur: &[(String, DeviceInfo)],
    ) -> (Vec<DeviceInfo>, Vec<DeviceInfo>) {
        s21_todo!("T5", "DeviceWatcher::diff")
    }
}

impl WatcherHandle {
    pub fn stop(self) {
        s21_todo!("T5", "WatcherHandle::stop")
    }
}
