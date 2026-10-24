//! P6 Slice 2.2：ConsoleHub 測試 harness（07 Task 1 簽章；本體由 Task 9 的測試作者補齊）。

use std::sync::Arc;

use sw_core::human_console::hub::{HubHandle, HubStatus};
use sw_core::mailbox::{ControlEvent, DataEvent};
use sw_pal::fakes::clock::FakeClock;
use sw_pal::fakes::console::{FakeConsoleFactory, FakeConsoleRef, FakeIo};

/// 以 `FakeConsoleFactory` 驅動真的 hub thread。
pub struct HubHarness {
    pub hub: HubHandle,
    pub fake: FakeConsoleFactory,
    pub clock: Arc<FakeClock>,
}

impl HubHarness {
    pub fn tcp(_com: &str) -> HubHarness {
        todo!("P6 Task 9")
    }
    pub fn pty(_com: &str) -> HubHarness {
        todo!("P6 Task 9")
    }
    pub fn adopt_pty_with(&self, _io: FakeIo) -> FakeConsoleRef {
        todo!("P6 Task 9")
    }
    /// 回 `try_send` 是否成功。
    pub fn reader_push(&self, _b: &[u8]) -> bool {
        todo!("P6 Task 9")
    }
    /// 等 hub 處理完目前所有 channel 內容（上限 2 s）。
    pub fn settle(&self) {
        todo!("P6 Task 9")
    }
    pub fn control_events(&self) -> Vec<ControlEvent> {
        todo!("P6 Task 9")
    }
    pub fn data_events(&self) -> Vec<DataEvent> {
        todo!("P6 Task 9")
    }
    pub fn status(&self) -> &HubStatus {
        todo!("P6 Task 9")
    }
    pub fn ring_display_bytes(&self, _cid: &str) -> usize {
        todo!("P6 Task 9")
    }
    pub fn block_mailbox(&self, _on: bool) {
        todo!("P6 Task 9")
    }
}
