//! 假單調時鐘。

use crate::traits::Clock;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Duration;

#[derive(Debug, Default)]
pub struct FakeClock {
    pub now_ns: AtomicU64,
    pub sleeps: Mutex<Vec<Duration>>,
}

impl FakeClock {
    pub fn advance(&self, d: std::time::Duration) {
        let ns = d.as_nanos().min(u64::MAX as u128) as u64;
        let _ = self
            .now_ns
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |now| {
                Some(now.saturating_add(ns))
            });
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
