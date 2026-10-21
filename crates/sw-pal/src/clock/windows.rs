//! Windows 單調時鐘與睡眠。

use crate::traits::Clock;
use std::sync::OnceLock;
use std::time::Duration;

#[link(name = "kernel32")]
unsafe extern "system" {
    fn QueryPerformanceCounter(counter: *mut i64) -> i32;
    fn QueryPerformanceFrequency(frequency: *mut i64) -> i32;
}

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeClock;

impl Clock for NativeClock {
    fn mono_ns(&self) -> u64 {
        static FREQUENCY: OnceLock<i64> = OnceLock::new();
        let frequency = *FREQUENCY.get_or_init(|| {
            let mut value = 0i64;
            // SAFETY: value 指向可寫 i64，QueryPerformanceFrequency 只寫入頻率。
            if unsafe { QueryPerformanceFrequency(&mut value) } == 0 || value <= 0 {
                1
            } else {
                value
            }
        });
        let mut counter = 0i64;
        // SAFETY: counter 指向可寫 i64，QueryPerformanceCounter 只寫入計數值。
        if unsafe { QueryPerformanceCounter(&mut counter) } == 0 || counter < 0 {
            return 0;
        }
        (counter as u128)
            .saturating_mul(1_000_000_000)
            .checked_div(frequency as u128)
            .unwrap_or(0)
            .min(u64::MAX as u128) as u64
    }

    fn sleep(&self, duration: Duration) {
        std::thread::sleep(duration);
    }
}
