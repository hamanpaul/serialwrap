#![cfg(feature = "test-fakes")]

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use crossbeam_channel::{bounded, Receiver, Sender};
mod support;
use support::ActorRig;
use sw_core::mailbox::ControlEvent;
use sw_core::session_core::SessionRequest;
use sw_core::uart_bridge::{RxShared, WaitOutcome};
use sw_pal::fakes::clock::FakeClock;
use sw_pal::traits::Clock;
use sw_proto::pyre::{compile, PyFlags};

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

const SELF_TEST_PROFILE: &str = "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    ready_probe: 'echo __READY__${nonce}'\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: t, act_no: 1, alias: t+1, device_by_id: /s/by-id/usb-A}\n";

fn wait_for_op_finished(rig: &ActorRig, before: usize) {
    let deadline = std::time::Instant::now() + Duration::from_millis(400);
    while rig.actor_probe().entries("OpFinished").len() == before
        && std::time::Instant::now() < deadline
    {
        thread::sleep(Duration::from_millis(2));
    }
    assert!(
        rig.actor_probe().entries("OpFinished").len() > before,
        "OpRunner 應以 OpFinished 收尾"
    );
}

fn assert_shutdown_cancels_self_test_wait(prompt_wait: bool) {
    let rig = ActorRig::new(SELF_TEST_PROFILE).ready();
    rig.dev.clear_replies();
    let tx_start = rig.dev.tx_len();
    let wait_start = rig.rx.regex_wait_starts_for_test();
    let op_finished_before = rig.actor_probe().entries("OpFinished").len();
    let self_test = rig.request_async(SessionRequest::SelfTest {
        timeout_s: 60.0,
        strict_human_lock: false,
    });

    rig.wait_for("self-test nonce probe TX", |actor| {
        actor.dev.tx_len() > tx_start
    });
    let probe = String::from_utf8_lossy(&rig.dev.tx_since(tx_start)).into_owned();
    let nonce = probe
        .split("__READY__")
        .nth(1)
        .expect("self-test probe 應帶 nonce")
        .chars()
        .take_while(char::is_ascii_hexdigit)
        .collect::<String>();
    assert_eq!(nonce.len(), 8, "self-test nonce 應為 8 位十六進位");

    rig.wait_for("self-test nonce regex wait", |actor| {
        actor.rx.regex_wait_starts_for_test() > wait_start
    });
    if prompt_wait {
        let marker_wait_count = rig.rx.regex_wait_starts_for_test();
        rig.inject_rx(format!("__SW_ST_{nonce}_OK__\r\n").as_bytes());
        rig.wait_for("self-test prompt regex wait", |actor| {
            actor.rx.regex_wait_starts_for_test() > marker_wait_count
        });
    }

    // Worker close 暫停在回條前，讓測試能確認 OpRunner 已因 Shutdown 取消等待並送出 OpFinished。
    let tx_at_shutdown = rig.dev.tx_len();
    rig.dev.pause_next_close();
    let (shutdown_tx, shutdown_rx) = bounded(1);
    rig.send_control(ControlEvent::Shutdown { reply: shutdown_tx });
    let self_test_reply = self_test.recv_timeout(Duration::from_millis(400)).ok();
    let close_paused = rig.dev.wait_paused(Duration::from_millis(400));

    wait_for_op_finished(&rig, op_finished_before);
    rig.dev.release_pause();
    let shutdown_reply = shutdown_rx.recv_timeout(Duration::from_secs(2)).ok();

    let flushed = self_test_reply
        .as_ref()
        .and_then(|result| result.as_ref().ok())
        .is_some_and(|dict| {
            dict.get("ok").and_then(sw_proto::pyjson::PyValue::as_bool) == Some(false)
                && dict
                    .get("error_code")
                    .and_then(sw_proto::pyjson::PyValue::as_str)
                    == Some("FLUSHED_BY_SHUTDOWN")
        });
    let shutdown_completed = shutdown_reply
        .as_ref()
        .and_then(|result| result.as_ref().ok())
        .is_some_and(|dict| {
            dict.get("ok").and_then(sw_proto::pyjson::PyValue::as_bool) == Some(true)
        });

    assert!(close_paused, "停機 drain 應在 WorkerStopped 回條前暫停");
    assert!(flushed, "停機時 self-test 應立即回 FLUSHED_BY_SHUTDOWN");
    assert_eq!(
        rig.dev.tx_len(),
        tx_at_shutdown,
        "Fence 凍結後 self-test 不得再送 TX"
    );
    assert!(shutdown_completed, "釋放 fake close 後 Shutdown 應收斂");
}

#[test]
fn shutdown_cancels_self_test_nonce_and_prompt_waits() {
    assert_shutdown_cancels_self_test_wait(false);
    assert_shutdown_cancels_self_test_wait(true);
}

#[test]
fn shutdown_cancels_verified_self_test_waits() {
    let rig = ActorRig::new(SELF_TEST_PROFILE).ready();
    rig.dev.clear_replies();
    rig.dev.script_write(vec![
        sw_pal::fakes::serial::FakeWriteStep::PauseBeforeSubmit,
    ]);
    let tx_before = rig.dev.tx_len();
    let clock_before = rig.clock.elapsed();
    let op_finished_before = rig.actor_probe().entries("OpFinished").len();
    let self_test = rig.request_async(SessionRequest::SelfTest {
        timeout_s: 60.0,
        strict_human_lock: false,
    });
    assert!(
        rig.dev.wait_paused(Duration::from_secs(2)),
        "TX 應停在提交前"
    );

    rig.dev.pause_next_close();
    let (shutdown_tx, shutdown_rx) = bounded(1);
    rig.send_control(ControlEvent::Shutdown { reply: shutdown_tx });
    let flushed = self_test
        .recv_timeout(Duration::from_millis(400))
        .expect("Phase 1 不得等待 runner 或 FakeClock")
        .expect("self-test 回覆應為字典");
    assert_eq!(
        flushed["error_code"],
        sw_proto::pyjson::PyValue::from("FLUSHED_BY_SHUTDOWN")
    );
    assert_eq!(
        rig.clock.elapsed(),
        clock_before,
        "Shutdown 測試不得推進 FakeClock"
    );
    assert_eq!(rig.dev.tx_len(), tx_before, "提交前停機不得新增 UART TX");
    assert_eq!(rig.fence.with(|fence| fence.self_test_probe_owner), None);

    rig.dev.release_pause();
    assert!(
        rig.dev.wait_paused(Duration::from_millis(400)),
        "runner 收尾後應進入 WorkerStopped 前的 close 暫停"
    );
    let deadline = std::time::Instant::now() + Duration::from_millis(400);
    while rig.actor_probe().entries("OpFinished").len() == op_finished_before
        && std::time::Instant::now() < deadline
    {
        thread::sleep(Duration::from_millis(2));
    }
    assert!(
        rig.actor_probe().entries("OpFinished").len() > op_finished_before,
        "取消後 runner 應收尾並回報 OpFinished"
    );
    assert!(
        self_test.try_recv().is_err(),
        "late OpFinished 不得重覆回覆"
    );
    rig.dev.release_pause();
    let shutdown = shutdown_rx
        .recv_timeout(Duration::from_secs(2))
        .expect("Shutdown 應於 close 後收斂")
        .expect("Shutdown 回覆應為字典");
    assert_eq!(shutdown["ok"], sw_proto::pyjson::PyValue::Bool(true));
    assert_eq!(rig.dev.tx_len(), tx_before, "凍結後不得回放或追加 TX");
}

#[test]
fn shutdown_discards_suspended_self_test_human_bytes() {
    let rig = ActorRig::new_with_hub(SELF_TEST_PROFILE).ready();
    let console = rig.attach_pty_console("i306-human");
    let consoles = rig.request(SessionRequest::ConsoleList);
    let rows = match consoles.get("consoles") {
        Some(sw_proto::pyjson::PyValue::List(rows)) => rows,
        other => panic!("console_list 應含 console 清單：{other:?}"),
    };
    assert!(
        rows.iter().any(|row| match row {
            sw_proto::pyjson::PyValue::Dict(console) => {
                console.get("client_id") == Some(&sw_proto::pyjson::PyValue::from("i306-human"))
                    && console.get("interactive_owner")
                        == Some(&sw_proto::pyjson::PyValue::Bool(true))
            }
            _ => false,
        }),
        "測試 console 必須先取得 raw interactive ownership"
    );
    rig.dev.clear_replies();
    let tx_start = rig.dev.tx_len();
    let wait_start = rig.rx.regex_wait_starts_for_test();
    let self_test = rig.request_async(SessionRequest::SelfTest {
        timeout_s: 60.0,
        strict_human_lock: false,
    });
    rig.wait_for("self-test probe TX", |actor| actor.dev.tx_len() > tx_start);
    rig.wait_for("execution marker wait", |actor| {
        actor.rx.regex_wait_starts_for_test() > wait_start
    });
    console.push_input(b"k");
    thread::sleep(Duration::from_millis(50));
    let tx_at_shutdown = rig.dev.tx_len();
    rig.dev.pause_next_close();
    let (shutdown_tx, shutdown_rx) = bounded(1);
    rig.send_control(ControlEvent::Shutdown { reply: shutdown_tx });
    let flushed = self_test
        .recv_timeout(Duration::from_millis(400))
        .expect("Shutdown Phase 1 應立即回覆")
        .expect("self-test 回覆應為字典");
    assert_eq!(
        flushed["error_code"],
        sw_proto::pyjson::PyValue::from("FLUSHED_BY_SHUTDOWN")
    );
    assert!(
        rig.dev.wait_paused(Duration::from_millis(400)),
        "Shutdown drain 應在 WorkerStopped 前等待 close"
    );
    assert_eq!(
        rig.dev.tx_len(),
        tx_at_shutdown,
        "停機不可回放 suspended human bytes"
    );
    assert!(!rig.dev.tx_since(tx_start).contains(&b'k'));
    assert!(
        self_test.try_recv().is_err(),
        "late runner result 不得重覆回覆"
    );
    rig.dev.release_pause();
    let shutdown = shutdown_rx
        .recv_timeout(Duration::from_secs(2))
        .unwrap()
        .unwrap();
    assert_eq!(shutdown["ok"], sw_proto::pyjson::PyValue::Bool(true));
    assert_eq!(rig.dev.tx_len(), tx_at_shutdown);
}

#[test]
fn shutdown_cancels_ready_recover_wait_without_second_key() {
    let rig = ActorRig::new(SELF_TEST_PROFILE).ready();
    rig.dev.clear_replies();
    let tx_start = rig.dev.tx_len();
    let wait_start = rig.rx.regex_wait_starts_for_test();
    let recover = rig.request_async(SessionRequest::Recover {
        timeout_s: 60.0,
        force: false,
    });

    rig.wait_for("READY recover 第一個救援鍵", |actor| {
        actor.dev.tx_len() > tx_start
    });
    assert_eq!(
        rig.dev.tx_since(tx_start),
        b"\x03",
        "READY recover 第一鍵應為 Ctrl-C"
    );
    rig.wait_for("READY recover prompt 等待", |actor| {
        actor.rx.regex_wait_starts_for_test() > wait_start
    });

    // 關閉動作暫停於回條前：此時可觀察 Phase 1 的 RPC 回覆與 OpRunner 等待是否已收斂。
    let tx_at_shutdown = rig.dev.tx_len();
    let op_finished_before = rig.actor_probe().entries("OpFinished").len();
    rig.dev.pause_next_close();
    let (shutdown_tx, shutdown_rx) = bounded(1);
    rig.send_control(ControlEvent::Shutdown { reply: shutdown_tx });
    let recover_reply = recover.recv_timeout(Duration::from_millis(400)).ok();
    let close_paused = rig.dev.wait_paused(Duration::from_millis(400));

    wait_for_op_finished(&rig, op_finished_before);
    rig.dev.release_pause();
    let shutdown_reply = shutdown_rx.recv_timeout(Duration::from_secs(2)).ok();

    let flushed = recover_reply
        .as_ref()
        .and_then(|result| result.as_ref().ok())
        .is_some_and(|dict| {
            dict.get("ok").and_then(sw_proto::pyjson::PyValue::as_bool) == Some(false)
                && dict
                    .get("error_code")
                    .and_then(sw_proto::pyjson::PyValue::as_str)
                    == Some("FLUSHED_BY_SHUTDOWN")
        });
    let shutdown_completed = shutdown_reply
        .as_ref()
        .and_then(|result| result.as_ref().ok())
        .is_some_and(|dict| {
            dict.get("ok").and_then(sw_proto::pyjson::PyValue::as_bool) == Some(true)
        });

    assert!(close_paused, "Shutdown drain 應在 WorkerStopped 回條前暫停");
    assert!(
        flushed,
        "READY recover Phase 1 應立即回 FLUSHED_BY_SHUTDOWN"
    );
    assert_eq!(
        rig.dev.tx_len(),
        tx_at_shutdown,
        "停機取消後不得再送 Ctrl-D 或其他救援鍵"
    );
    assert!(shutdown_completed, "釋放 fake close 後 Shutdown 應收斂");
}

#[test]
fn shutdown_wait_times_out_before_matching_expired_buffered_rx() {
    let rx = RxShared::new(64, 0);
    rx.append(b"ready", 0, 1, 1);
    let regex = compile("ready", PyFlags::default()).unwrap();
    let clock = FakeClock::new();

    assert_eq!(
        rx.wait_for_regex_from_shutdown_cancellable(&regex, 0, Duration::ZERO, &*clock,),
        WaitOutcome::TimedOut,
        "deadline 已到時不得再以已緩衝 RX 命中成功"
    );
}

#[test]
fn self_test_negative_timeout_ignores_immediate_nonce_reply() {
    let rig = ActorRig::new(SELF_TEST_PROFILE).ready();
    let result = rig
        .request_async(SessionRequest::SelfTest {
            timeout_s: -1.0,
            strict_human_lock: false,
        })
        .recv_timeout(Duration::from_secs(2))
        .expect("self-test 應完成")
        .expect("self-test 應回 RPC dict");

    assert_eq!(
        result
            .get("classification")
            .and_then(sw_proto::pyjson::PyValue::as_str),
        Some("TARGET_UNRESPONSIVE"),
        "負 timeout 應沿用逾期語意：{result:?}"
    );
    assert_eq!(
        result.get("probe_ok"),
        Some(&sw_proto::pyjson::PyValue::Bool(false))
    );
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
