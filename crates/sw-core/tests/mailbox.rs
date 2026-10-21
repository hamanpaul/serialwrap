//! Slice 2.1 Task 11：有界 `Mailbox`（control 優先、依賴 RX 的 stash、世代隔離）與 `spawn_actor` 的紅燈測試。
//!
//! 紅燈階段：`Mailbox::new`／`spawn_actor` 等尚未實作，失敗應為 `not implemented: slice21 T11 …`。

#![cfg(feature = "test-fakes")]

mod support;

use std::sync::Arc;
use std::time::{Duration, Instant};

use crossbeam_channel::TrySendError;
use sw_core::actor::spawn_actor;
use sw_core::login_fsm::LoginOutcome;
use sw_core::mailbox::{ControlEvent, DataEvent, Mailbox, MailboxItem};
use sw_core::session_core::SessionRequest;
use sw_pal::fakes::clock::FakeClock;
use sw_pal::traits::Clock;
use support::*;

fn get_state_req() -> ControlEvent {
    ControlEvent::Request {
        req: SessionRequest::GetState,
        reply: crossbeam_channel::bounded(1).0,
    }
}

fn release_req() -> ControlEvent {
    ControlEvent::Request {
        req: SessionRequest::Release {
            source: "agent".into(),
            reason: sw_proto::pyjson::PyValue::Null,
        },
        reply: crossbeam_channel::bounded(1).0,
    }
}

fn rx_chunk(epoch: u64, rx_seq: u64) -> DataEvent {
    DataEvent::UartRxChunk {
        epoch,
        rx_seq,
        wal_seq: rx_seq,
        bytes: vec![b'x'; 4],
    }
}

/// `after_rx_seq` 依賴的登入結果（同 epoch、fsm_gen 1）。
fn login_after(epoch: u64, after: u64) -> ControlEvent {
    ControlEvent::LoginFinished {
        epoch,
        fsm_gen: 1,
        result: LoginOutcome::Ready,
        after_rx_seq: Some(after),
    }
}

fn data_seq(item: &MailboxItem) -> Option<u64> {
    match item {
        MailboxItem::Data(DataEvent::UartRxChunk { rx_seq, .. }) => Some(*rx_seq),
        _ => None,
    }
}

#[test]
fn control_first_when_independent() {
    let mb = Mailbox::new(256, 1024);
    let clock = FakeClock::at(0, 0);
    let dtx = mb.data_sender();
    for i in 1..=1024u64 {
        dtx.try_send(rx_chunk(1, i)).expect("data lane 容量 1024");
    }
    mb.control_sender().send(get_state_req()).expect("control 送出");
    let mut mb = mb;
    let first = mb
        .recv_next((1, 0), Some(1), None, &*clock)
        .expect("應回傳一個項目");
    assert!(
        matches!(first, MailboxItem::Control(ControlEvent::Request { .. })),
        "獨立的 control 應先於已排好的 data"
    );
}

#[test]
fn control_waits_for_dependent_rx() {
    // LoginFinished{after_rx_seq: 3} 必須等 rx_seq 3 處理完才釋放：Data(1)、Data(2)、Data(3)、Control、Data(4)。
    let mut mb = Mailbox::new(256, 1024);
    let clock = FakeClock::at(0, 0);
    mb.control_sender().send(login_after(1, 3)).expect("control 送出");
    let dtx = mb.data_sender();
    for i in 1..=5 {
        dtx.send(rx_chunk(1, i)).expect("data 送出");
    }
    let mut processed = (1u64, 0u64);
    let mut order: Vec<String> = Vec::new();
    for _ in 0..5 {
        let item = mb
            .recv_next(processed, Some(1), None, &*clock)
            .expect("項目");
        if let Some(seq) = data_seq(&item) {
            processed = (1, seq);
            order.push(format!("D{seq}"));
        } else {
            order.push("C".to_string());
        }
    }
    assert_eq!(order, ["D1", "D2", "D3", "C", "D4"]);
}

#[test]
fn independent_control_served_while_dependent_stashed() {
    // 依賴 rx_seq 9 的 login 進入 stash 後，獨立的 Release 不得等待 rx_seq 9。
    let mut mb = Mailbox::new(256, 1024);
    let clock = FakeClock::at(0, 0);
    mb.control_sender().send(login_after(1, 9)).expect("control 送出");
    let dtx = mb.data_sender();
    for i in 1..=3 {
        dtx.send(rx_chunk(1, i)).expect("data 送出");
    }
    let mut processed = (1u64, 0u64);
    let first = mb
        .recv_next(processed, Some(1), None, &*clock)
        .expect("第一個項目");
    processed = (1, data_seq(&first).expect("第一個應為 Data(1)"));

    mb.control_sender().send(release_req()).expect("release 送出");
    let second = mb
        .recv_next(processed, Some(1), None, &*clock)
        .expect("第二個項目");
    assert!(
        matches!(second, MailboxItem::Control(ControlEvent::Request { .. })),
        "stash 中仍有依賴時，獨立 Release 應立即回傳"
    );
}

#[test]
fn stale_epoch_dependency_released_immediately() {
    // stash 內項目的 epoch 已非現行 epoch → 立即釋放，舊 epoch 的 data 不再等待。
    let mut mb = Mailbox::new(256, 1024);
    let clock = FakeClock::at(0, 0);
    mb.control_sender().send(login_after(1, 9)).expect("control 送出");
    let dtx = mb.data_sender();
    for i in 1..=3 {
        dtx.send(rx_chunk(1, i)).expect("data 送出");
    }
    let first = mb
        .recv_next((1, 0), Some(2), None, &*clock)
        .expect("項目");
    assert!(
        matches!(first, MailboxItem::Control(ControlEvent::LoginFinished { epoch: 1, .. })),
        "現行 epoch 已是 2，epoch 1 的依賴應立即釋放"
    );
}

#[test]
fn dependency_released_on_worker_stopped() {
    // 依賴的 rx_seq 不會再到（reader 已結束）：WorkerStopped 到達時，stash 先依序釋放，再回 receipt。
    let mut mb = Mailbox::new(256, 1024);
    let clock = FakeClock::at(0, 0);
    mb.control_sender().send(login_after(1, 9)).expect("control 送出");
    let dtx = mb.data_sender();
    for i in 1..=3 {
        dtx.send(rx_chunk(1, i)).expect("data 送出");
    }
    mb.control_sender()
        .send(ControlEvent::WorkerStopped(receipt(1, 1)))
        .expect("receipt 送出");
    let mut processed = (1u64, 0u64);
    let mut order: Vec<&'static str> = Vec::new();
    for _ in 0..5 {
        let item = mb
            .recv_next(processed, Some(1), None, &*clock)
            .expect("項目");
        match &item {
            MailboxItem::Data(DataEvent::UartRxChunk { rx_seq, .. }) => {
                processed = (1, *rx_seq);
                order.push("D");
            }
            MailboxItem::Control(ControlEvent::LoginFinished { .. }) => order.push("L"),
            MailboxItem::Control(ControlEvent::WorkerStopped(_)) => order.push("W"),
            _ => order.push("?"),
        }
    }
    assert_eq!(order, ["D", "D", "D", "L", "W"], "stash 應在 receipt 前全部釋放");
}

#[test]
fn stash_bounded_16() {
    // 17 個依賴 control（after_rx_seq = 100）與 100 個 data：stash 上限 16 不得丟棄任何項目。
    // data lane 獨立推進 processed；每個 rx_seq 到齊後，stash 內的依賴項目依序釋放，第 17 個於 stash 有空位後才進入。
    // 呼叫次數固定為 117，避免實作錯誤時無限阻塞。
    let mut mb = Mailbox::new(256, 1024);
    let clock = FakeClock::at(0, 0);
    let ctl = mb.control_sender();
    for _ in 0..17 {
        ctl.send(login_after(1, 100)).expect("control 送出");
    }
    let dtx = mb.data_sender();
    for i in 1..=100u64 {
        dtx.send(rx_chunk(1, i)).expect("data 送出");
    }
    let mut processed = (1u64, 0u64);
    let mut logins = 0usize;
    let mut data = 0usize;
    for _ in 0..117 {
        let item = mb
            .recv_next(processed, Some(1), None, &*clock)
            .expect("項目");
        match item {
            MailboxItem::Data(DataEvent::UartRxChunk { rx_seq, .. }) => {
                processed = (1, rx_seq);
                data += 1;
            }
            MailboxItem::Control(ControlEvent::LoginFinished { .. }) => logins += 1,
            _ => panic!("非預期項目"),
        }
    }
    assert_eq!((logins, data), (17, 100), "17 個依賴 control 與 100 個 data 皆不得丟棄");
}

#[test]
fn fake_clock_advance_wakes_timer_promptly() {
    // DrainTimeout（+3000 ms）在 FakeClock advance 後 200 ms 內被處理，不等下一個 1 s Tick。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let clock: Arc<dyn Clock> = h.clock.clone();
    let clock_fake = h.clock.clone();
    let core = h.core;
    let handle = spawn_actor(core, Mailbox::new(256, 1024), Box::new(ReplyExec), clock);

    // READY 後 DeviceDetached → BeginDrain＋Schedule(DrainTimeout +3000 ms)；executor 不完成 drain，故只會逾時。
    handle
        .control
        .send(ControlEvent::DeviceDetached {
            by_id: DEVICE_BY_ID.into(),
            reason: sw_core::mailbox::DetachReason::Removed,
        })
        .expect("control 送出");
    std::thread::sleep(Duration::from_millis(50));

    clock_fake.advance(Duration::from_millis(3_001));
    let started = Instant::now();
    let mut stalled = false;
    while started.elapsed() < Duration::from_millis(200) {
        let d = handle
            .request(SessionRequest::GetState, Duration::from_millis(100))
            .expect("GetState");
        if d.get("drain_stalled") == Some(&sw_proto::pyjson::PyValue::Bool(true)) {
            stalled = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(stalled, "advance 後 200 ms 內應處理 DrainTimeout（drain_stalled=true）");
}

#[test]
fn control_lane_bounded_256() {
    let mb = Mailbox::new(256, 1024);
    let ctl = mb.control_sender();
    for _ in 0..256 {
        ctl.try_send(ControlEvent::Tick).expect("256 筆應可排入");
    }
    match ctl.try_send(ControlEvent::Tick) {
        Err(TrySendError::Full(_)) => {}
        other => panic!("第 257 筆應回 Full，實際：{}", if other.is_ok() { "Ok" } else { "Err" }),
    }
}

#[test]
fn data_lane_bounded_1024() {
    let mb = Mailbox::new(256, 1024);
    let dtx = mb.data_sender();
    for i in 1..=1024u64 {
        dtx.try_send(rx_chunk(1, i)).expect("1024 筆應可排入");
    }
    match dtx.try_send(rx_chunk(1, 1025)) {
        Err(TrySendError::Full(_)) => {}
        other => panic!("第 1025 筆應回 Full，實際：{}", if other.is_ok() { "Ok" } else { "Err" }),
    }
}

#[test]
fn request_timeout_returns_internal_timeout() {
    // executor 永不回覆 → request 逾時回 PyErr("internal timeout")。
    let h = CoreHarness::new(SHELL_PROFILE);
    let clock: Arc<dyn Clock> = h.clock.clone();
    let core = h.core;
    let handle = spawn_actor(core, Mailbox::new(256, 1024), Box::new(SilentExec), clock);
    let err = handle
        .request(SessionRequest::GetState, Duration::from_millis(100))
        .expect_err("無回覆應逾時");
    assert_eq!(err.message, "internal timeout");
}
