//! Slice 2.1 Task 11：`SessionCore`（sans-IO 狀態機）與 `SessionActor` 的紅燈測試（plan 06 Task 11，§5.3 的 2.1 列）。
//!
//! 紅燈階段：所有行為尚未實作，失敗應為 `not implemented: slice21 T11 …`（執行期 panic），而非編譯錯誤。
//! 測試只經 `support::CoreHarness` 驅動，不起 thread（actor 相關者另見 `actor_event_panic_isolated`）。

#![cfg(feature = "test-fakes")]

mod support;

use std::sync::Arc;
use std::time::Duration;

use sw_core::actor::spawn_actor;
use sw_core::drain::StopId;
use sw_core::login_fsm::LoginOutcome;
use sw_core::mailbox::{ControlEvent, DataEvent, DetachReason, Mailbox, TimerEvent};
use sw_core::op_runner::OpSpec;
use sw_core::session_core::{Effect, ManagerNote, SessionRequest, SessionState};
use sw_core::tx_writer::OpId;
use sw_pal::traits::Clock;
use sw_proto::pyjson::{PyDict, PyValue};
use support::*;

// ---------------------------------------------------------------------------
// 共用輔助
// ---------------------------------------------------------------------------

fn submit(cmd: &str) -> SessionRequest {
    SessionRequest::Submit(agent_submit(cmd))
}

fn pget(d: &PyDict, k: &str) -> PyValue {
    d.get(k).cloned().unwrap_or(PyValue::Null)
}

fn keys(d: &PyDict) -> Vec<String> {
    d.0.keys().cloned().collect()
}

fn activity(h: &CoreHarness) -> String {
    pget(&h.public(), "activity_classification")
        .as_str()
        .unwrap_or("")
        .to_string()
}

/// 取出同次效果中的 `BeginDrain`（回傳 `(epoch, stop_id)`）。
fn begin_drain(effs: &[Effect]) -> (u64, StopId) {
    effs.iter()
        .find_map(|e| match e {
            Effect::BeginDrain { stop_id, epoch } => Some((*epoch, *stop_id)),
            _ => None,
        })
        .expect("應產生 BeginDrain")
}

fn has_start_op(effs: &[Effect]) -> bool {
    effs.iter().any(|e| matches!(e, Effect::StartOp { .. }))
}

fn has_start_login(effs: &[Effect]) -> bool {
    effs.iter().any(|e| matches!(e, Effect::StartLogin { .. }))
}

fn has_session_ready(effs: &[Effect]) -> bool {
    effs.iter()
        .any(|e| matches!(e, Effect::Notify(ManagerNote::SessionReady { .. })))
}

fn has_open_bridge(effs: &[Effect]) -> bool {
    effs.iter().any(|e| matches!(e, Effect::OpenBridge { .. }))
}

/// 目前 `conn_epoch` 的 IoError（觸發 RECOVERING 與 drain）。
fn io_error_event(epoch: u64) -> ControlEvent {
    ControlEvent::UartIoError {
        epoch,
        reason: "SERIAL_READ:5".into(),
    }
}

/// 以 `Release` 走完兩階段 drain 到 RELEASED（需先 READY）。
fn to_released(h: &mut CoreHarness) {
    h.to_ready();
    let (_rx, effs) = h.rpc_deferred(SessionRequest::Release {
        source: "agent".into(),
        reason: PyValue::Str("test".into()),
    });
    let (ep, stop) = begin_drain(&effs);
    h.feed(ControlEvent::WorkerStopped(receipt(ep, stop.0)));
    assert_eq!(h.core.state(), SessionState::Released, "to_released");
}

// ---------------------------------------------------------------------------
// 起始測試（plan Task 11 Step 1 的前導片段）
// ---------------------------------------------------------------------------

#[test]
fn recovering_submit_is_session_recovering_sc01() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let e = h.epoch();
    h.feed(io_error_event(e));
    assert_eq!(h.core.state(), SessionState::Recovering);
    assert_eq!(error_code(&h.rpc(submit("ls"))), "SESSION_RECOVERING");
}

#[test]
fn stale_epoch_io_error_is_ignored() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let old = h.epoch() - 1;
    assert!(h.feed(io_error_event(old)).is_empty());
    assert_eq!(h.core.state(), SessionState::Ready);
}

#[test]
fn wall_clock_jump_does_not_move_deadlines() {
    // Review Focus 4：wall 倒退到 1970，boot quiet 期限只依 monotonic 計。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.rx(b"U-Boot 2024.01\r\n");
    let q0 = h.public().get("boot_quiet_remaining_s").cloned();
    assert!(
        matches!(q0, Some(PyValue::Float(_))),
        "banner 後 boot quiet 應啟用"
    );
    h.set_wall_us(0);
    assert_eq!(h.public().get("boot_quiet_remaining_s").cloned(), q0);
    h.advance(Duration::from_secs(181));
    assert_eq!(
        h.public().get("boot_quiet_remaining_s"),
        Some(&PyValue::Null)
    );
}

// ---------------------------------------------------------------------------
// §5.3 狀態表（2.1 適用列）
// ---------------------------------------------------------------------------

#[test]
fn detached_device_attached_opens_and_attaching() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let effs = h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    assert!(effs
        .iter()
        .any(|e| matches!(e, Effect::OpenBridge { precheck_holders: false, .. })));
    assert_eq!(h.core.state(), SessionState::Attaching);
    assert_eq!(pget(&h.public(), "last_error"), PyValue::Null);
}

#[test]
fn detached_submit_not_ready() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let r = h.rpc(submit("ls"));
    assert_eq!(error_code(&r), "SESSION_NOT_READY");
    let d = r.expect("ok:false 形狀");
    assert_eq!(keys(&d), ["ok", "error_code", "session"]);
    assert_eq!(pget(&d, "ok"), PyValue::Bool(false));
}

#[test]
fn attaching_submit_not_ready() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    assert_eq!(h.core.state(), SessionState::Attaching);
    let r = h.rpc(submit("ls"));
    assert_eq!(error_code(&r), "SESSION_NOT_READY");
    assert_eq!(keys(&r.expect("ok:false 形狀")), ["ok", "error_code", "session"]);
}

#[test]
fn bridge_opened_allocates_epoch_once() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    assert_eq!(h.core.generations().conn_epoch, None);
    h.bridge_ok();
    assert!(h.core.generations().conn_epoch.is_some());
    // shell 類：bridge 採用於登入結果到達時才遞增（下一個測試驗證）。
    assert_eq!(h.core.generations().bridge_generation, 0);
}

#[test]
fn login_ok_ready_and_bridge_generation_plus_one() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.bridge_ok();
    let (epoch, fsm_gen) = (h.epoch(), h.core.generations().fsm_gen);
    let effs = h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen,
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    assert_eq!(h.core.state(), SessionState::Ready);
    assert_eq!(h.core.generations().bridge_generation, 1);
    assert_ne!(pget(&h.public(), "last_ready_at"), PyValue::Null);
    assert!(has_session_ready(&effs));
}

#[test]
fn login_not_ready_attached_also_bumps_bridge_generation() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.bridge_ok();
    let (epoch, fsm_gen) = (h.epoch(), h.core.generations().fsm_gen);
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen,
        result: LoginOutcome::NotReady("PROMPT_UNAVAILABLE".into()),
        after_rx_seq: None,
    });
    assert_eq!(h.core.state(), SessionState::Attached);
    assert_eq!(h.core.generations().bridge_generation, 1);
}

#[test]
fn passthrough_and_unresolved_bump_on_adoption() {
    // passthrough（LoginPlan::None）：bridge 採用即 ATTACHED。
    let mut p = CoreHarness::new(PASSTHROUGH_PROFILE);
    p.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    p.bridge_ok();
    assert_eq!(p.core.state(), SessionState::Attached);
    assert_eq!(p.core.generations().bridge_generation, 1);

    // CredentialsUnresolved（env_file 不存在）：採用即 ATTACHED。
    let mut c = CoreHarness::new(CRED_PROFILE);
    c.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    c.bridge_ok();
    assert_eq!(c.core.state(), SessionState::Attached);
    assert_eq!(c.core.generations().bridge_generation, 1);
}

#[test]
fn login_failed_detach_does_not_bump() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.bridge_ok();
    let (epoch, fsm_gen) = (h.epoch(), h.core.generations().fsm_gen);
    let effs = h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen,
        result: LoginOutcome::Failed("LOGIN_PROMPT_TIMEOUT".into()),
        after_rx_seq: None,
    });
    let (ep, stop) = begin_drain(&effs);
    h.feed(ControlEvent::WorkerStopped(receipt(ep, stop.0)));
    assert_eq!(h.core.state(), SessionState::Detached);
    assert_eq!(h.core.generations().bridge_generation, 0);
}

#[test]
fn login_finished_stale_fsm_gen_ignored() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.bridge_ok();
    let (epoch, fsm_gen) = (h.epoch(), h.core.generations().fsm_gen);
    let effs = h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: fsm_gen + 99,
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    assert!(effs.is_empty(), "stale fsm_gen 不得產生效果");
    assert_eq!(h.core.state(), SessionState::Attaching);
    assert_eq!(h.core.generations().bridge_generation, 0);
}

#[test]
fn attached_non_capable_submit_profile_not_command_capable() {
    // 非 passthrough 的空 ready_probe profile 維持基線錯誤碼。
    let mut h = CoreHarness::new(SHELL_NO_PROBE_PROFILE);
    h.to_attached(None);
    let r = h.rpc(submit("ls"));
    assert_eq!(error_code(&r), "PROFILE_NOT_COMMAND_CAPABLE");
    let d = r.expect("ok:false 形狀");
    assert!(d.get("hint").is_some(), "應附 hint");
    assert!(d.get("session").is_some(), "應附 session");
}

#[test]
fn attached_passthrough_submit_not_ready_with_ready_in() {
    // SC-35：passthrough ATTACHED 12 s 後 submit → SESSION_NOT_READY＋passthrough_ready_in_s。
    let mut h = CoreHarness::new(PASSTHROUGH_PROFILE);
    h.to_attached(None);
    h.advance(Duration::from_secs(12));
    let (r, effs) = h.rpc_full(submit("ls"));
    let d = r.expect("ok:false 形狀");
    assert_eq!(
        keys(&d),
        ["ok", "error_code", "passthrough_ready_in_s", "session"]
    );
    assert_eq!(pget(&d, "ok"), PyValue::Bool(false));
    assert_eq!(pget(&d, "error_code").as_str(), Some("SESSION_NOT_READY"));
    assert_eq!(pget(&d, "passthrough_ready_in_s"), PyValue::Float(18.0));
    assert!(!has_start_op(&effs), "未達完成條件前不得出列命令");
}

#[test]
fn passthrough_attach_no_login_no_probe() {
    // SC-35：passthrough 與 uboot 類 passthrough（ready_probe 非空）皆不登入、不 probe、零 TX。
    for yaml in [PASSTHROUGH_PROFILE, UBOOT_PASSTHROUGH_PROFILE] {
        let mut h = CoreHarness::new(yaml);
        let mut all = h.feed(ControlEvent::DeviceAttached {
            by_id: DEVICE_BY_ID.into(),
            real_path: REAL_PATH.into(),
        });
        all.extend(h.bridge_ok());
        assert_eq!(h.core.state(), SessionState::Attached);
        assert_eq!(pget(&h.public(), "last_error"), PyValue::Null);
        assert_eq!(h.core.generations().bridge_generation, 1);
        assert!(!has_start_login(&all), "passthrough 不得登入");
        assert!(!has_start_op(&all), "passthrough 不得送任何 probe／命令（零 TX）");
    }
}

#[test]
fn passthrough_quiet_tick_ready_at_30s() {
    let mut h = CoreHarness::new(PASSTHROUGH_PROFILE);
    h.to_attached(None);
    let mut all = h.advance(Duration::from_millis(29_900));
    assert_eq!(h.core.state(), SessionState::Attached, "29.9 s 仍 ATTACHED");
    all.extend(h.advance(Duration::from_millis(100)));
    assert_eq!(h.core.state(), SessionState::Ready, "30 s 的 Tick 轉 READY");
    assert_eq!(pget(&h.public(), "last_error"), PyValue::Null);
    assert!(has_session_ready(&all));
    assert!(!has_start_login(&all), "零 TX：不得登入");
    assert!(!has_start_op(&all), "零 TX：不得送命令");
    let r = h.rpc(submit("ls")).expect("READY 後 submit 應回 ok");
    assert_eq!(pget(&r, "ok"), PyValue::Bool(true));
}

#[test]
fn passthrough_rx_restarts_quiet_window() {
    // quiet_since = max(進入 ATTACHED, 最後 RX)；TX 不重算視窗。
    let mut h = CoreHarness::new(PASSTHROUGH_PROFILE);
    h.to_attached(None);
    h.advance(Duration::from_secs(20));
    h.rx(b"log\r\n"); // t=20 s
    h.advance(Duration::from_secs(20)); // t=40 s
    h.feed(ControlEvent::UartTxCompleted {
        epoch: h.epoch(),
        op_id: OpId(1),
        confirmed_bytes: 1,
        unknown: false,
    }); // TX 不得重算視窗
    h.advance(Duration::from_millis(9_900)); // t=49.9 s
    assert_eq!(h.core.state(), SessionState::Attached);
    h.advance(Duration::from_millis(100)); // t=50 s
    assert_eq!(h.core.state(), SessionState::Ready);
}

#[test]
fn passthrough_quiet_blocked_by_drain_flash_released() {
    // (a) drain 進行中（Release 等待 receipt）→ RECOVERING；(b) FLASHING；(c) RELEASED。
    // 三者在 ATTACHED 安靜 60 s 後皆不得升 READY，且不得有 SessionReady 通知。

    // (a)
    {
        let mut h = CoreHarness::new(PASSTHROUGH_PROFILE);
        h.to_attached(None);
        let (_rx, _effs) = h.rpc_deferred(SessionRequest::Release {
            source: "agent".into(),
            reason: PyValue::Str("test".into()),
        });
        let effs = h.advance(Duration::from_secs(60));
        assert_eq!(h.core.state(), SessionState::Recovering, "(a) drain 中不升 READY");
        assert!(!has_session_ready(&effs), "(a) 不得通知 SessionReady");
    }
    // (b)
    {
        let mut h = CoreHarness::new(PASSTHROUGH_PROFILE);
        h.to_attached(None);
        h.force_state(SessionState::Flashing);
        let effs = h.advance(Duration::from_secs(60));
        assert_eq!(h.core.state(), SessionState::Flashing, "(b) FLASHING 不升 READY");
        assert!(!has_session_ready(&effs), "(b) 不得通知 SessionReady");
    }
    // (c)
    {
        let mut h = CoreHarness::new(PASSTHROUGH_PROFILE);
        h.to_attached(None);
        let (rx, effs) = h.rpc_deferred(SessionRequest::Release {
            source: "agent".into(),
            reason: PyValue::Str("test".into()),
        });
        let (ep, stop) = begin_drain(&effs);
        h.feed(ControlEvent::WorkerStopped(receipt(ep, stop.0)));
        assert!(rx.try_recv().is_ok(), "(c) release 完成應回覆");
        assert_eq!(h.core.state(), SessionState::Released);
        let effs = h.advance(Duration::from_secs(60));
        assert_eq!(h.core.state(), SessionState::Released, "(c) RELEASED 不升 READY");
        assert!(!has_session_ready(&effs), "(c) 不得通知 SessionReady");
    }
}

#[test]
fn passthrough_ready_not_demoted_by_rx_or_banner() {
    let mut h = CoreHarness::new(PASSTHROUGH_PROFILE);
    h.to_attached(None);
    h.advance(Duration::from_secs(30));
    assert_eq!(h.core.state(), SessionState::Ready);
    h.rx(b"U-Boot 2023.01\r\nHit any key to stop autoboot:  3\r\n");
    assert_eq!(h.core.state(), SessionState::Ready, "banner 不得降級");
    let d = h.public();
    assert_eq!(pget(&d, "boot_quiet_remaining_s"), PyValue::Null);
    assert_eq!(pget(&d, "ready_reconfirm_pending"), PyValue::Bool(false));
    let r = h.rpc(submit("ls")).expect("ok 形狀");
    assert_eq!(pget(&r, "ok"), PyValue::Bool(true));
    assert_eq!(pget(&r, "status").as_str(), Some("accepted"));
}

#[test]
fn passthrough_io_error_recovers_and_requiets() {
    // SC-35：IO 錯誤後 drain → 重新 attach（ATTACHING→ATTACHED，無 probe、零 TX），再安靜 30 s 才回 READY。
    let mut h = CoreHarness::new(PASSTHROUGH_PROFILE);
    h.to_attached(None);
    h.advance(Duration::from_secs(30));
    assert_eq!(h.core.state(), SessionState::Ready);

    let effs = h.feed(io_error_event(h.epoch()));
    assert_eq!(h.core.state(), SessionState::Recovering);
    let (ep, stop) = begin_drain(&effs);
    let mut all = h.feed(ControlEvent::WorkerStopped(receipt(ep, stop.0)));

    all.extend(h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    }));
    assert_eq!(h.core.state(), SessionState::Attaching);
    all.extend(h.bridge_ok());
    assert_eq!(h.core.state(), SessionState::Attached);
    assert!(!has_start_login(&all), "passthrough 重新 attach 不得 probe／登入");
    assert!(!has_start_op(&all), "零 TX");

    all.extend(h.advance(Duration::from_secs(30)));
    assert_eq!(h.core.state(), SessionState::Ready, "再安靜 30 s 才回 READY");
}

#[test]
fn attached_capable_submit_not_ready() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_attached(None);
    assert_eq!(error_code(&h.rpc(submit("ls"))), "SESSION_NOT_READY");
}

#[test]
fn ready_submit_admitted() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (r, effs) = h.rpc_full(submit("ls"));
    let d = r.expect("ok:true 形狀");
    assert_eq!(pget(&d, "ok"), PyValue::Bool(true));
    assert_eq!(pget(&d, "status").as_str(), Some("accepted"));
    assert_eq!(pget(&d, "session_id").as_str(), Some("t:COM0"));
    assert!(d.get("cmd_id").is_some(), "應附 cmd_id");
    assert!(has_start_op(&effs), "前景空閒時應出列 StartOp");
}

#[test]
fn queue_full_at_256() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    for i in 0..256 {
        let r = h
            .rpc(submit("ls"))
            .unwrap_or_else(|e| panic!("第 {i} 筆 RPC 應回覆：{}", e.message));
        assert_eq!(pget(&r, "ok"), PyValue::Bool(true), "第 {i} 筆應被接受");
    }
    let r = h.rpc(submit("ls"));
    assert_eq!(error_code(&r), "SESSION_QUEUE_FULL", "第 257 筆應拒絕");
}

#[test]
fn released_submit_not_ready() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    to_released(&mut h);
    assert_eq!(error_code(&h.rpc(submit("ls"))), "SESSION_NOT_READY");
}

#[test]
fn released_device_attached_is_noop() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    to_released(&mut h);
    let effs = h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    assert!(!has_open_bridge(&effs), "RELEASED 不得開 bridge");
    assert_eq!(h.core.state(), SessionState::Released);
}

#[test]
fn flashing_submit_flashing_busy_sc02() {
    // 2.1 不會自然進入 FLASHING；此列只驗證碼映射。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.force_state(SessionState::Flashing);
    assert_eq!(error_code(&h.rpc(submit("ls"))), "FLASHING_BUSY");
}

#[test]
fn recovering_interactive_open_session_recovering() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let e = h.epoch();
    h.feed(io_error_event(e));
    let r = h.rpc(SessionRequest::InteractiveOpen {
        owner: "agent".into(),
        timeout_s: 10.0,
        command: "ls".into(),
        allow_attached: false,
    });
    assert_eq!(error_code(&r), "SESSION_RECOVERING");
}

#[test]
fn device_detached_starts_drain_removed() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let effs = h.feed(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: DetachReason::Removed,
    });
    let _ = begin_drain(&effs);
    assert_eq!(h.core.state(), SessionState::Recovering);
    let (frozen, active) = h.fence.with(|s| (s.frozen, s.active_epoch));
    assert!(frozen, "drain 開始時 fence 應凍結");
    assert_eq!(active, None, "drain 開始時 fence 應清除 active_epoch");
}

#[test]
fn stale_timer_ignored() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let before = h.public();
    // epoch 0 與 fsm_gen 0 皆不是現行世代（bridge 已於 to_ready 開出）。
    let effs = h.feed(ControlEvent::Timer(TimerEvent::BootQuietExpired {
        epoch: 0,
        fsm_gen: 0,
    }));
    assert!(effs.is_empty(), "過期 timer 不得產生效果");
    assert_eq!(h.core.state(), SessionState::Ready);
    assert_eq!(h.public(), before, "過期 timer 不得改動公開狀態");
}

#[test]
fn stale_rx_chunk_no_state_effect() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let before = h.public();
    let old = h.epoch() - 1;
    let effs = h.data(DataEvent::UartRxChunk {
        epoch: old,
        rx_seq: 1,
        wal_seq: 1,
        bytes: b"U-Boot 2024.01\r\n".to_vec(),
    });
    assert!(effs.is_empty(), "舊 epoch RX 不得產生狀態效果");
    let after = h.public();
    assert_eq!(pget(&after, "boot_quiet_remaining_s"), PyValue::Null, "quiet 不得啟動");
    assert_eq!(pget(&after, "last_rx_at"), pget(&before, "last_rx_at"), "last_rx_at 不得改變");
}

#[test]
fn public_dict_key_order_with_sc04_fields() {
    // 02 §6.1 插入順序；POSIX 不含 console_endpoint；末尾為 SC-04 的 drain 兩欄。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let d = h.public();
    let expected = [
        "session_id",
        "profile",
        "com",
        "alias",
        "act_no",
        "device_by_id",
        "platform",
        "profile_source",
        "command_capable",
        "state",
        "last_error",
        "last_error_detail",
        "detached_at",
        "last_ready_at",
        "vtty",
        "attached_real_path",
        "bridge_generation",
        "recovering",
        "interactive_session_id",
        "foreground_busy",
        "fg_cmd_expected_duration_s",
        "console_count",
        "last_state_change_at",
        "last_rx_at",
        "last_tx_at",
        "last_probe_at",
        "reprobe_attempts",
        "next_reprobe_at",
        "reprobe_exhausted",
        "boot_quiet_remaining_s",
        "ready_reconfirm_pending",
        "ready_reconfirm_failed",
        "ready_reconfirm_remaining_s",
        "idle_for_ms",
        "last_rx_age_s",
        "last_tx_age_s",
        "rx_bytes_last_10s",
        "rx_rate_bps",
        "outstanding_commands",
        "activity_classification",
        "released_by",
        "released_at",
        "released_reason",
        "capture",
        "drain_stalled",
        "drain_timeout_epoch",
    ];
    assert_eq!(keys(&d), expected);
    assert_eq!(pget(&d, "drain_stalled"), PyValue::Bool(false));
    assert_eq!(pget(&d, "drain_timeout_epoch"), PyValue::Null);
}

#[test]
fn public_console_endpoint_null_when_ctx_requests() {
    use sw_core::session_core::PublicCtx;
    let h = {
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        h
    };
    let ctx = PublicCtx {
        console_count: 0,
        vtty: None,
        console_endpoint: Some(None),
    };
    let d = h.core.public(h.clock.mono_ns(), &ctx);
    let ks = keys(&d);
    assert_eq!(ks.last().map(String::as_str), Some("console_endpoint"));
    assert_eq!(pget(&d, "console_endpoint"), PyValue::Null);
}

#[test]
fn activity_classification_thresholds() {
    // 以最後 RX 計：<5 s active；<60 s idle-healthy；否則 quiet-suspicious。
    for (ms, want) in [
        (4_900u64, "active"),
        (5_000, "idle-healthy"),
        (59_900, "idle-healthy"),
        (60_000, "quiet-suspicious"),
    ] {
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        h.rx(b"x");
        h.advance(Duration::from_millis(ms));
        assert_eq!(activity(&h), want, "RX 後 {ms} ms");
    }

    // 只有 TX（沒有 RX）：以 TX 時間計。
    {
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        let epoch = h.epoch();
        h.feed(ControlEvent::UartTxCompleted {
            epoch,
            op_id: OpId(1),
            confirmed_bytes: 3,
            unknown: false,
        });
        h.advance(Duration::from_millis(4_900));
        assert_eq!(activity(&h), "active", "只有 TX 時以 TX 時間計");
    }

    // RX 與 TX 都從未發生。
    {
        let mut h = CoreHarness::new(SHELL_PROFILE);
        h.to_ready();
        assert_eq!(activity(&h), "newly-attached");
    }

    // 非 READY／ATTACHED。
    {
        let h = CoreHarness::new(SHELL_PROFILE);
        assert_eq!(activity(&h), "offline");
    }
}

#[test]
fn last_error_change_clears_detail() {
    // CRED_PROFILE（env_file 不存在）→ CREDENTIALS_UNRESOLVED 並附 detail；之後 last_error 改變時 detail 必須清空。
    let mut h = CoreHarness::new(CRED_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.bridge_ok();
    let before = pget(&h.public(), "last_error");
    assert_ne!(before, PyValue::Null, "unresolved 應設定 last_error");
    assert_ne!(
        pget(&h.public(), "last_error_detail"),
        PyValue::Null,
        "unresolved 應附 last_error_detail"
    );
    let e = h.epoch();
    h.feed(io_error_event(e));
    assert_ne!(pget(&h.public(), "last_error"), before, "last_error 應改變");
    assert_eq!(pget(&h.public(), "last_error_detail"), PyValue::Null, "改變後 detail 清空");
}

#[test]
fn tick_expires_reconfirm_without_tx() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.rx(b"U-Boot 2024.01\r\n"); // 啟動 boot quiet，quiet 結束後進入 reconfirm pending
    h.advance(Duration::from_secs(181));
    assert_eq!(pget(&h.public(), "ready_reconfirm_pending"), PyValue::Bool(true));
    let effs = h.advance(Duration::from_secs(301));
    let d = h.public();
    assert_eq!(pget(&d, "ready_reconfirm_failed"), PyValue::Bool(true));
    assert_eq!(pget(&d, "ready_reconfirm_pending"), PyValue::Bool(false));
    assert!(!has_start_op(&effs), "reconfirm 逾時不得送 TX");
    assert!(!has_start_login(&effs), "reconfirm 逾時不得登入");
}

#[test]
fn wall_clock_jump_param() {
    // Review Focus 4：quiet、reconfirm、lease、drain、command timeout 五種期限，wall 跳 ±1 h 後皆不動；
    // 只推進 mono 到期限前 1 ms 不觸發、到期限後觸發；`*_at` 字串反映新 wall。
    for jump_s in [-3_600i64, 3_600] {
        let delta_us = jump_s * 1_000_000;

        // quiet（180 s）
        {
            let mut h = CoreHarness::new(SHELL_PROFILE);
            h.to_ready();
            h.rx(b"U-Boot 2024.01\r\n");
            let before = h.public().get("boot_quiet_remaining_s").cloned();
            h.jump_wall_us(delta_us);
            assert_eq!(h.public().get("boot_quiet_remaining_s").cloned(), before, "quiet {jump_s}");
            h.advance(Duration::from_millis(179_999));
            assert!(
                matches!(h.public().get("boot_quiet_remaining_s"), Some(PyValue::Float(_))),
                "quiet 到期前 1 ms 不得觸發 ({jump_s})"
            );
            h.advance(Duration::from_millis(1));
            assert_eq!(
                h.public().get("boot_quiet_remaining_s"),
                Some(&PyValue::Null),
                "quiet 到期後應觸發 ({jump_s})"
            );
        }

        // reconfirm（300 s，quiet 結束後起算）
        {
            let mut h = CoreHarness::new(SHELL_PROFILE);
            h.to_ready();
            h.rx(b"U-Boot 2024.01\r\n");
            h.advance(Duration::from_secs(180));
            let before = h.public().get("ready_reconfirm_remaining_s").cloned();
            h.jump_wall_us(delta_us);
            assert_eq!(
                h.public().get("ready_reconfirm_remaining_s").cloned(),
                before,
                "reconfirm {jump_s}"
            );
            h.advance(Duration::from_millis(299_999));
            assert_eq!(pget(&h.public(), "ready_reconfirm_failed"), PyValue::Bool(false));
            h.advance(Duration::from_millis(1));
            assert_eq!(pget(&h.public(), "ready_reconfirm_failed"), PyValue::Bool(true), "reconfirm {jump_s}");
        }

        // lease（20 s，agent-only Interactive Lease；Task 14 承接，此處驗證 public 欄位）
        {
            let mut h = CoreHarness::new(SHELL_PROFILE);
            h.to_ready();
            let r = h.rpc(SessionRequest::InteractiveOpen {
                owner: "agent".into(),
                timeout_s: 20.0,
                command: "".into(),
                allow_attached: false,
            });
            assert!(r.is_ok(), "lease 開啟應回應 ({jump_s})");
            let before = pget(&h.public(), "interactive_session_id");
            assert_ne!(before, PyValue::Null);
            h.jump_wall_us(delta_us);
            assert_eq!(pget(&h.public(), "interactive_session_id"), before, "lease {jump_s}");
            h.advance(Duration::from_millis(19_999));
            assert_ne!(pget(&h.public(), "interactive_session_id"), PyValue::Null, "lease 到期前 1 ms");
            h.advance(Duration::from_millis(1));
            assert_eq!(pget(&h.public(), "interactive_session_id"), PyValue::Null, "lease 到期後 ({jump_s})");
        }

        // drain（3000 ms）
        {
            let mut h = CoreHarness::new(SHELL_PROFILE);
            h.to_ready();
            h.jump_wall_us(delta_us);
            h.feed(ControlEvent::DeviceDetached {
                by_id: DEVICE_BY_ID.into(),
                reason: DetachReason::Removed,
            });
            let want_at = format!("wall-us-{}", h.wall_us());
            assert_eq!(
                pget(&h.public(), "last_state_change_at").as_str(),
                Some(want_at.as_str()),
                "*_at 應反映新 wall ({jump_s})"
            );
            assert_eq!(pget(&h.public(), "drain_stalled"), PyValue::Bool(false));
            h.advance(Duration::from_millis(2_999));
            assert_eq!(pget(&h.public(), "drain_stalled"), PyValue::Bool(false), "drain 3000 ms 前不得逾時");
            h.advance(Duration::from_millis(1));
            assert_eq!(pget(&h.public(), "drain_stalled"), PyValue::Bool(true), "drain 3000 ms 後逾時 ({jump_s})");
        }

        // command timeout（10 s）：期限由 OpRunner 以 monotonic 計；core 驗證 grant 的 timeout_s 不受 wall 影響。
        {
            let mut h = CoreHarness::new(SHELL_PROFILE);
            h.to_ready();
            let (r, effs) = h.rpc_full(submit("ls"));
            assert!(r.is_ok());
            let timeout = effs.iter().find_map(|e| match e {
                Effect::StartOp { spec: OpSpec::Command(q), .. } => Some(q.req.timeout_s),
                _ => None,
            });
            assert_eq!(timeout, Some(10.0), "命令 grant 應帶 timeout 10 s");
            h.jump_wall_us(delta_us);
            assert_eq!(pget(&h.public(), "outstanding_commands"), PyValue::Int(sw_proto::pyjson::PyInt::Small(1)));
            h.advance(Duration::from_millis(9_999));
            assert_eq!(pget(&h.public(), "outstanding_commands"), PyValue::Int(sw_proto::pyjson::PyInt::Small(1)));
        }
    }
}

#[test]
fn actor_event_panic_isolated() {
    // 注入 panic 的 seam：`SessionCore::arm_panic_on_next_event`（test-fakes 限定；Task 11 實作）。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.core.arm_panic_on_next_event();
    let clock: Arc<dyn Clock> = h.clock.clone();
    let core = h.core;
    let handle = spawn_actor(core, Mailbox::new(256, 1024), Box::new(ReplyExec), clock);

    let err = handle
        .request(SessionRequest::GetState, Duration::from_secs(2))
        .expect_err("panic 的事件應回 Err");
    assert!(
        err.message.starts_with("internal error: "),
        "回應應為 internal error：{}",
        err.message
    );

    let ok = handle
        .request(SessionRequest::GetState, Duration::from_secs(2))
        .expect("panic 後 actor 應續行");
    assert_eq!(pget(&ok, "ok"), PyValue::Bool(true));
}
