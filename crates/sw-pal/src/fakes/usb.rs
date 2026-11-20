//! 假 `UsbTopology`（P7 Task 0）：固定回傳建構時給定的 busid。
use crate::traits::UsbTopology;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct FakeUsb(pub Option<String>);

impl UsbTopology for FakeUsb {
    fn usb_busid(&self, _real_path: &str) -> Option<String> {
        self.0.clone()
    }
}
