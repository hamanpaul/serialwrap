//! Windows 單調時鐘與睡眠。

use crate::traits::Clock;
use std::sync::OnceLock;
use std::time::Duration;
use windows_sys::Win32::Foundation::FILETIME;
use windows_sys::Win32::System::SystemInformation::GetSystemTimeAsFileTime;
use windows_sys::Win32::System::Time::{
    GetTimeZoneInformation, TIME_ZONE_ID_INVALID, TIME_ZONE_INFORMATION,
};

const TIME_ZONE_ID_DAYLIGHT: u32 = 2;

#[link(name = "kernel32")]
unsafe extern "system" {
    fn QueryPerformanceCounter(counter: *mut i64) -> i32;
    fn QueryPerformanceFrequency(frequency: *mut i64) -> i32;
}

#[derive(Clone, Copy, Debug, Default)]
pub struct NativeClock;

impl Clock for NativeClock {
    fn wall_unix_us(&self) -> i64 {
        let mut file_time = FILETIME::default();
        // SAFETY: `file_time` 指向可寫 FILETIME；API 只填入目前 UTC 時間。
        unsafe { GetSystemTimeAsFileTime(&mut file_time) };
        let ticks =
            (u64::from(file_time.dwHighDateTime) << 32) | u64::from(file_time.dwLowDateTime);
        let unix_ticks = i128::from(ticks) - 116_444_736_000_000_000i128;
        (unix_ticks / 10).clamp(i128::from(i64::MIN), i128::from(i64::MAX)) as i64
    }
    fn local_utc_offset_s(&self) -> i32 {
        let mut timezone = TIME_ZONE_INFORMATION::default();
        // SAFETY: `timezone` 指向可寫結構；API 依目前系統時區填入規則與目前狀態。
        let state = unsafe { GetTimeZoneInformation(&mut timezone) };
        if state == TIME_ZONE_ID_INVALID {
            return 0;
        }
        let seasonal_bias = if state == TIME_ZONE_ID_DAYLIGHT {
            timezone.DaylightBias
        } else {
            timezone.StandardBias
        };
        timezone
            .Bias
            .saturating_add(seasonal_bias)
            .saturating_neg()
            .saturating_mul(60)
    }
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
