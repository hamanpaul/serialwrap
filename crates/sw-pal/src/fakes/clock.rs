//! 假單調時鐘。

use crate::traits::Clock;
use std::sync::atomic::{AtomicI32, AtomicI64, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

#[derive(Debug)]
pub struct FakeClock {
    pub now_ns: AtomicU64,
    pub wall_us: AtomicI64,
    pub local_offset_s: AtomicI32,
    pub sleeps: Mutex<Vec<Duration>>,
    is_threaded: bool,
    wait_mutex: Mutex<()>,
    cond: Condvar,
    subscribers: Mutex<Vec<crossbeam_channel::Sender<()>>>,
}

impl Default for FakeClock {
    fn default() -> Self {
        Self {
            now_ns: AtomicU64::new(0),
            wall_us: AtomicI64::new(0),
            local_offset_s: AtomicI32::new(0),
            sleeps: Mutex::new(Vec::new()),
            is_threaded: false,
            wait_mutex: Mutex::new(()),
            cond: Condvar::new(),
            subscribers: Mutex::new(Vec::new()),
        }
    }
}

impl FakeClock {
    /// 建立共享用的假時鐘（P4-T1 替身介面；回 `Arc`）。
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    pub fn advance(&self, d: Duration) {
        let ns = d.as_nanos().min(u64::MAX as u128) as u64;
        let _ = self
            .now_ns
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |v| {
                Some(v.saturating_add(ns))
            });
        let us = d.as_micros().min(i64::MAX as u128) as i64;
        let _ = self
            .wall_us
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |v| {
                Some(v.saturating_add(us))
            });

        // 喚醒 sleep 中的等待者
        let _guard = self.wait_mutex.lock().unwrap_or_else(|e| e.into_inner());
        self.cond.notify_all();
        drop(_guard);

        // 每次 advance 送一個 ()（容量 1、滿則略過）
        let mut subs = self.subscribers.lock().unwrap_or_else(|e| e.into_inner());
        subs.retain(|tx| {
            !matches!(
                tx.try_send(()),
                Err(crossbeam_channel::TrySendError::Disconnected(()))
            )
        });
    }

    /// 自建立起所有 `sleep` 與 `advance` 的總和（單調時間差）。
    pub fn elapsed(&self) -> Duration {
        Duration::from_nanos(self.now_ns.load(Ordering::SeqCst))
    }

    /// Slice 2.1（threaded 模式）：建立 mono／wall 皆可控的假時鐘。
    pub fn at(mono_ns: u64, wall_unix_us: i64) -> Arc<FakeClock> {
        Arc::new(Self {
            now_ns: AtomicU64::new(mono_ns),
            wall_us: AtomicI64::new(wall_unix_us),
            local_offset_s: AtomicI32::new(0),
            sleeps: Mutex::new(Vec::new()),
            is_threaded: true,
            wait_mutex: Mutex::new(()),
            cond: Condvar::new(),
            subscribers: Mutex::new(Vec::new()),
        })
    }

    /// Slice 2.1：只改 wall 時間，不動 mono（Review Focus 4）。
    pub fn set_wall_us(&self, wall_unix_us: i64) {
        self.wall_us.store(wall_unix_us, Ordering::SeqCst);
    }

    /// Slice 2.1：本地時區偏移（秒）。
    pub fn set_local_offset_s(&self, s: i32) {
        self.local_offset_s.store(s, Ordering::SeqCst);
    }

    /// Slice 2.1：每次 `advance` 送一個 `()`（容量 1，滿則略過）。
    pub fn subscribe(&self) -> crossbeam_channel::Receiver<()> {
        let (tx, rx) = crossbeam_channel::bounded(1);
        self.subscribers
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(tx);
        rx
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
        if self.is_threaded {
            let ns = d.as_nanos().min(u64::MAX as u128) as u64;
            let mut guard = self.wait_mutex.lock().unwrap_or_else(|e| e.into_inner());
            let target = self.now_ns.load(Ordering::SeqCst).saturating_add(ns);
            self.sleeps
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .push(d);
            while self.now_ns.load(Ordering::SeqCst) < target {
                guard = self.cond.wait(guard).unwrap_or_else(|e| e.into_inner());
            }
        } else {
            self.sleeps
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .push(d);
            self.advance(d);
        }
    }
}
