//! 假單調時鐘。

use crate::traits::Clock;
use std::sync::atomic::{AtomicI32, AtomicI64, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Duration;

#[derive(Debug, Default)]
pub struct FakeClock {
    pub now_ns: AtomicU64,
    pub wall_us: AtomicI64,
    pub local_offset_s: AtomicI32,
    pub sleeps: Mutex<Vec<Duration>>,
}

impl FakeClock {
    /// 建立共享用的假時鐘（P4-T1 替身介面；回 `Arc`）。
    pub fn new() -> std::sync::Arc<Self> {
        std::sync::Arc::new(Self::default())
    }

    pub fn advance(&self, d: std::time::Duration) {
        let ns = d.as_nanos().min(u64::MAX as u128) as u64;
        let _ = self
            .now_ns
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |now| {
                Some(now.saturating_add(ns))
            });
    }
    /// 自建立起所有 `sleep` 與 `advance` 的總和（單調時間差）。
    pub fn elapsed(&self) -> std::time::Duration {
        std::time::Duration::from_nanos(self.now_ns.load(Ordering::SeqCst))
    }

    /// Slice 2.1（threaded 模式）：建立 mono／wall 皆可控的假時鐘（骨架，Task 1 實作）。
    pub fn at(_mono_ns: u64, _wall_unix_us: i64) -> std::sync::Arc<FakeClock> {
        s21_todo!("T1", "FakeClock::at")
    }

    /// Slice 2.1：只改 wall 時間，不動 mono（Review Focus 4）。
    pub fn set_wall_us(&self, _wall_unix_us: i64) {
        s21_todo!("T1", "FakeClock::set_wall_us")
    }

    /// Slice 2.1：本地時區偏移（秒）。
    pub fn set_local_offset_s(&self, _s: i32) {
        s21_todo!("T1", "FakeClock::set_local_offset_s")
    }

    /// Slice 2.1：每次 `advance` 送一個 `()`（容量 1，滿則略過）。
    pub fn subscribe(&self) -> crossbeam_channel::Receiver<()> {
        s21_todo!("T1", "FakeClock::subscribe")
    }

    pub fn sleeps_s(&self) -> Vec<f64> {
        self.sleeps
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .iter()
            .map(Duration::as_secs_f64)
            .collect()
    }
}

impl Clock for FakeClock {
    fn wall_unix_us(&self) -> i64 {
        self.wall_us.load(Ordering::SeqCst)
    }
    fn local_utc_offset_s(&self) -> i32 {
        self.local_offset_s.load(Ordering::SeqCst)
    }
    fn mono_ns(&self) -> u64 {
        self.now_ns.load(Ordering::SeqCst)
    }

    fn sleep(&self, d: Duration) {
        self.sleeps
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(d);
        self.advance(d);
    }
}
