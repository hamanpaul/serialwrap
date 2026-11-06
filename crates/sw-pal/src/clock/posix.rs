//! POSIX 單調時鐘與睡眠。

use crate::traits::Clock;
use std::time::Duration;

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeClock;

impl Clock for NativeClock {
    fn mono_ns(&self) -> u64 {
        let mut ts = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        // SAFETY: `ts` 是可寫的有效 timespec，CLOCK_MONOTONIC 不需額外指標。
        let result = unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
        if result != 0 {
            return 0;
        }
        (ts.tv_sec as u64)
            .saturating_mul(1_000_000_000)
            .saturating_add(ts.tv_nsec as u64)
    }

    fn sleep(&self, duration: Duration) {
        std::thread::sleep(duration);
    }
}
