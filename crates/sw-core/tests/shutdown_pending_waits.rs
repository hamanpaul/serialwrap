#![cfg(feature = "test-fakes")]

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;

use crossbeam_channel::{Receiver, Sender, bounded};
use sw_core::uart_bridge::{RxShared, WaitOutcome};
use sw_pal::fakes::clock::FakeClock;
use sw_pal::traits::Clock;
use sw_proto::pyre::{PyFlags, compile};

struct WaitNotifyingClock {
    inner: Arc<FakeClock>,
    wait_started: Sender<()>,
}

impl Clock for WaitNotifyingClock {
    fn mono_ns(&self) -> u64 {
        self.inner.mono_ns()
    }

    fn sleep(&self, duration: Duration) {
        self.inner.sleep(duration);
    }

    fn wait_until_or_cancel(&self, deadline_ns: u64, cancel: &Receiver<()>) -> bool {
        let _ = self.wait_started.try_send(());
        self.inner.wait_until_or_cancel(deadline_ns, cancel)
    }

    fn wall_unix_us(&self) -> i64 {
        self.inner.wall_unix_us()
    }

    fn local_utc_offset_s(&self) -> i32 {
        self.inner.local_utc_offset_s()
    }
}

fn notifying_clock() -> (Arc<dyn Clock>, Arc<FakeClock>, Receiver<()>) {
    let fake = FakeClock::at(1, 0);
    let (wait_started_tx, wait_started_rx) = bounded(1);
    let clock: Arc<dyn Clock> = Arc::new(WaitNotifyingClock {
        inner: Arc::clone(&fake),
        wait_started: wait_started_tx,
    });
    (clock, fake, wait_started_rx)
}

#[test]
fn shutdown_cancels_existing_rx_wait_without_clock_advance() {
    let rx = RxShared::new(64, 0);
    let regex = compile("ready", PyFlags::default()).unwrap();
    let (clock, fake_clock, wait_started_rx) = notifying_clock();
    let (result_tx, result_rx) = bounded(1);
    let waiting_rx = Arc::clone(&rx);
    let waiting_clock = Arc::clone(&clock);
    let waiter = thread::spawn(move || {
        let outcome = waiting_rx.wait_for_regex_from_shutdown_cancellable(
            &regex,
            0,
            Duration::from_secs(60),
            &*waiting_clock,
        );
        result_tx.send(outcome).expect("回傳等待結果");
    });

    wait_started_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("等待已進入 Clock 阻塞路徑");
    let mono_before_shutdown = fake_clock.mono_ns();
    rx.cancel_shutdown_waits();

    let immediate = result_rx.recv_timeout(Duration::from_millis(250)).ok();
    if immediate.is_none() {
        // 若取消沒有生效，推進假時鐘讓執行緒收斂後再檢查結果，避免留下測試執行緒。
        fake_clock.advance(Duration::from_secs(60));
    }
    let outcome = immediate.or_else(|| result_rx.recv_timeout(Duration::from_secs(1)).ok());
    waiter.join().expect("等待執行緒應正常結束");

    assert!(immediate.is_some(), "Shutdown 取消不得等待 FakeClock 前進");
    assert_eq!(outcome, Some(WaitOutcome::Cancelled));
    assert_eq!(fake_clock.mono_ns(), mono_before_shutdown);
}

#[test]
fn rx_wait_started_after_shutdown_is_already_cancelled() {
    let rx = RxShared::new(64, 0);
    let regex = compile("ready", PyFlags::default()).unwrap();
    let (clock, fake_clock, _wait_started_rx) = notifying_clock();
    let (call_started_tx, call_started_rx) = bounded(1);
    let (result_tx, result_rx) = bounded(1);
    rx.cancel_shutdown_waits();

    let waiting_rx = Arc::clone(&rx);
    let waiting_clock = Arc::clone(&clock);
    let waiter = thread::spawn(move || {
        call_started_tx.send(()).expect("等待呼叫已開始");
        let outcome = waiting_rx.wait_for_regex_from_shutdown_cancellable(
            &regex,
            0,
            Duration::from_secs(60),
            &*waiting_clock,
        );
        result_tx.send(outcome).expect("回傳等待結果");
    });

    call_started_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("等待執行緒已開始呼叫介面");
    let immediate = result_rx.recv_timeout(Duration::from_millis(250)).ok();
    if immediate.is_none() {
        fake_clock.advance(Duration::from_secs(60));
    }
    let outcome = immediate.or_else(|| result_rx.recv_timeout(Duration::from_secs(1)).ok());
    waiter.join().expect("等待執行緒應正常結束");

    assert!(immediate.is_some(), "Shutdown 後才開始的等待應立即取消");
    assert_eq!(outcome, Some(WaitOutcome::Cancelled));
}

#[test]
fn shutdown_cancel_does_not_cancel_ordinary_rx_wait() {
    let rx = RxShared::new(64, 0);
    let (clock, fake_clock, _wait_started_rx) = notifying_clock();
    let (search_started_tx, search_started_rx) = bounded(1);
    let (release_search_tx, release_search_rx) = bounded(1);
    let (result_tx, result_rx) = bounded(1);
    let first_search = Arc::new(AtomicBool::new(false));
    let first_search_in_waiter = Arc::clone(&first_search);
    let waiting_rx = Arc::clone(&rx);
    let waiting_clock = Arc::clone(&clock);
    let waiter = thread::spawn(move || {
        let outcome = waiting_rx.wait_for_regex_from_with(
            0,
            Duration::from_secs(60),
            &*waiting_clock,
            move |text| {
                if !first_search_in_waiter.swap(true, Ordering::AcqRel) {
                    search_started_tx.send(()).expect("RX 比對已開始");
                    release_search_rx
                        .recv_timeout(Duration::from_secs(1))
                        .expect("測試釋放第一次比對");
                }
                text.contains("ready")
            },
        );
        result_tx.send(outcome).expect("回傳一般 RX 等待結果");
    });

    search_started_rx
        .recv_timeout(Duration::from_secs(1))
        .expect("一般 RX 等待已進入第一次比對");
    let mono_before_shutdown = fake_clock.mono_ns();
    rx.cancel_shutdown_waits();
    release_search_tx.send(()).expect("釋放第一次比對");

    let completed_before_match = result_rx.recv_timeout(Duration::from_millis(100));
    rx.append(b"ready", 2, 3, 1);
    let completed_early = completed_before_match.is_ok();
    let outcome = match completed_before_match {
        Ok(outcome) => outcome,
        Err(_) => result_rx
            .recv_timeout(Duration::from_secs(1))
            .expect("append 後一般 RX 等待應命中"),
    };
    waiter.join().expect("一般 RX 等待執行緒應正常結束");

    assert!(!completed_early, "一般 RX 等待不得因 Shutdown 取消而結束");
    assert_eq!(
        outcome.map(|matched| (matched.epoch, matched.rx_seq)),
        Some((3, 1))
    );
    assert_eq!(fake_clock.mono_ns(), mono_before_shutdown);
}
