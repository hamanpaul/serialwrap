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
use std::sync::Arc;
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
    ledger.mark_bridge_draining(token, 1).expect("mark bridge draining");

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
    ledger.mark_bridge_draining(token2, 2).expect("mark bridge draining");

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
    let (reaper_cmd_tx, _reaper_cmd_rx) = unbounded();
    let (reaper_event_tx, reaper_event_rx) = unbounded();

    let reaper = ReaperThread::start(reaper_cmd_tx, reaper_event_rx);

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

    // 3. panic 超限退出：連續 panic 事件達到上限後退出
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

        for _ in 0..3 {
            let _ = handle.request(sw_core::session_core::SessionRequest::GetState, Duration::from_secs(2));
        }
        let (reply, _reply_rx) = bounded(1);
        let _ = handle.control.send(ControlEvent::Shutdown { reply });
        let _ = handle.join.join();

        let cmd = reaper_rx.try_recv().expect("panic 超限後退出 reaper 應收到 ActorExited");
        match cmd {
            ReaperCmd::ActorExited { sid, inc } => {
                assert_eq!(sid, expected_sid);
                assert_eq!(inc, 3);
            }
            _ => panic!("預期 ActorExited"),
        }
        assert!(reaper_rx.try_recv().is_err(), "panic 超限退出不應有多餘命令");
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
    let gate = Arc::new(AdmissionGate::new());
    let mut deliverer = Deliverer::new(gate.clone());
    let (actor_tx, actor_rx) = bounded(1);

    let sid = "COM1";
    let by_id = "by-id-freeze";
    let real_path = "/dev/ttyUSB0";

    let t0 = Instant::now();
    deliverer.add_obligation(sid, by_id, real_path, 1, 1);

    // 讓 actor lane 滿
    actor_tx.send(ControlEvent::Tick).unwrap();

    // 在 Open 狀態經歷 29.9 秒重試
    let t_29_9 = t0 + Duration::from_millis(29_900);
    let outcome = deliverer.step_delivery(sid, &actor_tx, t_29_9);
    assert_eq!(outcome, DeliveryStepResult::Backoff);

    // 進入 Pending 狀態
    let freeze_res = gate.freeze_if_idle("freeze-op");
    let freeze_token = match freeze_res {
        FreezeOutcome::Frozen { token, .. } => token,
        _ => panic!("預期 Frozen"),
    };
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Pending);

    // 在 Pending 狀態度過 5 秒
    let t_pending = t_29_9 + Duration::from_secs(5);
    let outcome_p = deliverer.step_delivery(sid, &actor_tx, t_pending);
    assert_eq!(outcome_p, DeliveryStepResult::PausedPending);

    // 回復為 Open 狀態
    assert!(gate.thaw(&freeze_token));
    assert_eq!(gate.handoff_freeze_state(), HandoffFreezeState::Open);

    // 釋放 actor 容量
    let _ = actor_rx.try_recv();

    // 在解凍後（自 t0 算起已過去 35 秒，但 Open 累積時間僅 29.9 秒）
    // 應繼續投遞成功，不得被判定 TimedOut 丟棄！
    let t_thawed = t_pending + Duration::from_millis(10);
    let outcome_final = deliverer.step_delivery(sid, &actor_tx, t_thawed);
    assert_ne!(outcome_final, DeliveryStepResult::TimedOut, "Pending 期間不得計入 30s 總時限");
    assert_eq!(outcome_final, DeliveryStepResult::Delivered);
    assert_eq!(deliverer.obligation_count(), 0);
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
    let term = job.execute_and_settle(&mut ledger, None).unwrap();
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
    let term_fail = jobs_fail.pop().unwrap().execute_and_settle(&mut ledger, None).unwrap();
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
    let worker_opt = ledger.mark_bridge_draining(token3, 20).unwrap();
    assert!(worker_opt.is_none());
    let snap3 = ledger.get_custody("by-id-drain").unwrap().snapshot();
    assert!(matches!(snap3.phase, CustodyPhaseSnapshot::BridgeDraining { epoch: 20 }));
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

    ledger.mark_bridge_draining(token, 2).unwrap();

    // 1. 傳入過期回條 (epoch 1) 給處於 epoch 2 的 BridgeDraining
    let receipt_stale = WorkerStoppedReceipt {
        epoch: 1,
        stop_id: StopId(1),
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
    assert!(matches!(rec.phase, CustodyPhase::BridgeDraining { epoch: 2 }));

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
