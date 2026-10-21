//! 裝置來源替身（計畫 Task 1 的 `FakeDeviceSource`，此處為 P5-T5 紅燈用 stub）。
//!
//! 語意（Task 1 規格）：`new(initial)` 的 `initial` 為初始快照；`FakeDeviceScript::set` 之後每輪掃描都回傳該快照，
//! 直到下一次 `set`；`fail_next` 讓下一次 `scan` 回 `Err`。
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use crate::traits::{DeviceInfo, DeviceSource, PalError, PalResult};

type Scripted = PalResult<Vec<(String, DeviceInfo)>>;

/// 替身裝置來源。
#[allow(dead_code)]
pub struct FakeDeviceSource {
    state: Arc<Mutex<(VecDeque<Scripted>, Option<Vec<(String, DeviceInfo)>>)>>,
}

/// 操控 `FakeDeviceSource` 的腳本控制柄。
#[allow(dead_code)]
pub struct FakeDeviceScript {
    state: Arc<Mutex<(VecDeque<Scripted>, Option<Vec<(String, DeviceInfo)>>)>>,
}

impl FakeDeviceSource {
    pub fn new(_initial: Vec<(String, DeviceInfo)>) -> (FakeDeviceSource, FakeDeviceScript) {
        todo!("slice21 T1 FakeDeviceSource::new 尚未實作")
    }
}

impl FakeDeviceScript {
    pub fn set(&self, _snapshot: Vec<(String, DeviceInfo)>) {
        todo!("slice21 T1 FakeDeviceScript::set 尚未實作")
    }

    pub fn fail_next(&self, _err: PalError) {
        todo!("slice21 T1 FakeDeviceScript::fail_next 尚未實作")
    }
}

impl DeviceSource for FakeDeviceSource {
    fn scan(&mut self) -> PalResult<Vec<(String, DeviceInfo)>> {
        todo!("slice21 T1 FakeDeviceSource::scan 尚未實作")
    }
}
