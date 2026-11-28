#![cfg(feature = "test-fakes")]

mod support;

use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use crossbeam_channel::{Receiver, bounded};
use support::{CoreHarness, SHELL_PROFILE};
use sw_core::drain::{StopId, WorkerStoppedReceipt};
use sw_core::human_console::human_tx::{HumanTxPump, HumanTxTag};
use sw_core::mailbox::ControlEvent;
use sw_core::tx_writer::{OpId, TxClass, TxJob, TxPermit};
use sw_core::uart_bridge::{IoWorker, WorkerSnapshot};
use sw_pal::fakes::serial::{FakeDevice, FakeWriteStep};
use sw_pal::traits::PalError;

const REAL_PATH: &str = "/fake/tty-A";

fn worker_fixture() -> (CoreHarness, IoWorker, FakeDevice, u64) {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.fence.with(|state| {
        state.active_epoch = Some(1);
        state.frozen = false;
    });
    let device = h.fake_device.clone();
    let opened = h.opened_bridge(REAL_PATH);
    assert_eq!(opened.epoch, 1);
    (h, opened.worker, device, opened.epoch)
}

fn wait_until(timeout: Duration, mut predicate: impl FnMut() -> bool) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if predicate() {
            return true;
        }
        thread::sleep(Duration::from_millis(1));
    }
    predicate()
}

fn job(
    worker: &IoWorker,
    epoch: u64,
    op_id: u64,
    bytes: &[u8],
) -> Receiver<sw_core::tx_writer::TxReport> {
    let (reply, report) = bounded(1);
    worker
        .tx()
        .jobs
        .send(TxJob {
            permit: TxPermit {
                op_id: OpId(op_id),
                epoch,
                owner_gen: 0,
                fsm_gen: 0,
                class: TxClass::Command,
                source: "agent:test".into(),
                cmd_id: Some(format!("cmd-{op_id}")),
                log: true,
                input_epoch: None,
                replay_hold: None,
                probe_id: None,
            },
            payload: bytes.to_vec(),
            reply,
            aggregate: None,
        })
        .unwrap();
    report
}

fn finish(worker: IoWorker, stop_id: u64) -> Receiver<ControlEvent> {
    let (tx, rx) = bounded(1);
    worker.finish_async(StopId(stop_id), tx);
    rx
}

fn stopped(rx: &Receiver<ControlEvent>, timeout: Duration) -> WorkerStoppedReceipt {
    match rx.recv_timeout(timeout).expect("應收到 WorkerStopped") {
        ControlEvent::WorkerStopped(receipt) => receipt,
        _ => panic!("預期 WorkerStopped 事件"),
    }
}

fn assert_snapshot(worker: &IoWorker, expected: WorkerSnapshot) {
    let snapshot = worker.snapshot();
    assert_eq!(snapshot.serial_alive, expected.serial_alive);
    assert_eq!(snapshot.running, expected.running);
}

#[test]
fn idle_worker_finishes_with_receipt() {
    let (_h, worker, device, epoch) = worker_fixture();
    let _tx_opctx_clone = worker.tx().clone();
    let (tx, rx) = bounded(1);
    worker.begin_stop();
    worker.finish_async(StopId(1), tx);

    let receipt = stopped(&rx, Duration::from_millis(100));
    assert_eq!(receipt.epoch, epoch);
    assert_eq!(receipt.stop_id, StopId(1));
    assert!(receipt.handle_closed);
    assert_eq!(device.close_count(), 1);
}

#[test]
fn drain_completes_while_opctx_sender_alive() {
    let (_h, worker, device, epoch) = worker_fixture();
    device.script_write(vec![FakeWriteStep::BlockUntilCancel {
        accept_before_cancel: 4,
    }]);
    let _tx_opctx_clone = worker.tx().clone();
    let report_rx = job(&worker, epoch, 1, b"abcd");
    assert!(wait_until(Duration::from_secs(2), || device.os_submits() == 1));

    let stopped_rx = finish(worker, 2);
    let report = report_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(
        report.result.outcome,
        sw_pal::traits::TxOutcome::Completed(4)
    );
    let receipt = stopped(&stopped_rx, Duration::from_secs(2));
    assert_eq!(receipt.ledger.ops[&OpId(1)].confirmed_bytes, 4);
    assert!(receipt.handle_closed);
    assert_eq!(device.close_count(), 1);
}

#[test]
fn blocked_forever_writer_never_sends_receipt() {
    let (_h, worker, device, epoch) = worker_fixture();
    device.script_write(vec![FakeWriteStep::BlockForever]);
    let report_rx = job(&worker, epoch, 1, b"blocked");
    assert!(wait_until(Duration::from_secs(2), || device.os_submits() == 1));

    worker.begin_stop();
    let (tx, rx) = bounded(1);
    worker.finish_async(StopId(3), tx);
    assert!(rx.recv_timeout(Duration::from_secs(3)).is_err());
    assert_eq!(device.close_count(), 0);

    device.release_block();
    let report = report_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(
        report.result.outcome,
        sw_pal::traits::TxOutcome::Completed(7)
    );
    let receipt = stopped(&rx, Duration::from_secs(2));
    assert!(receipt.handle_closed);
    assert_eq!(device.close_count(), 1);
}

#[test]
fn writer_panic_wakes_full_queue_human_waiter_and_keeps_panic_receipt() {
    let (h, worker, device, epoch) = worker_fixture();
    let tx = worker.tx().clone();
    device.script_write(vec![FakeWriteStep::BlockForever]);
    let blocker = job(&worker, epoch, 16_294_001, b"block");
    assert!(wait_until(Duration::from_secs(2), || device.os_submits() == 1));
    device.wait_blocked();

    let capacity = tx.jobs.capacity().expect("writer queue is bounded");
    let mut fillers = Vec::with_capacity(capacity);
    for index in 0..capacity {
        fillers.push(job(
            &worker,
            epoch,
            16_294_100 + index as u64,
            b"FILL",
        ));
    }
    assert_eq!(tx.jobs.len(), capacity, "writer queue is full behind blocker");

    let (wait_tx, wait_rx) = bounded(1);
    tx.set_capacity_wait_hook_for_test(Some(Arc::new(move || {
        wait_tx.send(()).expect("human pump is waiting for queue capacity");
    })));
    let (pump_control_tx, pump_control_rx) = bounded(1);
    let pump = HumanTxPump::spawn("i294-r16", tx.clone(), Arc::clone(&h.rx), pump_control_tx);
    pump.try_submit(
        HumanTxTag {
            seq: 1,
            client_id: "i294-r16".into(),
            client_lifetime: 1,
            input_epoch: 0,
            class: TxClass::HumanRaw,
            len: 1,
        },
        TxPermit {
            op_id: OpId(16_294_999),
            epoch,
            owner_gen: 0,
            fsm_gen: 0,
            class: TxClass::HumanRaw,
            source: "human:i294-r16".into(),
            cmd_id: None,
            log: true,
            input_epoch: Some(0),
            replay_hold: None,
            probe_id: None,
        },
        b"P".to_vec(),
        h.rx.begin_human_tx_effect(),
    )
    .expect("pump accepts one human job");
    wait_rx
        .recv_timeout(Duration::from_secs(5))
        .expect("pump has entered its capacity wait");
    assert_eq!(h.rx.human_in_flight_for_test(), 1);

    let (panic_tx, panic_rx) = bounded(1);
    tx.set_panic_on_dequeue_hook_for_test(Some(Arc::new(move || {
        panic_tx.send(()).expect("writer reaches injected panic point");
    })));
    device.release_block();
    let _ = blocker.recv_timeout(Duration::from_secs(2)).expect("blocker report");
    panic_rx
        .recv_timeout(Duration::from_secs(2))
        .expect("writer dequeues the first queued job and panics");

    let (stopped_tx, stopped_rx) = bounded(1);
    worker.finish_async(StopId(16_294_001), stopped_tx);
    let receipt = stopped(&stopped_rx, Duration::from_secs(2));
    let panic_message = "UART worker thread panicked before returning its handle";
    assert_eq!(receipt.epoch, epoch);
    assert_eq!(receipt.stop_id, StopId(16_294_001));
    assert!(!receipt.handle_closed, "panic receipt preserves unconfirmed handle state");
    assert_eq!(
        receipt.wal_error.as_ref().map(|error| error.py_message.as_str()),
        Some(panic_message)
    );
    assert_eq!(
        receipt.close_error.as_ref().map(|error| error.py_message.as_str()),
        Some(panic_message)
    );

    let first_completion = pump_control_rx.recv_timeout(Duration::from_millis(300));
    let first_guard_zero = wait_until(Duration::from_millis(300), || {
        h.rx.human_in_flight_for_test() == 0
    });
    let woke_from_writer_end = first_completion.is_ok() && first_guard_zero;
    if !woke_from_writer_end {
        // 只供舊基線失敗後收尾，避免測試留下永久等待的 pump thread；判準保留清理前結果。
        tx.close_capacity_for_test();
    }
    let completion = match first_completion {
        Ok(event) => event,
        Err(_) => pump_control_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("cleanup releases a baseline waiter"),
    };
    match completion {
        ControlEvent::HumanTxCompleted { report, .. } => {
            assert_eq!(report.result.outcome, sw_pal::traits::TxOutcome::NotSubmitted);
        }
        _ => panic!("unexpected pump completion event"),
    }
    assert!(woke_from_writer_end, "writer panic must close capacity and wake the pump");
    assert_eq!(h.rx.human_in_flight_for_test(), 0, "human guard is dropped");
    pump.stop();
    drop(fillers);
}

#[test]
fn begin_stop_not_blocked_by_inflight_completion_wait() {
    let (h, worker, device, epoch) = worker_fixture();
    device.script_write(vec![FakeWriteStep::BlockForever]);
    let report_rx = job(&worker, epoch, 1, b"blocked");
    assert!(wait_until(Duration::from_secs(2), || device.os_submits() == 1));
    assert!(
        h.fence.try_with(|_| ()).is_some(),
        "writer 不得在完成等待期間持有 Fence"
    );

    let (tx, rx) = bounded(1);
    let start = Instant::now();
    h.fence.with(|state| state.frozen = true);
    thread::sleep(Duration::from_millis(20));
    worker.begin_stop();
    assert!(start.elapsed() < Duration::from_millis(50));
    assert!(h.fence.with(|state| state.frozen));
    assert!(rx.try_recv().is_err());
    assert_eq!(device.close_count(), 0);
    worker.finish_async(StopId(4), tx);
    thread::sleep(Duration::from_millis(20));
    assert!(rx.try_recv().is_err());

    device.release_block();
    let _ = report_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    let receipt = stopped(&rx, Duration::from_secs(2));
    assert!(receipt.handle_closed);
    assert_eq!(device.close_count(), 1);
}

#[test]
fn wal_unwritable_during_drain_lists_gap() {
    let (h, worker, device, _epoch) = worker_fixture();
    h.wfs.fail_next_writes(
        &h.wal.wal_path(),
        4,
        PalError::io(28, "[Errno 28] No space left on device"),
    );
    device.push_rx(b"x");
    assert!(wait_until(Duration::from_secs(2), || h.rx.total_bytes() == 1));

    // 不回覆 actor barrier，確認有界逾時仍會交付 receipt，並明確留下排序證據缺口。
    let (stopped_tx, stopped_rx) = bounded(2);
    worker.finish_async(StopId(5), stopped_tx);
    let (reply, expired) = match stopped_rx.recv_timeout(Duration::from_secs(2)).unwrap() {
        ControlEvent::DrainDataBarrier { reply, expired, .. } => (reply, expired),
        _ => panic!("預期先收到 drain barrier"),
    };
    let receipt = stopped(&stopped_rx, Duration::from_secs(8));
    assert!(receipt.handle_closed);
    assert!(!receipt.unrecorded.is_empty());
    assert!(receipt
        .unrecorded
        .iter()
        .any(|gap| gap.dir == "RX" && gap.len == 1));
    assert!(receipt.unrecorded.iter().any(|gap| {
        gap.dir == "RX" && gap.len == 0 && gap.error.py_message.contains("逾時")
    }));
    assert!(
        expired.load(std::sync::atomic::Ordering::Acquire),
        "逾時須釋放 actor mailbox 中的 barrier"
    );
    assert!(reply.try_send(()).is_err(), "receipt 交付後 barrier receiver 已關閉");
}

#[test]
fn close_failure_in_receipt() {
    let (_h, worker, device, epoch) = worker_fixture();
    device.fail_close(PalError::io(5, "[Errno 5] Input/output error"));

    let receipt = stopped(&finish(worker, 6), Duration::from_secs(2));
    assert_eq!(receipt.epoch, epoch);
    assert!(!receipt.handle_closed);
    assert!(receipt.close_error.is_some());
    assert_eq!(device.close_count(), 1);
}

#[test]
fn receipt_ledger_holds_only_unsettled_ops() {
    let (h, worker, device, epoch) = worker_fixture();
    for op_id in 1..=601 {
        if op_id == 3 {
            h.wfs.fail_next_writes(
                &h.wal.wal_path(),
                2,
                PalError::io(28, "[Errno 28] No space left on device"),
            );
        }
        let report = job(&worker, epoch, op_id, b"x")
            .recv_timeout(Duration::from_secs(2))
            .unwrap_or_else(|error| panic!("op {op_id} reply timed out: {error}"));
        assert_eq!(
            report.result.outcome,
            sw_pal::traits::TxOutcome::Completed(1)
        );
        if op_id == 3 {
            assert!(report.wal.as_ref().is_some_and(|append| append.loss_flag));
        }
        h.drain_control_events();
        if op_id <= 600 {
            assert!(worker.tx().settle(OpId(op_id)));
            assert!(wait_until(Duration::from_secs(2), || {
                worker.tx().ledger_gauge().settled_total >= op_id
            }));
        }
    }

    let receipt = stopped(&finish(worker, 7), Duration::from_secs(3));
    assert_eq!(
        receipt.ledger.ops.keys().copied().collect::<Vec<_>>(),
        vec![OpId(601)]
    );
    assert_eq!(receipt.ledger.settled_total, 600);
    assert_eq!(receipt.unrecorded.len(), 1);
    assert_eq!(receipt.unrecorded[0].op_id, Some(OpId(3)));
    assert_eq!(receipt.unrecorded[0].dir, "TX");
    assert_eq!(device.close_count(), 1);
}

#[test]
fn snapshot_serial_alive_and_running_semantics() {
    let (_h, worker, _device, _epoch) = worker_fixture();
    assert_snapshot(
        &worker,
        WorkerSnapshot {
            serial_alive: true,
            running: true,
        },
    );
    let receipt = stopped(&finish(worker, 8), Duration::from_secs(2));
    assert!(receipt.handle_closed);
}

#[test]
fn snapshot_tracks_handle_liveness_across_eof_error_and_stop() {
    let (h, worker, device, _epoch) = worker_fixture();
    device.close_peer();
    assert!(wait_until(Duration::from_secs(2), || device.eof_reads() > 0));
    assert_snapshot(
        &worker,
        WorkerSnapshot {
            serial_alive: true,
            running: true,
        },
    );

    device.remove();
    h.clock.advance(Duration::from_millis(200));
    assert!(wait_until(Duration::from_secs(2), || {
        device.read_errors() > 0 && !worker.snapshot().running
    }));
    assert_snapshot(
        &worker,
        WorkerSnapshot {
            serial_alive: true,
            running: false,
        },
    );

    worker.begin_stop();
    assert!(wait_until(Duration::from_secs(2), || !worker.snapshot().running));
    assert_snapshot(
        &worker,
        WorkerSnapshot {
            serial_alive: true,
            running: false,
        },
    );
    let receipt = stopped(&finish(worker, 9), Duration::from_secs(2));
    assert!(receipt.handle_closed);
    assert!(!device.is_open());
}
