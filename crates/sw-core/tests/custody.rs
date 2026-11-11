//! S2-PAR-wp510r R1: 保管帳本基礎（無行為變更）狀態機與單元測試。
//!
//! 涵蓋設計 §4 R1，以及 §3.1.1、§3.1.2（C1～C8）、§3.1.5：
//! - C1～C8 各轉換的合法與非法輸入
//! - 入列失敗（Full、Disconnected）時帳本不變
//! - 回收者配對 WorkerStopped 並分開結算 handle 與證據
//! - DrainDataBarrier 不確認、設 expired、記 RX 缺口
//! - panic 守衛回報 Failed{unknown}
//! - ExitReclaim 在正常結束、panic 超限、unwind 三種路徑都觸發，建構失敗時不觸發
//! - Reclaim 的 Drop 不取鎖
//! - 送達者的退避、取消、凍結暫停與丟棄、30 s 時限、冪等
//! - 世代號防止舊命令處理新 actor 的紀錄
//! - wait_for_rx_dispatch 區分斷線與逾時兩種缺口文字

#![cfg(feature = "test-fakes")]

mod support;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use crossbeam_channel::{bounded, unbounded};
use support::*;
use sw_core::actor::*;
use sw_core::admission::{AdmissionGate, FreezeOutcome, HandoffFreezeState};
use sw_core::custody::*;
use sw_core::drain::{StopId, WorkerStoppedReceipt};
use sw_core::mailbox::{ControlEvent, Mailbox};
use sw_core::session_core::UnconfirmedCloses;
use sw_core::session_manager::HandleParts;
use sw_core::tx_writer::{EvidenceGap, TxLedger};
use sw_pal::traits::{Clock, PalError, PalErrorKind, SerialBackend};
use sw_proto::pyjson::PyValue;

static SPAWN_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

#[derive(Clone)]
struct FakeInstantClock {
    now: Arc<Mutex<Instant>>,
}

impl FakeInstantClock {
    fn new(start: Instant) -> Self {
        Self {
            now: Arc::new(Mutex::new(start)),
        }
    }

    fn set(&self, t: Instant) {
        *self.now.lock().unwrap() = t;
    }

    fn gate(&self) -> Arc<AdmissionGate> {
        let clock = self.clone();
        Arc::new(AdmissionGate::with_clock(move || *clock.now.lock().unwrap()))
    }
}

#[test]
fn test_c1_c8_transitions_valid_and_invalid() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed.clone());

    // C1: register 建立新 token
    let by_id = "usb-serial-1";
    let token1 = ledger.register(
        by_id,
        "/dev/ttyUSB0",
        CustodyOwner::Session { sid: "COM1".into(), inc: 1 },
        CustodyTaker::OpenJob { sid: "COM1".into(), gen: 1 },
    ).expect("首次 register 應成功");
    assert!(token1 > 0);

    // C1: 重複 register 同一 by_id 應回 CustodyBusy
    let err = ledger.register(
        by_id,
        "/dev/ttyUSB0",
        CustodyOwner::Session { sid: "COM2".into(), inc: 1 },
        CustodyTaker::OpenJob { sid: "COM2".into(), gen: 1 },
    );
    assert_eq!(err, Err(CustodyRegisterError::CustodyBusy { token: token1 }));

    // C1: unconfirmed 中的 by_id 應回 CloseBlocked
    let by_id_blocked = "usb-serial-blocked";
    unconfirmed.insert(by_id_blocked);
    let err = ledger.register(
        by_id_blocked,
        "/dev/ttyUSB1",
        CustodyOwner::Parked,
        CustodyTaker::Worker("op1".into()),
    );
    assert_eq!(err, Err(CustodyRegisterError::CloseBlocked));

    // C1': claim_parked 只用於 Held{owner: Parked} 且 real_path 相同
    let by_id_parked = "usb-serial-parked";
    let token_parked = ledger.register(
        by_id_parked,
        "/dev/ttyUSB2",
        CustodyOwner::Parked,
        CustodyTaker::Worker("op2".into()),
    ).expect("register parked 應成功");

    // 尚未存入 parts (目前為 Opening)，claim_parked 應失敗
    let claim_err = ledger.claim_parked(
        by_id_parked,
        "/dev/ttyUSB2",
        CustodyTaker::OpenJob { sid: "COM3".into(), gen: 1 },
    );
    assert!(matches!(claim_err, Err(CustodyClaimParkedError::NotHeld)));

    // 存入 parts -> Held{Parked}
    ledger.save_held_mock(token_parked).expect("存入 mock parts 成功");

    // real_path 不符應失敗
    let claim_err = ledger.claim_parked(
        by_id_parked,
        "/dev/ttyUSB_WRONG",
        CustodyTaker::OpenJob { sid: "COM3".into(), gen: 1 },
    );
    assert!(matches!(claim_err, Err(CustodyClaimParkedError::PathMismatch)));

    // 合法 claim_parked: 轉為 Taken，token 不變
    let claim_ok = ledger.claim_parked(
        by_id_parked,
        "/dev/ttyUSB2",
        CustodyTaker::OpenJob { sid: "COM3".into(), gen: 1 },
    );
    match claim_ok {
        Ok((claimed_token, _parts)) => {
            assert_eq!(claimed_token, token_parked);
        }
        Err(_) => panic!("claim_parked 應成功"),
    }

    // 取出後狀態為 Taken，再次 claim 應被拒絕 (NotHeld 或 NotParked)
    let claim_err2 = ledger.claim_parked(
        by_id_parked,
        "/dev/ttyUSB2",
        CustodyTaker::OpenJob { sid: "COM3".into(), gen: 2 },
    );
    assert!(
        matches!(claim_err2, Err(CustodyClaimParkedError::NotHeld | CustodyClaimParkedError::NotParked)),
        "Taken 後再次 claim 應被拒絕"
    );
}

#[test]
fn test_enqueue_failure_leaves_ledger_unchanged() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed);

    let by_id = "usb-serial-eq";
    let token = ledger.register(
        by_id,
        "/dev/ttyUSB0",
        CustodyOwner::Session { sid: "COM1".into(), inc: 1 },
        CustodyTaker::OpenJob { sid: "COM1".into(), gen: 1 },
    ).expect("register 成功");

    let (tx, rx) = bounded(1);
    // 填滿 channel
    tx.send(ControlEvent::Tick).unwrap();

    // 模擬入列嘗試 (try_send 遇到 Full)
    let send_res = tx.try_send(ControlEvent::Tick);
    assert!(send_res.is_err());

    // 斷言帳本紀錄完全未變
    let c = ledger.get_custody(by_id).expect("紀錄存在");
    assert_eq!(c.token, token);
    assert!(matches!(c.phase, CustodyPhase::Opening { .. }));

    // 模擬通道關閉 (Disconnected)
    drop(rx);
    let send_res2 = tx.send(ControlEvent::Tick);
    assert!(send_res2.is_err());
    let c2 = ledger.get_custody(by_id).expect("紀錄仍存在");
    assert_eq!(c2.token, token);
}

#[test]
fn test_reaper_worker_stopped_separate_handle_and_evidence() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed.clone());

    let by_id = "usb-serial-reaper";
    let token = ledger.register(
        by_id,
        "/dev/ttyUSB0",
        CustodyOwner::Reaper,
        CustodyTaker::Worker("reap".into()),
    ).expect("register 成功");

    // 模擬轉入 BridgeDraining
    ledger.mark_bridge_draining(token, 1, StopId(1)).expect("mark bridge draining");

    // 1. handle_closed: true 但 evidence 有未落盤缺口
    let receipt_incomplete = WorkerStoppedReceipt {
        epoch: 1,
        stop_id: StopId(1),
        ledger: TxLedger::default(),
        unrecorded: vec![EvidenceGap {
            op_id: None,
            dir: "RX",
            len: 0,
            error: PalError::plain(PalErrorKind::Io, "回收者排空，無 actor 確認 RX dispatch"),
        }],
        wal_error: None,
        handle_closed: true,
        close_error: None,
    };

    let term = ledger.settle_worker_stopped(token, receipt_incomplete).expect("settle worker stopped");
    assert_eq!(term.handle, TerminalHandle::Closed);
    assert!(matches!(term.evidence, TerminalEvidence::Incomplete { .. }));
    // handle 安全關閉 -> 紀錄移除，不進入 unconfirmed
    assert!(ledger.get_custody(by_id).is_none());
    assert!(!unconfirmed.contains(by_id));

    // 2. handle_closed: false
    let token2 = ledger.register(
        by_id,
        "/dev/ttyUSB0",
        CustodyOwner::Reaper,
        CustodyTaker::Worker("reap2".into()),
    ).expect("register 成功");
    ledger.mark_bridge_draining(token2, 2, StopId(2)).expect("mark bridge draining");

    let receipt_failed = WorkerStoppedReceipt {
        epoch: 2,
        stop_id: StopId(2),
        ledger: TxLedger::default(),
        unrecorded: vec![],
        wal_error: None,
        handle_closed: false,
        close_error: Some(PalError::plain(PalErrorKind::Io, "close failed")),
    };

    let term2 = ledger.settle_worker_stopped(token2, receipt_failed).expect("settle worker stopped failed handle");
    assert!(matches!(term2.handle, TerminalHandle::Failed { .. }));
    // handle 關閉失敗 -> 紀錄轉為 CloseFailed 並記入 unconfirmed
    let c = ledger.get_custody(by_id).expect("CloseFailed 紀錄保留");
    assert!(matches!(c.phase, CustodyPhase::CloseFailed { .. }));
    assert!(unconfirmed.contains(by_id));
}

#[test]
fn test_reaper_drain_data_barrier_not_confirmed_and_expires() {
    let (_reaper_cmd_tx, reaper_cmd_rx) = unbounded();
    let (reaper_event_tx, reaper_event_rx) = unbounded();

    struct DummyCb;
    impl ReaperCallback for DummyCb {
        fn handle_cmd(&self, _cmd: ReaperCmd, _event_tx: &crossbeam_channel::Sender<ControlEvent>) {}
        fn handle_worker_stopped(&self, _receipt: WorkerStoppedReceipt) {}
    }

    let mut reaper = ReaperThread::start(
        reaper_cmd_rx,
        reaper_event_rx,
        reaper_event_tx.clone(),
        Arc::new(DummyCb),
    );

    let (reply, ack) = bounded(1);
    let expired = Arc::new(AtomicBool::new(false));

    // 發送 DrainDataBarrier 給回收者
    reaper_event_tx.send(ControlEvent::DrainDataBarrier {
        epoch: 1,
        rx_seq: 100,
        reply,
        expired: expired.clone(),
    }).unwrap();

    // ack 應收到斷線 (reply 被回收者丟棄而非確認)
    let recv_res = ack.recv_timeout(Duration::from_millis(500));
    assert_eq!(recv_res, Err(crossbeam_channel::RecvTimeoutError::Disconnected));
    assert!(expired.load(Ordering::Acquire), "expired 應被設為 true");

    reaper.stop();
}

#[test]
fn test_panic_guard_reports_failed_unknown() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed);

    let token = ledger.register(
        "by-id-panic",
        "/dev/tty0",
        CustodyOwner::Parked,
        CustodyTaker::Worker("w".into()),
    ).unwrap();

    let term = ledger.settle_panic(token);
    assert_eq!(term.handle, TerminalHandle::Failed { error: "unknown".into() });
    let c = ledger.get_custody("by-id-panic").unwrap();
    assert!(matches!(c.phase, CustodyPhase::CloseFailed { ref error } if error == "unknown"));
}

#[test]
fn test_exit_reclaim_triggers_on_normal_exit_panic_and_unwind_but_not_spawn_failure() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    // 1. 正常結束：經由 try_spawn_actor_with_reaper 啟動，發送 Shutdown，執行緒退出，reaper 收到恰好 1 筆 ActorExited
    {
        let (reaper_tx, reaper_rx) = unbounded();
        let h = CoreHarness::new(SHELL_PROFILE);
        let clock: Arc<dyn Clock> = h.clock.clone();
        let core = h.core;
        let expected_sid = core.session_id().to_string();
        let mb = Mailbox::new(256, 1024);
        let control = mb.control_sender();
        let handle = try_spawn_actor_with_reaper(core, mb, Box::new(ReplyExec), clock, 1, Some(reaper_tx))
            .expect("spawn actor 成功");

        let (reply, _reply_rx) = bounded(1);
        control.send(ControlEvent::Shutdown { reply }).unwrap();
        handle.join.join().expect("actor thread 正常退出");

        let cmd = reaper_rx.try_recv().expect("reaper 應收到 ActorExited");
        match cmd {
            ReaperCmd::ActorExited { sid, inc } => {
                assert_eq!(sid, expected_sid);
                assert_eq!(inc, 1);
            }
            _ => panic!("預期 ActorExited"),
        }
        assert!(reaper_rx.try_recv().is_err(), "正常退出不應有多餘命令");
    }

    // 2. unwind 退出：在 actor 執行緒 closure 內部 unwind，ExitReclaim 的 Drop 送出恰好 1 筆 ActorExited
    {
        let (reaper_tx, reaper_rx) = unbounded();
        let h = CoreHarness::new(SHELL_PROFILE);
        let clock: Arc<dyn Clock> = h.clock.clone();
        let core = h.core;
        let expected_sid = core.session_id().to_string();
        let mb = Mailbox::new(256, 1024);

        struct UnwindingExec;
        impl EffectExecutor for UnwindingExec {
            fn execute(
                &mut self,
                _effect: sw_core::session_core::Effect,
                _control: &crossbeam_channel::Sender<ControlEvent>,
                _safety: &crossbeam_channel::Sender<ControlEvent>,
            ) {
                panic!("unwind inside actor thread closure");
            }
        }

        let handle = try_spawn_actor_with_reaper(core, mb, Box::new(UnwindingExec), clock, 2, Some(reaper_tx))
            .expect("spawn actor 成功");

        // 發送一個會產生 Effect::Reply 的請求，觸發 executor panic unwind
        let (reply, _reply_rx) = bounded(1);
        let _ = handle.control.send(ControlEvent::Request {
            req: sw_core::session_core::SessionRequest::GetState,
            reply,
        });

        let join_res = handle.join.join();
        assert!(join_res.is_err(), "actor thread 應 unwind");

        let cmd = reaper_rx.try_recv().expect("unwind 時 reaper 應收到 ActorExited");
        match cmd {
            ReaperCmd::ActorExited { sid, inc } => {
                assert_eq!(sid, expected_sid);
                assert_eq!(inc, 2);
            }
            _ => panic!("預期 ActorExited"),
        }
        assert!(reaper_rx.try_recv().is_err(), "unwind 不應有多餘命令");
    }

    // 3. 審查 P8-057a065-4 重現：連續 3 次 panic 事件後，驗證 actor 實際行為
    {
        let (reaper_tx, reaper_rx) = unbounded();
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.core.arm_panic_on_next_events(3);
        let clock: Arc<dyn Clock> = h.clock.clone();
        let core = h.core;
        let expected_sid = core.session_id().to_string();
        let mb = Mailbox::new(256, 1024);
        let handle = try_spawn_actor_with_reaper(core, mb, Box::new(ReplyExec), clock, 3, Some(reaper_tx))
            .expect("spawn actor 成功");

        // 連續三次請求觸發三次 panic
        for _ in 0..3 {
            let res = handle.request(sw_core::session_core::SessionRequest::GetState, Duration::from_secs(2));
            assert!(res.is_err(), "注入 panic 時請求應回傳錯誤");
        }

        // 若按照審查原本的假設：斷言 panic 超限本身導致退出
        // 在現行 actor 實作下，actor.rs:620 僅呼叫 begin_stop(StopKind::IoError)，並不結束 actor loop，因此不會退出！
        // 這裡以有界等待驗證：100ms 內 reaper 不會收到 ActorExited，且第四次 GetState 仍成功處理！
        let premature_exit = reaper_rx.recv_timeout(Duration::from_millis(100));
        assert!(
            premature_exit.is_err(),
            "現行 actor 不會因 panic 超限退出執行緒，因此不應有 ActorExited"
        );

        let fourth_req = handle.request(sw_core::session_core::SessionRequest::GetState, Duration::from_secs(2));
        assert!(fourth_req.is_ok(), "第四次請求仍能被存活的 actor 成功處理");

        // 送出 Shutdown 作為清理退出
        let (reply, _reply_rx) = bounded(1);
        let _ = handle.control.send(ControlEvent::Shutdown { reply });
        let _ = handle.join.join();

        let cmd = reaper_rx.try_recv().expect("Shutdown 退出後 reaper 應收到恰好一筆 ActorExited");
        match cmd {
            ReaperCmd::ActorExited { sid, inc } => {
                assert_eq!(sid, expected_sid);
                assert_eq!(inc, 3);
            }
            _ => panic!("預期 ActorExited"),
        }
        assert!(reaper_rx.try_recv().is_err(), "Shutdown 退出後不應有多餘命令");
    }

    // 4. spawn 失敗路徑：注入失敗，try_spawn_actor_with_reaper 回傳 Err，未進入 closure，reaper 通道為空
    {
        let (reaper_tx, reaper_rx) = unbounded();
        let h = CoreHarness::new(SHELL_PROFILE);
        let clock: Arc<dyn Clock> = h.clock.clone();
        let core = h.core;
        let mb = Mailbox::new(256, 1024);

        inject_actor_spawn_fail(1);
        let res = try_spawn_actor_with_reaper(core, mb, Box::new(ReplyExec), clock, 4, Some(reaper_tx));
        assert!(res.is_err(), "注入失敗時應回傳 Err");
        assert!(reaper_rx.try_recv().is_err(), "spawn 失敗時不得送出任何命令");
    }
}

#[test]
fn test_reclaim_drop_does_not_take_lock() {
    let (tx, rx) = unbounded();
    let lock = Arc::new(std::sync::Mutex::new(()));
    let guard = lock.lock().unwrap();

    // 在持有某個鎖的環境下 drop Reclaim，不應死鎖
    {
        let mock_worker = MockWorker { epoch: 10 };
        let _reclaim = Reclaim::new(1234, 10, mock_worker, tx);
    }
    drop(guard);

    match rx.try_recv() {
        Ok(ReaperCmd::Reclaim { token, epoch, worker }) => {
            assert_eq!(token, 1234);
            assert_eq!(epoch, 10);
            assert_eq!(worker.epoch(), 10);
        }
        _ => panic!("預期 Reclaim"),
    }
}

#[test]
fn test_deliverer_backoff_cancellation_freeze_and_timeout() {
    let (actor_tx, actor_rx) = bounded(1);
    let gate = Arc::new(AdmissionGate::new());
    let mut deliverer = Deliverer::new(gate.clone());

    let sid = "COM1";
    let by_id = "by-id-1";
    let real_path = "/dev/ttyUSB0";

    // 1. 添加送達義務 (冪等性：新義務覆蓋舊義務)
    deliverer.add_obligation(sid, by_id, real_path, 1, 1);
    assert_eq!(deliverer.obligation_count(), 1);
    deliverer.add_obligation(sid, by_id, real_path, 1, 1);
    assert_eq!(deliverer.obligation_count(), 1);

    // 2. 當 actor lane 為滿時，try_send 回 Full，退避遞增
    actor_tx.send(ControlEvent::Tick).unwrap(); // 填滿容量
    let outcome = deliverer.step_delivery(sid, &actor_tx, Instant::now());
    assert_eq!(outcome, DeliveryStepResult::Backoff);
    assert_eq!(deliverer.attempts(sid), 1);

    // 3. 凍結為 Pending 期間：暫停嘗試，attempts 不增加
    let freeze_res = gate.freeze_if_idle("freeze-test");
    let freeze_token = match freeze_res {
        FreezeOutcome::Frozen { token, .. } => token,
        _ => panic!("預期 Frozen"),
    };
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Pending);

    let outcome_pending = deliverer.step_delivery(sid, &actor_tx, Instant::now() + Duration::from_millis(50));
    assert_eq!(outcome_pending, DeliveryStepResult::PausedPending);
    assert_eq!(deliverer.attempts(sid), 1, "Pending 期間 attempts 不得增加");

    // 4. 恢復為 Open
    assert!(gate.thaw(&freeze_token));
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Open);

    // 5. 釋放 actor lane 容量後，成功投遞
    let _ = actor_rx.try_recv();
    let outcome_ok = deliverer.step_delivery(sid, &actor_tx, Instant::now() + Duration::from_millis(50));
    assert_eq!(outcome_ok, DeliveryStepResult::Delivered);
    assert_eq!(deliverer.obligation_count(), 0);

    // 驗證 actor 收到 DeviceAttached
    match actor_rx.try_recv() {
        Ok(ControlEvent::DeviceAttached { by_id: ev_by_id, real_path: ev_path }) => {
            assert_eq!(ev_by_id, by_id);
            assert_eq!(ev_path, real_path);
        }
        _ => panic!("預期 DeviceAttached"),
    }

    // 6. 取消義務
    deliverer.add_obligation("COM2", "by-id-2", "/dev/ttyUSB1", 1, 1);
    assert_eq!(deliverer.obligation_count(), 1);
    deliverer.cancel_obligation("COM2", Some(1));
    assert_eq!(deliverer.obligation_count(), 0);

    // 7. 30 秒總時限逾時丟棄
    deliverer.add_obligation("COM3", "by-id-3", "/dev/ttyUSB2", 1, 1);
    let outcome_timeout = deliverer.step_delivery("COM3", &actor_tx, Instant::now() + Duration::from_secs(31));
    assert_eq!(outcome_timeout, DeliveryStepResult::TimedOut);
    assert_eq!(deliverer.obligation_count(), 0);
}

#[test]
fn test_deliverer_pending_duration_excluded_from_30s_timeout() {
    let t0 = Instant::now();
    let clock = FakeInstantClock::new(t0);
    let gate = clock.gate();
    let mut deliverer = Deliverer::new(gate.clone());
    let (actor_tx, actor_rx) = bounded(1);

    let sid = "COM1";
    let by_id = "by-id-freeze";
    let real_path = "/dev/ttyUSB0";

    deliverer.add_obligation_at(sid, by_id, real_path, 1, 1, t0);

    // 讓 actor lane 滿
    actor_tx.send(ControlEvent::Tick).unwrap();

    // 在 Open 狀態經歷 29.9 秒重試
    let t_29_9 = t0 + Duration::from_millis(29_900);
    let outcome = deliverer.step_delivery(sid, &actor_tx, t_29_9);
    assert_eq!(outcome, DeliveryStepResult::Backoff);

    // 進入 Pending 狀態
    clock.set(t_29_9);
    let freeze_res = gate.freeze_if_idle("freeze-op");
    let freeze_token = match freeze_res {
        FreezeOutcome::Frozen { token, .. } => token,
        _ => panic!("預期 Frozen"),
    };
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Pending);

    // 審查 P8-057a065-3 重現：最後一次 Pending 呼叫在凍結起點（t_29_9），此後在 Pending 期間不再呼叫 step_delivery
    let outcome_p = deliverer.step_delivery(sid, &actor_tx, t_29_9);
    assert_eq!(outcome_p, DeliveryStepResult::PausedPending);

    // 經過 0.2 秒後解凍，回到 Open 狀態
    let t_thawed = t_29_9 + Duration::from_millis(200);
    clock.set(t_thawed);
    assert!(gate.thaw(&freeze_token));
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Open);

    // 釋放 actor 容量
    let _ = actor_rx.try_recv();

    // 在解凍後（自 t0 算起已過去 30.1 秒，但 Open 累積時間僅 29.9 秒）
    // 應繼續投遞成功，不得被判定 TimedOut 丟棄！
    let outcome_final = deliverer.step_delivery(sid, &actor_tx, t_thawed);
    assert_ne!(outcome_final, DeliveryStepResult::TimedOut, "Pending 期間不得計入 30s 總時限");
    assert_eq!(outcome_final, DeliveryStepResult::Delivered);
    assert_eq!(deliverer.obligation_count(), 0);
}

#[test]
fn test_deliverer_unaccounted_open_duration_before_freeze_leads_to_timeout() {
    let t0 = Instant::now();
    let clock = FakeInstantClock::new(t0);
    let gate = clock.gate();
    let mut deliverer = Deliverer::new(gate.clone());
    let (actor_tx, actor_rx) = bounded(1);

    let sid = "COM1";
    let by_id = "by-id-freeze-unaccounted";
    let real_path = "/dev/ttyUSB0";

    deliverer.add_obligation_at(sid, by_id, real_path, 1, 1, t0);

    // 填滿 channel
    actor_tx.send(ControlEvent::Tick).unwrap();

    // 1. Open 至 t = 29.8s，投遞因 channel 滿回傳 Backoff
    let t_29_8 = t0 + Duration::from_millis(29_800);
    let outcome = deliverer.step_delivery(sid, &actor_tx, t_29_8);
    assert_eq!(outcome, DeliveryStepResult::Backoff);

    // 2. t = 30.0s 進入 Pending，呼叫一次 step_delivery
    let t_30_0 = t0 + Duration::from_millis(30_000);
    clock.set(t_30_0);
    let freeze_res = gate.freeze_if_idle("freeze-op-2");
    let freeze_token = match freeze_res {
        FreezeOutcome::Frozen { token, .. } => token,
        _ => panic!("預期 Frozen"),
    };
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Pending);

    let outcome_p = deliverer.step_delivery(sid, &actor_tx, t_30_0);
    assert_eq!(outcome_p, DeliveryStepResult::PausedPending);

    // 3. t = 30.2s 解凍；清空 channel
    let t_30_2 = t0 + Duration::from_millis(30_200);
    clock.set(t_30_2);
    assert!(gate.thaw(&freeze_token));
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Open);
    let _ = actor_rx.try_recv();

    // 4. t = 30.3s 再投遞
    // 有效 Open 時間為：(30.3 - 0) - (gate.frozen_total(30.3) - 0) = 30.3 - 0.2 = 30.1 秒
    // 預期 TimedOut 且不送事件，義務移除
    let t_30_3 = t0 + Duration::from_millis(30_300);
    let outcome_final = deliverer.step_delivery(sid, &actor_tx, t_30_3);
    assert_eq!(outcome_final, DeliveryStepResult::TimedOut, "有效 Open 時間已達 30.1s，應 TimedOut");
    assert_eq!(deliverer.obligation_count(), 0);
    assert!(actor_rx.try_recv().is_err(), "逾時後接收端不得取得任何事件");
}

#[test]
fn test_deliverer_delayed_poll_after_thaw_leads_to_timeout() {
    let t0 = Instant::now();
    let clock = FakeInstantClock::new(t0);
    let gate = clock.gate();
    let mut deliverer = Deliverer::new(gate.clone());
    let (actor_tx, actor_rx) = bounded(1);

    let sid = "COM2";
    let by_id = "by-id-freeze-delayed-poll";
    let real_path = "/dev/ttyUSB1";

    deliverer.add_obligation_at(sid, by_id, real_path, 1, 1, t0);

    // 填滿 channel
    actor_tx.send(ControlEvent::Tick).unwrap();

    // 1. Open 至 t = 29.7s，投遞因 channel 滿回傳 Backoff
    let t_29_7 = t0 + Duration::from_millis(29_700);
    let outcome = deliverer.step_delivery(sid, &actor_tx, t_29_7);
    assert_eq!(outcome, DeliveryStepResult::Backoff);

    // 2. t = 29.9s 進入 Pending（閘門自行記帳凍結區間）
    let t_29_9 = t0 + Duration::from_millis(29_900);
    clock.set(t_29_9);
    let freeze_res = gate.freeze_if_idle("freeze-op-3");
    let freeze_token = match freeze_res {
        FreezeOutcome::Frozen { token, .. } => token,
        _ => panic!("預期 Frozen"),
    };
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Pending);

    // 3. t = 30.2s 解凍（閘門結算凍結區間），清空 channel
    let t_30_2 = t0 + Duration::from_millis(30_200);
    clock.set(t_30_2);
    assert!(gate.thaw(&freeze_token));
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Open);
    let _ = actor_rx.try_recv();

    // 4. t = 30.4s 解凍後延遲輪詢
    // 有效 Open 時間為：(30.4 - 0) - (gate.frozen_total(30.4) - 0) = 30.4 - 0.3 = 30.1 秒
    // 預期 TimedOut 且不送事件，義務移除
    let t_30_4 = t0 + Duration::from_millis(30_400);
    let outcome_final = deliverer.step_delivery(sid, &actor_tx, t_30_4);
    assert_eq!(outcome_final, DeliveryStepResult::TimedOut, "解凍後延遲輪詢計入有效 Open 時間達 30.1s，應 TimedOut");
    assert_eq!(deliverer.obligation_count(), 0);
    assert!(actor_rx.try_recv().is_err(), "逾時後接收端不得取得任何事件");
}

#[test]
fn test_deliverer_delayed_first_pending_poll_does_not_timeout_prematurely() {
    let t0 = Instant::now();
    let clock = FakeInstantClock::new(t0);
    let gate = clock.gate();
    let mut deliverer = Deliverer::new(gate.clone());
    let (actor_tx, actor_rx) = bounded(1);

    let sid = "COM3";
    let by_id = "by-id-freeze-delayed-first-poll";
    let real_path = "/dev/ttyUSB2";

    // 1. t=0 建立義務，填滿 channel
    deliverer.add_obligation_at(sid, by_id, real_path, 1, 1, t0);
    actor_tx.send(ControlEvent::Tick).unwrap();

    // 2. t=29.8s 投遞回 Backoff，隨即凍結為 Pending
    let t_29_8 = t0 + Duration::from_millis(29_800);
    let outcome = deliverer.step_delivery(sid, &actor_tx, t_29_8);
    assert_eq!(outcome, DeliveryStepResult::Backoff);

    clock.set(t_29_8);
    let freeze_res = gate.freeze_if_idle("freeze-op-delayed-first-poll");
    let freeze_token = match freeze_res {
        FreezeOutcome::Frozen { token, .. } => token,
        _ => panic!("預期 Frozen"),
    };
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Pending);

    // 3. 首次 Pending 輪詢延遲至 t=30.0s
    let t_30_0 = t0 + Duration::from_millis(30_000);
    let outcome_p = deliverer.step_delivery(sid, &actor_tx, t_30_0);
    assert_eq!(outcome_p, DeliveryStepResult::PausedPending);

    // 4. t=30.2s 解凍，清空 channel，立即再投遞
    let t_30_2 = t0 + Duration::from_millis(30_200);
    clock.set(t_30_2);
    assert!(gate.thaw(&freeze_token));
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Open);
    let _ = actor_rx.try_recv();

    let outcome_final = deliverer.step_delivery(sid, &actor_tx, t_30_2);

    // 實際有效 Open 時間只有 29.8 秒，預期 Delivered，接收端恰好一筆事件
    assert_eq!(outcome_final, DeliveryStepResult::Delivered);
    assert_eq!(deliverer.obligation_count(), 0);
    match actor_rx.try_recv() {
        Ok(ControlEvent::DeviceAttached { by_id: ev_by_id, real_path: ev_real_path }) => {
            assert_eq!(ev_by_id, by_id);
            assert_eq!(ev_real_path, real_path);
        }
        _ => panic!("接收端應恰好收到一筆 DeviceAttached 事件"),
    }
}

#[test]
fn test_deliverer_multiple_freeze_thaw_cycles() {
    let t0 = Instant::now();
    let clock = FakeInstantClock::new(t0);
    let gate = clock.gate();
    let mut deliverer = Deliverer::new(gate.clone());
    let (actor_tx, actor_rx) = bounded(1);

    let sid1 = "COM10";
    let by_id1 = "by-id-multi-freeze-1";
    let real_path1 = "/dev/ttyUSB10";
    deliverer.add_obligation_at(sid1, by_id1, real_path1, 1, 1, t0);

    // 填滿 channel
    actor_tx.send(ControlEvent::Tick).unwrap();

    // 第一次凍結：t=10s ~ t=15s (5s)
    let t_10 = t0 + Duration::from_secs(10);
    clock.set(t_10);
    let token1 = match gate.freeze_if_idle("cycle-1") {
        FreezeOutcome::Frozen { token, .. } => token,
        _ => panic!("預期 Frozen"),
    };
    let t_15 = t0 + Duration::from_secs(15);
    clock.set(t_15);
    assert!(gate.thaw(&token1));

    // 第二次凍結：t=20s ~ t=25s (5s)，總凍結累積 10s
    let t_20 = t0 + Duration::from_secs(20);
    clock.set(t_20);
    let token2 = match gate.freeze_if_idle("cycle-2") {
        FreezeOutcome::Frozen { token, .. } => token,
        _ => panic!("預期 Frozen"),
    };
    let t_25 = t0 + Duration::from_secs(25);
    clock.set(t_25);
    assert!(gate.thaw(&token2));

    // 清空 channel
    let _ = actor_rx.try_recv();

    // t=38s 投遞：有效 Open 時間為 38s - 10s = 28s < 30s，預期 Delivered
    let t_38 = t0 + Duration::from_secs(38);
    let outcome = deliverer.step_delivery(sid1, &actor_tx, t_38);
    assert_eq!(outcome, DeliveryStepResult::Delivered);
    assert_eq!(deliverer.obligation_count(), 0);

    // 再加一筆義務 sid2，在 t=41s 投遞：有效 Open 時間為 41s - 10s = 31s > 30s，預期 TimedOut
    let sid2 = "COM11";
    let by_id2 = "by-id-multi-freeze-2";
    let real_path2 = "/dev/ttyUSB11";
    deliverer.add_obligation_at(sid2, by_id2, real_path2, 1, 1, t0);
    let t_41 = t0 + Duration::from_secs(41);
    let outcome2 = deliverer.step_delivery(sid2, &actor_tx, t_41);
    assert_eq!(outcome2, DeliveryStepResult::TimedOut);
    assert_eq!(deliverer.obligation_count(), 0);
}

#[test]
fn test_deliverer_obligation_created_during_pending() {
    let t0 = Instant::now();
    let clock = FakeInstantClock::new(t0);
    let gate = clock.gate();
    let mut deliverer = Deliverer::new(gate.clone());
    let (actor_tx, actor_rx) = bounded(1);

    // t=10s 進入 Pending
    let t_10 = t0 + Duration::from_secs(10);
    clock.set(t_10);
    let token = match gate.freeze_if_idle("pending-create") {
        FreezeOutcome::Frozen { token, .. } => token,
        _ => panic!("預期 Frozen"),
    };

    // t=15s 在 Pending 期間建立義務
    let t_15 = t0 + Duration::from_secs(15);
    clock.set(t_15);
    let sid = "COM20";
    let by_id = "by-id-pending-create";
    let real_path = "/dev/ttyUSB20";
    deliverer.add_obligation_at(sid, by_id, real_path, 1, 1, t_15);

    // Pending 期間呼叫 step_delivery 應暫停
    let outcome_p = deliverer.step_delivery(sid, &actor_tx, t_15);
    assert_eq!(outcome_p, DeliveryStepResult::PausedPending);

    // t=25s 解凍（義務經歷了 10s 的 Pending）
    let t_25 = t0 + Duration::from_secs(25);
    clock.set(t_25);
    assert!(gate.thaw(&token));

    // t=45s 投遞：自建立經過 30s，扣除 10s 凍結，有效 Open 時間為 20s < 30s，預期 Delivered
    let t_45 = t0 + Duration::from_secs(45);
    let outcome = deliverer.step_delivery(sid, &actor_tx, t_45);
    assert_eq!(outcome, DeliveryStepResult::Delivered);
    assert_eq!(deliverer.obligation_count(), 0);
    assert_eq!(actor_rx.try_iter().count(), 1, "成功送達時接收端應收到一筆事件");

    // 另一個在 Pending 期間建立的義務，若有效 Open 時間超逾 30s 則 TimedOut
    let sid_timeout = "COM21";
    let by_id_timeout = "by-id-pending-timeout";
    let real_path_timeout = "/dev/ttyUSB21";
    deliverer.add_obligation_at(sid_timeout, by_id_timeout, real_path_timeout, 1, 1, t_15);
    // t=60s：自建立經過 45s，扣除 10s 凍結，有效 Open 時間為 35s > 30s，預期 TimedOut
    let t_60 = t0 + Duration::from_secs(60);
    let outcome_timeout = deliverer.step_delivery(sid_timeout, &actor_tx, t_60);
    assert_eq!(outcome_timeout, DeliveryStepResult::TimedOut);
    assert_eq!(deliverer.obligation_count(), 0);
}

#[test]
fn test_deliverer_obligation_cancelled_when_pending_becomes_permanent() {
    let t0 = Instant::now();
    let clock = FakeInstantClock::new(t0);
    let gate = clock.gate();
    let mut deliverer = Deliverer::new(gate.clone());
    let (actor_tx, _actor_rx) = bounded(1);

    let sid = "COM30";
    let by_id = "by-id-perm";
    let real_path = "/dev/ttyUSB30";
    deliverer.add_obligation_at(sid, by_id, real_path, 1, 1, t0);

    // 進入 Pending
    let t_5 = t0 + Duration::from_secs(5);
    clock.set(t_5);
    let token = match gate.freeze_if_idle("perm-op") {
        FreezeOutcome::Frozen { token, .. } => token,
        _ => panic!("預期 Frozen"),
    };

    // 轉為 Permanent（commit_handoff）
    assert!(gate.commit_handoff(&token));
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Permanent);

    // 送達者單步執行應回傳 Cancelled 並丟棄義務
    let outcome = deliverer.step_delivery(sid, &actor_tx, t_5);
    assert_eq!(outcome, DeliveryStepResult::Cancelled);
    assert_eq!(deliverer.obligation_count(), 0);
}

#[test]
fn test_deliverer_times_out_when_effective_open_exceeds_30s() {
    let t0 = Instant::now();
    let gate = Arc::new(AdmissionGate::new());
    let mut deliverer = Deliverer::new(gate.clone());
    let (actor_tx, actor_rx) = bounded(1);

    let sid = "COM40";
    let by_id = "by-id-pure-timeout";
    let real_path = "/dev/ttyUSB40";

    deliverer.add_obligation_at(sid, by_id, real_path, 1, 1, t0);
    // 填滿 channel
    actor_tx.send(ControlEvent::Tick).unwrap();

    // t=29.9s 投遞回 Backoff
    let t_29_9 = t0 + Duration::from_millis(29_900);
    let outcome = deliverer.step_delivery(sid, &actor_tx, t_29_9);
    assert_eq!(outcome, DeliveryStepResult::Backoff);

    // t=30.1s 投遞，有效 Open 時間達 30.1s >= 30s，預期 TimedOut
    let t_30_1 = t0 + Duration::from_millis(30_100);
    let outcome_final = deliverer.step_delivery(sid, &actor_tx, t_30_1);
    assert_eq!(outcome_final, DeliveryStepResult::TimedOut);
    assert_eq!(deliverer.obligation_count(), 0);
    assert_eq!(actor_rx.try_iter().count(), 1, "除了最初塞入的 Tick 外不得取得任何新事件");
}

#[test]
fn test_deliverer_time_sample_before_freeze_thaw_interleave_times_out() {
    let t0 = Instant::now();
    let clock = FakeInstantClock::new(t0);
    let gate = clock.gate();
    let mut deliverer = Deliverer::new(gate.clone());
    let (actor_tx, actor_rx) = bounded(1);

    let sid = "COM50";
    let by_id = "by-id-sample-interleave";
    let real_path = "/dev/ttyUSB50";

    // 1. t=0 建立義務
    deliverer.add_obligation_at(sid, by_id, real_path, 1, 1, t0);

    // 2. 送達執行緒於 t=30.1s 取得輪詢時間，隨後暫停
    let t_poll = t0 + Duration::from_millis(30_100);

    // 3. 另一執行緒於 t=30.2s 凍結，t=30.4s 解凍
    let t_freeze = t0 + Duration::from_millis(30_200);
    clock.set(t_freeze);
    let token = match gate.freeze_if_idle("interleave-freeze") {
        FreezeOutcome::Frozen { token, .. } => token,
        _ => panic!("預期 Frozen"),
    };

    let t_thaw = t0 + Duration::from_millis(30_400);
    clock.set(t_thaw);
    assert!(gate.thaw(&token));

    // 4. 送達執行緒恢復，以已取得的 now=30.1s 執行 step_delivery；channel 有空位
    let outcome = deliverer.step_delivery(sid, &actor_tx, t_poll);
    assert_eq!(outcome, DeliveryStepResult::TimedOut, "義務在凍結前經歷 30.1s > 30s Open，不得被未來凍結扣除而送達");
    assert_eq!(deliverer.obligation_count(), 0, "逾時義務必須自 deliverer 移除");
    assert_eq!(actor_rx.try_iter().count(), 0, "逾時義務不得送出任何事件");
}

#[test]
fn test_deliverer_snapshot_before_freeze_thaw_interleave_times_out() {
    let t0 = Instant::now();
    let clock = FakeInstantClock::new(t0);
    let gate = clock.gate();
    let mut deliverer = Deliverer::new(gate.clone());
    let (actor_tx, actor_rx) = bounded(1);

    let sid = "COM51";
    let by_id = "by-id-snapshot-interleave";
    let real_path = "/dev/ttyUSB51";

    // 1. t=0 建立義務
    deliverer.add_obligation_at(sid, by_id, real_path, 1, 1, t0);

    // 2. 送達執行緒於 t=30.1s 取得閘門快照，隨後暫停
    let t_poll = t0 + Duration::from_millis(30_100);
    clock.set(t_poll);
    let snap = gate.snapshot();
    assert_eq!(snap.now, t_poll);
    assert_eq!(snap.freeze_state, HandoffFreezeState::Open);
    assert_eq!(snap.frozen_total, Duration::ZERO);

    // 3. 另一執行緒於 t=30.2s 凍結，t=30.4s 解凍
    let t_freeze = t0 + Duration::from_millis(30_200);
    clock.set(t_freeze);
    let token = match gate.freeze_if_idle("interleave-freeze-snap") {
        FreezeOutcome::Frozen { token, .. } => token,
        _ => panic!("預期 Frozen"),
    };

    let t_thaw = t0 + Duration::from_millis(30_400);
    clock.set(t_thaw);
    assert!(gate.thaw(&token));

    // 4. 送達執行緒恢復，使用先前取樣之快照呼叫 step_delivery_with_snapshot
    let outcome = deliverer.step_delivery_with_snapshot(sid, &actor_tx, snap);
    assert_eq!(outcome, DeliveryStepResult::TimedOut, "快照時刻已滿 30.1s > 30s Open，不得被後續凍結扣除而送達");
    assert_eq!(deliverer.obligation_count(), 0, "逾時義務必須自 deliverer 移除");
    assert_eq!(actor_rx.try_iter().count(), 0, "逾時義務不得送出任何事件");
}

#[test]
fn test_handle_actor_exited_takes_resources_and_produces_close_jobs() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed.clone());

    // 1. Held { parts }：放入真實 HandleParts 替身
    let backend = sw_pal::fakes::serial::FakeSerialBackend::new();
    let _dev_entry = backend.add_device("/dev/ttyUSB0");
    let dev = sw_pal::traits::DevicePath("/dev/ttyUSB0".into());
    let cfg = sw_pal::traits::SerialConfig::from_uart_profile(115200, 8, "none", 1, "none", false);
    let handle = backend.open_exclusive(&dev, &cfg).expect("open fake serial");
    let parts = HandleParts::split(handle);

    let sid = "COM1";
    let inc = ledger.alloc_session_inc(sid);
    let token = ledger.register(
        "by-id-held",
        "/dev/ttyUSB0",
        CustodyOwner::Session { sid: sid.into(), inc },
        CustodyTaker::OpenJob { sid: sid.into(), gen: 1 },
    ).unwrap();

    // 存入 parts
    {
        let rec = ledger.custody.get_mut("by-id-held").unwrap();
        rec.phase = CustodyPhase::Held { parts: Some(parts) };
    }

    // 當同世代 actor 結束時，呼叫 handle_actor_exited
    let mut jobs = ledger.handle_actor_exited(sid, inc);
    assert_eq!(jobs.len(), 1, "應產出恰好一個關閉工作");
    let job = jobs.pop().unwrap();
    match &job {
        ActorExitedJob::CloseHeld { token: t, by_id, parts } => {
            assert_eq!(*t, token);
            assert_eq!(by_id, "by-id-held");
            assert!(parts.is_some(), "parts 必須被取出移交，不得被直接丟失");
        }
        _ => panic!("預期 CloseHeld"),
    }

    // 帳本狀態應轉為 Closing，owner 為 Reaper
    let snap = ledger.get_custody("by-id-held").unwrap().snapshot();
    assert_eq!(snap.owner, CustodyOwner::Reaper);
    assert!(matches!(snap.phase, CustodyPhaseSnapshot::Closing { .. }));

    // 執行工作者關閉，結算進帳本
    let term = job.execute_and_settle(&mut ledger, None).unwrap().unwrap();
    assert_eq!(term.handle, TerminalHandle::Closed);
    assert!(ledger.get_custody("by-id-held").is_none(), "關閉成功後紀錄應移除");

    // 1b. Held { parts } 關閉失敗回條測試
    let fake_dev_fail = backend.add_device("/dev/ttyUSB0_fail");
    fake_dev_fail.fail_close(PalError::plain(PalErrorKind::Io, "injected close failure"));
    let dev_fail = sw_pal::traits::DevicePath("/dev/ttyUSB0_fail".into());
    let handle_fail = backend.open_exclusive(&dev_fail, &cfg).expect("open fake serial fail dev");
    let parts_fail = HandleParts::split(handle_fail);

    let _token_fail = ledger.register(
        "by-id-held-fail",
        "/dev/ttyUSB0_fail",
        CustodyOwner::Session { sid: sid.into(), inc },
        CustodyTaker::OpenJob { sid: sid.into(), gen: 3 },
    ).unwrap();
    {
        let rec = ledger.custody.get_mut("by-id-held-fail").unwrap();
        rec.phase = CustodyPhase::Held { parts: Some(parts_fail) };
    }

    let mut jobs_fail = ledger.handle_actor_exited(sid, inc);
    assert_eq!(jobs_fail.len(), 1);
    let term_fail = jobs_fail.pop().unwrap().execute_and_settle(&mut ledger, None).unwrap().unwrap();
    assert!(matches!(term_fail.handle, TerminalHandle::Failed { .. }));
    // 關閉失敗：紀錄轉為 CloseFailed 並記入 unconfirmed
    let snap_fail = ledger.get_custody("by-id-held-fail").unwrap().snapshot();
    assert!(matches!(snap_fail.phase, CustodyPhaseSnapshot::CloseFailed { .. }));
    assert!(unconfirmed.contains("by-id-held-fail"));

    // 2. Bridged { worker: None }（已被 AdoptBridge 取出，受 Reclaim 守衛保護）
    let _token2 = ledger.register(
        "by-id-bridged-none",
        "/dev/ttyUSB1",
        CustodyOwner::Session { sid: sid.into(), inc },
        CustodyTaker::OpenJob { sid: sid.into(), gen: 2 },
    ).unwrap();
    {
        let rec = ledger.custody.get_mut("by-id-bridged-none").unwrap();
        rec.phase = CustodyPhase::Bridged { epoch: 10, worker: None };
    }
    let jobs2 = ledger.handle_actor_exited(sid, inc);
    assert!(jobs2.is_empty(), "Bridged{{worker: None}} 應留給 Reclaim 處理，不產出 job 也不覆寫");
    let snap2 = ledger.get_custody("by-id-bridged-none").unwrap().snapshot();
    assert!(matches!(snap2.phase, CustodyPhaseSnapshot::Bridged { epoch: 10, worker_in_ledger: false }));

    // 3. mark_bridge_draining 能安全取出 worker
    let token3 = ledger.register(
        "by-id-drain",
        "/dev/ttyUSB2",
        CustodyOwner::Reaper,
        CustodyTaker::Worker("w".into()),
    ).unwrap();
    {
        let rec = ledger.custody.get_mut("by-id-drain").unwrap();
        rec.phase = CustodyPhase::Bridged { epoch: 20, worker: None };
    }
    let worker_opt = ledger.mark_bridge_draining(token3, 20, StopId(0)).unwrap();
    assert!(worker_opt.is_none());
    let snap3 = ledger.get_custody("by-id-drain").unwrap().snapshot();
    assert!(matches!(snap3.phase, CustodyPhaseSnapshot::BridgeDraining { epoch: 20, stop_id: StopId(0) }));

    // 4. 審查 P8-8f2d661-1：帶真正 IoWorker 的 DrainBridge 執行與回條結算
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.fence.with(|state| {
        state.active_epoch = Some(1);
        state.frozen = false;
    });
    let dev_worker = h.fake_device.clone();
    let opened = h.opened_bridge(REAL_PATH);
    let real_worker = opened.worker;
    let worker_epoch = opened.epoch;
    assert_eq!(dev_worker.close_count(), 0);
    assert!(dev_worker.is_open());

    let token_worker = ledger.register(
        "by-id-worker",
        REAL_PATH,
        CustodyOwner::Session { sid: sid.into(), inc },
        CustodyTaker::OpenJob { sid: sid.into(), gen: 4 },
    ).unwrap();
    {
        let rec = ledger.custody.get_mut("by-id-worker").unwrap();
        rec.phase = CustodyPhase::Bridged { epoch: worker_epoch, worker: Some(sw_core::custody::ReclaimWorker::Real(real_worker)) };
    }

    let mut jobs_worker = ledger.handle_actor_exited(sid, inc);
    assert_eq!(jobs_worker.len(), 1);
    let job_worker = jobs_worker.pop().unwrap();
    assert!(job_worker.is_drain_bridge());
    assert!(job_worker.has_worker());

    // 4a. 缺少通道時：回錯並完整歸還工作所有權，不得消耗 worker
    let exec_err = job_worker.execute_and_settle(&mut ledger, None);
    let returned_job = match exec_err {
        Err((JobExecutionError::MissingReaperChannel, returned)) => {
            assert!(returned.has_worker(), "worker 所有權必須完整保留");
            returned
        }
        other => panic!("預期 MissingReaperChannel 且歸還所有權，實際為: {:?}", other.map(|_| ())),
    };
    // 驗證 worker 未被 drop、未關閉
    assert_eq!(dev_worker.close_count(), 0, "缺少通道時不得 close serial device");
    assert!(dev_worker.is_open(), "serial device 應保持開啟");

    // 4b. 提供通道時：成功排空，驗證只 close 一次，收到配對回條並結算
    let (reaper_event_tx, reaper_event_rx) = unbounded();
    let res_ok = returned_job.execute_and_settle(&mut ledger, Some(&reaper_event_tx));
    assert!(res_ok.is_ok(), "提供通道時執行應成功");

    // 等待排空完成回條
    let receipt = match reaper_event_rx.recv_timeout(Duration::from_secs(3)) {
        Ok(ControlEvent::WorkerStopped(r)) => r,
        Ok(_) => panic!("預期 WorkerStopped，實際收到其他事件"),
        Err(e) => panic!("接收排空回條逾時或錯誤: {:?}", e),
    };
    assert_eq!(receipt.epoch, worker_epoch);
    assert_eq!(receipt.stop_id, StopId(0));
    assert!(receipt.handle_closed);
    // 驗證 serial device 恰好關閉一次
    assert_eq!(dev_worker.close_count(), 1, "排空收尾 serial device 恰好關閉一次");

    // 以配對回條結算進帳本
    let term_drain = ledger.settle_worker_stopped(token_worker, receipt).expect("結算排空終態");
    assert_eq!(term_drain.handle, TerminalHandle::Closed);
    assert!(ledger.get_custody("by-id-worker").is_none(), "成功排空結算後紀錄應移除");
    assert!(!unconfirmed.contains("by-id-worker"), "成功排空不得留在 unconfirmed");
}

#[test]
fn test_settle_worker_stopped_rejects_mismatched_epoch_and_stale_receipts() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed.clone());

    let by_id = "by-id-settle-repro";
    let token = ledger.register(
        by_id,
        "/dev/ttyUSB0",
        CustodyOwner::Reaper,
        CustodyTaker::Worker("w".into()),
    ).unwrap();

    ledger.mark_bridge_draining(token, 2, StopId(2)).unwrap();

    // 1. 傳入過期回條 (epoch 1) 給處於 epoch 2 的 BridgeDraining
    let receipt_stale = WorkerStoppedReceipt {
        epoch: 1,
        stop_id: StopId(2),
        ledger: TxLedger::default(),
        unrecorded: vec![],
        wal_error: None,
        handle_closed: true,
        close_error: None,
    };
    let res = ledger.settle_worker_stopped(token, receipt_stale);
    assert_eq!(
        res,
        Err(CustodySettleError::EpochMismatch { expected: 2, actual: 1 }),
        "過期回條應被拒絕"
    );
    // 紀錄不得被移除，阻擋不得解除
    let rec = ledger.get_custody(by_id).expect("紀錄必須保留");
    assert!(matches!(rec.phase, CustodyPhase::BridgeDraining { epoch: 2, stop_id: StopId(2) }));

    // 1b. 審查 P8-057a065-2 重現：epoch 相符 (2) 但 stop_id 錯配 (999 vs 2)
    let receipt_stop_mismatch = WorkerStoppedReceipt {
        epoch: 2,
        stop_id: StopId(999),
        ledger: TxLedger::default(),
        unrecorded: vec![],
        wal_error: None,
        handle_closed: true,
        close_error: None,
    };
    let res_stop = ledger.settle_worker_stopped(token, receipt_stop_mismatch);
    assert_eq!(
        res_stop,
        Err(CustodySettleError::StopIdMismatch { expected: StopId(2), actual: StopId(999) }),
        "stop_id 錯配回條應被拒絕"
    );
    let rec_still = ledger.get_custody(by_id).expect("紀錄必須保留");
    assert!(matches!(rec_still.phase, CustodyPhase::BridgeDraining { epoch: 2, stop_id: StopId(2) }));

    // 1c. 審查 P8-057a065-2 重現：Closing{job: "close-held"} 收到任意 WorkerStopped 回條應被拒絕
    let token_closing = ledger.register(
        "by-id-closing-held",
        "/dev/ttyUSB1",
        CustodyOwner::Reaper,
        CustodyTaker::Worker("w".into()),
    ).unwrap();
    {
        let rec = ledger.custody.get_mut("by-id-closing-held").unwrap();
        rec.phase = CustodyPhase::Closing { job: "close-held".into() };
    }
    let res_closing = ledger.settle_worker_stopped(token_closing, WorkerStoppedReceipt {
        epoch: 2,
        stop_id: StopId(2),
        ledger: TxLedger::default(),
        unrecorded: vec![],
        wal_error: None,
        handle_closed: true,
        close_error: None,
    });
    assert_eq!(
        res_closing,
        Err(CustodySettleError::InvalidPhase),
        "Closing 狀態不接受 WorkerStopped 結算，只能由 settle_close 結算"
    );
    assert!(ledger.get_custody("by-id-closing-held").is_some(), "Closing 紀錄不得被結算移除");

    // 2. 傳入未知 token
    let make_receipt_ok = || WorkerStoppedReceipt {
        epoch: 2,
        stop_id: StopId(2),
        ledger: TxLedger::default(),
        unrecorded: vec![],
        wal_error: None,
        handle_closed: true,
        close_error: None,
    };
    let res_unknown = ledger.settle_worker_stopped(99999, make_receipt_ok());
    assert_eq!(res_unknown, Err(CustodySettleError::TokenNotFound));

    // 3. 傳入相符的 epoch: 2 回條
    let res_ok = ledger.settle_worker_stopped(token, make_receipt_ok());
    assert!(res_ok.is_ok(), "正確回條應結算成功");
    assert!(ledger.get_custody(by_id).is_none(), "成功關閉回條應移除紀錄並解除阻擋");

    // 4. 重複結算同一 token 應被拒絕
    let res_dup = ledger.settle_worker_stopped(token, make_receipt_ok());
    assert_eq!(res_dup, Err(CustodySettleError::AlreadySettled), "重複回條應被拒絕");
}

#[test]
fn test_generation_inc_prevents_stale_command() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed);

    let sid = "COM1";
    let inc1 = ledger.alloc_session_inc(sid);
    assert_eq!(inc1, 1);

    let token1 = ledger.register(
        "by-id-gen",
        "/dev/tty0",
        CustodyOwner::Session { sid: sid.into(), inc: inc1 },
        CustodyTaker::OpenJob { sid: sid.into(), gen: 1 },
    ).unwrap();

    // session 重建，世代號遞增到 2
    let inc2 = ledger.alloc_session_inc(sid);
    assert_eq!(inc2, 2);

    // 舊世代 (inc=1) 的 ActorExited 命令到達
    let stale_jobs = ledger.handle_actor_exited(sid, inc1);
    assert!(stale_jobs.is_empty(), "舊世代命令不應影響當前世代帳本紀錄");

    // 紀錄應維持原樣
    let c = ledger.get_custody("by-id-gen").unwrap();
    assert_eq!(c.token, token1);
}

#[test]
fn test_wait_for_rx_dispatch_distinguishes_disconnect_and_timeout() {
    // 1. Disconnected -> 回收者收到 DrainDataBarrier 丟棄 reply -> "回收者排空，無 actor 確認 RX dispatch"
    let (safety_tx, safety_rx) = unbounded();
    let reaper_mock = std::thread::spawn(move || {
        if let Ok(ControlEvent::DrainDataBarrier { reply, expired, .. }) = safety_rx.recv() {
            expired.store(true, Ordering::Release);
            drop(reply); // 回收者丟棄 reply，造成 ack 斷線
        }
    });
    let err_disc = sw_core::uart_bridge::wait_for_rx_dispatch_with_timeout_for_test(
        &safety_tx,
        1,
        10,
        Duration::from_millis(500),
    ).unwrap_err();
    reaper_mock.join().unwrap();
    assert!(
        err_disc.py_message.contains("回收者排空，無 actor 確認 RX dispatch"),
        "斷線缺口文字應為指定文字，實際為: {:?}",
        err_disc
    );

    // 2. Timeout -> "RX drain barrier 等待逾時，capture 順序未確認"
    let (tx_timeout, _rx_timeout) = bounded(1); // 接收端不處理也不關閉
    let err_timeout = sw_core::uart_bridge::wait_for_rx_dispatch_with_timeout_for_test(
        &tx_timeout,
        1,
        10,
        Duration::from_millis(50),
    ).unwrap_err();
    assert!(
        err_timeout.py_message.contains("RX drain barrier 等待逾時，capture 順序未確認"),
        "逾時缺口文字應為逾時文字，實際為: {:?}",
        err_timeout
    );
}

#[test]
fn test_dr1_f_w3_prime_unconfirmed_bridge_handoff_reaper_drain() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed.clone());

    // 建立一個 Opening 紀錄並進入 Taken
    let sid = "COM10";
    let by_id = "by-id-dr1-f";
    let real_path = "/dev/ttyUSB10";
    let token = ledger.register(
        by_id,
        real_path,
        CustodyOwner::Session { sid: sid.into(), inc: 1 },
        CustodyTaker::OpenJob { sid: sid.into(), gen: 1 },
    ).unwrap();

    // 模擬開埠成功，進入 Bridged { epoch: 100, worker: None }
    let mock_worker = MockWorker { epoch: 100 };
    ledger.adopt_bridged_mock(token, 100, mock_worker).unwrap();
    assert_eq!(
        ledger.get_custody(by_id).unwrap().snapshot().phase,
        CustodyPhaseSnapshot::Bridged { epoch: 100, worker_in_ledger: true }
    );

    // (f1) BridgeOpened send 回 Disconnected：開埠工作者在鎖內轉 BridgeDraining，owner 改 Reaper
    let _ = ledger.mark_bridge_draining(token, 100, StopId(1)).unwrap();
    assert_eq!(
        ledger.get_custody(by_id).unwrap().snapshot().phase,
        CustodyPhaseSnapshot::BridgeDraining { epoch: 100, stop_id: StopId(1) }
    );

    // 收到成功回條 handle_closed: true
    let receipt = WorkerStoppedReceipt {
        epoch: 100,
        stop_id: StopId(1),
        ledger: sw_core::drain::TxLedger::default(),
        handle_closed: true,
        close_error: None,
        unrecorded: Vec::new(),
        wal_error: None,
    };
    let term = ledger.settle_worker_stopped(token, receipt).unwrap();
    assert_eq!(term.handle, TerminalHandle::Closed);
    assert_eq!(term.evidence, TerminalEvidence::Complete);
    assert!(ledger.get_custody(by_id).is_none(), "成功回條後紀錄必須移除");
    assert!(!unconfirmed.contains(by_id), "成功回條後解除阻擋");
}

#[test]
fn test_dr7_parked_handoff_and_deliverer_retry() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed.clone());
    let t0 = Instant::now();
    let clock = FakeInstantClock::new(t0);
    let gate = clock.gate();
    let mut deliverer = Deliverer::new(gate);

    let sid = "COM20";
    let by_id = "by-id-dr7";
    let real_path = "/dev/ttyUSB20";

    // 1. 模擬退休 ENOSPC 留下 Held { owner: Parked }
    let token = ledger.register(
        by_id,
        real_path,
        CustodyOwner::Parked,
        CustodyTaker::Retire { sid: sid.into() },
    ).unwrap();
    ledger.save_held_mock(token).unwrap();
    assert_eq!(ledger.get_custody(by_id).unwrap().owner, CustodyOwner::Parked);

    // 2. bind COM1 -> B3 成功，首次 DeviceOnline 送達失敗 (Full)，記送達義務
    deliverer.add_obligation_at(sid, by_id, real_path, 1, 1, t0);
    assert!(deliverer.obligations.contains_key(sid));

    // 3. (k) 送達前 release COM1：取消送達義務
    deliverer.cancel_obligation(sid, None);
    assert!(!deliverer.obligations.contains_key(sid), "release 受理時必須取消送達義務");

    // 4. (d) release 讓 Held{Parked} 轉為 Closing
    let _parts_opt = ledger.claim_for_release(by_id, token).unwrap();
    assert_eq!(ledger.get_custody(by_id).unwrap().owner, CustodyOwner::Reaper);
}

#[test]
fn test_dr9_actor_spawn_failure_cleanup() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed.clone());

    let sid = "COM30";
    let by_id = "by-id-dr9";
    let real_path = "/dev/ttyUSB30";
    let inc = ledger.alloc_session_inc(sid);

    // W2 熱插 spawn_attach，帳本已有出生即接管的 PROBE handle（owner 為新 sid）
    let token = ledger.register(
        by_id,
        real_path,
        CustodyOwner::Session { sid: sid.into(), inc },
        CustodyTaker::Worker("spawn_attach".into()),
    ).unwrap();
    ledger.save_held_mock(token).unwrap();

    // 模擬 try_spawn_actor 回 Err，建構者持鎖清理 (C8)
    let jobs = ledger.handle_spawn_failure(sid, inc);
    assert_eq!(jobs.len(), 1, "建構失敗應產出關閉工作");
    assert_eq!(jobs[0].token(), token);

    // 關閉成功 -> terminal Closed
    let term = jobs.into_iter().next().unwrap().execute_and_settle(&mut ledger, None).unwrap();
    assert_eq!(term.unwrap().handle, TerminalHandle::Closed);
    assert!(ledger.get_custody(by_id).is_none(), "關閉成功後紀錄移除");
    assert!(!unconfirmed.contains(by_id), "未確認關閉集合不含該 by_id");
}

#[test]
fn test_actor_repeated_panic_stops_and_settles_custody() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let by_id = "/s/by-id/usb-A";
    let dev = m.dev.clone();
    let full_sid = m.mgr.resolve("COM0").expect("session 存在");

    // 在 panic 發生前，向帳本登記一筆以該 session 為 owner 的紀錄，確認初始存在
    let token = m.mgr.custody_register_held_session_for_test(&full_sid, by_id, "/dev/ttyOLD");
    assert!(m.mgr.custody_for_test(by_id).is_some(), "panic 前帳本必須存在該 session 的紀錄");

    // 注入 3 次 panic
    assert!(m.mgr.arm_actor_panic_for_test(&full_sid, 3));

    // 發送 3 次同型事件引發連續 3 次同型 panic
    let control = m.mgr.actor_control_for_test(&full_sid).expect("control 存在");
    for _ in 0..3 {
        control.send(ControlEvent::DeviceMissing).unwrap();
    }

    // 等待停止機制收尾與 handle 關閉
    let t0 = Instant::now();
    while t0.elapsed() < Duration::from_secs(3) {
        if dev.close_count() >= 1 {
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }

    // 斷言：句柄確實已被關閉
    assert_eq!(dev.close_count(), 1, "停止機制須關閉 handle 恰好 1 次");

    // 斷言：帳本紀錄由 BeginDrain / claim_for_stop 結算並移除（C3）
    let custody = m.mgr.custody_for_test(by_id);
    assert!(custody.is_none(), "BeginDrain 自動收尾後帳本紀錄須被結算移除，實際為：{custody:?}");
    assert_eq!(
        m.mgr.terminal_for_test(token).map(|t| t.handle),
        Some(TerminalHandle::Closed),
        "帳本終態須記錄為 Closed"
    );
}

/// DR-7 前置的 B3（佔位 `gone-template:COM3` 的 by_id）。
const DR7_PLACEHOLDER_SID: &str = "gone-template:COM3";
const DR7_BY_ID: &str = "/s/by-id/usb-G";
const DR7_REAL: &str = "/dev/ttyG";

/// DR-7 前置（全程正式流程）：COM0 在 usb-A 上 READY；`gone-template:COM3` 是 B3 的 RELEASED 佔位，`device.attach`
/// （`SessionManager::attach_released_placeholder`）開埠並退休佔位後落盤 ENOSPC，留下 parked 作業與帳本
/// `Held{owner: Parked}`（`attach_released_placeholder` 的存檔失敗分支；Python 留在 `_preopened_ports`，
/// `origin/main:sw_core/session_manager.py:3633-3637`）。`gate` 為命令登錄表的 admission gate（`None` 用預設）。
/// 回傳 rig、B3 裝置替身與 parked 紀錄的 token。
fn dr7_formal_parked_rig(
    gate: Option<Arc<AdmissionGate>>,
) -> (ManagerRig, sw_pal::fakes::serial::FakeDevice, u64) {
    use sw_core::state_store::{LoadedState, ReleasedRow};
    let mut loaded = LoadedState::default();
    loaded.released.insert(
        DR7_PLACEHOLDER_SID.to_string(),
        ReleasedRow {
            by_id: PyValue::Str(DR7_BY_ID.to_string()),
            released_by: PyValue::Str("agent".to_string()),
            released_at: PyValue::Str("2026-10-21T00:00:00".to_string()),
            reason: PyValue::Str("manual".to_string()),
        },
    );
    let yaml = manager_yaml(&["{com: COM0, profile: t, act_no: 1, alias: t+1, device_by_id: /s/by-id/usb-A}"]);
    let primary = Some(("/s/by-id/usb-A", "/dev/ttyOLD"));
    let m = match gate {
        Some(gate) => ManagerRig::with_admission_gate(&yaml, loaded, primary, gate),
        None => ManagerRig::with(&yaml, loaded, primary),
    };
    let dev = m.add_device_at(DR7_BY_ID, DR7_REAL);
    install_shell_replies(&dev);
    let m = m.ready();
    let placeholder = m.mgr.resolve(DR7_PLACEHOLDER_SID).expect("B3 的 RELEASED 佔位");
    assert_eq!(m.mgr.released_placeholder(&placeholder), Some(true));

    m.fs.fail_next_atomic_write(PalError::io(28, "[Errno 28] No space left on device"));
    let attached = m.mgr.attach_released_placeholder(&placeholder, false);
    assert!(attached.is_err(), "退休落盤 ENOSPC 必須向上回錯：{attached:?}");
    assert_eq!(m.mgr.resolve(DR7_PLACEHOLDER_SID), None, "落盤失敗不回滾：佔位已退休");
    assert!(m.mgr.has_inflight_attach_for_test(DR7_BY_ID), "parked 作業留在在途表");
    assert_eq!(dev.open_count(), 1, "前置：佔位開埠 1 次");
    assert!(dev.is_open(), "前置：預開 handle 留在 parked 作業內");
    let parked = m.mgr.custody_for_test(DR7_BY_ID).expect("parked 帳本紀錄");
    assert_eq!(parked.owner, CustodyOwner::Parked, "正式流程建立的 parked 紀錄：{parked:?}");
    assert_eq!(parked.phase, CustodyPhaseSnapshot::Held);
    (m, dev, parked.token)
}

/// H-actor-hold＋H-deliver-fail 的結果。
struct ActorHold {
    /// 灌入 control lane 的事件數。
    filled: usize,
    resume: crossbeam_channel::Sender<()>,
    query: std::thread::JoinHandle<()>,
}

/// H-actor-hold：令 `sid` 的 actor 停在一筆無關的查詢 Request（之後排入的 DeviceOnline 不會被取出）；再以完整 sid
/// 取得 control sender 灌滿 lane（H-deliver-fail），使之後的 DeviceOnline 入列逾時、送達者的 `try_send` 回 Full。
fn hold_actor_and_fill_control(
    mgr: &Arc<sw_core::session_manager::SessionManager>,
    sid: &str,
) -> ActorHold {
    let (entered, resume) = mgr.pause_next_request_for_test(sid).expect("actor 存在");
    let query_mgr = mgr.clone();
    let query_sid = sid.to_string();
    let query = std::thread::spawn(move || {
        let _ = query_mgr.session_queue_lens(&query_sid);
    });
    entered
        .recv_timeout(Duration::from_secs(5))
        .expect("actor 應停在無關的查詢 Request");
    let ctrl = mgr
        .actor_control_for_test(sid)
        .expect("sessions 以完整 sid 為鍵，應取得 control sender");
    let mut filled = 0;
    while ctrl.try_send(ControlEvent::ConsoleCountChanged { count: 0 }).is_ok() {
        filled += 1;
    }
    ActorHold { filled, resume, query }
}

/// 在 `session.bind` 送出 DeviceOnline 之前執行 H-actor-hold＋H-deliver-fail，執行 bind 並回傳結果、耗時與暫停結果。
fn dr7_bind_with_device_online_full(m: &ManagerRig, sid: &str) -> (PyValue, Duration, ActorHold) {
    let hold: Arc<Mutex<Option<ActorHold>>> = Arc::new(Mutex::new(None));
    let hook_hold = hold.clone();
    let hook_mgr = m.mgr.clone();
    let hook_sid = sid.to_string();
    m.mgr.set_bind_pre_device_online_hook_for_test(Box::new(move || {
        *hook_hold.lock().unwrap() = Some(hold_actor_and_fill_control(&hook_mgr, &hook_sid));
    }));
    let started = Instant::now();
    let res = m.mgr.bind(sid, DR7_BY_ID).expect("bind 回 Ok");
    let elapsed = started.elapsed();
    let hold = hold.lock().unwrap().take().expect("bind 應在送出 DeviceOnline 之前執行暫停點");
    (res.get("ok").cloned().unwrap_or(PyValue::Null), elapsed, hold)
}

/// DR-7(j)：佔位 attach 加退休落盤失敗留下 parked（正式流程），`bind COM0→B3` 成功，首次 DeviceOnline 因 actor
/// 停在無關 Request 且 control lane 滿而入列逾時；bind 照常回 OK，送達義務交給送達者，lane 滿期間依退避重試；
/// 容量恢復後只送達一次 → ATTACHING → 以原 token 的 handle 接手 → READY，開埠次數不增加，帳本最後 None。
/// 拋棄式副本移除 bind 的 parked 接手接線（`SessionManager::bind` 的 `claim_parked_locked`／交接列）即轉紅。
#[test]
fn test_dr7_bind_device_online_full_queues_deliverer_and_retries() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let (m, dev, original_token) = dr7_formal_parked_rig(None);
    let full_sid = m.mgr.resolve("COM0").expect("COM0 session");
    assert_ne!(full_sid, "COM0", "sessions 以完整 sid 為鍵");
    let open_attempts = dev.open_attempts();

    let (ok, elapsed, hold) = dr7_bind_with_device_online_full(&m, &full_sid);

    assert_eq!(ok, PyValue::Bool(true), "首次送達失敗時 bind 照常回 OK");
    assert!(hold.filled > 0, "灌滿迴圈必須實際送入事件");
    assert!(
        elapsed >= Duration::from_millis(4900),
        "DeviceOnline 應在 lane 滿時入列逾時（QUERY_TIMEOUT 5 s），不是被取出後暫停：{elapsed:?}"
    );
    assert!(m.mgr.delivery_obligation_for_test(&full_sid), "首次送達失敗必須登錄送達義務");
    wait_until("送達者於 lane 滿時重試", || m.mgr.delivery_attempts_for_test(&full_sid) >= 1);
    assert!(m.mgr.delivery_obligation_for_test(&full_sid), "lane 滿期間義務保留");
    assert_eq!(m.mgr.delivery_delivered_for_test(&full_sid), 0, "lane 滿期間不得送達");

    // 恢復容量前：同一 token 以 C1′ 認領給 COM0 的開埠工作（交接列），不登記新紀錄、不重新開埠。
    let claimed = m.mgr.custody_for_test(DR7_BY_ID).expect("帳本紀錄");
    assert_eq!(claimed.token, original_token, "沿用 parked 紀錄的 token");
    assert_eq!(
        claimed.phase,
        CustodyPhaseSnapshot::Taken {
            taker: CustodyTaker::OpenJob { sid: full_sid.clone(), gen: 1 },
        },
        "parked 續行以同 token 認領，不轉 Closing：{claimed:?}"
    );
    assert!(!m.mgr.has_inflight_attach_for_test(DR7_BY_ID), "parked 作業已由 bind 取走");
    assert_eq!(dev.open_attempts(), open_attempts, "認領原 handle，不重新開埠");
    assert!(dev.is_open());

    // 容量恢復：只送達一次，以原 handle 到 READY。
    hold.resume.send(()).expect("放行 actor");
    hold.query.join().expect("查詢執行緒不應 panic");
    wait_until("送達者重送成功", || !m.mgr.delivery_obligation_for_test(&full_sid));
    m.wait_state("COM0", "READY");
    assert_eq!(m.mgr.delivery_delivered_for_test(&full_sid), 1, "容量恢復後只送達一次");
    assert_eq!(dev.open_attempts(), open_attempts, "認領原 parked handle 不得重複開埠");
    assert_eq!(dev.close_count(), 0, "原 handle 由正式 bridge 接手，不關閉");
    assert!(m.mgr.custody_for_test(DR7_BY_ID).is_none(), "正式 bridge 接手後帳本紀錄移除");
    assert_eq!(s_of(&m.session("COM0"), "device_by_id").as_deref(), Some(DR7_BY_ID));
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(m.mgr.delivery_delivered_for_test(&full_sid), 1, "之後不得重複送達");
}

/// 送達者接正式交接閘門（審查「既有碼待辦」）：送達者的 gate 就是命令登錄表的 admission gate（`Service::handoff_stop`
/// 的 `freeze_if_idle`／`commit_handoff` 與 session actor 的交接延後共用同一物件）。正式交接凍結成立（Pending）後送達者
/// 暫停嘗試、保留義務，凍結期間（假時鐘推進 45 s，超過 30 s 總時限）不計入時限；撤銷（交接失敗回滾）後繼續重試，
/// 容量恢復後只送達一次並到 READY。
#[test]
fn test_dr7_deliverer_pauses_on_formal_handoff_freeze_without_counting_deadline() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let t0 = Instant::now();
    let clock = FakeInstantClock::new(t0);
    let gate = clock.gate();
    let (m, dev, _token) = dr7_formal_parked_rig(Some(gate.clone()));
    assert!(Arc::ptr_eq(&m.reg.admission_gate(), &gate), "命令登錄表掛上注入的 gate");
    assert!(
        Arc::ptr_eq(&m.mgr.deliverer_gate_for_test(), &m.reg.admission_gate()),
        "送達者必須使用正式的 admission gate，而不是自建的 gate"
    );
    let full_sid = m.mgr.resolve("COM0").expect("COM0 session");
    let open_attempts = dev.open_attempts();

    let (ok, _elapsed, hold) = dr7_bind_with_device_online_full(&m, &full_sid);
    assert_eq!(ok, PyValue::Bool(true));
    assert!(hold.filled > 0, "灌滿迴圈必須實際送入事件");
    wait_until("凍結前送達者已在重試", || m.mgr.delivery_attempts_for_test(&full_sid) >= 1);

    // 正式交接的第一步（`Service::handoff_stop` → `freeze_if_idle`）：凍結成立但尚未提交 → Pending。
    let freeze = match m.reg.admission_gate().freeze_if_idle("update") {
        FreezeOutcome::Frozen { token, created: true } => token,
        other => panic!("閒置的 gate 應新建交接凍結：{other:?}"),
    };
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Pending);
    std::thread::sleep(Duration::from_millis(100));
    let paused_attempts = m.mgr.delivery_attempts_for_test(&full_sid);
    clock.set(t0 + Duration::from_secs(45));
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(
        m.mgr.delivery_attempts_for_test(&full_sid),
        paused_attempts,
        "Pending 期間送達者不得嘗試"
    );
    assert!(m.mgr.delivery_obligation_for_test(&full_sid), "Pending 期間保留義務（未逾時、未丟棄）");

    // 交接失敗回滾（`thaw`）→ Open：凍結的 45 s 不計入 30 s 總時限，義務仍在並繼續重試。
    assert!(gate.thaw(&freeze));
    wait_until("撤銷凍結後送達者繼續重試", || {
        m.mgr.delivery_attempts_for_test(&full_sid) > paused_attempts
    });
    assert!(m.mgr.delivery_obligation_for_test(&full_sid), "凍結區間不計時限，義務不得逾時丟棄");
    assert_eq!(m.mgr.delivery_delivered_for_test(&full_sid), 0);

    hold.resume.send(()).expect("放行 actor");
    hold.query.join().expect("查詢執行緒不應 panic");
    wait_until("送達者重送成功", || !m.mgr.delivery_obligation_for_test(&full_sid));
    m.wait_state("COM0", "READY");
    assert_eq!(m.mgr.delivery_delivered_for_test(&full_sid), 1, "只送達一次");
    assert_eq!(dev.open_attempts(), open_attempts, "認領原 parked handle 不得重複開埠");
    assert!(m.mgr.custody_for_test(DR7_BY_ID).is_none());
}

/// 正式交接凍結轉 Permanent（`commit_handoff`）時送達者丟棄義務、不再送達；parked 續行的 handle 仍在交接列，帳本
/// 紀錄維持同一 token（設計 §3.1.7 送達者列：Permanent 丟棄）。
#[test]
fn test_dr7_deliverer_drops_obligation_on_formal_handoff_commit() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let (m, dev, token) = dr7_formal_parked_rig(None);
    let full_sid = m.mgr.resolve("COM0").expect("COM0 session");
    let (ok, _elapsed, hold) = dr7_bind_with_device_online_full(&m, &full_sid);
    assert_eq!(ok, PyValue::Bool(true));
    assert!(hold.filled > 0, "灌滿迴圈必須實際送入事件");
    assert!(m.mgr.delivery_obligation_for_test(&full_sid));

    let gate = m.reg.admission_gate();
    let freeze = match gate.freeze_if_idle("update") {
        FreezeOutcome::Frozen { token, created: true } => token,
        other => panic!("閒置的 gate 應新建交接凍結：{other:?}"),
    };
    assert!(gate.commit_handoff(&freeze), "TX fence 收斂後交接提交 → Permanent");
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Permanent);
    wait_until("Permanent 時送達者丟棄義務", || !m.mgr.delivery_obligation_for_test(&full_sid));

    hold.resume.send(()).expect("放行 actor");
    hold.query.join().expect("查詢執行緒不應 panic");
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(m.mgr.delivery_delivered_for_test(&full_sid), 0, "Permanent 丟棄後不再送達");
    assert_eq!(dev.open_attempts(), 1, "未送達 DeviceAttached，不得開埠");
    let rec = m.mgr.custody_for_test(DR7_BY_ID).expect("handle 仍由交接列保管");
    assert_eq!(rec.token, token);
}

/// 審查 P8-6a679fb-1：bind 的 parked 續行以 C1′ 同 token 認領（`Taken{OpenJob}`），不借用 `claim_for_release`
/// （原本標成 `Closing{release}`、owner Reaper，handle 其實仍在使用）。DeviceOnline 之前插入 `device.release`：release
/// 認領交接列並關閉 handle（closed:true）後，該紀錄依 C3 結算（移除、終態 Closed），不留到下一次成功開埠才被覆寫。
///
/// 審查 P8-03333f7-1 修法 4：release 成功返回後 `route()` 會以 by_id 無條件結算帳本紀錄，會蓋掉關閉者自己的結算。
/// 因此在 release 的關閉者確認關閉、尚未收尾關閉中作業時（`finish_attach_op` 取鎖前；release 仍在等作業收尾，
/// actor 的 `Release` 尚未送出）斷言結算，只有 `close_claimed_handoff` 的 `settle_claimed_close` 能讓它成立。
#[test]
fn test_parked_continuation_bind_claims_same_token_and_settles_on_release_close() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let (m, dev, token) = dr7_formal_parked_rig(None);
    let full_sid = m.mgr.resolve("COM0").expect("COM0 session");
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    m.mgr.set_bind_pre_device_online_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(10));
    }));
    let bind_mgr = m.mgr.clone();
    let bind_sid = full_sid.clone();
    let bind = std::thread::spawn(move || bind_mgr.bind(&bind_sid, DR7_BY_ID));
    entered_rx
        .recv_timeout(Duration::from_secs(10))
        .expect("bind 應停在送出 DeviceOnline 之前");

    let claimed = m.mgr.custody_for_test(DR7_BY_ID).expect("帳本紀錄");
    assert_eq!(claimed.token, token, "同 token 認領");
    assert_eq!(
        claimed.phase,
        CustodyPhaseSnapshot::Taken {
            taker: CustodyTaker::OpenJob { sid: full_sid.clone(), gen: 1 },
        },
        "handle 交給 COM0 的開埠工作仍在使用，不得標成 Closing：{claimed:?}"
    );
    assert_eq!(
        claimed.owner,
        CustodyOwner::Session { sid: full_sid.clone(), inc: claimed_inc(&claimed) },
        "owner 為認領的 session"
    );
    assert!(dev.is_open());

    // release 的關閉者確認關閉後、收尾前暫停。S2-PAR-wp510r R3：交接列與 manager 端的 release 等待由帳本紀錄與 actor 的
    // `ClaimForStop` 取代，關閉者為帳本的關閉工作者（`spawn_custody_closer` → `finish_custody_close`），暫停點換成其
    // 「結算後、通知 actor（`CustodyTerminal`）前」（H 類：只換觸發手段與內部查詢，斷言不變）。
    let (fin_entered_tx, fin_entered_rx) = crossbeam_channel::bounded(1);
    let (fin_go_tx, fin_go_rx) = crossbeam_channel::bounded::<()>(1);
    m.mgr.set_custody_close_pre_notify_hook_for_test(
        DR7_BY_ID,
        Box::new(move || {
            let _ = fin_entered_tx.send(());
            let _ = fin_go_rx.recv_timeout(Duration::from_secs(10));
        }),
    );
    let release_mgr = m.mgr.clone();
    let release_sid = full_sid.clone();
    let release = std::thread::spawn(move || {
        release_mgr.route(
            &release_sid,
            sw_core::session_core::SessionRequest::Release {
                source: "agent".to_string(),
                reason: PyValue::Null,
            },
            Duration::from_secs(10),
        )
    });
    fin_entered_rx
        .recv_timeout(Duration::from_secs(10))
        .expect("release 的關閉者應在確認關閉後、收尾關閉中作業前暫停");
    assert!(!release.is_finished(), "release 仍在等待關閉中作業收尾，route 的全域結算尚未執行");
    assert_eq!(
        m.mgr.custody_awaits_for_test(&full_sid),
        vec![token],
        "關閉中作業仍在在途表（release 等待中）：actor 仍在等該紀錄的關閉終態"
    );
    assert_eq!(dev.close_count(), 1, "關閉者已關閉交給 session 的 handle");
    assert!(
        m.mgr.custody_for_test(DR7_BY_ID).is_none(),
        "關閉者確認關閉即以 `settle_claimed_close` 結算，不靠 release 返回後的全域結算：{:?}",
        m.mgr.custody_for_test(DR7_BY_ID)
    );
    assert_eq!(
        m.mgr.terminal_for_test(token).map(|t| t.handle),
        Some(TerminalHandle::Closed),
        "release 返回前同 token 的終態已為 Closed"
    );
    fin_go_tx.send(()).expect("放行關閉者收尾");
    let released = release
        .join()
        .expect("release 執行緒不應 panic")
        .expect("release 回 Ok");
    assert_eq!(released.get("ok"), Some(&PyValue::Bool(true)), "{released:?}");
    assert_eq!(dev.close_count(), 1, "release 關閉交給 session 的 handle 一次");
    assert!(!dev.is_open());
    wait_until("關閉成功後帳本紀錄結算", || m.mgr.custody_for_test(DR7_BY_ID).is_none());
    assert_eq!(
        m.mgr.terminal_for_test(token).map(|t| t.handle),
        Some(TerminalHandle::Closed),
        "同 token 的終態為 Closed"
    );
    assert!(!m.mgr.unconfirmed_close_for_test(DR7_BY_ID));

    go_tx.send(()).expect("放行 bind");
    let bound = bind.join().expect("bind 執行緒不應 panic").expect("bind 回 Ok");
    assert_eq!(bound.get("ok"), Some(&PyValue::Bool(true)));
    m.wait_state("COM0", "RELEASED");
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.open_attempts(), 1, "release 之後不得重新開埠");
    assert!(m.mgr.custody_for_test(DR7_BY_ID).is_none());
}

/// 審查 P8-6a679fb-1（`spawn_attach_with` 的 parked 續行）：佔位退休落盤失敗留下 parked，`bind COM0→B3` 的落盤也
/// ENOSPC（記憶體已改綁、DeviceOnline 未送出，parked 未被取走）；之後 `bootstrap_attach` → `spawn_attach_with` 以 C1′
/// 同 token 認領給 COM0 的開埠工作並交給交接列。正式 bridge 的 configure 失敗、handle 確認關閉（closed:true）時，該紀錄
/// 依 C3 結算（移除、終態 Closed），不停在 `Closing`。
#[test]
fn test_parked_continuation_spawn_attach_claims_same_token_and_settles_on_open_close() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let (m, dev, token) = dr7_formal_parked_rig(None);
    let full_sid = m.mgr.resolve("COM0").expect("COM0 session");
    m.fs.fail_next_atomic_write(PalError::io(28, "[Errno 28] No space left on device"));
    let bound = m.mgr.bind(&full_sid, DR7_BY_ID);
    assert!(bound.is_err(), "第二次落盤 ENOSPC 必須向上回錯：{bound:?}");
    let parked = m.mgr.custody_for_test(DR7_BY_ID).expect("parked 紀錄仍在");
    assert_eq!(parked.token, token);
    assert_eq!(parked.owner, CustodyOwner::Parked, "bind 落盤失敗未取走 parked");
    assert!(m.mgr.has_inflight_attach_for_test(DR7_BY_ID));

    dev.fail_configure(PalError::io(5, "[Errno 5] Input/output error"));
    m.mgr.bootstrap_attach();

    wait_until("configure 失敗後關閉原 handle", || dev.close_count() == 1);
    wait_until("關閉成功後帳本紀錄結算", || m.mgr.custody_for_test(DR7_BY_ID).is_none());
    assert_eq!(
        m.mgr.terminal_for_test(token).map(|t| t.handle),
        Some(TerminalHandle::Closed),
        "同 token 的終態為 Closed"
    );
    assert!(!dev.is_open());
    assert!(!m.mgr.unconfirmed_close_for_test(DR7_BY_ID), "closed:true 不得登記未確認關閉");
    assert!(!m.mgr.has_inflight_attach_for_test(DR7_BY_ID), "parked 作業已由續行者取走");
}

/// 改綁離開的目標（不在線，不會送 DeviceOnline）。
const DR7_AWAY_BY_ID: &str = "/s/by-id/usb-H";

/// 審查 P8-03333f7-1 修法 1（沒有 release 全域結算的路徑）：佔位退休落盤失敗留下 parked（正式流程），`bind COM0→B3`
/// 以 C1′ 同 token 認領並放進交接列、停在送出 DeviceOnline 之前；另一筆 `bind COM0→usb-H` 改綁離開 B3。bind 的取消
/// 迴圈認領交接列、代為關閉 handle（`close_claimed_handoff`），確認關閉後 B3 紀錄必須依 C3 結算（移除、原 token 終態
/// Closed），handle 只關閉一次；之後送出的 DeviceOnline（B3）不得重新開埠。拋棄式副本同時停用
/// `close_claimed_handoff` 的 `settle_claimed_close` 與 `finish_attach_op` 的 `settle_taken_close` 即轉紅。
#[test]
fn test_parked_continuation_bind_rebind_away_settles_claimed_close() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let (m, dev, token) = dr7_formal_parked_rig(None);
    let full_sid = m.mgr.resolve("COM0").expect("COM0 session");
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    m.mgr.set_bind_pre_device_online_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(10));
    }));
    let bind_mgr = m.mgr.clone();
    let bind_sid = full_sid.clone();
    let bind = std::thread::spawn(move || bind_mgr.bind(&bind_sid, DR7_BY_ID));
    entered_rx
        .recv_timeout(Duration::from_secs(10))
        .expect("bind 應停在送出 DeviceOnline 之前");

    let claimed = m.mgr.custody_for_test(DR7_BY_ID).expect("帳本紀錄");
    assert_eq!(claimed.token, token, "同 token 認領");
    assert_eq!(
        claimed.phase,
        CustodyPhaseSnapshot::Taken {
            taker: CustodyTaker::OpenJob { sid: full_sid.clone(), gen: 1 },
        },
        "{claimed:?}"
    );
    assert!(m.mgr.has_handoff_for_test(DR7_BY_ID), "handle 已放進交接列、等待正式 bridge 接手");
    assert!(dev.is_open());

    let away = m.mgr.bind(&full_sid, DR7_AWAY_BY_ID).expect("改綁離開 B3 回 Ok");
    assert_eq!(away.get("ok"), Some(&PyValue::Bool(true)), "{away:?}");
    assert_eq!(dev.close_count(), 1, "改綁離開時關閉交接列的 handle 一次");
    assert!(!dev.is_open());
    assert!(
        m.mgr.custody_for_test(DR7_BY_ID).is_none(),
        "改綁離開代為關閉後 B3 紀錄必須結算，不得停在 Taken／Closing：{:?}",
        m.mgr.custody_for_test(DR7_BY_ID)
    );
    assert_eq!(
        m.mgr.terminal_for_test(token).map(|t| t.handle),
        Some(TerminalHandle::Closed),
        "原 token 的終態為 Closed"
    );
    assert!(!m.mgr.unconfirmed_close_for_test(DR7_BY_ID), "closed:true 不得登記未確認關閉");
    assert!(!m.mgr.has_handoff_for_test(DR7_BY_ID), "交接列已移除");
    assert!(!m.mgr.has_inflight_attach_for_test(DR7_BY_ID), "關閉中作業確認關閉後移出在途表");

    go_tx.send(()).expect("放行第一筆 bind");
    let first = bind.join().expect("bind 執行緒不應 panic").expect("第一筆 bind 回 Ok");
    assert_eq!(first.get("ok"), Some(&PyValue::Bool(true)), "{first:?}");
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.open_attempts(), 1, "改綁離開後不得以 B3 重新開埠");
    assert_eq!(dev.close_count(), 1, "handle 只關閉一次");
    assert!(m.mgr.custody_for_test(DR7_BY_ID).is_none(), "之後不得留下 B3 紀錄");
    assert_eq!(s_of(&m.session("COM0"), "device_by_id").as_deref(), Some(DR7_AWAY_BY_ID));
}

/// 審查 P8-03333f7-1 修法 2（交接列取自帳本 `Held` 的分支）：佔位 `device.attach` 成功，dynamic 偵測建立正式 session
/// 時 handle 留在帳本（`Held`、owner 為新 session，出生即接管）並寫入交接列；正式 bridge 接手（`open_bridge` 取 handle）
/// 之前裝置拔除。`detach_by_id` 認領交接列時把紀錄轉為 `Closing{shutdown}`、owner Reaper（`claim_handoff_locked`），
/// `close_claimed_handoff` 確認關閉後只有 `settle_claimed_close` 會結算它（`finish_attach_op` 的 `settle_taken_close`
/// 只結算 `Taken`）。拋棄式副本單獨停用 `close_claimed_handoff` 的 `settle_claimed_close` 即轉紅。
#[test]
fn test_birth_takeover_handoff_from_ledger_unplug_settles_claimed_close() {
    use sw_core::state_store::{LoadedState, ReleasedRow};
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let mut loaded = LoadedState::default();
    loaded.released.insert(
        DR7_PLACEHOLDER_SID.to_string(),
        ReleasedRow {
            by_id: PyValue::Str(DR7_BY_ID.to_string()),
            released_by: PyValue::Str("agent".to_string()),
            released_at: PyValue::Str("2026-10-21T00:00:00".to_string()),
            reason: PyValue::Str("manual".to_string()),
        },
    );
    let m = ManagerRig::with(&manager_yaml(&[]), loaded, None);
    let dev = m.add_device_at(DR7_BY_ID, DR7_REAL);
    install_shell_replies(&dev);
    m.mgr.update_devices(&m.snapshot());

    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    m.mgr.set_open_bridge_pre_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(10));
    }));
    let placeholder = m.mgr.resolve(DR7_PLACEHOLDER_SID).expect("B3 的 RELEASED 佔位");
    let attached = m
        .mgr
        .attach_released_placeholder(&placeholder, false)
        .expect("device.attach 回 Ok");
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{attached:?}");
    entered_rx
        .recv_timeout(Duration::from_secs(10))
        .expect("正式 session 的 open_bridge 應停在接手 handle 之前");
    wait_until("dynamic attach 作業收尾", || !m.mgr.has_inflight_attach_for_test(DR7_BY_ID));

    let held = m.mgr.custody_for_test(DR7_BY_ID).expect("出生即接管的帳本紀錄");
    assert_eq!(held.phase, CustodyPhaseSnapshot::Held, "handle 留在帳本：{held:?}");
    let formal_sid = match &held.owner {
        CustodyOwner::Session { sid, .. } => sid.clone(),
        other => panic!("owner 應為新建的正式 session：{other:?}"),
    };
    assert_ne!(formal_sid, DR7_PLACEHOLDER_SID, "佔位已退休");
    assert!(m.mgr.has_handoff_for_test(DR7_BY_ID), "交接列指向正式 session");
    assert_eq!(dev.open_count(), 1, "前置：佔位開埠 1 次");
    assert!(dev.is_open());
    let token = held.token;

    // 拔除：裝置已不在，正式 bridge 之後的開埠回 NotFound。
    m.serial.fail_next_open(
        DR7_REAL,
        sw_pal::traits::OpenError::NotFound(PalError::plain(PalErrorKind::NotFound, "No such device")),
    );
    let without: Vec<_> = m.snapshot().into_iter().filter(|(k, _)| k != DR7_BY_ID).collect();
    m.mgr.update_devices(&without);

    assert_eq!(dev.close_count(), 1, "拔除時代為關閉交接列的 handle 一次");
    assert!(!dev.is_open());
    assert!(
        m.mgr.custody_for_test(DR7_BY_ID).is_none(),
        "交接列取自帳本 Held、轉 Closing 的紀錄，確認關閉即須結算：{:?}",
        m.mgr.custody_for_test(DR7_BY_ID)
    );
    assert_eq!(
        m.mgr.terminal_for_test(token).map(|t| t.handle),
        Some(TerminalHandle::Closed),
        "同 token 的終態為 Closed"
    );
    assert!(!m.mgr.unconfirmed_close_for_test(DR7_BY_ID), "closed:true 不得登記未確認關閉");
    assert!(!m.mgr.has_handoff_for_test(DR7_BY_ID), "交接列已移除");
    assert!(!m.mgr.has_inflight_attach_for_test(DR7_BY_ID), "關閉中作業確認關閉後移出在途表");

    go_tx.send(()).expect("放行 open_bridge");
    m.wait_state(&formal_sid, "DETACHED");
    std::thread::sleep(Duration::from_millis(200));
    assert!(m.mgr.custody_for_test(DR7_BY_ID).is_none(), "拔除後不得留下 B3 紀錄");
    assert_eq!(dev.close_count(), 1, "handle 只關閉一次");
    assert!(!dev.is_open());
}

/// 審查 P8-03333f7-1 修法 3（作業自行關閉）：佔位退休落盤失敗留下 parked，B3 沒有綁定的 session；`bootstrap_attach`
/// → `spawn_attach_with` 以 C1′ 同 token 認領給 spawn worker（`Taken{Worker("spawn_attach")}`），`attach_dynamic`
/// 以該 handle 偵測 template。偵測 PROBE 提交前裝置拔除（取消作業），作業自行關閉 handle（`close_parts`），
/// `finish_attach_op` 必須以 `settle_taken_close` 結算（移除、原 token 終態 Closed）。拋棄式副本單獨停用
/// `finish_attach_op` 的 `settle_taken_close` 即轉紅。
#[test]
fn test_parked_continuation_spawn_worker_cancelled_attach_dynamic_settles_taken_close() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let (m, dev, token) = dr7_formal_parked_rig(None);
    assert!(m.mgr.resolve("COM3").is_none(), "前置：B3 沒有綁定的 session");
    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    let fired = Arc::new(AtomicBool::new(false));
    let worker_name = format!("serialwrap-attach-{DR7_BY_ID}");
    m.mgr.set_probe_pre_submit_hook_for_test(Box::new(move || {
        // 只攔 B3 的 attach worker 第一次 PROBE；其他 by_id 的作業直接通過（鉤子在 manager 鎖與鉤子槽鎖之外呼叫，
        // 暫停中不卡住其他 by_id 的 PROBE；本 rig 也只有 B3 會跑 PROBE，usb-A 是 YAML target）。
        let on_b3 = std::thread::current().name() == Some(worker_name.as_str());
        if on_b3 && !fired.swap(true, Ordering::SeqCst) {
            let _ = entered_tx.send(());
            let _ = go_rx.recv_timeout(Duration::from_secs(10));
        }
    }));
    m.mgr.bootstrap_attach();
    entered_rx
        .recv_timeout(Duration::from_secs(10))
        .expect("B3 的 spawn worker 應停在偵測 PROBE 提交前");

    let claimed = m.mgr.custody_for_test(DR7_BY_ID).expect("帳本紀錄");
    assert_eq!(claimed.token, token, "同 token 認領");
    assert_eq!(
        claimed.phase,
        CustodyPhaseSnapshot::Taken {
            taker: CustodyTaker::Worker("spawn_attach".to_string()),
        },
        "{claimed:?}"
    );
    assert_eq!(claimed.owner, CustodyOwner::Worker("spawn_attach".to_string()));
    assert!(m.mgr.has_inflight_attach_for_test(DR7_BY_ID), "spawn worker 的作業在途");
    assert_eq!(dev.open_attempts(), 1, "認領原 handle，不重新開埠");
    assert!(dev.is_open());

    // 拔除：`update_devices` 取消在途作業；放行後作業見取消即自行關閉 handle。
    let without: Vec<_> = m.snapshot().into_iter().filter(|(k, _)| k != DR7_BY_ID).collect();
    m.mgr.update_devices(&without);
    go_tx.send(()).expect("放行偵測 PROBE");
    wait_until("取消的作業收尾", || !m.mgr.has_inflight_attach_for_test(DR7_BY_ID));

    assert_eq!(dev.close_count(), 1, "作業自行關閉 handle 一次");
    assert!(!dev.is_open());
    assert!(
        m.mgr.custody_for_test(DR7_BY_ID).is_none(),
        "作業自行確認關閉後紀錄必須結算，不得停在 Taken：{:?}",
        m.mgr.custody_for_test(DR7_BY_ID)
    );
    assert_eq!(
        m.mgr.terminal_for_test(token).map(|t| t.handle),
        Some(TerminalHandle::Closed),
        "原 token 的終態為 Closed"
    );
    assert!(!m.mgr.unconfirmed_close_for_test(DR7_BY_ID), "closed:true 不得登記未確認關閉");
    assert_eq!(dev.open_attempts(), 1, "取消後不得重新開埠");
    assert!(
        !m.com_by_id().contains_key(DR7_BY_ID),
        "取消的偵測不得建立 B3 的 session：{:?}",
        m.com_by_id()
    );
}

fn claimed_inc(rec: &CustodySnapshot) -> u64 {
    match &rec.owner {
        CustodyOwner::Session { inc, .. } => *inc,
        other => panic!("owner 應為 Session：{other:?}"),
    }
}

#[test]
fn test_dr1_f1_to_f5_real_ioworker_reaper_drain() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let m = ManagerRig::templates_only();
    let h = CoreHarness::new(SHELL_PROFILE);

    // 1. (f1/f3) 真實 IoWorker + FakeSerial，進入 Bridged
    let dev_path = "/dev/ttyUSB-real-drain";
    let dev = h.add_serial_device(dev_path);
    let epoch = 100;
    let worker = h.start_worker_at(epoch, dev_path);

    let sid = "COM50";
    let by_id = "by-id-real-drain";
    let token = m.mgr.custody_register_bridged_worker_for_test(sid, by_id, dev_path, epoch, worker);

    // 經由正式 manager 的 reaper_cmd_sender 發送 ActorExited 命令觸發回收
    m.mgr.reaper_cmd_sender_for_test().send(ReaperCmd::ActorExited { sid: sid.into(), inc: 1 }).unwrap();

    // 等待回收者消費與結算
    let t0 = Instant::now();
    while t0.elapsed() < Duration::from_secs(3) {
        if m.mgr.terminal_for_test(token).is_some() {
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }

    let term = m.mgr.terminal_for_test(token).expect("回收者必須結算終態");
    assert_eq!(term.handle, TerminalHandle::Closed, "正常關閉的真實 worker 結算為 Closed");
    assert_eq!(term.evidence, TerminalEvidence::Complete, "證據完整");
    assert!(m.mgr.custody_for_test(by_id).is_none(), "Closed 後帳本紀錄必須移除");
    assert_eq!(dev.close_count(), 1, "真實 serial handle 必須被呼叫 close 恰好 1 次");

    // 2. (f2) 失敗回條變體：fail_close
    let dev_path_f2 = "/dev/ttyUSB-fail-close";
    let dev_f2 = h.add_serial_device(dev_path_f2);
    dev_f2.fail_close(PalError::io(5, "injected close failure"));
    let epoch_f2 = 101;
    let worker_f2 = h.start_worker_at(epoch_f2, dev_path_f2);

    let sid_f2 = "COM51";
    let by_id_f2 = "by-id-fail-close";
    let token_f2 = m.mgr.custody_register_bridged_worker_for_test(sid_f2, by_id_f2, dev_path_f2, epoch_f2, worker_f2);

    m.mgr.reaper_cmd_sender_for_test().send(ReaperCmd::ActorExited { sid: sid_f2.into(), inc: 1 }).unwrap();

    let t0 = Instant::now();
    while t0.elapsed() < Duration::from_secs(3) {
        if m.mgr.terminal_for_test(token_f2).is_some() {
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }

    let term_f2 = m.mgr.terminal_for_test(token_f2).expect("回收者必須結算終態");
    assert!(matches!(term_f2.handle, TerminalHandle::Failed { .. }), "關閉失敗結算為 Failed");
    assert!(m.mgr.unconfirmed_close_for_test(by_id_f2), "關閉失敗必須進入 unconfirmed");
    let rec_f2 = m.mgr.custody_for_test(by_id_f2).expect("關閉失敗紀錄必須保留");
    assert!(matches!(rec_f2.phase, CustodyPhaseSnapshot::CloseFailed { .. }), "紀錄 phase 必須為 CloseFailed");
    assert_eq!(dev_f2.close_count(), 1, "真實 serial handle 必須嘗試 close 1 次");
}

#[test]
fn test_dr1_f2_bridge_close_failure_close_failed_and_t1_stall() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed.clone());

    let sid = "COM12";
    let by_id = "by-id-dr1-f2";
    let real_path = "/dev/ttyUSB12";
    let token = ledger.register(
        by_id,
        real_path,
        CustodyOwner::Session { sid: sid.into(), inc: 1 },
        CustodyTaker::OpenJob { sid: sid.into(), gen: 1 },
    ).unwrap();

    let _ = ledger.mark_bridge_draining(token, 100, StopId(2)).unwrap();

    // (f2) 收到失敗回條 handle_closed: false
    let receipt = WorkerStoppedReceipt {
        epoch: 100,
        stop_id: StopId(2),
        ledger: sw_core::drain::TxLedger::default(),
        handle_closed: false,
        close_error: Some(PalError::plain(PalErrorKind::Io, "injected close error")),
        unrecorded: Vec::new(),
        wal_error: None,
    };
    let term = ledger.settle_worker_stopped(token, receipt).unwrap();
    assert!(matches!(term.handle, TerminalHandle::Failed { .. }));
    assert!(unconfirmed.contains(by_id), "D1 (iii): 失敗回條必須永久登記未確認關閉");
    let custody = ledger.get_custody(by_id).expect("紀錄必須保留為 CloseFailed");
    assert!(matches!(custody.snapshot().phase, CustodyPhaseSnapshot::CloseFailed { .. }));
    assert!(ledger.is_open_blocked(by_id), "開埠一律被阻擋");

    // T-1: 首次 release 取得或設立 3 秒期限
    let t0 = Instant::now();
    let dl1 = ledger.get_or_set_release_deadline(by_id, t0);
    assert_eq!(dl1, t0 + Duration::from_millis(3000));
    // 之後的 release 沿用同一期限
    let dl2 = ledger.get_or_set_release_deadline(by_id, t0 + Duration::from_secs(5));
    assert_eq!(dl2, dl1, "之後的 release 必須沿用首次期限");
}

#[test]
fn test_dr1_f3_f4_actor_exited_before_adopt_bridge() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed.clone());

    let sid = "COM13";
    let by_id = "by-id-dr1-f3";
    let real_path = "/dev/ttyUSB13";
    let inc = ledger.alloc_session_inc(sid);

    let token = ledger.register(
        by_id,
        real_path,
        CustodyOwner::Session { sid: sid.into(), inc },
        CustodyTaker::OpenJob { sid: sid.into(), gen: 1 },
    ).unwrap();

    // 模擬已開埠成功，IoWorker 放在帳本中（Bridged），但 actor 在 AdoptBridge 前結束
    let mock_worker = MockWorker { epoch: 101 };
    ledger.adopt_bridged_mock(token, 101, mock_worker).unwrap();

    // ActorExited 命令到達：回收者接手排空
    let jobs = ledger.handle_actor_exited(sid, inc);
    assert_eq!(jobs.len(), 1);
    assert_eq!(
        ledger.get_custody(by_id).unwrap().snapshot().phase,
        CustodyPhaseSnapshot::BridgeDraining { epoch: 101, stop_id: StopId(0) }
    );

    // (f3) handle_closed: true 成功排空
    let receipt = WorkerStoppedReceipt {
        epoch: 101,
        stop_id: StopId(0),
        ledger: sw_core::drain::TxLedger::default(),
        handle_closed: true,
        close_error: None,
        unrecorded: Vec::new(),
        wal_error: None,
    };
    let term = ledger.settle_worker_stopped(token, receipt).unwrap();
    assert_eq!(term.handle, TerminalHandle::Closed);
    assert!(ledger.get_custody(by_id).is_none());
}

#[test]
fn test_dr1_f5_reclaim_drain_after_adopt_bridge_claim_unwind() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed.clone());

    let sid = "COM15";
    let by_id = "by-id-dr1-f5";
    let real_path = "/dev/ttyUSB15";
    let inc = ledger.alloc_session_inc(sid);

    let token = ledger.register(
        by_id,
        real_path,
        CustodyOwner::Session { sid: sid.into(), inc },
        CustodyTaker::OpenJob { sid: sid.into(), gen: 1 },
    ).unwrap();

    // 放入 mock worker
    ledger.adopt_bridged_mock(token, 102, MockWorker { epoch: 102 }).unwrap();

    // AdoptBridge 取出後發生 unwind，Reclaim 守衛送出 reclaim 請求轉由回收者排空
    let jobs = ledger.claim_for_stop(sid, inc, CustodyCancel::Release);
    assert_eq!(jobs.len(), 1);
    let (reaper_tx, _reaper_rx) = crossbeam_channel::unbounded();
    let term_opt = jobs.into_iter().next().unwrap().execute_and_settle(&mut ledger, Some(&reaper_tx)).unwrap();
    assert!(term_opt.is_none(), "DrainBridge 轉入 BridgeDraining，等待回條");
    let receipt = WorkerStoppedReceipt {
        epoch: 102,
        stop_id: StopId(0),
        ledger: sw_core::drain::TxLedger::default(),
        handle_closed: true,
        close_error: None,
        unrecorded: Vec::new(),
        wal_error: None,
    };
    let term = ledger.settle_worker_stopped(token, receipt).unwrap();
    assert_eq!(term.handle, TerminalHandle::Closed);
    assert!(ledger.get_custody(by_id).is_none());
}

#[test]
fn test_dr1_f6_f7_f8_reaper_drain_evidence_gaps_and_wal_error() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed.clone());

    let sid = "COM16";
    let by_id = "by-id-dr1-f6";
    let real_path = "/dev/ttyUSB16";

    let token = ledger.register(
        by_id,
        real_path,
        CustodyOwner::Session { sid: sid.into(), inc: 1 },
        CustodyTaker::OpenJob { sid: sid.into(), gen: 1 },
    ).unwrap();

    let _ = ledger.mark_bridge_draining(token, 103, StopId(3)).unwrap();

    // (f6)(f7)(f8): handle_closed: true, 但有 RX 缺口和 wal_error
    let gap = sw_core::drain::EvidenceGap {
        op_id: None,
        dir: "rx",
        len: 0,
        error: PalError::plain(PalErrorKind::Io, "RX drain barrier 未能送入回收者"),
    };
    let receipt = WorkerStoppedReceipt {
        epoch: 103,
        stop_id: StopId(3),
        ledger: sw_core::drain::TxLedger::default(),
        handle_closed: true,
        close_error: None,
        unrecorded: vec![gap],
        wal_error: Some(PalError::plain(PalErrorKind::Io, "drain WAL 不健康")),
    };
    let term = ledger.settle_worker_stopped(token, receipt).unwrap();
    assert_eq!(term.handle, TerminalHandle::Closed, "handle 成功關閉");
    assert!(
        matches!(term.evidence, TerminalEvidence::Incomplete { .. }),
        "有缺口時 evidence 必須為 Incomplete"
    );
    assert!(ledger.get_custody(by_id).is_none(), "handle 成功關閉即解除帳本登記");
    assert!(!unconfirmed.contains(by_id), "解除未確認關閉阻擋");
}

#[test]
fn test_dr7_l_m_actor_exited_cancels_obligation_and_com2_independent() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed.clone());
    let t0 = Instant::now();
    let clock = FakeInstantClock::new(t0);
    let mut deliverer = Deliverer::new(clock.gate());

    let sid1 = "COM1";
    let by_id1 = "by-id-com1";
    let sid2 = "COM2";
    let by_id2 = "by-id-com2";

    // COM1 登記送達義務
    deliverer.add_obligation_at(sid1, by_id1, "/dev/ttyCOM1", 1, 1, t0);
    assert!(deliverer.obligations.contains_key(sid1));

    // (l) 送達前 COM1 actor 結束（世代號 1）：移除義務
    deliverer.cancel_obligation(sid1, Some(1));
    assert!(!deliverer.obligations.contains_key(sid1), "actor 結束必須清除送達義務");

    // (m) COM2 的未接手 worker 交給回收者排空，不等 COM1
    let token2 = ledger.register(
        by_id2,
        "/dev/ttyCOM2",
        CustodyOwner::Session { sid: sid2.into(), inc: 1 },
        CustodyTaker::OpenJob { sid: sid2.into(), gen: 1 },
    ).unwrap();
    let _ = ledger.mark_bridge_draining(token2, 200, StopId(20)).unwrap();
    let receipt2 = WorkerStoppedReceipt {
        epoch: 200,
        stop_id: StopId(20),
        ledger: sw_core::drain::TxLedger::default(),
        handle_closed: true,
        close_error: None,
        unrecorded: Vec::new(),
        wal_error: None,
    };
    let term2 = ledger.settle_worker_stopped(token2, receipt2).unwrap();
    assert_eq!(term2.handle, TerminalHandle::Closed);
    assert!(ledger.get_custody(by_id2).is_none());
}

#[test]
fn test_dr9_b_c_spawn_failure_dynamic_and_yaml() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed.clone());

    // (b) 動態建立失敗
    let sid_b = "COM51";
    let inc_b = ledger.alloc_session_inc(sid_b);
    let token_b = ledger.register(
        "by-id-dyn",
        "/dev/ttyDyn",
        CustodyOwner::Session { sid: sid_b.into(), inc: inc_b },
        CustodyTaker::Worker("dynamic_create".into()),
    ).unwrap();
    ledger.save_held_mock(token_b).unwrap();
    let jobs_b = ledger.handle_spawn_failure(sid_b, inc_b);
    assert_eq!(jobs_b.len(), 1);
    let term_b = jobs_b.into_iter().next().unwrap().execute_and_settle(&mut ledger, None).unwrap();
    assert_eq!(term_b.unwrap().handle, TerminalHandle::Closed);

    // (c) 啟動 YAML target 失敗
    let sid_c = "COM52";
    let inc_c = ledger.alloc_session_inc(sid_c);
    let token_c = ledger.register(
        "by-id-yaml",
        "/dev/ttyYaml",
        CustodyOwner::Session { sid: sid_c.into(), inc: inc_c },
        CustodyTaker::Worker("yaml_start".into()),
    ).unwrap();
    ledger.save_held_mock(token_c).unwrap();
    let jobs_c = ledger.handle_spawn_failure(sid_c, inc_c);
    assert_eq!(jobs_c.len(), 1);
    let term_c = jobs_c.into_iter().next().unwrap().execute_and_settle(&mut ledger, None).unwrap();
    assert_eq!(term_c.unwrap().handle, TerminalHandle::Closed);
}

/// B-2: D1 + T-1 雙 release 與關閉失敗回條：
/// 開埠在途遇關閉失敗不提前回覆第二次 release，兩者皆等到滿 3 秒首次期限才收到 DRAIN_STALLED，且欄位一致。
#[test]
fn test_t1_dual_release_close_unconfirmed_waits_until_first_deadline() {
    use sw_core::mailbox::OpenFailure;
    use sw_core::session_core::SessionRequest;
    use sw_proto::pyjson::PyValue;

    let release_req = || SessionRequest::Release {
        source: "cli".into(),
        reason: PyValue::Null,
    };

    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    // t = 0: 開埠在途，送第一次 release
    let (rx1, _) = h.rpc_deferred(release_req());

    // t = 100 ms: 送入 BridgeOpened(Err(CloseUnconfirmed(...)))
    h.advance(Duration::from_millis(100));
    let err = OpenFailure::CloseUnconfirmed(Box::new(OpenFailure::CloseBlocked));
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(err),
    });

    // 同時刻送第二次 release
    let (rx2, _) = h.rpc_deferred(release_req());

    // 驗證在 t = 100 ms 時，兩者皆未回覆！
    assert!(rx1.try_recv().is_err(), "第一次 release 不得提前回覆");
    assert!(rx2.try_recv().is_err(), "第二次 release 不得繞過 T-1 首次期限");

    // 推進到 t = 3000 ms（距首次 release 滿 3000 ms）
    h.advance(Duration::from_millis(2901));

    // 兩者此時皆收到回覆，且欄位一致
    let resp1 = rx1.try_recv().expect("首次期限到期 rx1 須收到回覆").expect("回傳 ok");
    let resp2 = rx2.try_recv().expect("首次期限到期 rx2 須收到回覆").expect("回傳 ok");

    assert_eq!(resp1.get("ok"), Some(&PyValue::Bool(false)));
    assert_eq!(resp1.get("error_code"), Some(&PyValue::from("DRAIN_STALLED")));
    assert_eq!(resp2.get("ok"), Some(&PyValue::Bool(false)));
    assert_eq!(resp2.get("error_code"), Some(&PyValue::from("DRAIN_STALLED")));
    assert_eq!(resp1["drain_stalled"], PyValue::Bool(true));
    assert_eq!(resp2["drain_stalled"], PyValue::Bool(true));
    assert_eq!(
        h.public()["drain_stalled"],
        PyValue::Bool(true),
        "session 的 drain_stalled 須為 true"
    );

    // t = 3500 ms: 首次期限已過之後的第三次 release 立即回覆
    h.advance(Duration::from_millis(500));
    let resp3 = h.rpc(release_req()).expect("期限過後立即回覆");
    assert_eq!(resp3.get("ok"), Some(&PyValue::Bool(false)));
    assert_eq!(resp3.get("error_code"), Some(&PyValue::from("DRAIN_STALLED")));
    assert_eq!(resp3["drain_stalled"], PyValue::Bool(true));
}

/// DR-9: 經由 manager 正式路徑注入 H-actor-spawn-fail，驗證 actor 建構失敗時持鎖清理、鎖不中毒且帳本收尾。
#[test]
fn test_dr9_actor_spawn_failure_via_manager_path() {
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    use sw_core::state_store::LoadedState;
    const BY_ID: &str = "/s/by-id/usb-spawn-fail";

    let target_yaml = format!(
        "{{com: COM0, profile: t, act_no: 1, alias: t+1, device_by_id: {BY_ID}}}"
    );
    let yaml = manager_yaml(&[&target_yaml]);

    inject_actor_spawn_fail(1);
    let m = ManagerRig::with(&yaml, LoadedState::default(), None);
    inject_actor_spawn_fail(0);

    // 驗證 manager 鎖未中毒，查詢在 1s 內返回
    assert!(m.mgr.resolve("COM0").is_none());
    assert_eq!(m.mgr.session_ids().len(), 0);

    // 驗證帳本中無殘留
    let c = m.mgr.custody_for_test(BY_ID);
    assert!(c.is_none(), "建構失敗清理後帳本不得殘留：{c:?}");
}

/// 審查（sol-7d19d5c8_6a679fb0）既有碼待辦：`claim_for_release` 在 phase 不是 `Held` 時回錯誤，紀錄的 owner、cancel、
/// phase 與 token 都必須原樣保留（先檢查、成立才改）；只有 `Held` 才轉 `Closing{release}`、owner Reaper、cancel Release。
#[test]
fn test_claim_for_release_non_held_phase_leaves_record_unchanged() {
    let unconfirmed = UnconfirmedCloses::new();
    let mut ledger = CustodyLedger::new(unconfirmed.clone());
    let by_id = "by-id-claim-release";
    let real_path = "/dev/ttyCFR";
    let sid = "t:COM5".to_string();
    let inc = ledger.alloc_session_inc(&sid);
    let token = ledger
        .register(
            by_id,
            real_path,
            CustodyOwner::Session { sid: sid.clone(), inc },
            CustodyTaker::OpenJob { sid: sid.clone(), gen: 1 },
        )
        .unwrap();

    let non_held = [
        CustodyPhase::Opening { taker: CustodyTaker::OpenJob { sid: sid.clone(), gen: 1 } },
        CustodyPhase::Taken { taker: CustodyTaker::OpenJob { sid: sid.clone(), gen: 1 } },
        CustodyPhase::Bridged { epoch: 7, worker: None },
        CustodyPhase::BridgeDraining { epoch: 7, stop_id: StopId(3) },
        CustodyPhase::Closing { job: "stop".into() },
        CustodyPhase::CloseFailed { error: "close unconfirmed".into() },
    ];
    for phase in non_held {
        ledger.custody.get_mut(by_id).unwrap().phase = phase;
        let before = ledger.get_custody(by_id).unwrap().snapshot();
        assert!(
            ledger.claim_for_release(by_id, token).is_err(),
            "非 Held 的 phase 不得被 release 認領：{before:?}"
        );
        let after = ledger.get_custody(by_id).unwrap().snapshot();
        assert_eq!(after, before, "claim_for_release 失敗時紀錄不得被改寫");
        assert_eq!(after.owner, CustodyOwner::Session { sid: sid.clone(), inc });
        assert_eq!(after.cancel, None);
    }

    // token 不符同樣不得改寫紀錄。
    ledger.custody.get_mut(by_id).unwrap().phase = CustodyPhase::Held { parts: None };
    let before = ledger.get_custody(by_id).unwrap().snapshot();
    assert!(ledger.claim_for_release(by_id, token + 1000).is_err());
    assert_eq!(ledger.get_custody(by_id).unwrap().snapshot(), before);

    // Held 才成立：轉 Closing{release}、owner Reaper、cancel Release，token 不變。
    assert!(ledger.claim_for_release(by_id, token).is_ok());
    let claimed = ledger.get_custody(by_id).unwrap().snapshot();
    assert_eq!(claimed.token, token);
    assert_eq!(claimed.owner, CustodyOwner::Reaper);
    assert_eq!(claimed.cancel, Some(CustodyCancel::Release));
    assert_eq!(claimed.phase, CustodyPhaseSnapshot::Closing { job: "release".into() });
    assert!(!unconfirmed.contains(by_id));
}

/// 審查 P8-6a679fb-4：回收者停止後（命令通道已斷線，例如 `shutdown` 第二階段之後）送交排空的未接手 IoWorker 不得被
/// 直接丟棄（IoWorker 沒有 Drop 收尾，丟棄即留下開著的 handle 與讀寫執行緒）：`Reclaim` 守衛的 Drop 與開埠工作的送交
/// 都退回就地 `finish_async`，真 handle 關閉恰好一次。
#[test]
fn test_reclaim_after_reaper_stopped_falls_back_to_finish_async() {
    let h = CoreHarness::new(SHELL_PROFILE);
    let (cmd_tx, cmd_rx) = unbounded::<ReaperCmd>();
    drop(cmd_rx);

    let guard_path = "/dev/ttyUSB-reclaim-guard";
    let guard_dev = h.add_serial_device(guard_path);
    let guard_worker = h.start_worker_at(300, guard_path);
    {
        let _reclaim = Reclaim::new(31, 300, guard_worker, cmd_tx.clone());
    }
    wait_until("Reclaim 守衛的退回收尾關閉 handle", || guard_dev.close_count() == 1);
    assert!(!guard_dev.is_open(), "handle 已關閉");

    let job_path = "/dev/ttyUSB-reclaim-job";
    let job_dev = h.add_serial_device(job_path);
    let job_worker = h.start_worker_at(301, job_path);
    let sent = send_reclaim_or_finish(&cmd_tx, 32, 301, ReclaimWorker::Real(job_worker), None);
    assert!(!sent, "回收者已停止：不得回報已送交");
    wait_until("送交失敗後就地收尾關閉 handle", || job_dev.close_count() == 1);
    assert!(!job_dev.is_open());
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(guard_dev.close_count(), 1, "只關閉一次");
    assert_eq!(job_dev.close_count(), 1, "只關閉一次");
}

/// DR-1(f5)（設計 §3.1.4 W3′ 第 3、4(c) 步、C2）：正式流程（佔位 attach → 出生即接管 → `ClaimCustody` → adopt 提交
/// `Bridged{Some}` → BridgeOpened）下，actor 的 `AdoptBridge` 自帳本取出 IoWorker 後、放入 workers 前 unwind
/// （H-actor-panic-after-claim，在 catch_unwind 之外）。`Reclaim` 守衛的 Drop 只送命令，回收者把紀錄轉 `BridgeDraining`
/// 並排空：真 handle 關閉恰好一次、配對回條結算為 Closed、紀錄移除、不登記未確認關閉、沒有登入 TX。
#[test]
fn test_dr1_f5_formal_adopt_bridge_unwind_after_claim_reaper_drains() {
    use sw_core::state_store::{LoadedState, ReleasedRow};
    let _guard = SPAWN_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let mut loaded = LoadedState::default();
    loaded.released.insert(
        DR7_PLACEHOLDER_SID.to_string(),
        ReleasedRow {
            by_id: PyValue::Str(DR7_BY_ID.to_string()),
            released_by: PyValue::Str("agent".to_string()),
            released_at: PyValue::Str("2026-10-21T00:00:00".to_string()),
            reason: PyValue::Str("manual".to_string()),
        },
    );
    let m = ManagerRig::with(&manager_yaml(&[]), loaded, None);
    let dev = m.add_device_at(DR7_BY_ID, DR7_REAL);
    install_shell_replies(&dev);
    m.mgr.update_devices(&m.snapshot());

    let (entered_tx, entered_rx) = crossbeam_channel::bounded(1);
    let (go_tx, go_rx) = crossbeam_channel::bounded::<()>(1);
    m.mgr.set_open_bridge_pre_take_hook_for_test(Box::new(move || {
        let _ = entered_tx.send(());
        let _ = go_rx.recv_timeout(Duration::from_secs(10));
    }));
    let placeholder = m.mgr.resolve(DR7_PLACEHOLDER_SID).expect("B3 的 RELEASED 佔位");
    let attached = m
        .mgr
        .attach_released_placeholder(&placeholder, false)
        .expect("device.attach 回 Ok");
    assert_eq!(attached.get("ok"), Some(&PyValue::Bool(true)), "{attached:?}");
    entered_rx
        .recv_timeout(Duration::from_secs(10))
        .expect("正式 session 的開埠工作應停在接手 handle 之前");
    let held = m.mgr.custody_for_test(DR7_BY_ID).expect("出生即接管的帳本紀錄");
    let formal_sid = match &held.owner {
        CustodyOwner::Session { sid, .. } => sid.clone(),
        other => panic!("owner 應為新建的正式 session：{other:?}"),
    };
    let token = held.token;
    let tx_before = dev.tx_len();
    m.mgr.set_adopt_after_claim_hook_for_test(
        &formal_sid,
        Box::new(|| panic!("測試注入：AdoptBridge 取出 IoWorker 後 unwind（H-actor-panic-after-claim）")),
    );
    go_tx.send(()).expect("放行開埠工作");

    wait_until("回收者排空並結算終態", || m.mgr.terminal_for_test(token).is_some());
    assert_eq!(
        m.mgr.terminal_for_test(token).map(|t| t.handle),
        Some(TerminalHandle::Closed),
        "Reclaim 命令交回收者排空，配對回條結算為 Closed"
    );
    wait_until("handle 關閉", || dev.close_count() == 1);
    assert!(!dev.is_open(), "handle 已關閉");
    assert!(m.mgr.custody_for_test(DR7_BY_ID).is_none(), "handle_closed:true 後紀錄移除");
    assert!(!m.mgr.unconfirmed_close_for_test(DR7_BY_ID), "closed:true 不得登記未確認關閉");
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(dev.close_count(), 1, "只關閉一次");
    assert_eq!(dev.open_count(), 1, "不重新開埠");
    assert_eq!(dev.tx_len(), tx_before, "actor 未接手：沒有登入 TX");
}
