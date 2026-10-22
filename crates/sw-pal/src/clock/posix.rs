//! POSIX 單調時鐘與睡眠。

use crate::traits::Clock;
use std::time::Duration;

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeClock;

impl Clock for NativeClock {
    fn wall_unix_us(&self) -> i64 {
        match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
            Ok(duration) => duration.as_micros().min(i64::MAX as u128) as i64,
            Err(error) => -(error.duration().as_micros().min(i64::MAX as u128) as i64),
        }
    }
    fn local_utc_offset_s(&self) -> i32 {
        // SAFETY: `time` 與 `localtime_r` 使用有效輸出指標；`tm` 僅在呼叫成功後讀取。
        unsafe {
            let now = libc::time(std::ptr::null_mut());
            let mut local = std::mem::MaybeUninit::<libc::tm>::uninit();
            if libc::localtime_r(&now, local.as_mut_ptr()).is_null() {
                return 0;
            }
            let offset = local.assume_init().tm_gmtoff;
            offset.clamp(i64::from(i32::MIN), i64::from(i32::MAX)) as i32
        }
    }
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
