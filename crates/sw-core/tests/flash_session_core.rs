#![cfg(feature = "test-fakes")]
//! P8 Task 8：SessionCore FLASHING 仲裁（probe 授權、Matched、HolderGone、flash-critical、SC-32）。
//!
//! 對應 `docs/rust-refactor/plans/09-slice24-transfer-flash.md` Task 8（狀態轉移表、Fence 規則表、測試表）；
//! Python 基線語意：`session_manager.py` `collect_flash_candidates_and_mark`／`enter_flashing`／`exit_flashing`／
//! `_flash_busy_locked`，`service.py` `_BridgeProbe.probe` 的 `set_flash_mode(True)`，`uart_io.py` flash gate 靜默丟棄
//! （UIO-12.3-d）。SC-32（Rust-only）：Probe／FLASHING 期間 UartIoError 進 drain→RECOVERING、DeviceDetached 延後。
//!
//! 「零 TX」一律以 FakeDevice 實際寫出序列與 WAL TX 紀錄（`bb` 黑箱，真 actor＋OpRunner＋TxWriter）斷言；`kit`
//! （CoreHarness）只用於效果、世代與 fence 的狀態機斷言。

mod support;

use std::time::{Duration, Instant};

use support::flash_core::{bb, code, has_start_op, human_tx_payloads, is_begin_drain, is_open_bridge, is_persist, keys, kit, session_state};
use support::{agent_submit, install_shell_replies, manager_yaml, open_req, s_of, send_req, wait_until, ManagerRig, DEVICE_BY_ID, REAL_PATH};
use sw_core::drain::StopKind;
use sw_core::mailbox::{ControlEvent, DetachReason};
use sw_core::op_runner::OpOutcome;
use sw_core::session_core::{FlashReject, ProbeGrant, SessionRequest, SessionState};
use sw_core::state_store::LoadedState;
use sw_core::tx_writer::{FenceReject, FlashMode, TxClass};
use sw_pal::traits::TxOutcome;
use sw_proto::pyjson::{PyDict, PyValue};

const BUSY_KEYS: [&str; 4] = ["ok", "error_code", "selector", "session"];

fn busy_keys() -> Vec<String> {
    BUSY_KEYS.iter().map(|s| s.to_string()).collect()
}

fn io_error(h: &mut support::CoreHarness) -> Vec<sw_core::session_core::Effect> {
    let epoch = h.epoch();
    h.feed(ControlEvent::UartIoError { epoch, reason: "SERIAL_READ".into() })
}

fn detach(h: &mut support::CoreHarness, reason: DetachReason) -> Vec<sw_core::session_core::Effect> {
    h.feed(ControlEvent::DeviceDetached { by_id: DEVICE_BY_ID.into(), reason })
}

fn recover_req(force: bool) -> SessionRequest {
    SessionRequest::Recover { timeout_s: 2.0, force }
}

fn bind_req() -> SessionRequest {
    SessionRequest::Bind { by_id: "/s/by-id/usb-OTHER".into() }
}

// ---------------------------------------------------------------------------
// cases.yaml s24-flash-arbitration 的 evidence
// ---------------------------------------------------------------------------

#[test]
fn probe_then_matched_enters_flashing_and_invalidates_human_input() {
    let mut h = kit::attached_passthrough();
    h.attach_pty_console("c1", true);
    h.seed_deferred("c1", b"stale-keys");
    assert_eq!(h.deferred_len("c1"), 10);
    let conn0 = kit::conn_epoch(&h);
    let ie0 = kit::input_epoch(&h);
    let og0 = kit::owner_gen(&h);
    let mut fx = Vec::new();

    let (g, f) = kit::probe_start_fx(&mut h, 7);
    fx.extend(f);
    assert_eq!(g, Ok(ProbeGrant { epoch: conn0.expect("bridge"), probe_id: 7 }));
    assert_eq!(kit::flash_mode(&h), FlashMode::Probe(7));
    assert!(kit::ownership_flash_mode(&h), "probe 窗口即擋 human 輸入（service.py _BridgeProbe set_flash_mode(True)）");

    // MCU ACK 經真 reader 進 WAL RX 與 data lane（actor 不丟 probe／FLASHING 期間的 RX；回放 buffer 由 T9 coordinator 持有）。
    kit::inject_rx(&mut h, b"\x00\xcc");
    let (m, f) = kit::matched_fx(&mut h, 7);
    fx.extend(f);
    assert_eq!(m, Ok(()));
    assert_eq!(kit::state(&h), SessionState::Flashing);
    assert_eq!(kit::flash_mode(&h), FlashMode::Flashing);
    assert_eq!(h.deferred_len("c1"), 0, "BC-02：FlashSyncMatched 使舊 input_epoch 的 deferred 全部失效");
    assert_eq!(kit::input_epoch(&h), ie0 + 1);
    assert_eq!(h.fence.with(|f| f.input_epoch), ie0 + 1);
    assert_eq!(kit::owner_gen(&h), og0, "owner_gen 不用於 deferred 失效（P6 D-02）");
    assert!(kit::has_warn(&h, "reason=flash"), "失效原因為 InvalidateReason::Flash：{:?}", h.warns());
    assert!(
        kit::wal_dir_payloads(&h, "RX").iter().any(|p| p == b"\x00\xcc"),
        "ACK 回放資料不丟（WAL RX 照記）"
    );

    fx.extend(kit::holder_gone(&mut h, 7));
    assert_eq!(kit::state(&h), SessionState::Attached, "還原 flash_prev_state（UIO-12.9-d）");
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
    assert!(!kit::ownership_flash_mode(&h));
    // §5.2.6：同 handle 模式切換。
    assert!(!fx.iter().any(|e| is_open_bridge(e) || is_begin_drain(e)), "Probe→Matched→Gone 不得換 handle");
    assert_eq!(kit::conn_epoch(&h), conn0);
    assert_eq!(h.fake_device.close_count(), 0);
    assert_eq!(h.fake_device.open_count(), 1);
    assert!(kit::dut_tx(&h).is_empty(), "整段沒有任何 agent／human TX");
}

#[test]
fn probe_rejected_when_foreground_busy_sc14() {
    let mut h = kit::ready_passthrough();
    let _cmd = h.start_foreground_command("cat /proc/version");
    assert!(h.foreground_busy());
    let g = kit::probe_start(&mut h, 1);
    assert_eq!(g, Err(FlashReject::ForegroundBusy));
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
    assert!(!kit::ownership_flash_mode(&h));
}

#[test]
fn unmatched_release_only_clears_same_probe() {
    let mut h = kit::attached_passthrough();
    kit::probe_start(&mut h, 3).expect("probe 3");
    kit::released(&mut h, 99);
    assert_eq!(kit::flash_mode(&h), FlashMode::Probe(3), "其他 probe 的 Released 不得解除本 probe");
    assert!(kit::ownership_flash_mode(&h));
    kit::released(&mut h, 3);
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
    assert!(!kit::ownership_flash_mode(&h));
    assert_eq!(kit::state(&h), SessionState::Attached);
    assert_eq!(kit::probe_start(&mut h, 4).map(|g| g.probe_id), Ok(4), "解除後可再 probe");
}

// ---------------------------------------------------------------------------
// cases.yaml s24-flash-ioerror-recovering 的 evidence（SC-32）
// ---------------------------------------------------------------------------

#[test]
fn io_error_during_flashing_drains_to_recovering() {
    let mut h = kit::flashing(5);
    let fx = io_error(&mut h);
    assert!(fx.iter().any(is_begin_drain), "Phase 1 drain");
    assert!(!fx.iter().any(is_open_bridge), "handoff 回條前不重開 UART");
    assert_eq!(kit::state(&h), SessionState::Recovering);
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
    assert!(!kit::ownership_flash_mode(&h));
    assert!(h.fence.with(|f| f.frozen));
    let fx = kit::holder_gone(&mut h, 5);
    assert_eq!(kit::state(&h), SessionState::Recovering, "事後 exit flashing 對狀態為 no-op");
    assert!(!fx.iter().any(is_open_bridge));
}

#[test]
fn device_detached_during_flashing_is_deferred() {
    let mut h = kit::flashing(6);
    let fx = detach(&mut h, DetachReason::Removed);
    assert!(!fx.iter().any(is_begin_drain), "BSL／reset 的短暫 by-id 消失不得中斷燒錄（#83 RACE-2）");
    assert_eq!(kit::state(&h), SessionState::Flashing);
    assert_eq!(kit::flash_mode(&h), FlashMode::Flashing);
    let fx = kit::holder_gone(&mut h, 6);
    assert_ne!(kit::state(&h), SessionState::Flashing);
    assert!(fx.iter().any(is_begin_drain), "延後的 detach 在 exit flashing 後補做");
    assert_eq!(kit::state(&h), SessionState::Recovering);
    assert_eq!(kit::last_error(&h).as_deref(), Some("DEVICE_REMOVED"));
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
}

// ---------------------------------------------------------------------------
// FlashSyncProbeStart 的拒絕（FlashReject 逐一覆蓋）
// ---------------------------------------------------------------------------

#[test]
fn probe_start_rejects_command_capable() {
    let mut h = kit::ready_shell();
    let before = kit::dut_tx(&h);
    assert_eq!(kit::probe_start(&mut h, 1), Err(FlashReject::CommandCapable));
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
    assert!(!kit::ownership_flash_mode(&h));
    assert_eq!(kit::dut_tx(&h), before, "零 TX");
}

#[test]
fn probe_start_rejects_bad_state() {
    // DETACHED（尚未 attach）
    let mut h = support::CoreHarness::new(support::PASSTHROUGH_PROFILE);
    assert_eq!(kit::probe_start(&mut h, 1), Err(FlashReject::BadState(SessionState::Detached)));
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
    // RECOVERING（I/O error drain 中）
    let mut h = kit::attached_passthrough();
    io_error(&mut h);
    assert_eq!(kit::probe_start(&mut h, 1), Err(FlashReject::BadState(SessionState::Recovering)));
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
    // RELEASED
    let mut h = kit::attached_passthrough();
    kit::to_released(&mut h);
    assert_eq!(kit::probe_start(&mut h, 1), Err(FlashReject::BadState(SessionState::Released)));
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
    // FLASHING（BadState 先於 FlashAlreadyActive）
    let mut h = kit::flashing(1);
    assert_eq!(kit::probe_start(&mut h, 2), Err(FlashReject::BadState(SessionState::Flashing)));
    assert_eq!(kit::flash_mode(&h), FlashMode::Flashing);
}

#[test]
fn probe_start_rejects_released() {
    // RELEASED 轉移中的競態：release metadata 仍在、狀態已顯示候選態（以 test-fakes 強制）。
    let mut h = kit::attached_passthrough();
    kit::to_released(&mut h);
    h.force_state(SessionState::Attached);
    assert_eq!(kit::probe_start(&mut h, 1), Err(FlashReject::Released));
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
    assert!(!kit::ownership_flash_mode(&h));
}

#[test]
fn probe_start_rejects_recovering() {
    // reboot 自動恢復中（recovering 旗標為真）；狀態以 test-fakes 強制回候選態以驗證旗標檢查。
    let mut h = kit::ready_passthrough();
    let cmd = h.start_foreground_command("reboot");
    let epoch = h.epoch();
    h.feed(ControlEvent::OpFinished { op_id: cmd.op_id, epoch, outcome: OpOutcome::RebootExpected, after_rx_seq: None });
    assert_eq!(kit::state(&h), SessionState::Recovering);
    h.force_state(SessionState::Ready);
    assert_eq!(kit::probe_start(&mut h, 1), Err(FlashReject::Recovering));
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
}

#[test]
fn probe_start_rejects_drain_in_progress() {
    let mut h = kit::attached_passthrough();
    io_error(&mut h);
    assert!(h.core.drain_in_progress());
    h.force_state(SessionState::Attached);
    assert_eq!(kit::probe_start(&mut h, 1), Err(FlashReject::DrainInProgress));
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
}

#[test]
fn gpb_foreground_tx_barrier_waits_for_wal() {
    const COMMAND: &str = "cat /proc/version";
    const COMMAND_BYTES: &[u8] = b"cat /proc/version\n";

    let rig = bb::ready(false);
    let tx0 = rig.dev.tx_len();
    rig.dev
        .script_write(vec![sw_pal::fakes::FakeWriteStep::AcceptThenWouldBlock(
            COMMAND_BYTES.len(),
        )]);
    let cmd_id = rig.submit_ok(COMMAND);
    rig.wait_running(&cmd_id);

    let paused = rig.dev.wait_paused(Duration::from_secs(5));
    if !paused {
        rig.dev.release_pause();
        panic!("FakeWriteStep 未在完整命令 TX 後暫停");
    }
    let dut_received_command = rig.dev.tx_since(tx0) == COMMAND_BYTES;
    let wal_has_command_tx = bb::command_tx_in_wal(&rig, &cmd_id, COMMAND_BYTES);

    let (started_tx, started_rx) = std::sync::mpsc::channel();
    let (done_tx, done_rx) = std::sync::mpsc::channel();
    std::thread::scope(|scope| {
        let waiter = scope.spawn(|| {
            started_tx.send(()).expect("通知等待執行緒已啟動");
            bb::wait_command_tx_in_wal(&rig, &cmd_id, COMMAND_BYTES);
            done_tx.send(()).expect("回報 WAL barrier 完成");
        });

        let waiter_started = started_rx.recv_timeout(Duration::from_secs(5)).is_ok();
        let returned_while_writer_paused = done_rx.recv_timeout(Duration::from_millis(300)).is_ok();
        rig.dev.release_pause();
        let returned_after_release =
            returned_while_writer_paused || done_rx.recv_timeout(Duration::from_secs(5)).is_ok();
        let waiter_joined = waiter.join().is_ok();
        let wal_has_command_tx_after_release = bb::command_tx_in_wal(&rig, &cmd_id, COMMAND_BYTES);

        assert!(waiter_started, "WAL barrier 等待執行緒未啟動");
        assert!(dut_received_command, "暫停前 DUT 必須已收到完整命令 bytes");
        assert!(!wal_has_command_tx, "writer 暫停期間 WAL 不得已有該命令 TX");
        assert!(
            !returned_while_writer_paused,
            "WAL barrier 在指定命令尚未落 WAL 時提前返回"
        );
        assert!(returned_after_release, "放行 writer 後 WAL barrier 應完成");
        assert!(waiter_joined, "WAL barrier 等待執行緒應正常結束");
        assert!(
            wal_has_command_tx_after_release,
            "放行 writer 後 WAL 應含相同 cmd_id 與完整 payload"
        );
    });
}

#[test]
fn probe_start_rejects_foreground_busy() {
    // 黑箱：passthrough READY 有前景命令執行中（SC-14）→ ForegroundBusy，之後 DUT 不再收到任何 bytes。
    let rig = bb::ready(false);
    let cmd = rig.submit_ok("cat /proc/version");
    rig.wait_running(&cmd);
    bb::wait_command_tx_in_wal(&rig, &cmd, b"cat /proc/version\n");
    let tx0 = rig.dev.tx_len();
    let wal0 = bb::wal_tx(&rig).len();
    assert_eq!(bb::probe_start(&rig, 1), Err(FlashReject::ForegroundBusy));
    assert_eq!(bb::flash_mode(&rig), FlashMode::Off);
    bb::settle(&rig);
    assert_eq!(rig.dev.tx_len(), tx0, "外部零 TX");
    assert_eq!(bb::wal_tx(&rig).len(), wal0);
}

#[test]
fn probe_start_rejects_flash_already_active() {
    let mut h = kit::attached_passthrough();
    kit::probe_start(&mut h, 1).expect("probe 1");
    assert_eq!(kit::probe_start(&mut h, 2), Err(FlashReject::FlashAlreadyActive));
    assert_eq!(kit::flash_mode(&h), FlashMode::Probe(1));
}

#[test]
fn matched_requires_same_probe() {
    let mut h = kit::attached_passthrough();
    assert_eq!(kit::matched(&mut h, 1), Err(FlashReject::ProbeMismatch), "無 probe 時不得 Matched");
    kit::probe_start(&mut h, 1).expect("probe 1");
    assert_eq!(kit::matched(&mut h, 2), Err(FlashReject::ProbeMismatch));
    assert_eq!(kit::state(&h), SessionState::Attached);
    assert_eq!(kit::flash_mode(&h), FlashMode::Probe(1));
}

// ---------------------------------------------------------------------------
// Matched／HolderGone／Released
// ---------------------------------------------------------------------------

#[test]
fn matched_switches_mode_without_open_bridge_or_begin_drain() {
    let mut h = kit::attached_passthrough();
    h.attach_pty_console("c1", true);
    h.seed_deferred("c1", b"zz");
    let conn0 = kit::conn_epoch(&h);
    let ie0 = kit::input_epoch(&h);
    kit::probe_start(&mut h, 9).expect("probe 9");
    let (m, fx) = kit::matched_fx(&mut h, 9);
    assert_eq!(m, Ok(()));
    assert!(!fx.iter().any(|e| is_open_bridge(e) || is_begin_drain(e)), "Matched 不得 OpenBridge／BeginDrain");
    assert_eq!(kit::conn_epoch(&h), conn0, "conn_epoch 不變");
    assert_eq!(kit::input_epoch(&h), ie0 + 1, "input_epoch +1（BC-02 經 invalidate_human_input）");
    assert_eq!(h.input_epoch_pub(), ie0 + 1);
    assert_eq!(h.deferred_len("c1"), 0);
    assert!(kit::has_warn(&h, "reason=flash"));
    assert_eq!(h.fake_device.close_count(), 0);
    assert!(!h.fence.with(|f| f.frozen));
}

#[test]
fn holder_gone_with_stale_probe_id_is_noop() {
    let mut h = kit::flashing(5);
    let fx = kit::holder_gone(&mut h, 4);
    assert_eq!(kit::state(&h), SessionState::Flashing);
    assert_eq!(kit::flash_mode(&h), FlashMode::Flashing);
    assert!(kit::ownership_flash_mode(&h));
    assert!(fx.is_empty() || !fx.iter().any(|e| is_open_bridge(e) || is_begin_drain(e)));
    // Probe 窗口同理：舊 probe 的 HolderGone 不解除新 probe。
    let mut h = kit::attached_passthrough();
    kit::probe_start(&mut h, 5).expect("probe 5");
    kit::holder_gone(&mut h, 4);
    assert_eq!(kit::flash_mode(&h), FlashMode::Probe(5));
    assert!(kit::ownership_flash_mode(&h));
}

#[test]
fn stale_holder_gone_ignored_during_new_flashing() {
    let mut h = kit::attached_passthrough();
    kit::probe_start(&mut h, 1).expect("probe 1");
    kit::released(&mut h, 1);
    kit::probe_start(&mut h, 2).expect("probe 2");
    kit::matched(&mut h, 2).expect("matched 2");
    kit::holder_gone(&mut h, 1);
    assert_eq!(kit::state(&h), SessionState::Flashing);
    assert_eq!(kit::flash_mode(&h), FlashMode::Flashing);
    kit::holder_gone(&mut h, 2);
    assert_eq!(kit::state(&h), SessionState::Attached);
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
}

#[test]
fn holder_gone_from_ready_prev() {
    let mut h = kit::ready_passthrough();
    kit::probe_start(&mut h, 1).expect("probe");
    kit::matched(&mut h, 1).expect("matched");
    assert_eq!(kit::state(&h), SessionState::Flashing);
    kit::holder_gone(&mut h, 1);
    assert_eq!(kit::state(&h), SessionState::Ready);
    // READY 照常可下命令（佇列與 registry 未被 flash 汙染）。
    let r = h.rpc(SessionRequest::Submit(agent_submit("uname"))).expect("RPC");
    assert_eq!(r.get("ok"), Some(&PyValue::Bool(true)), "{r:?}");
}

#[test]
fn holder_gone_without_bridge() {
    // prev 已被 I/O error 清除、bridge 已關：HolderGone 的後備路徑為 DETACHED（Python `exit_flashing`）。
    let mut h = kit::flashing(5);
    io_error(&mut h);
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    assert_eq!(kit::conn_epoch(&h), None);
    h.force_state(SessionState::Flashing);
    kit::holder_gone(&mut h, 5);
    assert_eq!(kit::state(&h), SessionState::Detached);
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
}

#[test]
fn flashing_not_persisted() {
    let mut h = kit::attached_passthrough();
    let commits = h.state_store.commits();
    let mut fx = Vec::new();
    fx.extend(kit::probe_start_fx(&mut h, 1).1);
    fx.extend(kit::matched_fx(&mut h, 1).1);
    fx.extend(kit::holder_gone(&mut h, 1));
    assert!(!fx.iter().any(is_persist), "FLASHING 不寫 state.json");
    assert_eq!(h.state_store.commits(), commits);
    let snap = format!("{:?}", h.state_store.last_released());
    assert!(!snap.contains("FLASHING") && !snap.contains("flash"), "{snap}");
}

// ---------------------------------------------------------------------------
// SC-32：Probe／FLASHING 期間的 I/O error 與裝置消失
// ---------------------------------------------------------------------------

#[test]
fn uart_io_error_in_flashing_enters_recovering_and_clears_both_flash_modes() {
    let mut h = kit::flashing(5);
    let fx = io_error(&mut h);
    assert!(fx.iter().any(is_begin_drain));
    assert!(!fx.iter().any(is_open_bridge));
    assert_eq!(kit::state(&h), SessionState::Recovering);
    assert_eq!(kit::flash_mode(&h), FlashMode::Off, "Fence.flash_mode 清除");
    assert!(!kit::ownership_flash_mode(&h), "OwnershipState.flash_mode 清除（reset_for_stop 會保留，須明確清）");
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    let after = kit::state(&h);
    kit::holder_gone(&mut h, 5);
    assert_eq!(kit::state(&h), after, "回條後的 HolderGone 對狀態為 no-op");
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
}

#[test]
fn uart_io_error_in_probe_enters_recovering_and_clears_both_flash_modes() {
    let mut h = kit::attached_passthrough();
    kit::probe_start(&mut h, 3).expect("probe");
    let fx = io_error(&mut h);
    assert!(fx.iter().any(is_begin_drain));
    assert_eq!(kit::state(&h), SessionState::Recovering);
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
    assert!(!kit::ownership_flash_mode(&h));
    assert_eq!(kit::matched(&mut h, 3), Err(FlashReject::ProbeMismatch), "I/O error 後不得再進 FLASHING");
    kit::released(&mut h, 3);
    assert_eq!(kit::state(&h), SessionState::Recovering);
}

#[test]
fn device_detached_during_probe_is_deferred_until_holder_gone() {
    let mut h = kit::attached_passthrough();
    kit::probe_start(&mut h, 2).expect("probe");
    let fx = detach(&mut h, DetachReason::Removed);
    assert!(!fx.iter().any(is_begin_drain));
    assert_eq!(kit::state(&h), SessionState::Attached);
    assert_eq!(kit::flash_mode(&h), FlashMode::Probe(2));
    kit::matched(&mut h, 2).expect("matched");
    let fx = kit::holder_gone(&mut h, 2);
    assert!(fx.iter().any(is_begin_drain));
    assert_eq!(kit::state(&h), SessionState::Recovering);
    assert_eq!(kit::last_error(&h).as_deref(), Some("DEVICE_REMOVED"));
}

#[test]
fn device_detached_during_probe_reconciled_on_probe_released() {
    let mut h = kit::attached_passthrough();
    kit::probe_start(&mut h, 2).expect("probe");
    detach(&mut h, DetachReason::Removed);
    let fx = kit::released(&mut h, 2);
    assert!(fx.iter().any(is_begin_drain), "未匹配釋放也是 flash 結束路徑：補做延後的 detach");
    assert_eq!(kit::last_error(&h).as_deref(), Some("DEVICE_REMOVED"));
}

#[test]
fn device_back_on_same_path_during_flashing_needs_no_detach() {
    let mut h = kit::flashing(3);
    detach(&mut h, DetachReason::Removed);
    let fx = h.feed(ControlEvent::DeviceAttached { by_id: DEVICE_BY_ID.into(), real_path: REAL_PATH.into() });
    assert!(!fx.iter().any(is_open_bridge), "FLASHING 期間 DeviceAttached 不開新 bridge");
    assert_eq!(kit::state(&h), SessionState::Flashing);
    let fx = kit::holder_gone(&mut h, 3);
    assert!(!fx.iter().any(is_begin_drain), "裝置已回到同一路徑：reconcile 無差異");
    assert_eq!(kit::state(&h), SessionState::Attached);
}

#[test]
fn device_rebound_during_flashing_reconciled_as_rebound() {
    let mut h = kit::flashing(3);
    detach(&mut h, DetachReason::Rebound);
    h.feed(ControlEvent::DeviceAttached { by_id: DEVICE_BY_ID.into(), real_path: "/fake/tty-B".into() });
    assert_eq!(kit::state(&h), SessionState::Flashing);
    let fx = kit::holder_gone(&mut h, 3);
    assert!(fx.iter().any(is_begin_drain));
    assert_eq!(kit::last_error(&h).as_deref(), Some("DEVICE_REBOUND_REQUIRED"));
}

#[test]
fn device_detached_then_io_error_during_flashing_keeps_removed_post_kind() {
    let mut h = kit::flashing(4);
    detach(&mut h, DetachReason::Removed);
    io_error(&mut h);
    assert_eq!(kit::state(&h), SessionState::Recovering);
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).feed();
    kit::holder_gone(&mut h, 4);
    assert_eq!(kit::last_error(&h).as_deref(), Some("DEVICE_REMOVED"), "延後的移除在 drain 收尾後套用");
    assert!(!matches!(kit::state(&h), SessionState::Flashing | SessionState::Ready | SessionState::Attached));
}

// ---------------------------------------------------------------------------
// human console：Probe／FLASHING 期間丟棄（DropFlash，不進 DeferredBuffer、不落 WAL）
// ---------------------------------------------------------------------------

#[test]
fn console_input_dropped_during_probe_and_flashing() {
    let mut h = kit::attached_passthrough();
    h.attach_pty_console("c1", true);
    kit::probe_start(&mut h, 1).expect("probe");
    let fx = h.input("c1", b"abc");
    assert!(human_tx_payloads(&fx).is_empty(), "probe 窗口 human 輸入不送 UART");
    assert_eq!(h.deferred_len("c1"), 0, "不進 DeferredBuffer");
    kit::matched(&mut h, 1).expect("matched");
    let fx = h.input("c1", b"def");
    assert!(human_tx_payloads(&fx).is_empty(), "FLASHING human 輸入不送 UART");
    assert_eq!(h.deferred_len("c1"), 0);
    assert!(kit::has_warn(&h, "reason=flash"));
    assert!(h.human_tx().is_empty());
    assert!(kit::wal_dir_payloads(&h, "TX").is_empty(), "不落 WAL");
    assert!(kit::dut_tx(&h).is_empty());
}

#[test]
fn console_input_during_probe_not_backlogged_behind_inflight_tx() {
    let mut h = kit::attached_passthrough();
    h.attach_pty_console("c1", true);
    h.set_auto_complete_human_tx(false);
    let fx = h.input("c1", b"x");
    assert_eq!(human_tx_payloads(&fx), vec![b"x".to_vec()], "c1 為 raw owner，x 進 human TX lane");
    kit::probe_start(&mut h, 1).expect("probe");
    let fx = h.input("c1", b"yz");
    assert!(human_tx_payloads(&fx).is_empty());
    // probe 前已交 OS 的 x 照實結算；probe 期間的 yz 不得排在其後送出或轉入 deferred。
    let fx = h.complete_human_tx(TxOutcome::Completed(1), None);
    assert!(human_tx_payloads(&fx).is_empty(), "yz 不得自 backlog 送出：{:?}", human_tx_payloads(&fx));
    assert_eq!(h.human_tx().len(), 1);
    assert_eq!(h.deferred_len("c1"), 0);
    kit::released(&mut h, 1);
    let fx = h.input("c1", b"k");
    assert_eq!(human_tx_payloads(&fx), vec![b"k".to_vec()], "probe 解除後輸入恢復");
}

#[test]
fn stale_console_input_queued_before_probe_dropped_after_matched() {
    let mut h = kit::attached_passthrough();
    h.attach_pty_console("c1", true);
    let ie0 = kit::input_epoch(&h);
    kit::probe_start(&mut h, 1).expect("probe");
    kit::matched(&mut h, 1).expect("matched");
    kit::holder_gone(&mut h, 1);
    assert_eq!(kit::input_epoch(&h), ie0 + 1);
    // probe 前以舊 input_epoch 排入 data lane、exit flashing 後才處理的輸入。
    let fx = kit::console_input_with_epoch(&mut h, "c1", ie0, b"old-keys");
    assert!(human_tx_payloads(&fx).is_empty(), "BC-02：舊上下文的輸入不回放");
    assert!(!h.human_tx().iter().any(|(_, _, p)| p == b"old-keys"));
    assert_eq!(h.deferred_len("c1"), 0);
    assert!(kit::has_warn(&h, "reason=stale_epoch"));
}

#[test]
fn probe_window_console_bytes_dropped() {
    // 黑箱：ConsoleHub（PTY 替身）→ data lane → actor → TxWriter → FakeDevice。
    let rig = bb::attached(true);
    let c1 = rig.attach_pty_console("c1");
    bb::settle(&rig);
    bb::probe_start(&rig, 1).expect("probe");
    let tx0 = rig.dev.tx_len();
    c1.push_input(b"hello");
    bb::settle(&rig);
    assert_eq!(rig.dev.tx_len(), tx0, "probe 窗口 human 按鍵外部零 TX");
    assert!(bb::wal_tx(&rig).is_empty(), "不落 WAL");
    assert!(
        rig.stderr_lines().iter().any(|l| l.contains("reason=flash")),
        "經 DropFlash 丟棄：{:?}",
        rig.stderr_lines()
    );
    // 對照：probe 解除後同一 console 的按鍵確實送達（證明上面的零 TX 不是 console 未接通）。
    bb::released(&rig, 1);
    c1.push_input(b"k");
    bb::wait_dut_contains(&rig, tx0, b"k");
    assert!(!rig.dev.tx_since(tx0).windows(5).any(|w| w == b"hello"), "probe 期間的按鍵不得事後送出");
}

#[test]
fn flashing_console_bytes_dropped_blackbox() {
    let rig = bb::attached(true);
    let c1 = rig.attach_pty_console("c1");
    bb::settle(&rig);
    bb::probe_start(&rig, 2).expect("probe");
    bb::matched(&rig, 2).expect("matched");
    rig.wait_state("FLASHING");
    let tx0 = rig.dev.tx_len();
    // Matched 使 input_epoch +1：hub 在切換前取的快照所帶的第一筆輸入依 P6 D-02 以 stale_epoch 丟棄；之後的輸入帶新
    // 世代，才走到 FLASHING 的 DropFlash。
    c1.push_input(b"x");
    bb::settle(&rig);
    c1.push_input(b"sbl-poison");
    bb::settle(&rig);
    assert_eq!(rig.dev.tx_len(), tx0, "FLASHING human 按鍵外部零 TX");
    assert!(
        rig.stderr_lines().iter().any(|l| l.contains("reason=flash")),
        "新世代輸入經 DropFlash 丟棄：{:?}",
        rig.stderr_lines()
    );
    bb::holder_gone(&rig, 2);
    rig.wait_state("ATTACHED");
    bb::type_until_dut_receives(&rig, &c1, tx0, b"q");
    assert!(!rig.dev.tx_since(tx0).windows(10).any(|w| w == b"sbl-poison"));
}

#[test]
fn console_input_resumes_after_flash_ioerror_recovery() {
    let rig = bb::attached(true);
    let c1 = rig.attach_pty_console("c1");
    bb::settle(&rig);
    let grant = bb::probe_start(&rig, 3).expect("probe");
    bb::matched(&rig, 3).expect("matched");
    rig.wait_state("FLASHING");
    rig.send_control(ControlEvent::UartIoError { epoch: grant.epoch, reason: "SERIAL_READ".into() });
    rig.wait_for("I/O error 後 drain 收尾並重新 ATTACHED", |r| {
        r.get_state().get("state").and_then(PyValue::as_str) == Some("ATTACHED")
            && r.fence.with(|f| f.active_epoch.is_some_and(|e| e != grant.epoch))
    });
    assert_eq!(bb::flash_mode(&rig), FlashMode::Off);
    let tx0 = rig.dev.tx_len();
    let warns0 = rig.stderr_lines().len();
    // drain Phase 1 令 input_epoch 前進：切換前的 hub 快照所帶的輸入依 P6 D-02 以 stale_epoch 丟棄，之後的按鍵應送達。
    bb::type_until_dut_receives(&rig, &c1, tx0, b"r");
    assert!(
        !rig.stderr_lines()[warns0..].iter().any(|l| l.contains("reason=flash")),
        "ownership.flash_mode 已清，不再 DropFlash：{:?}",
        &rig.stderr_lines()[warns0..]
    );
    bb::holder_gone(&rig, 3);
    assert_eq!(bb::state(&rig), "ATTACHED", "事後 HolderGone 對狀態為 no-op");
}

// ---------------------------------------------------------------------------
// FLASHING／probe 窗口的 RPC 仲裁（SC-02；零 TX 以黑箱斷言）
// ---------------------------------------------------------------------------

#[test]
fn flashing_state_rejects_submit_with_flashing_busy() {
    let mut h = kit::flashing(1);
    let (r, fx) = h.rpc_full(SessionRequest::Submit(agent_submit("ls")));
    let r = r.expect("RPC");
    assert_eq!(code(&r).as_deref(), Some("FLASHING_BUSY"));
    assert!(!has_start_op(&fx), "不得排程任何 op");
    assert!(h.reg.records_for_test().is_empty(), "不登錄命令");
}

#[test]
fn flashing_cmd_submit_busy() {
    let (rig, _) = bb::flashing(false, 1);
    let tx0 = rig.dev.tx_len();
    let r = bb::request(&rig, SessionRequest::Submit(agent_submit("ls")));
    assert_eq!(code(&r).as_deref(), Some("FLASHING_BUSY"));
    bb::settle(&rig);
    assert_eq!(rig.dev.tx_len(), tx0, "零 TX");
    assert!(bb::wal_tx(&rig).is_empty());
}

#[test]
fn flashing_interactive_open_busy() {
    let (rig, _) = bb::flashing(false, 1);
    let tx0 = rig.dev.tx_len();
    let r = bb::request(&rig, open_req("agent:x", 5.0, "uname", true));
    assert_eq!(code(&r).as_deref(), Some("FLASHING_BUSY"));
    bb::settle(&rig);
    assert_eq!(rig.dev.tx_len(), tx0, "零 TX");
    assert!(bb::wal_tx(&rig).is_empty());
}

#[test]
fn flashing_interactive_send_busy() {
    let rig = bb::ready(false);
    let r = bb::request(&rig, open_req("agent:x", 60.0, "", false));
    assert_eq!(r.get("ok"), Some(&PyValue::Bool(true)), "{r:?}");
    let id = s_of(&r, "interactive_id").expect("interactive_id");
    bb::probe_start(&rig, 4).expect("probe");
    bb::matched(&rig, 4).expect("matched");
    rig.wait_state("FLASHING");
    let tx0 = rig.dev.tx_len();
    let r = bb::request(&rig, send_req(&id, "ab", "plain"));
    assert_eq!(code(&r).as_deref(), Some("FLASHING_BUSY"));
    assert_eq!(s_of(&r, "interactive_id").as_deref(), Some(id.as_str()));
    bb::settle(&rig);
    assert_eq!(rig.dev.tx_len(), tx0, "零 TX");
    assert!(bb::wal_tx(&rig).is_empty());
}

#[test]
fn probe_window_interactive_send_silent() {
    // KEEP UIO-12.3-d：probe 窗口（state 仍 READY）的 lease 送出被 flash gate 靜默丟棄：回 ok＋bytes、外部零 TX。
    let rig = bb::ready(false);
    let r = bb::request(&rig, open_req("agent:x", 60.0, "", false));
    let id = s_of(&r, "interactive_id").expect("interactive_id");
    bb::probe_start(&rig, 5).expect("probe");
    let tx0 = rig.dev.tx_len();
    let r = bb::request(&rig, send_req(&id, "ab", "plain"));
    assert_eq!(r.get("ok"), Some(&PyValue::Bool(true)), "{r:?}");
    assert_eq!(s_of(&r, "interactive_id").as_deref(), Some(id.as_str()));
    assert_eq!(r.get("bytes"), Some(&PyValue::from(2)));
    bb::settle(&rig);
    assert_eq!(rig.dev.tx_len(), tx0, "外部零 TX");
    assert!(bb::wal_tx(&rig).is_empty(), "不落 WAL");
    // 對照：probe 解除後同一 lease 送出確實寫到 DUT。
    bb::released(&rig, 5);
    let r = bb::request(&rig, send_req(&id, "cd", "plain"));
    assert_eq!(r.get("ok"), Some(&PyValue::Bool(true)), "{r:?}");
    bb::wait_dut_contains(&rig, tx0, b"cd");
    assert!(!rig.dev.tx_since(tx0).windows(2).any(|w| w == b"ab"));
}

#[test]
fn flashing_release_busy() {
    let mut h = kit::flashing(1);
    let (r, fx) = h.rpc_full(kit::release_req());
    let r = r.expect("RPC");
    assert_eq!(code(&r).as_deref(), Some("FLASHING_BUSY"));
    assert_eq!(keys(&r), busy_keys(), "Python release 鍵序 ok／error_code／selector／session");
    assert_eq!(s_of(&r, "selector").as_deref(), Some("COM0"));
    assert_eq!(session_state(&r).as_deref(), Some("FLASHING"));
    assert!(!fx.iter().any(is_begin_drain));
    assert_eq!(kit::state(&h), SessionState::Flashing);
}

#[test]
fn flashing_recover_clear_bind_busy() {
    let mut h = kit::flashing(1);
    for req in [recover_req(false), recover_req(true), SessionRequest::Clear, bind_req()] {
        let label = format!("{req:?}");
        let (r, fx) = h.rpc_full(req);
        let r = r.expect("RPC");
        assert_eq!(code(&r).as_deref(), Some("FLASHING_BUSY"), "{label}");
        assert_eq!(keys(&r), busy_keys(), "{label}");
        assert_eq!(s_of(&r, "selector").as_deref(), Some("COM0"), "{label}");
        assert!(!fx.iter().any(|e| is_begin_drain(e) || is_open_bridge(e) || has_start_op(std::slice::from_ref(e))), "{label}");
        assert_eq!(kit::state(&h), SessionState::Flashing, "{label}");
    }
    assert_eq!(h.fake_device.close_count(), 0);
}

#[test]
fn probe_window_release_busy() {
    // actor 層再檢查（fence.flash_mode != Off）：probe 窗口 state 仍 ATTACHED，destructive op 一律 FLASHING_BUSY。
    let mut h = kit::attached_passthrough();
    kit::probe_start(&mut h, 1).expect("probe");
    for req in [kit::release_req(), SessionRequest::Clear, bind_req(), recover_req(false), recover_req(true)] {
        let label = format!("{req:?}");
        let (r, fx) = h.rpc_full(req);
        let r = r.expect("RPC");
        assert_eq!(code(&r).as_deref(), Some("FLASHING_BUSY"), "{label}");
        assert_eq!(keys(&r), busy_keys(), "{label}");
        assert!(!fx.iter().any(|e| is_begin_drain(e) || has_start_op(std::slice::from_ref(e))), "{label}");
        assert_eq!(kit::state(&h), SessionState::Attached, "{label}");
        assert_eq!(kit::flash_mode(&h), FlashMode::Probe(1), "{label}");
    }
    assert_eq!(h.fake_device.close_count(), 0);
}

// ---------------------------------------------------------------------------
// fence 黑箱：FLASHING／Probe 的 TX class 規則（真 TxWriter＋FakeDevice）
// ---------------------------------------------------------------------------

#[test]
fn flashing_non_flash_tx_not_submitted() {
    let (rig, grant) = bb::flashing(false, 5);
    let tx0 = rig.dev.tx_len();
    for (class, probe_id) in [
        (TxClass::LoginProbe, None),
        (TxClass::Command, None),
        (TxClass::LeaseOwner, None),
        (TxClass::HumanRaw, None),
        (TxClass::SelfTestProbe, None),
        (TxClass::SystemRecoverKey, None),
        (TxClass::FlashProbe, Some(5)),
    ] {
        let rep = bb::tx(&rig, grant.epoch, class, probe_id, b"zz");
        assert_eq!(rep.result.outcome, TxOutcome::NotSubmitted, "{class:?}");
        assert_eq!(rep.reject, Some(FenceReject::Flash), "{class:?}");
        assert!(rep.wal.is_none(), "{class:?}");
    }
    assert_eq!(rig.dev.tx_len(), tx0, "零 TX");
    assert!(bb::wal_tx(&rig).is_empty(), "WAL 無 TX 紀錄");
}

#[test]
fn flashing_flash_tx_allowed() {
    let (rig, grant) = bb::flashing(false, 5);
    let tx0 = rig.dev.tx_len();
    let rep = bb::tx(&rig, grant.epoch, TxClass::Flash, None, b"\x55\x55\x01");
    assert_eq!(rep.result.outcome, TxOutcome::Completed(3));
    assert_eq!(rig.dev.tx_since(tx0), b"\x55\x55\x01".to_vec());
    assert_eq!(bb::wal_tx(&rig), vec![("flash".to_string(), b"\x55\x55\x01".to_vec())], "WAL source:\"flash\"");
}

#[test]
fn probe_class_only_in_probe() {
    let rig = bb::attached(false);
    let grant = bb::probe_start(&rig, 4).expect("probe");
    let tx0 = rig.dev.tx_len();
    let rep = bb::tx(&rig, grant.epoch, TxClass::FlashProbe, Some(4), b"\x55\x55");
    assert_eq!(rep.result.outcome, TxOutcome::Completed(2), "同一 probe 的 FlashProbe 放行");
    assert_eq!(rig.dev.tx_since(tx0), b"\x55\x55".to_vec());
    let rep = bb::tx(&rig, grant.epoch, TxClass::Flash, None, b"\x01");
    assert_eq!(rep.result.outcome, TxOutcome::NotSubmitted, "Probe 窗口不放行 Flash class");
    bb::matched(&rig, 4).expect("matched");
    rig.wait_state("FLASHING");
    let tx1 = rig.dev.tx_len();
    let rep = bb::tx(&rig, grant.epoch, TxClass::FlashProbe, Some(4), b"\x55\x55");
    assert_eq!(rep.result.outcome, TxOutcome::NotSubmitted, "FLASHING 不再放行 FlashProbe");
    assert_eq!(rep.reject, Some(FenceReject::Flash));
    assert_eq!(rig.dev.tx_len(), tx1, "零 TX");
    assert_eq!(bb::wal_tx(&rig), vec![("flash".to_string(), b"\x55\x55".to_vec())]);
}

#[test]
fn stale_flashprobe_permit_refused() {
    let rig = bb::attached(false);
    let g1 = bb::probe_start(&rig, 1).expect("probe 1");
    bb::released(&rig, 1);
    let g2 = bb::probe_start(&rig, 2).expect("probe 2");
    let tx0 = rig.dev.tx_len();
    let rep = bb::tx(&rig, g1.epoch, TxClass::FlashProbe, Some(1), b"\x55\x55");
    assert_eq!(rep.result.outcome, TxOutcome::NotSubmitted, "上一個 probe 的 permit");
    assert_eq!(rep.reject, Some(FenceReject::Flash));
    assert_eq!(rig.dev.tx_len(), tx0, "零 TX");
    assert!(bb::wal_tx(&rig).is_empty(), "WAL 無紀錄");
    let rep = bb::tx(&rig, g2.epoch, TxClass::FlashProbe, Some(2), b"\x55\x55");
    assert_eq!(rep.result.outcome, TxOutcome::Completed(2), "對照：現行 probe 的 permit 放行");
}

#[test]
fn self_test_during_flashing_unresponsive() {
    let (rig, _) = bb::flashing(false, 1);
    let tx0 = rig.dev.tx_len();
    let rx = rig.request_async(SessionRequest::SelfTest { timeout_s: 0.5, strict_human_lock: false });
    let deadline = Instant::now() + Duration::from_secs(5);
    let r = loop {
        if let Ok(v) = rx.try_recv() {
            break v.expect("self-test");
        }
        assert!(Instant::now() < deadline, "self-test 未回覆");
        rig.clock.advance(Duration::from_millis(250));
        std::thread::sleep(Duration::from_millis(30));
    };
    assert_eq!(r.get("classification").and_then(PyValue::as_str), Some("TARGET_UNRESPONSIVE"), "{r:?}");
    assert_eq!(bb::state(&rig), "FLASHING", "不 confirm_ready（RPC-H-07）");
    assert_eq!(rig.dev.tx_len(), tx0, "外部零 TX");
    assert!(bb::wal_tx(&rig).is_empty());
}

// ---------------------------------------------------------------------------
// passthrough 安靜升 READY 與 flash（SC-35）
// ---------------------------------------------------------------------------

#[test]
fn probe_blocks_passthrough_quiet_tick() {
    let mut h = kit::attached_passthrough();
    h.advance(Duration::from_secs(29));
    kit::probe_start(&mut h, 1).expect("probe");
    h.advance(Duration::from_secs(3));
    assert_eq!(kit::state(&h), SessionState::Attached, "probe 窗口不升 READY");
    kit::released(&mut h, 1);
    h.advance(Duration::from_secs(1));
    assert_eq!(kit::state(&h), SessionState::Ready);
}

// ---------------------------------------------------------------------------
// SessionManager：collect_flash_candidates_and_mark／flash-critical
// ---------------------------------------------------------------------------

fn others(com: &str, act_no: u32, by_id: &str) -> String {
    format!("{{com: {com}, profile: others-template, act_no: {act_no}, alias: o+{act_no}, device_by_id: {by_id}}}")
}

fn shell(com: &str, act_no: u32, by_id: &str) -> String {
    format!("{{com: {com}, profile: t, act_no: {act_no}, alias: t+{act_no}, device_by_id: {by_id}}}")
}

const RPC: Duration = Duration::from_secs(5);

fn rpc_ok(r: Result<PyDict, sw_proto::pycompat::PyErr>) -> PyDict {
    r.unwrap_or_else(|e| panic!("RPC 例外：{}", e.message))
}

fn route(m: &ManagerRig, sel: &str, req: SessionRequest) -> PyDict {
    let sid = m.mgr.resolve(sel).expect("session");
    rpc_ok(m.mgr.route(&sid, req, RPC))
}

#[test]
fn candidates_atomic_mark() {
    let yaml = manager_yaml(&[&others("COM0", 1, "/s/by-id/usb-A"), &others("COM1", 2, "/s/by-id/usb-B"), &shell("COM2", 3, "/s/by-id/usb-C")]);
    let m = ManagerRig::with(&yaml, LoadedState::default(), None);
    m.add_device_at("/s/by-id/usb-A", "/dev/ttyA");
    m.add_device_at("/s/by-id/usb-B", "/dev/ttyB");
    let c = m.add_device_at("/s/by-id/usb-C", "/dev/ttyC");
    install_shell_replies(&c);
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_state("COM0", "ATTACHED");
    m.wait_state("COM1", "ATTACHED");
    m.wait_state("COM2", "READY");
    let cands = m.mgr.collect_flash_candidates_and_mark();
    let got: Vec<(String, Option<String>, Option<String>, bool)> =
        cands.iter().map(|c| (c.com.clone(), c.by_id.clone(), c.real_path.clone(), c.command_capable)).collect();
    assert_eq!(
        got,
        vec![
            ("COM0".into(), Some("/s/by-id/usb-A".into()), Some("/dev/ttyA".into()), false),
            ("COM1".into(), Some("/s/by-id/usb-B".into()), Some("/dev/ttyB".into()), false),
        ],
        "只回非 command_capable 的 passthrough（session 順序）"
    );
    assert!(m.mgr.flash_critical("COM0"));
    assert!(m.mgr.flash_critical("COM1"));
    assert!(!m.mgr.flash_critical("COM2"), "shell（command_capable）不標記");
    m.mgr.unmark_flash_critical("COM0");
    assert!(!m.mgr.flash_critical("COM0"));
    assert!(m.mgr.flash_critical("COM1"));
    m.mgr.unmark_flash_critical("COM1");
    assert!(!m.mgr.flash_critical("COM1"));
}

#[test]
fn candidates_include_passthrough_quiet_ready() {
    let yaml = manager_yaml(&[&others("COM0", 1, "/s/by-id/usb-A"), &others("COM1", 2, "/s/by-id/usb-B"), &others("COM2", 3, "/s/by-id/usb-C")]);
    let m = ManagerRig::with(&yaml, LoadedState::default(), None);
    m.add_device_at("/s/by-id/usb-A", "/dev/ttyA");
    m.add_device_at("/s/by-id/usb-C", "/dev/ttyC");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_state("COM0", "ATTACHED");
    m.wait_state("COM2", "ATTACHED");
    m.clock.advance(Duration::from_secs(31));
    m.wait_state("COM0", "READY");
    m.wait_state("COM2", "READY");
    // COM2：READY 但有前景命令執行中（SC-14）。
    let r = route(&m, "COM2", SessionRequest::Submit(agent_submit("cat /proc/version")));
    assert_eq!(r.get("ok"), Some(&PyValue::Bool(true)), "{r:?}");
    wait_until("COM2 前景忙", || m.session("COM2").get("foreground_busy") == Some(&PyValue::Bool(true)));
    // COM1：之後才上線，停在 ATTACHED。
    m.add_device_at("/s/by-id/usb-B", "/dev/ttyB");
    m.mgr.update_devices(&m.snapshot());
    m.wait_state("COM1", "ATTACHED");
    let cands = m.mgr.collect_flash_candidates_and_mark();
    let coms: Vec<&str> = cands.iter().map(|c| c.com.as_str()).collect();
    assert_eq!(coms, vec!["COM0", "COM1"], "READY（安靜升格）與 ATTACHED 皆為候選；前景忙者不列入");
    assert!(cands.iter().all(|c| !c.command_capable));
    assert!(m.mgr.flash_critical("COM0") && m.mgr.flash_critical("COM1"));
    assert!(!m.mgr.flash_critical("COM2"), "有前景操作者不標記");
}

#[test]
fn flash_critical_blocks_passthrough_quiet_tick() {
    let yaml = manager_yaml(&[&others("COM0", 1, "/s/by-id/usb-A")]);
    let m = ManagerRig::with(&yaml, LoadedState::default(), None);
    m.add_device_at("/s/by-id/usb-A", "/dev/ttyA");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_state("COM0", "ATTACHED");
    m.clock.advance(Duration::from_millis(29_500));
    std::thread::sleep(Duration::from_millis(150));
    assert_eq!(m.state_of("COM0").as_deref(), Some("ATTACHED"));
    let cands = m.mgr.collect_flash_candidates_and_mark();
    assert_eq!(cands.len(), 1);
    m.clock.advance(Duration::from_secs(1));
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(m.state_of("COM0").as_deref(), Some("ATTACHED"), "flash 臨界期間不轉 READY");
    m.mgr.unmark_flash_critical("COM0");
    m.clock.advance(Duration::from_secs(1));
    m.wait_state("COM0", "READY");
}

#[test]
fn manager_flash_critical_blocks_destructive_rpcs() {
    let yaml = manager_yaml(&[&others("COM0", 1, "/s/by-id/usb-A")]);
    let m = ManagerRig::with(&yaml, LoadedState::default(), None);
    let dev = m.add_device_at("/s/by-id/usb-A", "/dev/ttyA");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_state("COM0", "ATTACHED");
    assert_eq!(m.mgr.collect_flash_candidates_and_mark().len(), 1);
    let release = SessionRequest::Release { source: "cli".into(), reason: PyValue::Null };
    for req in [release, SessionRequest::Clear, recover_req(false), recover_req(true)] {
        let label = format!("{req:?}");
        let r = route(&m, "COM0", req);
        assert_eq!(code(&r).as_deref(), Some("FLASHING_BUSY"), "{label}：{r:?}");
        assert_eq!(keys(&r), busy_keys(), "{label}");
        assert_eq!(s_of(&r, "selector").as_deref(), Some("COM0"), "{label}");
    }
    let r = rpc_ok(m.mgr.bind("COM0", "/s/by-id/usb-Z"));
    assert_eq!(code(&r).as_deref(), Some("FLASHING_BUSY"), "{r:?}");
    assert_eq!(keys(&r), busy_keys());
    assert_eq!(m.state_of("COM0").as_deref(), Some("ATTACHED"));
    assert_eq!(dev.close_count(), 0, "probe 窗口不得 detach bridge");
    m.mgr.unmark_flash_critical("COM0");
    let r = route(&m, "COM0", SessionRequest::Clear);
    assert_ne!(code(&r).as_deref(), Some("FLASHING_BUSY"), "解除後 clear 恢復：{r:?}");
}

#[test]
fn begin_stop_shutdown_clears_flash_modes() {
    // 「停止」也是 flash 結束路徑：無條件清 Fence 與 Ownership 的 flash_mode。
    let mut h = kit::flashing(2);
    h.begin_stop(StopKind::Shutdown);
    assert_eq!(kit::flash_mode(&h), FlashMode::Off);
    assert!(!kit::ownership_flash_mode(&h));
}

// ---------------------------------------------------------------------------
// 編排者裁決（2026-11-08，rulings/s2.md「P8 T8 的兩項 SPEC-GAP」）：manager 已標 flash-critical、ProbeStart 尚未到時的
// DeviceDetached 對齊 Python 略過（`session_manager.py:4443-4455` `_detach_by_id` 以 `_flash_busy_locked` 過濾，含
// `_flash_critical_coms`）——不 drain、不延後；之後的 ProbeStart 與 Python 相同照常授權、probe TX 照常送出。
// ---------------------------------------------------------------------------

#[test]
fn device_detached_while_flash_critical_before_probe_is_skipped() {
    let yaml = manager_yaml(&[&others("COM0", 1, "/s/by-id/usb-A")]);
    let m = ManagerRig::with(&yaml, LoadedState::default(), None);
    let dev = m.add_device_at("/s/by-id/usb-A", "/dev/ttyA");
    m.mgr.update_devices(&m.snapshot());
    m.mgr.bootstrap_attach();
    m.wait_state("COM0", "ATTACHED");
    assert_eq!(m.mgr.collect_flash_candidates_and_mark().len(), 1);
    // BSL／reset 期間 by-id 短暫消失：manager 送出 DeviceDetached（`_detach_by_id`）。
    m.mgr.update_devices(&[]);
    let s = m.session("COM0"); // GetState 與 DeviceDetached 同一 control lane：以此為處理完成的屏障
    assert_eq!(s_of(&s, "state").as_deref(), Some("ATTACHED"), "flash-critical 期間略過 detach：{s:?}");
    assert_eq!(s.get("last_error"), Some(&PyValue::Null), "略過＝不設 DEVICE_REMOVED");
    assert_eq!(dev.close_count(), 0, "probe 窗口前不得關閉 bridge");
    m.mgr.unmark_flash_critical("COM0");
    let s = m.session("COM0");
    assert_eq!(s_of(&s, "state").as_deref(), Some("ATTACHED"), "略過不是延後：解除標記後也不補做 detach（Python 不重新 reconcile）");
    assert_eq!(dev.close_count(), 0);
}

#[test]
fn flash_critical_device_detached_then_probe_start_matches_python_blackbox() {
    let rig = bb::attached(false);
    // manager 的 collect_flash_candidates_and_mark 只寫 Fence.flash_critical（Inner 鎖內）；此處直接設定等價狀態。
    rig.fence.with(|f| f.flash_critical = true);
    rig.send_control(ControlEvent::DeviceDetached { by_id: DEVICE_BY_ID.into(), reason: DetachReason::Removed });
    bb::settle(&rig);
    assert_eq!(bb::state(&rig), "ATTACHED", "Python _detach_by_id 對 flash-critical 略過：不 drain");
    assert_eq!(rig.dev.close_count(), 0);
    assert_eq!(rig.dev.tx_len(), 0);
    // 之後的 ProbeStart：Python `_BridgeProbe.probe` 照常在原 bridge 上送 sync（`sess.bridge` 仍在）。
    let grant = bb::probe_start(&rig, 7).expect("略過 detach 後 ProbeStart 照常授權");
    assert_eq!(bb::flash_mode(&rig), FlashMode::Probe(7));
    let rep = bb::tx(&rig, grant.epoch, TxClass::FlashProbe, Some(7), b"\x55\x55");
    assert_eq!(rep.result.outcome, TxOutcome::Completed(2), "{rep:?}");
    bb::wait_dut_contains(&rig, 0, b"\x55\x55");
    assert_eq!(bb::wal_tx(&rig), vec![("flash".to_string(), b"\x55\x55".to_vec())]);
    bb::released(&rig, 7);
    rig.fence.with(|f| f.flash_critical = false);
    bb::settle(&rig);
    assert_eq!(bb::flash_mode(&rig), FlashMode::Off);
    assert_eq!(bb::state(&rig), "ATTACHED", "略過不延後：probe 結束後也不補做 detach");
    assert_eq!(rig.dev.close_count(), 0);
}

// ---------------------------------------------------------------------------
// S2-PAR-wp510r R6：G-6 flash 入口讀未確認集合（設計 v1 §1.8、v3 §1.9）
// ---------------------------------------------------------------------------

/// G-6：by_id 的 handle 尚未確認關閉時不得授權 flash probe。Python 以 `_draining_by_ids` 涵蓋「排空中（含 attach 衍生的
/// 退休 bridge）」與「關閉失敗後保留」兩種情況：`collect_flash_candidates_and_mark` 略過、`enter_flashing` 回
/// SESSION_RECOVERING（`origin/main:sw_core/session_manager.py:3282-3288`、`:3317-3326`；作業只在安全回條後移除，
/// `:1346-1368`）。Rust 對應退休 bridge（retired drains）與未確認關閉集合：退休 bridge 排空中拒絕；回條
/// `handle_closed:true` 後恢復為候選；`handle_closed:false`（進未確認集合）持續拒絕。
#[test]
fn wp510r_r6_g6_probe_start_rejects_draining_or_unconfirmed_by_id() {
    for closed in [true, false] {
        let mut h = kit::attached_passthrough();
        let stale = h.opened_bridge("/fake/tty-stale");
        let stale_epoch = stale.epoch;
        h.feed(ControlEvent::BridgeOpened { epoch_hint: 0, result: Ok(stale) });
        let (epoch, stop) = h.pending_stop();
        assert_eq!(epoch, stale_epoch, "前置：多餘的 bridge 退休並開始排空");
        assert_eq!(kit::state(&h), SessionState::Attached);
        assert_eq!(
            kit::probe_start(&mut h, 1),
            Err(FlashReject::ByIdDraining),
            "退休 bridge 排空中不得授權 probe（closed={closed}）"
        );
        assert_eq!(kit::flash_mode(&h), FlashMode::Off);
        h.receipt(epoch, stop).handle_closed(closed).feed();
        if closed {
            kit::probe_start(&mut h, 2).expect("安全回條後恢復為 flash 候選");
            assert_eq!(kit::flash_mode(&h), FlashMode::Probe(2));
        } else {
            assert!(h.unconfirmed.contains(DEVICE_BY_ID), "關閉失敗登記未確認集合");
            assert_eq!(kit::probe_start(&mut h, 2), Err(FlashReject::ByIdDraining), "未確認關閉持續拒絕");
            assert_eq!(kit::flash_mode(&h), FlashMode::Off);
            assert_eq!(kit::state(&h), SessionState::Attached);
        }
    }
}

/// G-6：probe 窗口期間 by_id 轉為排空中（多餘 bridge 退休）時，`FlashSyncMatched`（Python `enter_flashing`）不得進
/// FLASHING（Python 回 SESSION_RECOVERING、service 隨即釋放 gate，`origin/main:sw_core/session_manager.py:3317-3326`、
/// `sw_core/service.py:510-512`）。
#[test]
fn wp510r_r6_g6_matched_rejects_draining_by_id() {
    let mut h = kit::attached_passthrough();
    kit::probe_start(&mut h, 1).expect("probe 1");
    let stale = h.opened_bridge("/fake/tty-stale");
    h.feed(ControlEvent::BridgeOpened { epoch_hint: 0, result: Ok(stale) });
    assert_eq!(kit::matched(&mut h, 1), Err(FlashReject::ByIdDraining));
    assert_eq!(kit::state(&h), SessionState::Attached, "不得進 FLASHING");
    assert_ne!(kit::flash_mode(&h), FlashMode::Flashing);
}
