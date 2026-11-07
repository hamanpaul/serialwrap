//! P8 Task 5：`OpTransferPort`——echo-ACK 節流、TX 證據聚合與中止（計畫 09 Task 5；brief「計畫與實際不符」覆蓋）。
//!
//! 外部行為以 `rig.dut.writes()`（外部 peer 實際收到的寫入）斷言；WAL 以 `FakeWalFs` 讀回。
//! 中止一律回 `TransferAbort::TxRejected`（OD-62），reason 依 fence 現況分類（brief 不符 #2）。

mod support;

use std::time::Duration;

use support::fake_shell::{FixedIds, MemFiles};
use support::transfer_port::{FakeDut, PortRig, EPOCH, OP};
use support::{CoreHarness, SHELL_PROFILE};
use sw_core::file_transfer::templates as t;
use sw_core::file_transfer::{
    push_file, EchoPaced, PushParams, RejectReason, TransferAbort, TransferPort, TxRejection,
};
use sw_core::mailbox::ControlEvent;
use sw_core::session_core::SessionRequest;
use sw_core::tx_writer::{
    AggregateKey, AggregateOp, FlashMode, TxClass, TxJob, TxPermit, TX_AGGREGATE_OPEN_MAX,
};
use sw_core::uart_bridge::RunCancel;
use sw_pal::traits::{PalError, TxOutcome};
use sw_proto::pyjson::PyValue;
use sw_proto::pyre::{compile, PyFlags};

fn rejected(reason: RejectReason, rejected_confirmed: usize, unknown: bool) -> TransferAbort {
    TransferAbort::rejected(TxRejection { reason, rejected_confirmed, unknown })
}

fn re(pattern: &str) -> sw_proto::pyre::PyRegex {
    compile(pattern, PyFlags(0)).expect("測試 pattern")
}

fn paced(
    rig: &PortRig,
    cmd: &str,
    cmd_id: &'static str,
    slice: usize,
    echo_timeout: Duration,
) -> support::transfer_port::PortTask<Result<EchoPaced, TransferAbort>> {
    let cmd = cmd.to_string();
    rig.spawn(move |p| p.send_command_echo_paced(&cmd, "agent", Some(cmd_id), slice, echo_timeout))
}

fn send(rig: &PortRig, cmd: &'static str, cmd_id: Option<&'static str>) -> Result<(), TransferAbort> {
    rig.spawn(move |p| p.send_command(cmd, "agent", cmd_id)).join()
}

fn permit(class: TxClass, source: &str, cmd_id: Option<&str>, log: bool) -> TxPermit {
    TxPermit {
        op_id: OP,
        epoch: EPOCH,
        owner_gen: 0,
        fsm_gen: 0,
        class,
        source: source.to_string(),
        cmd_id: cmd_id.map(str::to_string),
        log,
        input_epoch: None,
        replay_hold: None,
        probe_id: None,
    }
}

// ── brief 指定先寫的 6 支 ──

#[test]
fn echo_paced_slices_confirmed_in_order() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    let cmd = "x".repeat(150);
    let r = paced(&rig, &cmd, "ft-1-0", 64, Duration::from_secs(5)).join();
    assert_eq!(r, Ok(EchoPaced { ok: true, acked_chars: 150, sent_chars: 150 }));
    assert_eq!(
        rig.dut.writes(),
        vec![b"x".repeat(64), b"x".repeat(64), b"x".repeat(22), b"\n".to_vec()]
    );
    // 每段送出當下，前面各段的 echo 已回到 RX（echo 即流控）。
    let rx_at = rig.dut.rx_at_submit();
    assert_eq!(rx_at.len(), 4);
    assert!(rx_at[1] >= 64 && rx_at[2] >= 128 && rx_at[3] >= 150, "未等 echo 就送下一段：{rx_at:?}");
    let tx = rig.tx_records();
    assert_eq!(tx.len(), 1, "一命令一筆 TX");
    assert_eq!((tx[0].source.as_str(), tx[0].cmd_id.as_deref()), ("agent", Some("ft-1-0")));
    assert_eq!(tx[0].payload, format!("{cmd}\n").into_bytes());
}

#[test]
fn echo_stall_sends_one_cancel_line_only() {
    let rig = PortRig::new("COM0", FakeDut::shell().stall_lines_starting_with("printf '%s' '", 64));
    let data = b"hello world, echo paced".to_vec();
    let tmp = "/tmp/.sw_upload_aaaaaaaaaaaa";
    let chunk = t::chunk_command(&t::b64encode(&data), "base64", ">", tmp).unwrap();
    assert!(chunk.chars().count() > 64, "chunk 命令須超過一段");
    let task = rig.spawn(move |port| {
        let files = MemFiles::with("/l/f.bin", &data);
        let mut ids = FixedIds::new(&["aaaaaaaaaaaa", "deadbeef"]);
        let p = PushParams {
            local_path: "/l/f.bin".into(),
            remote_path: "/tmp/f.bin".into(),
            chunk_size: 512,
            timeout: Duration::from_secs(3600),
            prompt_regex: "# $".into(),
            source: "agent".into(),
            ack_mode: "auto".into(),
            echo_slice_size: 64,
            echo_timeout: Duration::from_secs(2),
            max_console_line_chars: None,
        };
        push_file(port, &files, &mut ids, &p)
    });
    // chunk 命令全部 slice 已送出、echo 停在第 64 字元；推進時鐘讓 echo 等待逾時，看到取消半行即停。
    rig.dut.wait_contains(chunk.as_bytes());
    rig.pump_clock_until(Duration::from_secs(1), || rig.dut.bytes().contains(&0x15));
    let d = task.join().expect("echo 停滯是一般失敗結果");
    assert_eq!(d.get("error_code"), Some(&PyValue::from("TRANSFER_ECHO_STALL")));
    assert_eq!(d.get("acked_chars"), Some(&PyValue::from(64i64)));
    assert_eq!(d.get("sent_chars"), Some(&PyValue::from(chunk.chars().count() as i64)));

    let writes = rig.dut.writes();
    assert_eq!(writes.iter().filter(|w| w.as_slice() == b"\x15\n").count(), 1, "只送一次取消半行：{writes:?}");
    assert!(!writes.iter().any(|w| w.as_slice() == b"\n"), "echo 停滯時不得送出命令換行");
    let tx = rig.tx_records();
    let chunk_tx: Vec<_> = tx.iter().filter(|r| r.cmd_id.as_deref() == Some("ft-deadbeef-0")).collect();
    assert_eq!(chunk_tx.len(), 1, "停滯的 chunk 一筆 TX");
    assert_eq!(chunk_tx[0].payload, chunk.as_bytes(), "只記實際送出的 slice（無換行）");
    let cancel_tx: Vec<_> = tx.iter().filter(|r| r.payload == b"\x15\n").collect();
    assert_eq!(cancel_tx.len(), 1);
    assert_eq!(cancel_tx[0].cmd_id, None);
}

#[test]
fn aggregate_flushed_before_drain_receipt() {
    // port 卡在 echo 等待、來不及送 Finish 時 TxWriter 收尾：聚合緩衝在回條之前落 WAL，且之後不重複。
    let mut rig = PortRig::new("COM0", FakeDut::echo_first_n_chars(128));
    let task = paced(&rig, &"w".repeat(300), "ft-4-0", 64, Duration::from_secs(30));
    rig.dut.wait_bytes(192);
    let at = rig.finish_worker();
    let tx: Vec<_> = at.records_at_receipt.iter().filter(|r| r.dir == "TX").collect();
    assert_eq!(tx.len(), 1, "回條送出前必須已有已確認 slice 的 TX");
    assert_eq!(tx[0].payload, b"w".repeat(192));
    assert_eq!(tx[0].cmd_id.as_deref(), Some("ft-4-0"));
    assert!(tx[0].seq <= at.wal_seq_at_receipt, "TX 紀錄必須早於回條（§5.2.4）");
    assert!(at.receipt.unrecorded.is_empty(), "{:?}", at.receipt.unrecorded);
    assert_eq!(at.receipt.ledger.ops.get(&OP).map(|o| o.confirmed_bytes), Some(192));

    rig.phase1();
    assert_eq!(task.join(), Err(rejected(RejectReason::Draining, 0, false)));
    assert_eq!(rig.tx_records().len(), 1, "writer 停止後的 Finish 不得重複落 WAL");
    assert_eq!(rig.dut.bytes().len(), 192, "不得重送");
}

#[test]
fn wait_cancelled_by_drain_returns_promptly() {
    let rig = PortRig::new("COM0", FakeDut::silent());
    let t0 = rig.clock.elapsed();
    let task = rig.spawn(|p| p.wait_for_regex_from(&re("never-matches"), 0, Duration::from_secs(600)));
    std::thread::sleep(Duration::from_millis(30));
    assert!(!task.is_finished(), "drain 前不得返回");
    rig.phase1();
    let r = task.join_within(Duration::from_secs(2));
    assert_eq!(r, Err(rejected(RejectReason::Draining, 0, false)));
    assert!(rig.clock.elapsed() - t0 < Duration::from_secs(1), "不得等滿 timeout（FakeClock 未推進）");
    assert!(rig.dut.writes().is_empty(), "取消判定零 TX");
}

#[test]
fn quiet_arm_midway_maps_to_quiet_reason_zero_confirmed() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    send(&rig, "echo one", Some("ft-q-0")).unwrap();
    let task = rig.spawn(|p| p.wait_for_regex_from(&re("never-matches"), 0, Duration::from_secs(600)));
    std::thread::sleep(Duration::from_millis(30));
    assert!(!task.is_finished());
    // 會擋 probe 的 quiet arm：fence 先 arm，再 cancel_login_runs（session_core `arm_quiet`／SC-30 順序）。
    rig.fence.with(|f| {
        f.quiet_active = true;
        f.reconfirm_pending = true;
    });
    rig.rx.cancel_login_runs();
    assert_eq!(task.join_within(Duration::from_secs(2)), Err(rejected(RejectReason::Quiet, 0, false)));
    let before = rig.dut.writes();
    assert_eq!(send(&rig, "echo two", Some("ft-q-1")), Err(rejected(RejectReason::Quiet, 0, false)));
    assert_eq!(rig.dut.writes(), before, "被拒那筆零寫出");
    assert_eq!(rig.tx_records().len(), 1);
}

#[test]
fn timeout_zero_returns_false_even_if_matched() {
    let rig = PortRig::new("COM0", FakeDut::silent());
    rig.push_rx(b"root@t:~# ");
    let r = rig.spawn(|p| p.wait_for_regex_from(&re("# "), 0, Duration::ZERO)).join();
    assert_eq!(r, Ok(false), "EVT-D-F1：timeout 為零不做任何檢查");
    let r = rig.spawn(|p| p.wait_for_regex_from(&re("# "), 0, Duration::from_secs(1))).join();
    assert_eq!(r, Ok(true));
}

// ── 計畫 Task 5 表格其餘案例 ──

#[test]
fn paced_stall_records_only_sent_slices_and_never_sends_newline() {
    let rig = PortRig::new("COM0", FakeDut::echo_first_n_chars(64));
    let task = paced(&rig, &"y".repeat(150), "ft-2-0", 64, Duration::from_millis(50));
    rig.dut.wait_bytes(128);
    rig.pump_clock_until(Duration::from_millis(50), || task.is_finished());
    assert_eq!(task.join(), Ok(EchoPaced { ok: false, acked_chars: 64, sent_chars: 128 }));
    assert!(!rig.dut.writes().iter().any(|w| w.as_slice() == b"\n"));
    let tx = rig.tx_records();
    assert_eq!(tx.len(), 1);
    assert_eq!(tx[0].payload, b"y".repeat(128));
}

#[test]
fn paced_abort_after_partial_write_records_confirmed_bytes_then_errors() {
    let rig = PortRig::new("COM0", FakeDut::echo_all().partial_write_on(2, 10));
    let r = paced(&rig, &"z".repeat(150), "ft-3-0", 64, Duration::from_secs(5)).join();
    assert_eq!(r, Err(rejected(RejectReason::Other, 10, false)));
    let tx = rig.tx_records();
    assert_eq!(tx.len(), 1);
    assert_eq!(tx[0].payload.len(), 64 + 10, "只記 OS 確認的 bytes（SC-07）");
    assert_eq!(rig.dut.writes().len(), 2, "不得重送失敗的 slice");
}

#[test]
fn paced_unknown_slice_writes_meta_then_single_tx() {
    let rig = PortRig::new("COM0", FakeDut::echo_all().unknown_on(2, 5));
    let r = paced(&rig, &"u".repeat(150), "ft-5-0", 64, Duration::from_secs(5)).join();
    assert_eq!(r, Err(rejected(RejectReason::Other, 5, true)));
    let recs = rig.records();
    let meta: Vec<_> = recs.iter().filter(|r| r.dir == "META").collect();
    let tx: Vec<_> = recs.iter().filter(|r| r.dir == "TX").collect();
    assert_eq!(meta.len(), 1);
    let m = meta[0].meta.as_ref().unwrap();
    assert_eq!(m.get("event"), Some(&PyValue::from("tx_outcome_unknown")));
    assert_eq!(m.get("requested_bytes"), Some(&PyValue::from(64i64)));
    assert_eq!(m.get("confirmed_bytes"), Some(&PyValue::from(5i64)));
    assert_eq!(meta[0].cmd_id.as_deref(), Some("ft-5-0"));
    assert_eq!(tx.len(), 1);
    assert_eq!(tx[0].payload.len(), 64 + 5);
    assert!(meta[0].seq < tx[0].seq, "Python：先 META，finally 再記 TX");
}

#[test]
fn paced_strips_trailing_crlf_from_body() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    let r = paced(&rig, "abc\r\n", "ft-6-0", 64, Duration::from_secs(5)).join();
    assert_eq!(r, Ok(EchoPaced { ok: true, acked_chars: 3, sent_chars: 3 }));
    assert_eq!(rig.dut.writes(), vec![b"abc".to_vec(), b"\n".to_vec()]);
    assert_eq!(rig.tx_records()[0].payload, b"abc\n");
}

#[test]
fn paced_empty_body_sends_only_newline() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    let r = paced(&rig, "\r\n", "ft-6-1", 64, Duration::from_secs(5)).join();
    assert_eq!(r, Ok(EchoPaced { ok: true, acked_chars: 0, sent_chars: 0 }));
    assert_eq!(rig.dut.writes(), vec![b"\n".to_vec()]);
    assert_eq!(rig.tx_records()[0].payload, b"\n");
}

#[test]
fn paced_tolerates_noise_between_slices() {
    let rig = PortRig::new("COM0", FakeDut::echo_all().with_noise("[  1.0] kmsg\r\n"));
    let r = paced(&rig, &"n".repeat(150), "ft-7-0", 64, Duration::from_secs(5)).join();
    assert_eq!(r, Ok(EchoPaced { ok: true, acked_chars: 150, sent_chars: 150 }));
}

#[test]
fn paced_ignores_ansi_in_echo() {
    let rig = PortRig::new("COM0", FakeDut::echo_all().with_ansi());
    let r = paced(&rig, &"a".repeat(150), "ft-8-0", 64, Duration::from_secs(5)).join();
    assert_eq!(r, Ok(EchoPaced { ok: true, acked_chars: 150, sent_chars: 150 }));
}

#[test]
fn paced_counts_chars_not_bytes() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    let r = paced(&rig, &"中".repeat(70), "ft-9-0", 64, Duration::from_secs(5)).join();
    assert_eq!(r, Ok(EchoPaced { ok: true, acked_chars: 70, sent_chars: 70 }));
    let w = rig.dut.writes();
    assert_eq!(w.iter().map(Vec::len).collect::<Vec<_>>(), vec![192, 18, 1]);
    assert_eq!(rig.tx_records()[0].payload, format!("{}\n", "中".repeat(70)).into_bytes());
}

#[test]
fn paced_slice_size_zero_treated_as_one() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    let r = paced(&rig, "abc", "ft-10-0", 0, Duration::from_secs(5)).join();
    assert_eq!(r, Ok(EchoPaced { ok: true, acked_chars: 3, sent_chars: 3 }));
    assert_eq!(rig.dut.writes(), vec![b"a".to_vec(), b"b".to_vec(), b"c".to_vec(), b"\n".to_vec()]);
}

#[test]
fn send_command_appends_lf_once() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    send(&rig, "ls", Some("ft-11-0")).unwrap();
    send(&rig, "ls\n", Some("ft-11-1")).unwrap();
    assert_eq!(rig.dut.writes(), vec![b"ls\n".to_vec(), b"ls\n".to_vec()]);
    let tx = rig.tx_records();
    assert_eq!(tx.len(), 2);
    assert_eq!(tx[1].cmd_id.as_deref(), Some("ft-11-1"));
    assert!(tx.iter().all(|r| r.payload == b"ls\n"));
}

#[test]
fn send_command_not_submitted_after_fence_freeze() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    rig.fence.with(|f| f.frozen = true);
    assert_eq!(send(&rig, "ls", Some("ft-12-0")), Err(rejected(RejectReason::Draining, 0, false)));
    assert!(rig.dut.writes().is_empty());
    assert!(rig.tx_records().is_empty());
}

#[test]
fn stale_epoch_maps_to_stale_generation() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    rig.fence.with(|f| f.active_epoch = Some(EPOCH + 1));
    assert_eq!(send(&rig, "ls", None), Err(rejected(RejectReason::StaleGeneration, 0, false)));
    rig.fence.with(|f| {
        f.active_epoch = Some(EPOCH);
        f.cancelled.insert(OP);
    });
    assert_eq!(send(&rig, "ls", None), Err(rejected(RejectReason::Cancelled, 0, false)));
    assert!(rig.dut.writes().is_empty());
}

#[test]
fn quiet_armed_between_chunks_refused() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    send(&rig, "echo 0", Some("ft-13-0")).unwrap();
    rig.fence.with(|f| f.quiet_active = true);
    assert_eq!(send(&rig, "echo 1", Some("ft-13-1")), Err(rejected(RejectReason::Quiet, 0, false)));
    let r = paced(&rig, "echo 2", "ft-13-2", 64, Duration::from_secs(5)).join();
    assert_eq!(r, Err(rejected(RejectReason::Quiet, 0, false)));
    assert_eq!(rig.dut.writes(), vec![b"echo 0\n".to_vec()]);
    assert_eq!(rig.tx_records().len(), 1, "被拒的聚合零寫出、不落 WAL");
}

#[test]
fn reconfirm_pending_blocks_cleanup() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    rig.fence.with(|f| f.reconfirm_pending = true);
    let r = rig.spawn(|p| p.cancel_input_line("agent")).join();
    assert_eq!(r, Err(rejected(RejectReason::Quiet, 0, false)));
    assert!(rig.dut.writes().is_empty());
}

#[test]
fn cancel_input_line_bytes_and_wal() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    rig.spawn(|p| p.cancel_input_line("agent")).join().unwrap();
    assert_eq!(rig.dut.writes(), vec![b"\x15\n".to_vec()]);
    let tx = rig.tx_records();
    assert_eq!(tx.len(), 1);
    assert_eq!((tx[0].source.as_str(), tx[0].cmd_id.clone()), ("agent", None));
    assert_eq!(tx[0].payload, b"\x15\n");
}

#[test]
fn rx_snapshot_and_text_delegate_to_rx_window() {
    let rig = PortRig::new("COM0", FakeDut::silent());
    rig.push_rx("前置-# ".as_bytes());
    let port = rig.port();
    assert!(port.supports_echo_paced());
    assert_eq!(port.rx_snapshot_len(), 6, "字元計");
    assert_eq!(port.rx_text_from(3), "-# ");
}

#[test]
fn wait_regex_full_window_when_trimmed() {
    let rig = PortRig::new("COM0", FakeDut::silent());
    let window = sw_core::resources::RX_WINDOW_MAX_CHARS;
    let mut rx = "x".repeat(1000);
    rx.push_str("HEAD-MARK");
    rx.push_str(&"y".repeat(window - "HEAD-MARK".len()));
    rig.push_rx(rx.as_bytes());
    assert_eq!(rig.rx.trimmed(), 1000);
    let r = rig.spawn(|p| p.wait_for_regex_from(&re("^HEAD-MARK"), 10, Duration::from_secs(1))).join();
    assert_eq!(r, Ok(true), "舊 offset 已被修剪時對整窗 search");
    assert!(rig.port().rx_text_from(10).starts_with("HEAD-MARK"));
}

#[test]
fn wait_regex_timeout_uses_clock() {
    let rig = PortRig::new("COM0", FakeDut::silent());
    let t0 = rig.clock.elapsed();
    let real = std::time::Instant::now();
    let task = rig.spawn(|p| p.wait_for_regex_from(&re("never-matches"), 0, Duration::from_secs(10)));
    rig.pump_clock_until(Duration::from_secs(1), || task.is_finished());
    assert_eq!(task.join(), Ok(false));
    assert!(rig.clock.elapsed() - t0 >= Duration::from_secs(10));
    assert!(real.elapsed() < Duration::from_secs(5), "期限依注入 Clock，不用實時");
}

#[test]
fn flash_mode_blocks_file_transfer_class() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    rig.fence.with(|f| f.flash_mode = FlashMode::Flashing);
    assert_eq!(send(&rig, "ls", Some("ft-14-0")), Err(rejected(RejectReason::Other, 0, false)));
    let r = paced(&rig, "ls", "ft-14-1", 64, Duration::from_secs(5)).join();
    assert_eq!(r, Err(rejected(RejectReason::Other, 0, false)));
    assert!(rig.dut.writes().is_empty(), "FLASHING 期間零 TX");
    assert!(rig.tx_records().is_empty());
}

#[test]
fn drain_during_echo_paced_records_confirmed_slices_before_receipt() {
    let mut rig = PortRig::new("COM0", FakeDut::echo_first_n_chars(128));
    let task = paced(&rig, &"w".repeat(300), "ft-15-0", 64, Duration::from_secs(30));
    rig.dut.wait_bytes(192);
    rig.phase1();
    assert_eq!(task.join_within(Duration::from_secs(2)), Err(rejected(RejectReason::Draining, 0, false)));
    let at = rig.finish_worker();
    let tx: Vec<_> = at.records_at_receipt.iter().filter(|r| r.dir == "TX").collect();
    assert_eq!(tx.len(), 1);
    assert_eq!(tx[0].payload, b"w".repeat(192), "已確認 slice 必須落 WAL");
    assert!(tx[0].seq <= at.wal_seq_at_receipt);
    assert!(at.receipt.unrecorded.is_empty());
    assert_eq!(rig.tx_records().len(), 1);
}

#[test]
fn shutdown_during_paced_push_wal_has_confirmed_slices() {
    // shutdown Phase 1 只凍結（port 未被喚醒）→ Phase 2 收尾 flush → wal 關閉；關閉前 WAL 已有確認 bytes。
    let mut rig = PortRig::new("COM0", FakeDut::echo_first_n_chars(128));
    let task = paced(&rig, &"s".repeat(300), "ft-16-0", 64, Duration::from_secs(30));
    rig.dut.wait_bytes(192);
    rig.fence.with(|f| {
        f.frozen = true;
        f.active_epoch = None;
    });
    let at = rig.finish_worker();
    rig.wal.shutdown().expect("WAL 關閉");
    assert!(at.receipt.unrecorded.is_empty());
    let tx = rig.tx_records();
    assert_eq!(tx.len(), 1);
    assert_eq!(tx[0].payload, b"s".repeat(192));
    rig.rx.cancel_transfer_runs();
    assert_eq!(task.join(), Err(rejected(RejectReason::Draining, 0, false)));
    assert_eq!(rig.tx_records().len(), 1, "WAL 關閉後不再寫入");
}

#[test]
fn aggregate_flush_wal_failure_listed_unrecorded() {
    let mut rig = PortRig::new("COM0", FakeDut::echo_first_n_chars(128));
    let task = paced(&rig, &"f".repeat(300), "ft-17-0", 64, Duration::from_secs(30));
    rig.dut.wait_bytes(192);
    rig.wfs.fail_next_writes(&rig.wal.wal_path(), 1, PalError::io(28, "[Errno 28] No space left on device"));
    let at = rig.finish_worker();
    let gaps: Vec<_> = at
        .receipt
        .unrecorded
        .iter()
        .filter(|g| g.dir == "TX" && g.op_id == Some(OP))
        .collect();
    assert_eq!(gaps.len(), 1, "{:?}", at.receipt.unrecorded);
    assert_eq!(gaps[0].len, 192, "列出該 op 的確認 bytes");
    rig.rx.cancel_transfer_runs();
    let _ = task.join();
}

#[test]
fn selftest_probe_between_paced_slices_never_splits_slice() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    let tx = rig.ctx.tx.clone();
    let prober = std::thread::spawn(move || {
        for _ in 0..20 {
            let (reply, report) = crossbeam_channel::bounded(1);
            tx.jobs
                .send(TxJob {
                    permit: permit(TxClass::SelfTestProbe, "system", None, true),
                    payload: b"PROBE\n".to_vec(),
                    reply,
                    aggregate: None,
                })
                .unwrap();
            let _ = tx.wait_report(&report);
            std::thread::sleep(Duration::from_millis(1));
        }
    });
    let r = paced(&rig, &"s".repeat(300), "ft-18-0", 64, Duration::from_secs(5)).join();
    prober.join().unwrap();
    assert_eq!(r, Ok(EchoPaced { ok: true, acked_chars: 300, sent_chars: 300 }));
    let slices: Vec<Vec<u8>> = rig
        .dut
        .writes()
        .into_iter()
        .filter(|w| w.as_slice() != b"PROBE\n")
        .collect();
    assert_eq!(
        slices,
        vec![
            b"s".repeat(64),
            b"s".repeat(64),
            b"s".repeat(64),
            b"s".repeat(64),
            b"s".repeat(44),
            b"\n".to_vec()
        ],
        "probe 只出現在 slice 邊界"
    );
}

#[test]
fn aggregate_append_over_limit_refused_zero_tx() {
    let rig = PortRig::new("COM0", FakeDut::echo_all());
    let key = AggregateKey { op_id: OP, source: "agent".into(), cmd_id: Some("ft-19-0".into()), max_bytes: 4 };
    let submit = |payload: &[u8], op: AggregateOp| {
        let (reply, report) = crossbeam_channel::bounded(1);
        rig.ctx
            .tx
            .jobs
            .send(TxJob {
                permit: permit(TxClass::FileTransfer, "agent", Some("ft-19-0"), false),
                payload: payload.to_vec(),
                reply,
                aggregate: Some(op),
            })
            .unwrap();
        rig.ctx.tx.wait_report(&report).expect("writer 回覆")
    };
    let ok = submit(b"abc", AggregateOp::Append(key.clone()));
    assert!(matches!(ok.result.outcome, TxOutcome::Completed(3)));
    let over = submit(b"de", AggregateOp::Append(key.clone()));
    assert!(matches!(over.result.outcome, TxOutcome::NotSubmitted), "超過一行命令長度為程式錯誤：零 TX");
    assert!(over.result.error.is_some());
    assert_eq!(rig.dut.bytes(), b"abc");
    let fin = submit(b"", AggregateOp::Finish(key.clone()));
    assert!(fin.wal.is_some_and(|w| !w.loss_flag));
    let again = submit(b"", AggregateOp::Finish(key));
    assert!(again.wal.is_none(), "緩衝為空時不寫");
    let tx = rig.tx_records();
    assert_eq!(tx.len(), 1);
    assert_eq!(tx[0].payload, b"abc");
    assert!(TX_AGGREGATE_OPEN_MAX >= 1);
}

#[test]
fn begin_stop_cancels_transfer_waits_for_every_stop_kind() {
    type Trigger = fn(&mut CoreHarness);
    let triggers: [(&str, Trigger); 3] = [
        ("release", |h| {
            let _ = h.rpc_deferred(SessionRequest::Release { source: "cli".into(), reason: PyValue::Null });
        }),
        ("shutdown", |h| {
            let (reply, _rx) = crossbeam_channel::bounded(1);
            h.feed(ControlEvent::Shutdown { reply });
        }),
        ("io_error", |h| {
            let epoch = h.epoch();
            h.feed(ControlEvent::UartIoError { epoch, reason: "SERIAL_READ:5".into() });
        }),
    ];
    for (name, trigger) in triggers {
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        let transfer = RunCancel::new(&h.rx).watching_transfers();
        let login = RunCancel::new(&h.rx);
        trigger(&mut h);
        assert!(transfer.is_triggered(), "{name}：drain Phase 1 必須取消傳輸等待");
        assert!(!login.is_triggered(), "{name}：不得改 P7 login run 的取消行為");
    }
}
