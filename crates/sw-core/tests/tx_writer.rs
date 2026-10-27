#![cfg(feature = "test-fakes")]
//! P5-T9 紅燈測試：TxWriter＋Fence（送出前檢查、BC-03 三態、SC-07）。
//!
//! 對應 `docs/rust-refactor/plans/06-slice21-daemon-core.md` Task 9。實作尚未存在，本檔只定義行為斷言。
use std::{
    path::Path,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use sw_core::{mailbox::ControlEvent, tx_writer::*, wal::*};
use sw_pal::{
    fakes::{clock::FakeClock, serial::*, wal_fs::FakeWalFs},
    *,
};

const WAL_PATH: &str = "/w/raw.wal.ndjson";

#[allow(dead_code)]
struct Rig {
    fence: Arc<Fence>,
    wal: Arc<WalWriter>,
    dev: FakeDevice,
    h: TxWriterHandle,
    wfs: Arc<FakeWalFs>,
    cancel: CancelToken,
    stop: Option<TxWriterStop>,
    join: Option<std::thread::JoinHandle<(Box<dyn SerialWriter>, TxLedger)>>,
    /// drainer thread 收集 writer 送出的 control 事件，避免 bounded(256) 塞住 writer。
    ctl: Arc<Mutex<Vec<ControlEvent>>>,
}

/// 每 2 ms 輪詢一次帳本 gauge，2 s 內未滿足即 panic 並印出最後的 gauge。
fn wait_gauge(r: &Rig, pred: impl Fn(&TxLedgerGauge) -> bool) -> TxLedgerGauge {
    let t0 = Instant::now();
    loop {
        let g = r.h.ledger_gauge();
        if pred(&g) {
            return g;
        }
        assert!(t0.elapsed() < Duration::from_secs(2), "gauge {g:?}");
        std::thread::sleep(Duration::from_millis(2));
    }
}

/// 等待 writer 在 `op` 的寫入中（`in_flight` 已由 gate 設定）。
fn wait_in_flight(r: &Rig, op: u64) {
    let t0 = Instant::now();
    while r.fence.with(|s| s.in_flight.as_ref().map(|f| f.op_id)) != Some(OpId(op)) {
        assert!(t0.elapsed() < Duration::from_secs(2), "in_flight 未出現於 op {op}");
        std::thread::sleep(Duration::from_millis(1));
    }
}

fn rig() -> Rig {
    let be = FakeSerialBackend::new();
    let dev = be.add_device("/dev/f");
    let h = be
        .open_exclusive(
            &DevicePath("/dev/f".into()),
            &SerialConfig::from_uart_profile(115200, 8, "N", 1, "none", false),
        )
        .unwrap();
    let (_r, w, waker, _c) = h.split();
    let wfs = FakeWalFs::new();
    let wal = Arc::new(WalWriter::open(
        WalConfig { dir: "/w".into(), rotate_bytes: 1 << 26 },
        wfs.clone(),
        FakeClock::at(1, 1),
    ));
    let fence = Fence::new();
    fence.with(|s| s.active_epoch = Some(7));
    let cancel = CancelToken::new(waker);
    let (ctl_tx, ctl_rx) = crossbeam_channel::bounded::<ControlEvent>(256);
    let ctl = Arc::new(Mutex::new(Vec::new()));
    {
        let ctl = ctl.clone();
        std::thread::spawn(move || {
            for ev in ctl_rx.iter() {
                ctl.lock().unwrap().push(ev)
            }
        });
    }
    let (h, stop, j) =
        spawn_tx_writer("COM0".into(), w, cancel.clone(), fence.clone(), wal.clone(), 7, ctl_tx);
    Rig { fence, wal, dev, h, wfs, cancel, stop: Some(stop), join: Some(j), ctl }
}

/// 建立一筆 TX job 與其回覆 receiver（epoch 固定 7、cmd_id 固定 "c"、source "agent:x"）。
fn job(
    op: u64,
    class: TxClass,
    owner_gen: u64,
    fsm_gen: u64,
    log: bool,
    payload: &[u8],
) -> (TxJob, crossbeam_channel::Receiver<TxReport>) {
    let (tx, rx) = crossbeam_channel::bounded(1);
    (
        TxJob {
            permit: TxPermit {
                op_id: OpId(op),
                epoch: 7,
                owner_gen,
                fsm_gen,
                class,
                source: "agent:x".into(),
                cmd_id: Some("c".into()),
                log,
                input_epoch: None,
                replay_hold: None,
            },
            payload: payload.to_vec(),
            reply: tx,
        },
        rx,
    )
}

fn send(r: &Rig, class: TxClass, op: u64, epoch: u64, payload: &[u8]) -> TxReport {
    let (tx, rx) = crossbeam_channel::bounded(1);
    r.h.jobs
        .send(TxJob {
            permit: TxPermit {
                op_id: OpId(op),
                epoch,
                owner_gen: 0,
                fsm_gen: 0,
                class,
                source: "agent:x".into(),
                cmd_id: Some("c".into()),
                log: true,
                input_epoch: None,
                replay_hold: None,
            },
            payload: payload.to_vec(),
            reply: tx,
        })
        .unwrap();
    rx.recv_timeout(Duration::from_secs(2)).unwrap()
}

fn send_async(
    r: &Rig,
    class: TxClass,
    op: u64,
    owner_gen: u64,
    payload: &[u8],
) -> crossbeam_channel::Receiver<TxReport> {
    let (j, rx) = job(op, class, owner_gen, 0, true, payload);
    r.h.jobs.send(j).unwrap();
    rx
}

fn tx_lens(r: &Rig) -> Vec<String> {
    wal_lines(r)
        .into_iter()
        .filter(|line| line.contains("\"dir\":\"TX\""))
        .collect()
}

fn meta_lens(r: &Rig) -> Vec<String> {
    wal_lines(r)
        .into_iter()
        .filter(|line| line.contains("\"dir\":\"META\""))
        .collect()
}

fn wal_lines(r: &Rig) -> Vec<String> {
    String::from_utf8(r.wfs.contents(WAL_PATH.as_ref()).unwrap_or_default())
        .unwrap()
        .lines()
        .map(str::to_string)
        .collect()
}

fn enospc() -> sw_pal::PalError {
    PalError::io(28, "[Errno 28] No space left on device")
}

/// 標準 base64（含 `=` 補齊），用於比對 TX WAL 行中的 `payload_b64`。
fn b64(bytes: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(T[((n >> (18 - 6 * i)) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

fn payload_b64(line: &str) -> String {
    let key = "\"payload_b64\":\"";
    let start = line.find(key).expect("WAL 行必須含 payload_b64") + key.len();
    let end = line[start..].find('"').expect("payload_b64 結尾引號") + start;
    line[start..end].to_string()
}

fn count_saturated(r: &Rig) -> usize {
    r.ctl
        .lock()
        .unwrap()
        .iter()
        .filter(|e| matches!(e, ControlEvent::UartIoError { epoch: 7, reason } if reason == "TX_LEDGER_SATURATED"))
        .count()
}

const ALL_CLASSES: &[TxClass] = &[
    TxClass::Flash,
    TxClass::FlashProbe,
    TxClass::LeaseOwner,
    TxClass::HumanRaw,
    TxClass::HumanReplay,
    TxClass::LoginProbe,
    TxClass::ReadyReconfirm,
    TxClass::Command,
    TxClass::FileTransfer,
    TxClass::SystemRecoverKey,
    TxClass::SelfTestProbe,
    TxClass::InteractiveOpenCmd,
];

const NON_FLASH_CLASSES: &[TxClass] = &[
    TxClass::FlashProbe,
    TxClass::LeaseOwner,
    TxClass::HumanRaw,
    TxClass::HumanReplay,
    TxClass::LoginProbe,
    TxClass::ReadyReconfirm,
    TxClass::Command,
    TxClass::FileTransfer,
    TxClass::SystemRecoverKey,
    TxClass::SelfTestProbe,
    TxClass::InteractiveOpenCmd,
];

const PROBE_CLASSES: &[TxClass] = &[
    TxClass::LoginProbe,
    TxClass::ReadyReconfirm,
    TxClass::SystemRecoverKey,
    TxClass::SelfTestProbe,
];

// ---------------------------------------------------------------------------
// 送出前檢查（Fence::admit）與 OS 提交的序列化（R0-F1）
// ---------------------------------------------------------------------------

#[test]
fn stale_epoch_is_not_submitted_and_never_calls_os_write() {
    let r = rig();
    let rep = send(&r, TxClass::Command, 1, 6, b"ls\n");
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::StaleEpoch)));
    assert_eq!(r.dev.write_calls(), 0);
    assert!(tx_lens(&r).is_empty());
}

#[test]
fn partial_write_records_only_confirmed_prefix() {
    let r = rig();
    r.dev.script_write(vec![FakeWriteStep::AcceptThenError(3, PalError::io(5, "[Errno 5] Input/output error"))]);
    let rep = send(&r, TxClass::Command, 1, 7, b"0123456789");
    assert_eq!(rep.result.outcome, TxOutcome::Completed(3));
    let lines = tx_lens(&r);
    assert_eq!(lines.len(), 1);
    assert!(lines[0].contains("\"len\":3,") && lines[0].contains("\"payload_b64\":\"MDEy\""));
}

#[test]
fn freeze_during_in_flight_counts_as_committed_and_next_is_rejected() {
    let r = rig();
    r.dev.script_write(vec![FakeWriteStep::BlockUntilCancel { accept_before_cancel: 2 }]);
    let (j, rx) = job(1, TxClass::Command, 0, 0, true, b"abcdef");
    r.h.jobs.send(j).unwrap();
    wait_in_flight(&r, 1);
    r.fence.with(|s| {
        assert!(s.in_flight.is_some());
        s.frozen = true;
        s.active_epoch = None;
    });
    r.cancel.cancel();
    let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(rep.result.outcome, TxOutcome::Completed(2));
    assert_eq!(tx_lens(&r).len(), 1);
    let rep2 = send(&r, TxClass::Command, 2, 7, b"x");
    assert_eq!(rep2.result.outcome, TxOutcome::NotSubmitted);
}

// ---- R0-F1：授權與 OS 提交的序列化（arch §5.4 第 3 點、規格 §5.2.2）----

#[test]
fn authorized_then_quiet_before_os_submit_is_zero_tx() {
    // 舊設計（鎖內驗證→放鎖→write）會在此寫出 bytes
    let r = rig();
    r.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let rx = send_async(&r, TxClass::Command, 1, 0, b"reboot\n");
    assert!(r.dev.wait_paused(Duration::from_secs(2))); // 出列快速檢查已通過、尚未提交 OS
    r.fence.with(|s| s.quiet_active = true); // 不得阻塞：writer 此時不持鎖
    r.dev.release_pause();
    let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::Quiet)));
    assert!(rep.result.error.is_none());
    assert_eq!(r.dev.os_submits(), 0);
    assert!(r.dev.take_tx().is_empty());
    assert!(tx_lens(&r).is_empty());
    assert!(r.fence.with(|s| s.in_flight.is_none()));
}

#[test]
fn authorized_then_drain_freeze_before_os_submit_is_zero_tx() {
    let r = rig();
    r.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let rx = send_async(&r, TxClass::Command, 1, 0, b"ls\n");
    assert!(r.dev.wait_paused(Duration::from_secs(2)));
    r.fence.with(|s| {
        s.frozen = true;
        s.active_epoch = None;
    }); // Phase 1（換手／release）
    r.dev.release_pause();
    let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::Frozen)));
    assert_eq!(r.dev.os_submits(), 0);
    assert!(r.dev.take_tx().is_empty() && tx_lens(&r).is_empty());
}

#[test]
fn authorized_then_lease_owner_gen_bump_before_os_submit_is_zero_tx() {
    // LeaseOwner 世代在提交臨界區重驗
    let r = rig();
    r.fence.with(|s| s.owner_gen = 3);
    r.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let rx = send_async(&r, TxClass::LeaseOwner, 1, 3, b"\x03");
    assert!(r.dev.wait_paused(Duration::from_secs(2)));
    r.fence.with(|s| s.owner_gen = 4); // lease close／expiry／換手
    r.dev.release_pause();
    let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::OwnerMismatch)));
    assert_eq!(r.dev.os_submits(), 0);
    assert!(r.dev.take_tx().is_empty() && tx_lens(&r).is_empty());
}

#[test]
fn quiet_during_os_submit_waits_for_submit_and_counts_committed() {
    let r = rig();
    r.dev.script_write(vec![FakeWriteStep::PauseInsideSubmit]);
    let rx = send_async(&r, TxClass::Command, 1, 0, b"abc");
    assert!(r.dev.wait_paused(Duration::from_secs(2))); // 已重驗通過、持 Fence 鎖、os_submit 進行中
    let (done_tx, done_rx) = crossbeam_channel::bounded(1);
    let f = r.fence.clone();
    let inv = std::thread::spawn(move || {
        f.with(|s| s.quiet_active = true);
        done_tx.send(()).unwrap();
    });
    assert!(done_rx.recv_timeout(Duration::from_millis(100)).is_err(), "失效操作必須等到 OS 提交返回");
    assert!(r.dev.take_tx().is_empty(), "暫停點在提交之前，bytes 尚未交 OS");
    r.dev.release_pause();
    inv.join().unwrap();
    assert!(done_rx.recv_timeout(Duration::from_secs(1)).is_ok());
    let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::Completed(3), None));
    assert_eq!(r.dev.take_tx(), b"abc".to_vec());
    assert_eq!(tx_lens(&r).len(), 1);
    let rep2 = send(&r, TxClass::Command, 2, 7, b"x");
    assert_eq!((rep2.result.outcome, rep2.reject), (TxOutcome::NotSubmitted, Some(FenceReject::Quiet)));
    assert_eq!(r.dev.os_submits(), 1);
}

#[test]
fn quiet_between_partial_submits_stops_tail() {
    let r = rig();
    r.dev.script_write(vec![FakeWriteStep::AcceptThenWouldBlock(2)]);
    let rx = send_async(&r, TxClass::Command, 1, 0, b"abcdef");
    assert!(r.dev.wait_paused(Duration::from_secs(2))); // 已提交 "ab"，於 gate 外等 POLLOUT
    r.fence.with(|s| {
        assert!(s.in_flight.is_some());
        s.quiet_active = true;
    }); // 不得阻塞
    r.dev.release_pause();
    let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::Completed(2), Some(FenceReject::Quiet)));
    assert_eq!(rep.result.error.as_ref().unwrap().kind, PalErrorKind::Cancelled);
    assert_eq!(r.dev.take_tx(), b"ab".to_vec()); // 尾段 "cdef" 永不交 OS
    assert_eq!(r.dev.os_submits(), 1);
    let lines = tx_lens(&r);
    assert_eq!(lines.len(), 1);
    assert!(lines[0].contains("\"len\":2,"));
}

#[test]
fn os_submit_always_inside_fence_critical_section() {
    let r = rig();
    let f = r.fence.clone();
    let unlocked = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let u2 = unlocked.clone();
    r.dev.on_os_submit(Box::new(move || {
        if f.try_with(|_| ()).is_some() {
            u2.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        }
    }));
    r.dev.script_write(vec![FakeWriteStep::AcceptThenWouldBlock(1)]);
    let rx = send_async(&r, TxClass::Command, 1, 0, b"xyz");
    assert!(r.dev.wait_paused(Duration::from_secs(2)));
    r.dev.release_pause();
    assert_eq!(rx.recv_timeout(Duration::from_secs(2)).unwrap().result.outcome, TxOutcome::Completed(3));
    for i in 0..20u64 {
        send(&r, TxClass::Command, 10 + i, 7, b"q");
    }
    assert!(r.dev.os_submits() >= 22);
    assert_eq!(
        unlocked.load(std::sync::atomic::Ordering::SeqCst),
        0,
        "每次 OS 提交時 Fence 鎖必為持有"
    );
}

// ---------------------------------------------------------------------------
// TX 帳本結算（R1-F8）
// ---------------------------------------------------------------------------

#[test]
fn ledger_retires_completed_ops_without_reconnect() {
    // 同一 epoch 長跑、不重連，已結算 op 不得累積
    let mut r = rig();
    for i in 0..600u64 {
        // 超過 CMD_HISTORY_MAX(512)：history 淘汰後帳本也不得殘留
        assert_eq!(send(&r, TxClass::Command, 1000 + i, 7, b"ok\n").result.outcome, TxOutcome::Completed(3));
        assert!(r.h.settle(OpId(1000 + i))); // 等同 actor 終態處理點的 Effect::SettleTx
    }
    let g = wait_gauge(&r, |g| g.settled_total == 600);
    assert_eq!(
        (g.live_ops, g.unresolved_gaps, g.gaps_overflow, g.settle_dropped, g.saturated),
        (0, 0, 0, 0, false)
    );
    assert_eq!(send(&r, TxClass::Command, 5000, 7, b"tail").result.outcome, TxOutcome::Completed(4)); // 未結算 op
    wait_gauge(&r, |g| g.live_ops == 1);
    r.stop.as_ref().unwrap().send();
    let (_w, ledger) = r.join.take().unwrap().join().unwrap();
    assert_eq!(ledger.ops.keys().copied().collect::<Vec<_>>(), vec![OpId(5000)]); // 只剩未結算者
    assert_eq!((ledger.ops[&OpId(5000)].confirmed_bytes, ledger.settled_total), (4, 600));
    assert!(ledger.unresolved_gaps.is_empty() && ledger.gaps_overflow == GapOverflow::default());
    assert_eq!(tx_lens(&r).len(), 601); // 結算只動記憶體帳本，不動 WAL 證據
}

#[test]
fn ledger_retire_racing_drain_preserves_bc03() {
    // 結算與 drain 競速時，在途 op 的 BC-03 證據不得被淘汰
    for settle_after_stop in [false, true] {
        let mut r = rig();
        assert_eq!(send(&r, TxClass::Command, 1, 7, b"done\n").result.outcome, TxOutcome::Completed(5)); // 已終態的舊命令
        r.dev.script_write(vec![FakeWriteStep::BlockUntilCancel { accept_before_cancel: 2 }]);
        let rx2 = send_async(&r, TxClass::Command, 2, 0, b"abcdef"); // 前景在途＝aborted_op
        wait_in_flight(&r, 2);
        if !settle_after_stop {
            assert!(r.h.settle(OpId(1))); // writer 卡在 op 2 的完成等待，settle 先排隊
        }
        r.fence.with(|s| {
            s.frozen = true;
            s.active_epoch = None;
        }); // drain Phase 1 線性化點
        r.stop.as_ref().unwrap().send();
        if settle_after_stop {
            let _ = r.h.settle(OpId(1)); // 可能被 stop 後的 settle 收尾套用，也可能 Disconnected
        }
        r.cancel.cancel();
        assert_eq!(rx2.recv_timeout(Duration::from_secs(2)).unwrap().result.outcome, TxOutcome::Completed(2));
        let (queued_job, queued_reply) = job(3, TxClass::Command, 0, 0, true, b"zz"); // stop 後才到：不得寫 OS
        let queued_sent = r.h.jobs.send(queued_job).is_ok();
        let (_w, ledger) = r.join.take().unwrap().join().unwrap();
        let op2 = &ledger.ops[&OpId(2)]; // aborted_op 永不被結算，帳本必有其證據
        assert_eq!((op2.confirmed_bytes, op2.unknown), (2, false), "settle_after_stop={settle_after_stop}");
        if !settle_after_stop {
            assert!(!ledger.ops.contains_key(&OpId(1))); // stop 前已排隊的 settle 必在交帳前套用
        }
        if let Some(op1) = ledger.ops.get(&OpId(1)) {
            assert_eq!(op1.confirmed_bytes, 5); // 未套用時證據完整、不被部分改寫
        }
        if queued_sent {
            if let Ok(rep) = queued_reply.try_recv() {
                assert_eq!(rep.result.outcome, TxOutcome::NotSubmitted);
            }
        }
        assert_eq!(r.dev.take_tx(), b"done\nab".to_vec()); // 結算不改變任何已寫出／未寫出的事實
    }
}

// ---------------------------------------------------------------------------
// Gate 表（Fence::admit 參數化）
// ---------------------------------------------------------------------------

struct GateRow {
    name: &'static str,
    classes: &'static [TxClass],
    setup: fn(&mut FenceState),
    permit_owner_gen: u64,
    permit_fsm_gen: u64,
    expect: Result<(), FenceReject>,
}

fn gate_row(
    name: &'static str,
    classes: &'static [TxClass],
    setup: fn(&mut FenceState),
    expect: Result<(), FenceReject>,
) -> GateRow {
    GateRow { name, classes, setup, permit_owner_gen: 0, permit_fsm_gen: 0, expect }
}

#[test]
fn gate_rules() {
    // 每列以 `Fence::admit(&state, &permit)` 斷言；狀態預設 active_epoch=Some(7)、其餘欄位為初始值。
    let rows: Vec<GateRow> = vec![
        gate_row("Command+quiet", &[TxClass::Command], |s| s.quiet_active = true, Err(FenceReject::Quiet)),
        gate_row(
            "Command+reconfirm",
            &[TxClass::Command],
            |s| s.reconfirm_pending = true,
            Err(FenceReject::ReconfirmPending),
        ),
        gate_row("FileTransfer+quiet", &[TxClass::FileTransfer], |s| s.quiet_active = true, Err(FenceReject::Quiet)),
        gate_row("probe 類+quiet", PROBE_CLASSES, |s| s.quiet_active = true, Err(FenceReject::Quiet)),
        gate_row("probe 類+wal_unhealthy", PROBE_CLASSES, |s| s.wal_unhealthy = true, Err(FenceReject::WalUnhealthy)),
        gate_row(
            "SelfTestProbe+reconfirm（quiet=false）",
            &[TxClass::SelfTestProbe],
            |s| s.reconfirm_pending = true,
            Ok(()),
        ),
        GateRow {
            name: "SelfTestProbe+fsm 不一致（quiet=false）",
            classes: &[TxClass::SelfTestProbe],
            setup: |s| s.fsm_gen = 2,
            permit_owner_gen: 0,
            permit_fsm_gen: 1,
            expect: Ok(()),
        },
        gate_row("Command+wal_unhealthy", &[TxClass::Command], |s| s.wal_unhealthy = true, Ok(())),
        gate_row(
            "LeaseOwner／InteractiveOpenCmd+quiet",
            &[TxClass::LeaseOwner, TxClass::InteractiveOpenCmd],
            |s| s.quiet_active = true,
            Ok(()),
        ),
        // 表列「Command＋reconfirm；SystemRecoverKey 同狀態」：兩類分列斷言
        gate_row(
            "Command+reconfirm（quiet=false）",
            &[TxClass::Command],
            |s| s.reconfirm_pending = true,
            Err(FenceReject::ReconfirmPending),
        ),
        gate_row(
            "SystemRecoverKey+reconfirm（quiet=false）",
            &[TxClass::SystemRecoverKey],
            |s| s.reconfirm_pending = true,
            Ok(()),
        ),
        gate_row("非 Flash class+Flashing", NON_FLASH_CLASSES, |s| s.flash_mode = FlashMode::Flashing, Err(FenceReject::Flash)),
        gate_row("Flash+Off", &[TxClass::Flash], |s| s.flash_mode = FlashMode::Off, Err(FenceReject::Flash)),
        gate_row("任一+frozen", ALL_CLASSES, |s| s.frozen = true, Err(FenceReject::Frozen)),
        gate_row("任一+epoch 不符", ALL_CLASSES, |s| s.active_epoch = Some(6), Err(FenceReject::StaleEpoch)),
        gate_row("任一+active_epoch=None", ALL_CLASSES, |s| s.active_epoch = None, Err(FenceReject::StaleEpoch)),
        gate_row("任一+op 已取消", ALL_CLASSES, |s| {
            s.cancelled.insert(OpId(1));
        }, Err(FenceReject::Cancelled)),
    ];
    for row in rows {
        for &class in row.classes {
            let f = Fence::new();
            f.with(|s| {
                s.active_epoch = Some(7);
                (row.setup)(s);
            });
            let p = TxPermit {
                op_id: OpId(1),
                epoch: 7,
                owner_gen: row.permit_owner_gen,
                fsm_gen: row.permit_fsm_gen,
                class,
                source: "agent:x".into(),
                cmd_id: None,
                log: true,
                input_epoch: None,
                replay_hold: None,
            };
            let got = f.with(|s| Fence::admit(s, &p));
            assert_eq!(got, row.expect, "列={} class={class:?}", row.name);
        }
    }
}

// ---------------------------------------------------------------------------
// 其他 TX 三態、WAL 證據與帳本上限
// ---------------------------------------------------------------------------

#[test]
fn unknown_outcome_records_no_tx_and_reports_unknown() {
    let r = rig();
    let e = PalError::io(5, "[Errno 5] Input/output error");
    r.dev.script_write(vec![FakeWriteStep::Unknown { confirmed: 0, error: e }]);
    let rep = send(&r, TxClass::Command, 1, 7, b"abc");
    assert_eq!(rep.result.outcome, TxOutcome::Unknown { confirmed: 0 });
    assert!(rep.wal.is_none());
    assert!(tx_lens(&r).is_empty());
}

#[test]
fn unknown_with_log_writes_tx_outcome_unknown_meta() {
    let r = rig();
    let error = PalError::io(5, "[Errno 5] Input/output error");
    r.dev.script_write(vec![
        FakeWriteStep::Unknown { confirmed: 0, error: error.clone() },
        FakeWriteStep::Unknown { confirmed: 2, error },
    ]);

    let zero = send(&r, TxClass::Command, 1, 7, b"abc");
    assert_eq!(zero.result.outcome, TxOutcome::Unknown { confirmed: 0 });
    assert!(zero.wal.is_none());
    let partial = send(&r, TxClass::Command, 2, 7, b"abcde");
    assert_eq!(partial.result.outcome, TxOutcome::Unknown { confirmed: 2 });
    assert!(matches!(partial.wal, Some(WalAppend { ok: true, loss_flag: false, .. })));

    let metas = meta_lens(&r);
    assert_eq!(metas.len(), 2, "Unknown 的零確認與部分確認都要有 META：{metas:?}");
    assert!(metas[0].contains("\"event\":\"tx_outcome_unknown\""));
    assert!(metas[0].contains("\"payload_b64\":\"\""));
    assert!(metas[0].contains("\"cmd_id\":\"c\""));
    assert!(metas[0].contains("\"com\":\"COM0\""));
    assert!(metas[0].contains("\"source\":\"agent:x\""));
    assert!(metas[0].contains("\"confirmed_bytes\":0"));
    assert!(metas[0].contains("\"requested_bytes\":3"));
    assert!(metas[0].contains("\"unknown_range\":[0,3]"));
    assert!(metas[0].contains("\"error\":\"[Errno 5] Input/output error\""));
    assert!(metas[1].contains("\"confirmed_bytes\":2"));
    assert!(metas[1].contains("\"requested_bytes\":5"));
    assert!(metas[1].contains("\"unknown_range\":[2,5]"));
    assert_eq!(tx_lens(&r).len(), 1, "只記錄部分確認的 TX 前綴");
    assert!(tx_lens(&r)[0].contains("\"payload_b64\":\"YWI=\""));
}

#[test]
fn unknown_with_log_false_writes_no_meta() {
    let r = rig();
    r.dev.script_write(vec![FakeWriteStep::Unknown {
        confirmed: 2,
        error: PalError::io(5, "[Errno 5] Input/output error"),
    }]);
    let (j, rx) = job(1, TxClass::Command, 0, 0, false, b"secret");
    r.h.jobs.send(j).unwrap();
    assert_eq!(
        rx.recv_timeout(Duration::from_secs(2)).unwrap().result.outcome,
        TxOutcome::Unknown { confirmed: 2 }
    );
    assert!(tx_lens(&r).is_empty());
    assert!(meta_lens(&r).is_empty());
}

#[test]
fn unknown_meta_wal_failure_is_recorded_as_evidence_gap() {
    let mut r = rig();
    r.wfs.fail_next_writes(Path::new(WAL_PATH), 2, enospc());
    r.dev.script_write(vec![FakeWriteStep::Unknown {
        confirmed: 0,
        error: PalError::io(5, "[Errno 5] Input/output error"),
    }]);
    let rep = send(&r, TxClass::Command, 1, 7, b"abc");
    assert_eq!(rep.result.outcome, TxOutcome::Unknown { confirmed: 0 });
    r.stop.as_ref().unwrap().send();
    let (_w, ledger) = r.join.take().unwrap().join().unwrap();
    let op = &ledger.ops[&OpId(1)];
    assert!(!op.wal_ok);
    assert_eq!(op.wal_gaps.len(), 1);
    assert_eq!((op.wal_gaps[0].dir, op.wal_gaps[0].len), ("META", 0));
}

#[test]
fn flash_probe_does_not_reuse_op_id_as_probe_identity() {
    let fence = Fence::new();
    fence.with(|state| {
        state.active_epoch = Some(7);
        state.flash_mode = FlashMode::Probe(1);
    });
    let permit = TxPermit {
        op_id: OpId(1),
        epoch: 7,
        owner_gen: 0,
        fsm_gen: 0,
        class: TxClass::FlashProbe,
        source: "system:flash".into(),
        cmd_id: None,
        log: true,
    };
    assert_eq!(fence.with(|state| Fence::admit(state, &permit)), Err(FenceReject::Flash));
}

#[test]
fn log_false_secret_not_in_wal() {
    let r = rig();
    let (j, rx) = job(1, TxClass::LoginProbe, 0, 0, false, b"secret\n");
    r.h.jobs.send(j).unwrap();
    let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(rep.result.outcome, TxOutcome::Completed(7)); // OS 收到 bytes
    assert_eq!(r.dev.take_tx(), b"secret\n".to_vec());
    assert!(rep.wal.is_none());
    assert!(tx_lens(&r).is_empty());
}

#[test]
fn wal_order_equals_write_order() {
    let mut r = rig();
    for i in 0..50u8 {
        assert_eq!(send(&r, TxClass::Command, 1 + u64::from(i), 7, &[i]).result.outcome, TxOutcome::Completed(1));
    }
    let want_bytes: Vec<u8> = (0..50u8).collect();
    assert_eq!(r.dev.take_tx(), want_bytes);
    let lines = tx_lens(&r);
    let got: Vec<String> = lines.iter().map(|l| payload_b64(l)).collect();
    let want: Vec<String> = (0..50u8).map(|i| b64(&[i])).collect();
    assert_eq!(got, want);
    r.stop.as_ref().unwrap().send();
    let _ = r.join.take().unwrap().join().unwrap();
}

#[test]
fn fence_sink_sets_wal_unhealthy() {
    let fence = Fence::new();
    WalHealthSink::set_wal_unhealthy(&*fence, true);
    assert!(fence.with(|s| s.wal_unhealthy));
}

#[test]
fn job_queue_bounded_64() {
    // writer 卡在 BlockForever 的在途 job（已由 writer 取出，不佔佇列）；
    // 之後佇列容量 64 必須精確接受 64 個排隊 job，第 65 個排隊 job 回 Full。
    let r = rig();
    r.dev.script_write(vec![FakeWriteStep::BlockForever]);
    let (j0, _rx0) = job(1, TxClass::Command, 0, 0, true, b"a");
    r.h.jobs.try_send(j0).unwrap();
    let t0 = Instant::now();
    while r.dev.write_calls() == 0 {
        assert!(t0.elapsed() < Duration::from_secs(2), "writer 未取出在途 job");
        std::thread::sleep(Duration::from_millis(1));
    }
    for op in 2..=65u64 {
        let (j, _rx) = job(op, TxClass::Command, 0, 0, true, b"b");
        r.h.jobs.try_send(j).unwrap();
    }
    let (j, _rx) = job(66, TxClass::Command, 0, 0, true, b"c");
    assert!(matches!(r.h.jobs.try_send(j), Err(crossbeam_channel::TrySendError::Full(_))));
    r.dev.release_block(); // 收尾：解除 D-state 模擬
    r.cancel.cancel();
}

#[test]
fn writer_returned_on_shutdown() {
    let mut r = rig();
    r.stop.as_ref().unwrap().send();
    let (_w, ledger) = r.join.take().unwrap().join().unwrap();
    assert!(ledger.ops.is_empty());
}

#[test]
fn stop_ends_writer_while_other_senders_alive() {
    let mut r = rig();
    let _keep = r.h.clone(); // 模擬 OpCtx 另持有的 Handle（不 drop）
    r.stop.as_ref().unwrap().send();
    let join = r.join.take().unwrap();
    let (done_tx, done_rx) = crossbeam_channel::bounded(1);
    std::thread::spawn(move || {
        let _ = done_tx.send(join.join());
    });
    assert!(
        done_rx.recv_timeout(Duration::from_secs(2)).is_ok(),
        "stop 後 writer 必須在期限內結束（不依賴 Sender 全數 drop）"
    );
}

#[test]
fn send_after_writer_join_is_disconnected() {
    let mut r = rig();
    r.stop.as_ref().unwrap().send();
    let _ = r.join.take().unwrap().join().unwrap();
    let (late_job, _reply) = job(1, TxClass::Command, 0, 0, true, b"late");
    assert!(r.h.jobs.send(late_job).is_err());
}

#[test]
fn queued_jobs_after_stop_are_not_submitted_in_ledger() {
    let mut r = rig();
    r.dev.script_write(vec![FakeWriteStep::BlockUntilCancel { accept_before_cancel: 2 }]);
    let rx1 = send_async(&r, TxClass::Command, 1, 0, b"abcdef");
    wait_in_flight(&r, 1);
    let rx2 = send_async(&r, TxClass::Command, 2, 0, b"gh");
    let rx3 = send_async(&r, TxClass::Command, 3, 0, b"ij");
    r.stop.as_ref().unwrap().send();
    r.cancel.cancel();
    let rep1 = rx1.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(rep1.result.outcome, TxOutcome::Completed(2));
    for rx in [rx2, rx3] {
        let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::Frozen)));
    }
    assert_eq!(r.dev.write_calls(), 1);
    let (_w, ledger) = r.join.take().unwrap().join().unwrap();
    assert!(ledger.ops.contains_key(&OpId(1)));
    assert!(ledger.ops.contains_key(&OpId(2)));
    assert!(ledger.ops.contains_key(&OpId(3)));
}

#[test]
fn ledger_records_partial_and_unknown() {
    let mut r = rig();
    r.dev.script_write(vec![
        FakeWriteStep::AcceptThenError(3, PalError::io(5, "[Errno 5] Input/output error")),
        FakeWriteStep::Unknown { confirmed: 0, error: PalError::io(5, "[Errno 5] Input/output error") },
    ]);
    assert_eq!(send(&r, TxClass::Command, 1, 7, b"abcdef").result.outcome, TxOutcome::Completed(3));
    assert_eq!(send(&r, TxClass::Command, 2, 7, b"ghi").result.outcome, TxOutcome::Unknown { confirmed: 0 });
    r.stop.as_ref().unwrap().send();
    let (_w, ledger) = r.join.take().unwrap().join().unwrap();
    assert_eq!(ledger.ops[&OpId(1)].confirmed_bytes, 3);
    assert!(ledger.ops[&OpId(2)].unknown);
}

#[test]
fn reply_receiver_gone_is_kept_in_ledger() {
    let mut r = rig();
    let (j, rx) = job(1, TxClass::Command, 0, 0, true, b"abc");
    drop(rx); // reply receiver 先 drop
    r.h.jobs.send(j).unwrap();
    wait_gauge(&r, |g| g.live_ops == 1); // 等 writer 處理完成（stop 優先於 jobs，需先確定已提交）
    r.stop.as_ref().unwrap().send();
    let (_w, ledger) = r.join.take().unwrap().join().unwrap();
    assert!(ledger.ops[&OpId(1)].reply_lost);
    assert_eq!(ledger.ops[&OpId(1)].confirmed_bytes, 3);
    assert_eq!(r.dev.take_tx(), b"abc".to_vec());
}

#[test]
fn tx_wal_failure_reported_and_gap_listed() {
    let mut r = rig();
    r.wfs.fail_next_writes(Path::new(WAL_PATH), 2, enospc());
    let rep = send(&r, TxClass::Command, 1, 7, b"abc");
    assert_eq!(rep.result.outcome, TxOutcome::Completed(3)); // bytes 已寫出
    assert!(matches!(rep.wal, Some(WalAppend { ok: false, loss_flag: true, .. })));
    r.stop.as_ref().unwrap().send();
    let (_w, ledger) = r.join.take().unwrap().join().unwrap();
    assert_eq!(ledger.ops[&OpId(1)].wal_gaps.len(), 1);
}

#[test]
fn settled_op_wal_gaps_move_to_unresolved_set() {
    let mut r = rig();
    r.wfs.fail_next_writes(Path::new(WAL_PATH), 2, enospc());
    assert_eq!(send(&r, TxClass::Command, 9, 7, b"abc").result.outcome, TxOutcome::Completed(3));
    assert!(r.h.settle(OpId(9)));
    wait_gauge(&r, |g| g.live_ops == 0 && g.unresolved_gaps == 1);
    r.stop.as_ref().unwrap().send();
    let (_w, ledger) = r.join.take().unwrap().join().unwrap();
    assert!(!ledger.ops.contains_key(&OpId(9)));
    assert_eq!(ledger.unresolved_gaps.len(), 1);
    assert_eq!(ledger.unresolved_gaps[0].op_id, Some(OpId(9)));
    assert_eq!(ledger.unresolved_gaps[0].len, 3); // 證據隨結算移轉，不丟棄
}

#[test]
fn unresolved_gaps_capped_with_overflow_summary() {
    let mut r = rig();
    r.wfs.fail_next_writes(Path::new(WAL_PATH), 10_000, enospc()); // WAL 持續失敗
    for i in 1..=300u64 {
        assert_eq!(send(&r, TxClass::Command, i, 7, b"ab").result.outcome, TxOutcome::Completed(2));
        assert!(r.h.settle(OpId(i)));
    }
    let g = wait_gauge(&r, |g| g.settled_total == 300);
    assert_eq!(g.gaps_overflow, 44);
    assert_eq!(g.unresolved_gaps, 256);
    r.stop.as_ref().unwrap().send();
    let (_w, ledger) = r.join.take().unwrap().join().unwrap();
    assert_eq!(ledger.unresolved_gaps.len(), TX_LEDGER_UNRESOLVED_GAPS_MAX);
    assert_eq!(TX_LEDGER_UNRESOLVED_GAPS_MAX, 256);
    assert_eq!(ledger.unresolved_gaps.front().unwrap().op_id, Some(OpId(45))); // 保留最新 256 筆
    assert_eq!(
        ledger.gaps_overflow,
        GapOverflow { gaps: 44, bytes: 88, first_op: Some(OpId(1)), last_op: Some(OpId(44)) }
    );
}

#[test]
fn per_op_wal_gaps_capped() {
    let mut r = rig();
    r.wfs.fail_next_writes(Path::new(WAL_PATH), 10_000, enospc()); // WAL 持續失敗
    for _ in 0..40 {
        assert_eq!(send(&r, TxClass::Command, 1, 7, b"x").result.outcome, TxOutcome::Completed(1));
    }
    r.stop.as_ref().unwrap().send();
    let (_w, ledger) = r.join.take().unwrap().join().unwrap();
    assert_eq!(TX_OP_WAL_GAPS_MAX, 16);
    let op = &ledger.ops[&OpId(1)];
    assert_eq!(op.wal_gaps.len(), TX_OP_WAL_GAPS_MAX);
    assert_eq!(op.wal_gaps_overflow.gaps, 24);
    assert_eq!(op.wal_gaps_overflow.bytes, 24);
    assert_eq!(op.confirmed_bytes, 40); // 計數不受缺口合併影響
}

#[test]
fn ledger_saturation_never_evicts_live_ops() {
    let mut r = rig();
    for op in 1..=256u64 {
        assert_eq!(send(&r, TxClass::Command, op, 7, b"q").result.outcome, TxOutcome::Completed(1)); // 不 settle
    }
    assert_eq!(TX_LEDGER_LIVE_MAX, 256);
    // 第 257 個新 op：帳本已滿 → 不呼叫 PAL、不建項目
    let writes_before = r.dev.write_calls();
    let rep = send(&r, TxClass::Command, 257, 7, b"q");
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::Frozen)));
    assert_eq!(r.dev.write_calls(), writes_before);
    // 第 258 個新 op：不再重送 saturation 事件
    let rep = send(&r, TxClass::Command, 258, 7, b"q");
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::Frozen)));
    // 既有 op 1 仍照常提交（既有 op 不受限）
    assert_eq!(send(&r, TxClass::Command, 1, 7, b"q").result.outcome, TxOutcome::Completed(1));
    let t0 = Instant::now();
    while count_saturated(&r) == 0 {
        assert!(t0.elapsed() < Duration::from_secs(2), "未收到 TX_LEDGER_SATURATED");
        std::thread::sleep(Duration::from_millis(2));
    }
    r.stop.as_ref().unwrap().send();
    let (_w, ledger) = r.join.take().unwrap().join().unwrap();
    std::thread::sleep(Duration::from_millis(20)); // 讓 drainer 消化尾端事件
    assert_eq!(count_saturated(&r), 1, "UartIoError{{TX_LEDGER_SATURATED}} 只能送一次");
    assert_eq!(ledger.ops.len(), 256);
    assert!(ledger.saturated);
    assert!(!ledger.ops.contains_key(&OpId(257)));
    assert!(!ledger.ops.contains_key(&OpId(258)));
    for op in 1..=256u64 {
        let want = if op == 1 { 2 } else { 1 };
        assert_eq!(ledger.ops[&OpId(op)].confirmed_bytes, want, "op {op} 證據必須保留");
    }
}

#[test]
fn settle_unknown_or_already_settled_is_noop() {
    let mut r = rig();
    assert_eq!(send(&r, TxClass::Command, 1, 7, b"a").result.outcome, TxOutcome::Completed(1));
    assert!(r.h.settle(OpId(1)));
    wait_gauge(&r, |g| g.settled_total == 1 && g.live_ops == 0);
    assert!(r.h.settle(OpId(77))); // 無 TX
    assert!(r.h.settle(OpId(1))); // 已結算
    // barrier：settle 優先於 jobs，後續 job 完成時前面的 settle 必已套用
    assert_eq!(send(&r, TxClass::Command, 2, 7, b"b").result.outcome, TxOutcome::Completed(1));
    let g = wait_gauge(&r, |g| g.live_ops == 1);
    assert_eq!((g.live_ops, g.unresolved_gaps, g.settled_total), (1, 0, 1));
    r.stop.as_ref().unwrap().send();
    let _ = r.join.take().unwrap().join().unwrap();
}

#[test]
fn settle_queue_full_retains_entry() {
    let mut r = rig();
    assert_eq!(send(&r, TxClass::Command, 1, 7, b"o").result.outcome, TxOutcome::Completed(1)); // op 1 先完成（不 settle）
    r.dev.script_write(vec![FakeWriteStep::BlockUntilCancel { accept_before_cancel: 1 }]);
    let rx99 = send_async(&r, TxClass::Command, 99, 0, b"ab");
    wait_in_flight(&r, 99); // writer 卡在 op 99 的完成等待，佇列不被消費
    for i in 0..256u64 {
        assert!(r.h.settle(OpId(1000 + i)), "settle 佇列未滿前必須接受 (i={i})");
    }
    assert!(!r.h.settle(OpId(1))); // 佇列已滿 → 丟棄（過度保留、不失證據）
    assert_eq!(r.h.ledger_gauge().settle_dropped, 1);
    r.stop.as_ref().unwrap().send();
    r.cancel.cancel();
    assert_eq!(rx99.recv_timeout(Duration::from_secs(2)).unwrap().result.outcome, TxOutcome::Completed(1));
    let (_w, ledger) = r.join.take().unwrap().join().unwrap();
    assert_eq!(ledger.ops[&OpId(1)].confirmed_bytes, 1); // 仍保留
    assert_eq!(ledger.ops[&OpId(99)].confirmed_bytes, 1);
}

#[test]
fn lease_owner_job_after_owner_gen_bump_rejected() {
    let r = rig();
    r.fence.with(|s| s.owner_gen = 3);
    r.fence.with(|s| s.owner_gen = 4); // lease close／expiry（送出前）
    let before = r.dev.write_calls();
    let rep = send_async(&r, TxClass::LeaseOwner, 1, 3, b"\x03").recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::OwnerMismatch)));
    assert_eq!(r.dev.write_calls(), before);
}

#[test]
fn interactive_open_cmd_after_owner_gen_bump_rejected() {
    let r = rig();
    r.fence.with(|s| s.owner_gen = 3);
    r.fence.with(|s| s.owner_gen = 4); // lease close／expiry（送出前）
    let before = r.dev.write_calls();
    let rep =
        send_async(&r, TxClass::InteractiveOpenCmd, 1, 3, b"open\n").recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::OwnerMismatch)));
    assert_eq!(r.dev.write_calls(), before);
}

#[test]
fn login_probe_after_fsm_gen_bump_rejected() {
    let r = rig();
    r.fence.with(|s| s.fsm_gen = 2); // quiet re-arm 之後 fsm 世代已前進
    let (j, rx) = job(1, TxClass::LoginProbe, 0, 1, true, b"root\n"); // permit fsm_gen=1
    r.h.jobs.send(j).unwrap();
    let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::FsmMismatch)));
}
