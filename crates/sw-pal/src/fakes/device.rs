//! 裝置來源替身（計畫 Task 1 的 `FakeDeviceSource`）。
//!
//! 語意（Task 1 規格）：`new(initial)` 的 `initial` 為初始快照；`FakeDeviceScript::set` 之後每輪掃描都回傳該快照，
//! 直到下一次 `set`；`fail_next` 讓下一次 `scan` 回 `Err`。
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use crate::traits::{DeviceInfo, DeviceSource, PalError, PalResult};

type Scripted = PalResult<Vec<(String, DeviceInfo)>>;

/// 替身裝置來源。
pub struct FakeDeviceSource {
    state: Arc<Mutex<(VecDeque<Scripted>, Option<Vec<(String, DeviceInfo)>>)>>,
}

/// 操控 `FakeDeviceSource` 的腳本控制柄。
pub struct FakeDeviceScript {
    state: Arc<Mutex<(VecDeque<Scripted>, Option<Vec<(String, DeviceInfo)>>)>>,
}

impl FakeDeviceSource {
    pub fn new(initial: Vec<(String, DeviceInfo)>) -> (FakeDeviceSource, FakeDeviceScript) {
        let state = Arc::new(Mutex::new((VecDeque::new(), Some(initial))));
        (
            FakeDeviceSource {
                state: Arc::clone(&state),
            },
            FakeDeviceScript { state },
        )
    }
}

impl FakeDeviceScript {
    pub fn set(&self, snapshot: Vec<(String, DeviceInfo)>) {
        let mut guard = self.state.lock().unwrap_or_else(|e| e.into_inner());
        guard.1 = Some(snapshot);
    }

    pub fn fail_next(&self, err: PalError) {
        let mut guard = self.state.lock().unwrap_or_else(|e| e.into_inner());
        guard.0.push_back(Err(err));
    }
}

impl DeviceSource for FakeDeviceSource {
    fn scan(&mut self) -> PalResult<Vec<(String, DeviceInfo)>> {
        let mut guard = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(res) = guard.0.pop_front() {
            res
        } else if let Some(ref snapshot) = guard.1 {
            Ok(snapshot.clone())
        } else {
            Ok(Vec::new())
        }
    }
}
