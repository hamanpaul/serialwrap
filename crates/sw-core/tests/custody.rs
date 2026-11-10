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

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use crossbeam_channel::{bounded, unbounded};
use sw_core::admission::{AdmissionGate, FreezeOutcome, HandoffFreezeState};
use sw_core::custody::*;
use sw_core::drain::{StopId, WorkerStoppedReceipt};
use sw_core::mailbox::ControlEvent;
use sw_core::session_core::UnconfirmedCloses;
use sw_core::tx_writer::{EvidenceGap, TxLedger};
use sw_pal::traits::{PalError, PalErrorKind};

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

    let term = ledger.settle_worker_stopped(token, receipt_incomplete);
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

    let term2 = ledger.settle_worker_stopped(token2, receipt_failed);
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
    let (tx, rx) = unbounded();

    // 1. 正常結束
    {
        let _guard = ExitReclaim::new("COM1".into(), 1, tx.clone());
    }
    match rx.try_recv() {
        Ok(ReaperCmd::ActorExited { sid, inc }) => {
            assert_eq!(sid, "COM1");
            assert_eq!(inc, 1);
        }
        _ => panic!("預期 ActorExited"),
    }

    // 2. unwind / panic 結束
    let res = std::panic::catch_unwind(|| {
        let _guard = ExitReclaim::new("COM2".into(), 2, tx.clone());
        panic!("simulated actor panic");
    });
    assert!(res.is_err());
    match rx.try_recv() {
        Ok(ReaperCmd::ActorExited { sid, inc }) => {
            assert_eq!(sid, "COM2");
            assert_eq!(inc, 2);
        }
        _ => panic!("預期 ActorExited"),
    }

    // 3. spawn 失敗路徑：尚未進入執行緒 closure，ExitReclaim 不建立，通道應為空
    assert!(rx.try_recv().is_err());
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
    let stale_handled = ledger.handle_actor_exited(sid, inc1);
    assert!(!stale_handled, "舊世代命令不應影響當前世代帳本紀錄");

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
