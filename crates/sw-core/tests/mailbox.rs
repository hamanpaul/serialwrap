//! Slice 2.1 Task 11：有界 `Mailbox`（control 優先、依賴 RX 的 stash、世代隔離）與 `spawn_actor` 的紅燈測試。
//!
//! 紅燈階段：`Mailbox::new`／`spawn_actor` 等尚未實作，失敗應為 `not implemented: slice21 T11 …`。

#![cfg(feature = "test-fakes")]

mod support;

use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use crossbeam_channel::TrySendError;
use support::*;
use sw_core::actor::spawn_actor;
use sw_core::login_fsm::LoginOutcome;
use sw_core::mailbox::{ControlEvent, DataEvent, Mailbox, MailboxItem};
use sw_core::session_core::{Effect, ManagerNote, SessionRequest};
use sw_pal::fakes::clock::FakeClock;
use sw_pal::traits::Clock;

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
    mb.control_sender()
        .send(get_state_req())
        .expect("control 送出");
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
    mb.control_sender()
        .send(login_after(1, 3))
        .expect("control 送出");
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
    mb.control_sender()
        .send(login_after(1, 9))
        .expect("control 送出");
    let dtx = mb.data_sender();
    for i in 1..=3 {
        dtx.send(rx_chunk(1, i)).expect("data 送出");
    }
    let mut processed = (1u64, 0u64);
    let first = mb
        .recv_next(processed, Some(1), None, &*clock)
        .expect("第一個項目");
    processed = (1, data_seq(&first).expect("第一個應為 Data(1)"));

    mb.control_sender()
        .send(release_req())
        .expect("release 送出");
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
    mb.control_sender()
        .send(login_after(1, 9))
        .expect("control 送出");
    let dtx = mb.data_sender();
    for i in 1..=3 {
        dtx.send(rx_chunk(1, i)).expect("data 送出");
    }
    let first = mb.recv_next((1, 0), Some(2), None, &*clock).expect("項目");
    assert!(
        matches!(
            first,
            MailboxItem::Control(ControlEvent::LoginFinished { epoch: 1, .. })
        ),
        "現行 epoch 已是 2，epoch 1 的依賴應立即釋放"
    );
}

#[test]
fn zero_rx_dependency_is_ready_on_current_epoch() {
    let mut mb = Mailbox::new(256, 1024);
    let clock = FakeClock::at(0, 0);
    mb.control_sender()
        .send(login_after(2, 0))
        .expect("control 送出");
    let item = mb
        .recv_next((1, 99), Some(2), None, &*clock)
        .expect("after_rx_seq=0 不需等待新 chunk");
    assert!(matches!(
        item,
        MailboxItem::Control(ControlEvent::LoginFinished { epoch: 2, .. })
    ));
}

#[test]
fn dependency_released_on_worker_stopped() {
    // 依賴的 rx_seq 不會再到（reader 已結束）：WorkerStopped 到達時，stash 先依序釋放，再回 receipt。
    let mut mb = Mailbox::new(256, 1024);
    let clock = FakeClock::at(0, 0);
    mb.control_sender()
        .send(login_after(1, 9))
        .expect("control 送出");
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
    assert_eq!(
        order,
        ["D", "D", "D", "L", "W"],
        "stash 應在 receipt 前全部釋放"
    );
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
    assert_eq!(
        (logins, data),
        (17, 100),
        "17 個依賴 control 與 100 個 data 皆不得丟棄"
    );
}

#[test]
fn full_dependency_stash_does_not_block_worker_receipt() {
    let (event, _) =
        receive_after_full_dependency_backlog(ControlEvent::WorkerStopped(receipt(1, 1)));
    assert!(matches!(
        event,
        Some(MailboxItem::Control(ControlEvent::WorkerStopped(_)))
    ));
}

#[test]
fn full_dependency_stash_does_not_block_independent_release() {
    let (event, _) = receive_after_full_dependency_backlog(release_req());
    assert!(matches!(
        event,
        Some(MailboxItem::Control(ControlEvent::Request {
            req: SessionRequest::Release { .. },
            ..
        }))
    ));
}

fn receive_after_full_dependency_backlog(
    safety_event: ControlEvent,
) -> (Option<MailboxItem>, usize) {
    let mut mb = Mailbox::new(256, 1024);
    let clock = FakeClock::at(0, 0);
    let ctl = mb.control_sender();
    let safety = mb.safety_sender();
    let expect_receipt = matches!(&safety_event, ControlEvent::WorkerStopped(_));
    let receiver_clock = clock.clone();
    let (item_tx, item_rx) = crossbeam_channel::bounded(1);
    let (backlog_full_tx, backlog_full_rx) = crossbeam_channel::bounded(1);
    mb.notify_when_dependency_backlog_full(backlog_full_tx);
    let receiver = thread::spawn(move || {
        let dependency_count =
            sw_core::resources::MAILBOX_STASH_MAX + 1 + sw_core::resources::MAILBOX_CONTROL_CAP;
        let mut result = None;
        let mut released_dependencies = 0;
        for _ in 0..=dependency_count {
            let Some(item) = mb.recv_next((1, 0), Some(1), Some(2_000_000_000), &*receiver_clock)
            else {
                break;
            };
            let is_safety_event = if expect_receipt {
                matches!(&item, MailboxItem::Control(ControlEvent::WorkerStopped(_)))
            } else {
                matches!(
                    &item,
                    MailboxItem::Control(ControlEvent::Request {
                        req: SessionRequest::Release { .. },
                        ..
                    })
                )
            };
            if is_safety_event {
                result = Some(item);
                break;
            }
            if matches!(
                item,
                MailboxItem::Control(ControlEvent::LoginFinished { .. })
            ) {
                released_dependencies += 1;
            }
        }
        item_tx
            .send((result, released_dependencies))
            .expect("接收端仍存在");
    });

    let (producer_done_tx, producer_done_rx) = crossbeam_channel::bounded(1);
    let producer = thread::spawn(move || {
        let dependency_count =
            sw_core::resources::MAILBOX_STASH_MAX + 1 + sw_core::resources::MAILBOX_CONTROL_CAP;
        for _ in 0..dependency_count {
            ctl.send(login_after(1, 100)).expect("依賴控制事件送出");
        }
        backlog_full_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("consumer 已確認完整依賴 backlog");
        safety.send(safety_event).expect("安全控制事件送出");
        producer_done_tx
            .send(())
            .expect("producer 同步接收端仍存在");
    });
    producer_done_rx
        .recv_timeout(Duration::from_secs(2))
        .expect("滿載依賴與安全控制事件都應可送入有界佇列");

    let (item, released_dependencies) = match item_rx.recv_timeout(Duration::from_millis(200)) {
        Ok(item) => item,
        Err(crossbeam_channel::RecvTimeoutError::Timeout) => {
            // 舊實作在滿載分支只等 data；推進假時鐘讓錯誤路徑有界結束。
            clock.advance(Duration::from_secs(2));
            item_rx
                .recv_timeout(Duration::from_secs(2))
                .expect("滿載分支應在假期限到達後結束")
        }
        Err(crossbeam_channel::RecvTimeoutError::Disconnected) => {
            panic!("mailbox 接收端不應提前結束")
        }
    };
    if expect_receipt {
        assert_eq!(
            released_dependencies,
            sw_core::resources::MAILBOX_STASH_MAX + 1 + sw_core::resources::MAILBOX_CONTROL_CAP,
            "WorkerStopped receipt 前須釋放完整容量的同 epoch 依賴"
        );
    } else {
        assert_eq!(released_dependencies, 0, "Release 不需等待依賴事件釋放");
    }
    producer.join().expect("producer 應正常結束");
    receiver.join().expect("receiver 應正常結束");
    (item, released_dependencies)
}

struct ReadyNotifyExec(crossbeam_channel::Sender<()>);

impl sw_core::actor::EffectExecutor for ReadyNotifyExec {
    fn execute(
        &mut self,
        effect: Effect,
        _tx: &crossbeam_channel::Sender<ControlEvent>,
        _safety: &crossbeam_channel::Sender<ControlEvent>,
    ) {
        match effect {
            Effect::Notify(ManagerNote::SessionReady { .. }) => {
                let _ = self.0.try_send(());
            }
            Effect::Reply(reply, result) => {
                let _ = reply.try_send(result);
            }
            _ => {}
        }
    }
}

#[test]
fn actor_ticks_passthrough_ready_without_followup_event() {
    let mut h = CoreHarness::new(PASSTHROUGH_PROFILE);
    h.to_attached(None);
    let clock: Arc<dyn Clock> = h.clock.clone();
    let fake_clock = h.clock.clone();
    let core = h.core;
    let (ready_tx, ready_rx) = crossbeam_channel::bounded(1);
    let handle = spawn_actor(
        core,
        Mailbox::new(256, 1024),
        Box::new(ReadyNotifyExec(ready_tx)),
        clock,
    );

    handle
        .request(SessionRequest::GetState, Duration::from_millis(200))
        .expect("actor 啟動後應可回應");
    fake_clock.advance(Duration::from_secs(31));
    ready_rx
        .recv_timeout(Duration::from_millis(200))
        .expect("沒有後續事件時仍須由 actor tick 推進 READY");
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
        let stalled_in_session = match d.get("session") {
            Some(sw_proto::pyjson::PyValue::Dict(session)) => {
                session.get("drain_stalled") == Some(&sw_proto::pyjson::PyValue::Bool(true))
            }
            _ => false,
        };
        if stalled_in_session {
            stalled = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(
        stalled,
        "advance 後 200 ms 內應處理 DrainTimeout（drain_stalled=true）"
    );
}

#[test]
fn fake_clock_advance_observes_drain_timeout_in_session() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let clock: Arc<dyn Clock> = h.clock.clone();
    let clock_fake = h.clock.clone();
    let core = h.core;
    let handle = spawn_actor(core, Mailbox::new(256, 1024), Box::new(ReplyExec), clock);

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
        let response = handle
            .request(SessionRequest::GetState, Duration::from_millis(100))
            .expect("GetState");
        let nested = response.get("session").and_then(|value| match value {
            sw_proto::pyjson::PyValue::Dict(session) => session.get("drain_stalled"),
            _ => None,
        });
        if nested == Some(&sw_proto::pyjson::PyValue::Bool(true)) {
            stalled = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(
        stalled,
        "GetState 的 session.drain_stalled 應於 FakeClock 到期後為 true"
    );
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
        other => panic!(
            "第 257 筆應回 Full，實際：{}",
            if other.is_ok() { "Ok" } else { "Err" }
        ),
    }

    let safety = mb.safety_sender();
    for _ in 0..sw_core::resources::MAILBOX_CONTROL_CAP {
        safety
            .try_send(ControlEvent::Tick)
            .expect("safety lane 256 筆應可排入");
    }
    match safety.try_send(ControlEvent::Tick) {
        Err(TrySendError::Full(_)) => {}
        other => panic!(
            "safety lane 第 257 筆應回 Full，實際：{}",
            if other.is_ok() { "Ok" } else { "Err" }
        ),
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
        other => panic!(
            "第 1025 筆應回 Full，實際：{}",
            if other.is_ok() { "Ok" } else { "Err" }
        ),
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

#[test]
fn op_finished_waits_for_dependent_rx() {
    // 審查 F52：OpFinished{after_rx_seq: 5} 不得先於 Data(5) 被取出（命令結算依賴最後一段 RX 已處理）。
    let mut mb = Mailbox::new(256, 1024);
    let clock = FakeClock::at(0, 0);
    mb.control_sender()
        .send(ControlEvent::OpFinished {
            op_id: sw_core::tx_writer::OpId(1),
            epoch: 1,
            outcome: sw_core::op_runner::OpOutcome::SendFailed,
            after_rx_seq: Some(5),
        })
        .expect("control 送出");
    let dtx = mb.data_sender();
    for i in 1..=6 {
        dtx.send(rx_chunk(1, i)).expect("data 送出");
    }
    let mut processed = (1u64, 0u64);
    let mut order: Vec<String> = Vec::new();
    for _ in 0..7 {
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
    assert_eq!(order, ["D1", "D2", "D3", "D4", "D5", "C", "D6"]);
}

/// 方法審閱 [High]（2026-10-29）：延遲探針須以事件自己的送入／處理界點計數。`WorkerStopped` 因同 epoch 依賴而移入
/// `stopped` 後，mailbox 仍先取 data；探針不得因 `control_waiting()` 看不到 `stopped` 而少算。D,D,D,L,W 情境下
/// receipt 送入後 actor 實際處理了 3 個 data item，見證數須為 3（drain_receipt 的 `at_most_one_data_item_before_control`
/// 與此排空順序衝突；OD-64 已裁決界限改為「依賴等待前已讀入筆數＋1」，見 `stopped_receipt_records_pre_dependency_data`）。
#[cfg(feature = "test-fakes")]
#[test]
fn witness_counts_data_overtaking_stopped_receipt() {
    let mut mb = Mailbox::new(256, 1024);
    let clock = FakeClock::at(0, 0);
    mb.control_sender()
        .send(login_after(1, 9))
        .expect("control 送出");
    let dtx = mb.data_sender();
    for i in 1..=3 {
        dtx.send(rx_chunk(1, i)).expect("data 送出");
    }
    mb.control_sender()
        .send(ControlEvent::WorkerStopped(receipt(1, 1)))
        .expect("receipt 送出");
    let mut processed = (1u64, 0u64);
    let mut witnesses: Vec<(&'static str, Option<u64>)> = Vec::new();
    for _ in 0..5 {
        let item = mb
            .recv_next(processed, Some(1), None, &*clock)
            .expect("項目");
        let w = mb.take_control_witness();
        match &item {
            MailboxItem::Data(DataEvent::UartRxChunk { rx_seq, .. }) => {
                processed = (1, *rx_seq);
                witnesses.push(("D", w));
            }
            MailboxItem::Control(ControlEvent::LoginFinished { .. }) => witnesses.push(("L", w)),
            MailboxItem::Control(ControlEvent::WorkerStopped(_)) => witnesses.push(("W", w)),
            _ => witnesses.push(("?", w)),
        }
    }
    assert_eq!(witnesses.iter().map(|(k, _)| *k).collect::<Vec<_>>(), ["D", "D", "D", "L", "W"]);
    assert_eq!(witnesses[4].1, Some(3), "receipt 等待期間處理了 3 個 data item：{witnesses:?}");
    assert!(witnesses[..3].iter().all(|(_, w)| w.is_none()), "data item 不帶見證數：{witnesses:?}");
}

/// OD-64（擁有者裁決 (a)，2026-10-29）：drain_receipt 的 l1_only 界限為「receipt 送出後，至多處理同 epoch 於依賴等待前
/// 已讀入 mailbox 的 data，再加 1 筆」；排空順序維持與 Python 1:1（D,D,D,L,W）。探針須同時記錄 receipt 開始依賴等待
/// （移入 `stopped`）時已讀入 mailbox 的同 epoch data 筆數，供依新界限判定。
#[cfg(feature = "test-fakes")]
#[test]
fn stopped_receipt_records_pre_dependency_data() {
    let mut mb = Mailbox::new(256, 1024);
    let clock = FakeClock::at(0, 0);
    mb.control_sender()
        .send(login_after(1, 9))
        .expect("control 送出");
    let dtx = mb.data_sender();
    for i in 1..=3 {
        dtx.send(rx_chunk(1, i)).expect("data 送出");
    }
    mb.control_sender()
        .send(ControlEvent::WorkerStopped(receipt(1, 1)))
        .expect("receipt 送出");
    let mut processed = (1u64, 0u64);
    let mut rows: Vec<(&'static str, Option<u64>, Option<u64>)> = Vec::new();
    for _ in 0..5 {
        let item = mb
            .recv_next(processed, Some(1), None, &*clock)
            .expect("項目");
        let w = mb.take_control_witness();
        let pre = mb.take_control_pre_dependency_data();
        let kind = match &item {
            MailboxItem::Data(DataEvent::UartRxChunk { rx_seq, .. }) => {
                processed = (1, *rx_seq);
                "D"
            }
            MailboxItem::Control(ControlEvent::LoginFinished { .. }) => "L",
            MailboxItem::Control(ControlEvent::WorkerStopped(_)) => "W",
            _ => "?",
        };
        rows.push((kind, w, pre));
    }
    assert_eq!(rows.iter().map(|(k, _, _)| *k).collect::<Vec<_>>(), ["D", "D", "D", "L", "W"], "{rows:?}");
    let (_, w, pre) = rows[4];
    assert_eq!(pre, Some(3), "receipt 依賴等待前 mailbox 已有 3 筆同 epoch data：{rows:?}");
    assert_eq!(w, Some(3), "{rows:?}");
    assert!(w.unwrap() <= pre.unwrap() + 1, "OD-64 界限：{rows:?}");
}

/// OD-64：只計 receipt 開始依賴等待「之前」已讀入的同 epoch data——之後才讀入的 data 與其他 epoch 的 data 不計入，
/// 使超越者被探針揭露（見證數 > 已讀入筆數＋1）。
#[cfg(feature = "test-fakes")]
#[test]
fn pre_dependency_data_excludes_later_and_other_epoch_data() {
    let mut mb = Mailbox::new(256, 1024);
    let clock = FakeClock::at(0, 0);
    mb.control_sender()
        .send(login_after(2, 9))
        .expect("control 送出");
    let dtx = mb.data_sender();
    dtx.send(rx_chunk(1, 7)).expect("舊 epoch data 送出");
    dtx.send(rx_chunk(2, 1)).expect("data 送出");
    dtx.send(rx_chunk(2, 2)).expect("data 送出");
    mb.control_sender()
        .send(ControlEvent::WorkerStopped(receipt(2, 1)))
        .expect("receipt 送出");
    // 第一次取出：LoginFinished 進 stash、receipt 移入 stopped（此刻已讀入 1 筆舊 epoch＋2 筆同 epoch），回傳第一筆 data。
    let mut processed = (2u64, 0u64);
    let first = mb.recv_next(processed, Some(2), None, &*clock).expect("項目");
    assert!(matches!(first, MailboxItem::Data(DataEvent::UartRxChunk { epoch: 1, .. })));
    // 依賴等待開始之後才讀入的同 epoch data。
    for i in 3..=5 {
        dtx.send(rx_chunk(2, i)).expect("data 送出");
    }
    let mut receipt_row = None;
    for _ in 0..7 {
        let item = mb
            .recv_next(processed, Some(2), None, &*clock)
            .expect("項目");
        let w = mb.take_control_witness();
        let pre = mb.take_control_pre_dependency_data();
        match &item {
            MailboxItem::Data(DataEvent::UartRxChunk { epoch: 2, rx_seq, .. }) => processed = (2, *rx_seq),
            MailboxItem::Control(ControlEvent::WorkerStopped(_)) => {
                receipt_row = Some((w, pre));
                break;
            }
            _ => {}
        }
    }
    let (w, pre) = receipt_row.expect("receipt 應被取出");
    assert_eq!(pre, Some(2), "只計依賴等待前已讀入的同 epoch data（舊 epoch 與之後讀入者不計）");
    assert_eq!(w, Some(6), "receipt 送入後共處理 6 筆 data");
    assert!(w.unwrap() > pre.unwrap() + 1, "超越者須可被探針揭露");
}

/// OD-64：無同 epoch 依賴的 receipt 不進 `stopped`，依賴等待前筆數為 0（界限即原本的 1 筆）。
#[cfg(feature = "test-fakes")]
#[test]
fn receipt_without_dependency_has_zero_pre_dependency_data() {
    let mut mb = Mailbox::new(256, 1024);
    let clock = FakeClock::at(0, 0);
    let dtx = mb.data_sender();
    for i in 1..=3 {
        dtx.send(rx_chunk(1, i)).expect("data 送出");
    }
    mb.safety_sender()
        .send(ControlEvent::WorkerStopped(receipt(1, 1)))
        .expect("receipt 送出");
    let item = mb.recv_next((1, 0), Some(1), None, &*clock).expect("項目");
    assert!(matches!(item, MailboxItem::Control(ControlEvent::WorkerStopped(_))), "safety lane 優先於 data");
    assert_eq!(mb.take_control_witness(), Some(0));
    assert_eq!(mb.take_control_pre_dependency_data(), Some(0));
}

/// OD-64 接線：actor 探針把 mailbox 的「依賴等待前已讀入筆數」記入 `ProbeEntry::pre_dependency_data`，
/// drain_receipt 依新界限（見證數 ≤ 依賴等待前筆數＋1）判定。
#[test]
fn actor_probe_records_pre_dependency_data_for_drain_receipt() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let epoch = h.epoch();
    let clock: Arc<dyn Clock> = h.clock.clone();
    let core = h.core;
    let mb = Mailbox::new(256, 1024);
    mb.control_sender()
        .send(login_after(epoch, 9))
        .expect("control 送出");
    let dtx = mb.data_sender();
    for i in 1..=3 {
        dtx.send(rx_chunk(epoch, i)).expect("data 送出");
    }
    mb.control_sender()
        .send(ControlEvent::WorkerStopped(receipt(epoch, 99)))
        .expect("receipt 送出");
    let handle = spawn_actor(core, mb, Box::new(ReplyExec), clock);
    let deadline = Instant::now() + Duration::from_secs(5);
    while handle.probe.entries("WorkerStopped").is_empty() {
        assert!(Instant::now() < deadline, "actor 未處理 receipt");
        thread::sleep(Duration::from_millis(1));
    }
    let e = handle.probe.entries("WorkerStopped")[0].clone();
    assert_eq!(e.pre_dependency_data, 3, "{e:?}");
    assert_eq!(e.data_while_waiting, 3, "{e:?}");
    assert!(e.data_while_waiting <= e.pre_dependency_data + 1, "OD-64 界限：{e:?}");
}

/// event_latency 審閱 2 C4（L-10）：lane 見證數的正向自測——事件停在 control lane 期間，mailbox 經 `stopped` 路徑或
/// `pending_control` 路徑回傳 N 筆 data，該事件被取出時見證數須恰為 N（探針退化成 0 時 l1_gate 不得靜默放行）。
#[cfg(feature = "test-fakes")]
#[test]
fn lane_witness_counts_data_returned_via_stopped_and_pending_paths() {
    // stopped 路徑：L（同 epoch 依賴）、3 筆 data、W（移入 stopped）之後，G 停在 control lane。
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
    mb.control_sender().send(get_state_req()).expect("G 送出");
    let mut processed = (1u64, 0u64);
    let mut g_witness = None;
    for _ in 0..6 {
        let item = mb.recv_next(processed, Some(1), None, &*clock).expect("項目");
        let w = mb.take_control_witness();
        match &item {
            MailboxItem::Data(DataEvent::UartRxChunk { rx_seq, .. }) => processed = (1, *rx_seq),
            MailboxItem::Control(ControlEvent::Request { .. }) => g_witness = Some(w),
            _ => {}
        }
    }
    assert_eq!(g_witness, Some(Some(3)), "G 在 control lane 等待期間 stopped 路徑回傳 3 筆 data");

    // pending 路徑：stash 滿（16）＋第 17 個依賴事件成為 pending 時，mailbox 改取 data；G 停在 control lane。
    let mut mb = Mailbox::new(256, 1024);
    let ctl = mb.control_sender();
    for _ in 0..=sw_core::resources::MAILBOX_STASH_MAX {
        ctl.send(login_after(1, 100)).expect("control 送出");
    }
    let dtx = mb.data_sender();
    for i in 1..=3 {
        dtx.send(rx_chunk(1, i)).expect("data 送出");
    }
    ctl.send(get_state_req()).expect("G 送出");
    let mut processed = (1u64, 0u64);
    let mut g_witness = None;
    for _ in 0..4 {
        let item = mb.recv_next(processed, Some(1), None, &*clock).expect("項目");
        let w = mb.take_control_witness();
        match &item {
            MailboxItem::Data(DataEvent::UartRxChunk { rx_seq, .. }) => processed = (1, *rx_seq),
            MailboxItem::Control(ControlEvent::Request { .. }) => {
                g_witness = Some(w);
                break;
            }
            _ => {}
        }
    }
    assert_eq!(g_witness, Some(Some(3)), "G 在 control lane 等待期間 pending 路徑回傳 3 筆 data");
}
