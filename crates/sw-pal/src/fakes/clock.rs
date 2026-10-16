//! 假 `Clock`（P3-T9 骨架：欄位與方法於 P3-T9 補上）。

pub struct FakeClock;

impl FakeClock {
    pub fn advance(&self, d: std::time::Duration) {
        todo!("P3-T9: FakeClock::advance")
    }
    pub fn sleeps_s(&self) -> Vec<f64> {
        todo!("P3-T9: FakeClock::sleeps_s")
    }
}
