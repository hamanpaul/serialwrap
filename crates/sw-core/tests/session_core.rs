//! Slice 2.1 Task 11：`SessionCore`（sans-IO 狀態機）與 `SessionActor` 的紅燈測試（plan 06 Task 11，§5.3 的 2.1 列）。
//!
//! 紅燈階段：所有行為尚未實作，失敗應為 `not implemented: slice21 T11 …`（執行期 panic），而非編譯錯誤。
//! 測試只經 `support::CoreHarness` 驅動，不起 thread（actor 相關者另見 `actor_event_panic_isolated`）。

#![cfg(feature = "test-fakes")]

mod support;

use std::sync::Arc;
use std::time::Duration;

use support::*;
use sw_core::actor::spawn_actor;
use sw_core::drain::StopId;
use sw_core::login_fsm::LoginOutcome;
use sw_core::mailbox::{ControlEvent, DataEvent, DetachReason, Mailbox, OpenFailure, TimerEvent};
use sw_core::op_runner::{OpOutcome, OpSpec};
use sw_core::session_core::{Effect, ManagerNote, SessionRequest, SessionState};
use sw_core::tx_writer::{Fence, FenceReject, OpId, TxClass, TxOpLedger, TxPermit};
use sw_pal::traits::{Clock, HolderReport};
use sw_proto::pyjson::{PyDict, PyValue};

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
    assert!(effs.iter().any(|e| matches!(
        e,
        Effect::OpenBridge {
            precheck_holders: false,
            ..
        }
    )));
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
    assert_eq!(
        keys(&r.expect("ok:false 形狀")),
        ["ok", "error_code", "session"]
    );
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
        assert!(
            !has_start_op(&all),
            "passthrough 不得送任何 probe／命令（零 TX）"
        );
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
        assert_eq!(
            h.core.state(),
            SessionState::Recovering,
            "(a) drain 中不升 READY"
        );
        assert!(!has_session_ready(&effs), "(a) 不得通知 SessionReady");
    }
    // (b)
    {
        let mut h = CoreHarness::new(PASSTHROUGH_PROFILE);
        h.to_attached(None);
        h.force_state(SessionState::Flashing);
        let effs = h.advance(Duration::from_secs(60));
        assert_eq!(
            h.core.state(),
            SessionState::Flashing,
            "(b) FLASHING 不升 READY"
        );
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
        assert_eq!(
            h.core.state(),
            SessionState::Released,
            "(c) RELEASED 不升 READY"
        );
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
    assert!(
        !has_start_login(&all),
        "passthrough 重新 attach 不得 probe／登入"
    );
    assert!(!has_start_op(&all), "零 TX");

    all.extend(h.advance(Duration::from_secs(30)));
    assert_eq!(
        h.core.state(),
        SessionState::Ready,
        "再安靜 30 s 才回 READY"
    );
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
fn device_removed_during_attach_uses_python_last_error() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    assert_eq!(h.core.state(), SessionState::Attaching);
    h.feed(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: DetachReason::Removed,
    });
    assert_eq!(
        pget(&h.public(), "last_error"),
        PyValue::from("DEVICE_REMOVED_DURING_ATTACH")
    );
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
    assert_eq!(
        pget(&after, "boot_quiet_remaining_s"),
        PyValue::Null,
        "quiet 不得啟動"
    );
    assert_eq!(
        pget(&after, "last_rx_at"),
        pget(&before, "last_rx_at"),
        "last_rx_at 不得改變"
    );
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
    // 先建立合法的非空 detail，再改變 last_error，驗證舊 detail 不殘留。
    let mut h = CoreHarness::new(CRED_PROFILE);
    h.core
        .set_last_error_for_test(Some("first"), Some("detail"));
    assert_eq!(
        pget(&h.public(), "last_error_detail"),
        PyValue::from("detail")
    );
    h.core.set_last_error_for_test(Some("second"), None);
    assert_eq!(pget(&h.public(), "last_error"), PyValue::from("second"));
    assert_eq!(
        pget(&h.public(), "last_error_detail"),
        PyValue::Null,
        "last_error 改變後必須清除既有 detail"
    );
}

#[test]
fn tick_expires_reconfirm_without_tx() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.rx(b"U-Boot 2024.01\r\n"); // 啟動 boot quiet，quiet 結束後進入 reconfirm pending
    h.advance(Duration::from_secs(181));
    assert_eq!(
        pget(&h.public(), "ready_reconfirm_pending"),
        PyValue::Bool(true)
    );
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
            assert_eq!(
                h.public().get("boot_quiet_remaining_s").cloned(),
                before,
                "quiet {jump_s}"
            );
            h.advance(Duration::from_millis(179_999));
            assert!(
                matches!(
                    h.public().get("boot_quiet_remaining_s"),
                    Some(PyValue::Float(_))
                ),
                "quiet 到期前 1 ms 不得觸發 ({jump_s})"
            );
            h.advance(Duration::from_millis(1));
            assert_eq!(
                h.public().get("boot_quiet_remaining_s"),
                Some(&PyValue::Null),
                "quiet 到期後應觸發 ({jump_s})"
            );
        }

        // reconfirm（300 s，從首次 banner 起算；quiet 結束後剩 120 s）
        {
            let mut h = CoreHarness::new(SHELL_PROFILE);
            h.to_ready();
            h.rx(b"U-Boot 2024.01\r\n");
            h.advance(Duration::from_secs(180));
            let before = h.public().get("ready_reconfirm_remaining_s").cloned();
            assert_eq!(
                before,
                Some(PyValue::Float(120.0)),
                "deadline 從首次 banner 起算"
            );
            h.jump_wall_us(delta_us);
            assert_eq!(
                h.public().get("ready_reconfirm_remaining_s").cloned(),
                before,
                "reconfirm {jump_s}"
            );
            h.advance(Duration::from_millis(119_999));
            assert_eq!(
                pget(&h.public(), "ready_reconfirm_failed"),
                PyValue::Bool(false)
            );
            h.advance(Duration::from_millis(1));
            assert_eq!(
                pget(&h.public(), "ready_reconfirm_failed"),
                PyValue::Bool(true),
                "reconfirm {jump_s}"
            );
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
            assert_eq!(
                pget(&h.public(), "interactive_session_id"),
                before,
                "lease {jump_s}"
            );
            h.advance(Duration::from_millis(19_999));
            assert_ne!(
                pget(&h.public(), "interactive_session_id"),
                PyValue::Null,
                "lease 到期前 1 ms"
            );
            // Python `expired()` 為嚴格大於（審查 F36）：恰在 20 s 仍 active，超過期限才收回。
            h.advance(Duration::from_millis(1));
            assert_ne!(
                pget(&h.public(), "interactive_session_id"),
                PyValue::Null,
                "恰在期限 ({jump_s})"
            );
            h.advance(Duration::from_millis(1));
            assert_eq!(
                pget(&h.public(), "interactive_session_id"),
                PyValue::Null,
                "lease 到期後 ({jump_s})"
            );
        }

        // drain（3000 ms）
        {
            let mut h = CoreHarness::new(SHELL_PROFILE);
            h.to_ready();
            h.feed(ControlEvent::DeviceDetached {
                by_id: DEVICE_BY_ID.into(),
                reason: DetachReason::Removed,
            });
            h.jump_wall_us(delta_us);
            assert_eq!(
                pget(&h.public(), "drain_stalled"),
                PyValue::Bool(false),
                "drain 建立後 wall 跳動不得提前到期 ({jump_s})"
            );
            h.advance(Duration::from_millis(2_999));
            assert_eq!(
                pget(&h.public(), "drain_stalled"),
                PyValue::Bool(false),
                "drain 3000 ms 前不得逾時"
            );
            h.advance(Duration::from_millis(1));
            assert_eq!(
                pget(&h.public(), "drain_stalled"),
                PyValue::Bool(true),
                "drain 3000 ms 後逾時 ({jump_s})"
            );
        }

        // command timeout（10 s）：使用 command grant 的 timeout 驅動 RX monotonic wait。
        {
            let mut h = CoreHarness::new(SHELL_PROFILE);
            h.to_ready();
            let (r, effs) = h.rpc_full(submit("ls"));
            assert!(r.is_ok());
            let timeout = effs.iter().find_map(|e| match e {
                Effect::StartOp {
                    spec: OpSpec::Command(q),
                    ..
                } => Some(q.req.timeout_s),
                _ => None,
            });
            assert_eq!(timeout, Some(10.0), "命令 grant 應帶 timeout 10 s");
            let (done_tx, done_rx) = crossbeam_channel::bounded(1);
            let (started_tx, started_rx) = crossbeam_channel::bounded(1);
            let rx = h.rx.clone();
            let clock: Arc<dyn Clock> = h.clock.clone();
            let timeout_s = timeout.expect("command timeout");
            let waiter = std::thread::spawn(move || {
                let result = rx.wait_for_regex_from_with(
                    0,
                    Duration::from_secs_f64(timeout_s),
                    &*clock,
                    |_| {
                        let _ = started_tx.try_send(());
                        false
                    },
                );
                done_tx.send(result).expect("timeout 結果接收端仍存在");
            });
            started_rx
                .recv_timeout(Duration::from_millis(250))
                .expect("命令 wait 應先建立 monotonic deadline");
            h.jump_wall_us(delta_us);
            h.advance(Duration::from_millis(9_999));
            assert!(
                matches!(
                    done_rx.try_recv(),
                    Err(crossbeam_channel::TryRecvError::Empty)
                ),
                "命令 deadline 前 1 ms 不得逾時 ({jump_s})"
            );
            h.advance(Duration::from_millis(1));
            assert_eq!(
                done_rx
                    .recv_timeout(Duration::from_millis(250))
                    .expect("deadline 應喚醒等待"),
                None,
                "命令 deadline 到期應回 timeout ({jump_s})"
            );
            waiter.join().expect("命令 deadline waiter 應正常結束");
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

#[test]
fn actor_repeated_panics_begin_io_error_drain() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.core.arm_panic_on_next_events(3);
    let clock: Arc<dyn Clock> = h.clock.clone();
    let core = h.core;
    let handle = spawn_actor(core, Mailbox::new(256, 1024), Box::new(ReplyExec), clock);

    for _ in 0..3 {
        let error = handle
            .request(SessionRequest::GetState, Duration::from_secs(2))
            .expect_err("連續 panic 的事件應回 Err");
        assert!(error.message.starts_with("internal error: "));
    }
    let state = handle
        .request(SessionRequest::GetState, Duration::from_secs(2))
        .expect("第三次 panic 後 actor 應續行");
    let session = match pget(&state, "session") {
        PyValue::Dict(session) => session,
        other => panic!("session 應為 dict，實際 {other:?}"),
    };
    assert_eq!(
        session.get("state").and_then(PyValue::as_str),
        Some("RECOVERING")
    );
}

#[test]
fn release_while_bridge_open_is_pending_waits_for_drain_receipt() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let (reply, _) = h.rpc_deferred(SessionRequest::Release {
        source: "agent".into(),
        reason: PyValue::Null,
    });
    assert!(matches!(
        reply.try_recv(),
        Err(crossbeam_channel::TryRecvError::Empty)
    ));
    assert_eq!(h.core.state(), SessionState::Recovering);

    let effects = h.bridge_ok();
    let (epoch, stop_id) = begin_drain(&effects);
    assert!(matches!(
        reply.try_recv(),
        Err(crossbeam_channel::TryRecvError::Empty)
    ));
    assert_eq!(h.core.state(), SessionState::Recovering);

    h.feed(ControlEvent::WorkerStopped(receipt(epoch, stop_id.0)));
    let result = reply
        .try_recv()
        .expect("receipt 後才回覆 release")
        .expect("RPC");
    assert_eq!(pget(&result, "ok"), PyValue::Bool(true));
    assert_eq!(h.core.state(), SessionState::Released);
}

#[test]
fn release_during_nonrelease_drain_returns_session_recovering() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let effects = h.feed(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: DetachReason::Removed,
    });
    let (epoch, stop_id) = begin_drain(&effects);

    let (reply, _) = h.rpc_deferred(SessionRequest::Release {
        source: "agent".into(),
        reason: PyValue::Str("during removal".into()),
    });
    let result = reply
        .try_recv()
        .expect("drain 中的 Release 應立即回覆")
        .expect("RPC");
    assert_eq!(error_code(&Ok(result.clone())), "SESSION_RECOVERING");
    assert!(result.get("session").is_some(), "應附 session snapshot");

    h.feed(ControlEvent::WorkerStopped(receipt(epoch, stop_id.0)));
    assert_ne!(
        h.core.state(),
        SessionState::Released,
        "拒絕的 Release 不得延後生效"
    );
}

#[test]
fn release_while_flashing_includes_selector() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.force_state(SessionState::Flashing);
    let result = h
        .rpc(SessionRequest::Release {
            source: "agent".into(),
            reason: PyValue::Null,
        })
        .expect("RPC");
    assert_eq!(error_code(&Ok(result.clone())), "FLASHING_BUSY");
    assert_eq!(pget(&result, "selector"), PyValue::from("COM0"));
}

#[test]
fn interactive_open_rejects_foreground_and_existing_lease() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (_, effects) = h.rpc_full(submit("sleep 1"));
    assert!(has_start_op(&effects));
    let busy = h.rpc(SessionRequest::InteractiveOpen {
        owner: "agent".into(),
        timeout_s: 60.0,
        command: String::new(),
        allow_attached: false,
    });
    assert_eq!(error_code(&busy), "SESSION_BUSY");
    assert_eq!(pget(&busy.unwrap(), "selector"), PyValue::from("COM0"));

    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let opened = h.rpc(SessionRequest::InteractiveOpen {
        owner: "agent".into(),
        timeout_s: 60.0,
        command: String::new(),
        allow_attached: false,
    });
    let interactive_id = pget(&opened.unwrap(), "interactive_id");
    let second = h.rpc(SessionRequest::InteractiveOpen {
        owner: "agent".into(),
        timeout_s: 60.0,
        command: String::new(),
        allow_attached: false,
    });
    assert_eq!(error_code(&second), "SESSION_INTERACTIVE_BUSY");
    assert_eq!(
        pget(&second.unwrap(), "interactive_session_id"),
        interactive_id
    );
}

#[test]
fn interactive_open_allows_non_agent_owner() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let result = h
        .rpc(SessionRequest::InteractiveOpen {
            owner: "human:operator".into(),
            timeout_s: 60.0,
            command: String::new(),
            allow_attached: false,
        })
        .expect("RPC");
    assert_eq!(pget(&result, "ok"), PyValue::Bool(true));
    assert_ne!(pget(&result, "interactive_id"), PyValue::Null);
}

#[test]
fn boot_banner_invalidates_old_fsm_and_owner_generations() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.bridge_ok();
    let epoch = h.epoch();
    let old_fsm_gen = h.core.generations().fsm_gen;
    let old_owner_gen = h.core.generations().owner_gen;
    h.rx(b"U-Boot 2026.01\r\n");
    assert_eq!(h.core.generations().fsm_gen, old_fsm_gen + 1);
    assert_eq!(h.core.generations().owner_gen, old_owner_gen + 1);
    assert!(matches!(
        h.fence.with(|state| Fence::admit(
            state,
            &TxPermit {
                op_id: OpId(900),
                epoch,
                owner_gen: old_owner_gen,
                fsm_gen: old_fsm_gen,
                class: TxClass::LeaseOwner,
                source: "agent".into(),
                cmd_id: None,
                log: false,
            }
        )),
        Err(FenceReject::OwnerMismatch)
    ));
    // 審查 F15：banner 推進的是 core 世代，不是新的 login run——現行 attach run（以 old_fsm_gen 啟動）的
    // Ready 結果照 Python 套用（READY＋confirm_ready）。舊版斷言「停在 ATTACHING」鎖住的是永久卡住的缺陷。
    assert!(!h
        .feed(ControlEvent::LoginFinished {
            epoch,
            fsm_gen: old_fsm_gen,
            result: LoginOutcome::Ready,
            after_rx_seq: None,
        })
        .is_empty());
    assert_eq!(h.core.state(), SessionState::Ready);
}

#[test]
fn reconfirm_deadline_is_300s_from_first_banner_and_does_not_extend() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.rx(b"U-Boot 2026.01\r\n");
    h.advance(Duration::from_secs(100));
    h.rx(b"CFE version 1.0\r\n");
    h.advance(Duration::from_millis(199_999));
    assert_eq!(
        pget(&h.public(), "ready_reconfirm_failed"),
        PyValue::Bool(false)
    );
    h.advance(Duration::from_millis(1));
    assert_eq!(
        pget(&h.public(), "ready_reconfirm_failed"),
        PyValue::Bool(true)
    );
}

#[test]
fn queued_commands_do_not_increase_outstanding_count() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.rpc(submit("one")).expect("第一筆接受");
    h.rpc(submit("two")).expect("第二筆接受");
    h.rpc(submit("three")).expect("第三筆接受");
    assert_eq!(
        pget(&h.public(), "outstanding_commands"),
        PyValue::Int(sw_proto::pyjson::PyInt::Small(1))
    );
}

#[test]
fn aborted_op_is_settled_only_from_worker_receipt() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let (admitted, effects) = h.rpc_full(submit("printf hello"));
    let cmd_id = pget(&admitted.unwrap(), "cmd_id");
    let (op_id, epoch) = effects
        .iter()
        .find_map(|effect| match effect {
            Effect::StartOp { grant, .. } => Some((grant.op_id, grant.epoch)),
            _ => None,
        })
        .expect("前景命令 grant");
    let drain = h.feed(ControlEvent::DeviceDetached {
        by_id: DEVICE_BY_ID.into(),
        reason: DetachReason::Removed,
    });
    let (drain_epoch, stop_id) = begin_drain(&drain);
    let mut stdout = PyDict::new();
    stdout.insert("ok", PyValue::Bool(true));
    stdout.insert("stdout", PyValue::from("partial"));
    assert!(h
        .feed(ControlEvent::OpFinished {
            op_id,
            epoch,
            outcome: OpOutcome::CommandResult(stdout),
            after_rx_seq: None,
        })
        .is_empty());
    assert_eq!(
        pget(
            &h.reg.get(cmd_id.as_str().expect("cmd_id")).unwrap(),
            "status"
        ),
        PyValue::from("running")
    );

    let mut stopped = receipt(drain_epoch, stop_id.0);
    let mut op_ledger = TxOpLedger::default();
    op_ledger.confirmed_bytes = 3;
    stopped.ledger.ops.insert(op_id, op_ledger);
    let effects = h.feed(ControlEvent::WorkerStopped(stopped));
    assert!(!effects.iter().any(
        |effect| matches!(effect, Effect::SettleTx { op_id: settled, .. } if *settled == op_id)
    ));
    assert!(effects
        .iter()
        .any(|effect| matches!(effect, Effect::WalMeta { meta, .. }
        if pget(meta, "event") == PyValue::from("command_aborted_in_flight"))));
    let record = h.reg.get(cmd_id.as_str().expect("cmd_id")).unwrap();
    assert_eq!(
        pget(&record, "error_code"),
        PyValue::from("COMMAND_ABORTED_IN_FLIGHT")
    );
}

#[test]
fn credentials_env_file_accepts_export_comments_and_quotes() {
    let path = "/fake/s21.env";
    let profile = CRED_PROFILE.replace("/nonexistent/s21.env", path);
    let mut h = CoreHarness::new(&profile);
    h.set_env_file(
        path,
        "# SW_T_U=ignored\nexport SW_T_U='test-user'\nSW_T_P=\"test-password\"\n",
    );
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.bridge_ok();
    assert_eq!(pget(&h.public(), "last_error"), PyValue::Null);
}

#[test]
fn empty_env_file_values_remain_unresolved() {
    let path = "/fake/s21.env";
    let profile = CRED_PROFILE.replace("/nonexistent/s21.env", path);
    let mut h = CoreHarness::new(&profile);
    h.set_env_file(
        path,
        "# credentials intentionally empty\nSW_T_U=\nSW_T_P=\"\"\n",
    );
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.bridge_ok();
    assert_eq!(
        pget(&h.public(), "last_error"),
        PyValue::from("CREDENTIALS_UNRESOLVED")
    );
    assert_eq!(pget(&h.public(), "last_error_detail"), PyValue::Null);
}

#[test]
fn access_denied_maps_to_permission_error() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::AccessDenied(HolderReport {
            pids: Vec::new(),
            holder_unknown: true,
            status: sw_pal::proc_scan::HolderScanStatus::Unknown,
        })),
    });
    assert_eq!(
        pget(&h.public(), "last_error"),
        PyValue::from("ATTACH_FAILED:PermissionError")
    );
}

#[test]
fn credentials_warning_uses_python_missing_path_placeholder() {
    let profile = CRED_PROFILE.replace("    env_file: /nonexistent/s21.env\n", "");
    let mut h = CoreHarness::new(&profile);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let effects = h.bridge_ok();
    let warning = effects.iter().find_map(|effect| match effect {
        Effect::Warn(message) => Some(message.as_str()),
        _ => None,
    });
    assert!(warning.is_some_and(|message| { message.contains("reason=key_absent, env_file=-") }));
}

#[test]
fn env_file_without_login_requirement_does_not_block_probe() {
    let profile = "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    env_file: /nonexistent/s21.env\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: t, act_no: 1, alias: t+1, device_by_id: /s/by-id/usb-A}\n";
    let profile = profile.replace("    env_file:", "    ready_probe: ''\n    env_file:");
    let mut h = CoreHarness::new(&profile);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let effects = h.bridge_ok();
    // Rust ProfileTemplate 會像 Python 一樣補入預設 login_regex；此處明確令 profile
    // 不具 command capability，驗證 Python 在 command_capability gate 後不解析 env_file。
    assert_ne!(
        pget(&h.public(), "last_error"),
        PyValue::from("CREDENTIALS_UNRESOLVED")
    );
    assert!(
        !has_start_login(&effects),
        "不具 command capability 時不得啟動 login/probe"
    );
}

#[test]
fn console_unlock_env_is_a_credential_source() {
    let profile = "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    console_unlock_env: SW_T_UNLOCK\n    env_file: /fake/s21-unlock.env\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: t, act_no: 1, alias: t+1, device_by_id: /s/by-id/usb-A}\n";

    let mut missing = CoreHarness::new(profile);
    missing.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    missing.bridge_ok();
    assert_eq!(
        pget(&missing.public(), "last_error"),
        PyValue::from("CREDENTIALS_UNRESOLVED"),
        "只宣告 console_unlock_env 而缺值時須阻擋 login"
    );

    let mut resolved = CoreHarness::new(profile);
    resolved.set_env_file("/fake/s21-unlock.env", "SW_T_UNLOCK='unlock-value'\n");
    resolved.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let effects = resolved.bridge_ok();
    assert_ne!(
        pget(&resolved.public(), "last_error"),
        PyValue::from("CREDENTIALS_UNRESOLVED"),
        "有效 console unlock 不需要 user/pass"
    );
    assert!(has_start_login(&effects));
}

#[test]
fn credential_gate_follows_command_capability_and_boot_quiet() {
    let non_capable_profile = SHELL_NO_PROBE_PROFILE.replace(
        "    ready_probe: ''\n",
        "    ready_probe: ''\n    env_file: /nonexistent/s21.env\n",
    );
    let mut non_capable = CoreHarness::new(&non_capable_profile);
    non_capable.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let effects = non_capable.bridge_ok();
    assert_eq!(pget(&non_capable.public(), "last_error"), PyValue::Null);
    assert!(
        !has_start_login(&effects),
        "非 command-capable 不檢查登入帳密"
    );

    let env_path = "/fake/s21-quiet.env";
    let profile = CRED_PROFILE.replace("/nonexistent/s21.env", env_path);
    let mut h = CoreHarness::new(&profile);
    let quiet_until = h.clock.mono_ns().saturating_add(1_000_000_000);
    h.core.set_boot_quiet_until_for_test(quiet_until);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let effects = h.bridge_ok();
    assert_eq!(
        pget(&h.public(), "last_error"),
        PyValue::from("PROMPT_UNAVAILABLE")
    );
    assert!(
        !has_start_login(&effects),
        "boot quiet 期間不得 probe/login"
    );
}

#[test]
fn credentials_warning_is_deduplicated_until_manual_recover() {
    fn retry_attach(h: &mut CoreHarness) -> Vec<Effect> {
        let effects = h.feed(ControlEvent::DeviceDetached {
            by_id: DEVICE_BY_ID.into(),
            reason: DetachReason::Removed,
        });
        let (epoch, stop_id) = begin_drain(&effects);
        h.feed(ControlEvent::WorkerStopped(receipt(epoch, stop_id.0)));
        h.feed(ControlEvent::DeviceAttached {
            by_id: DEVICE_BY_ID.into(),
            real_path: REAL_PATH.into(),
        });
        h.bridge_ok()
    }

    let mut h = CoreHarness::new(CRED_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let first = h.bridge_ok();
    assert_eq!(
        first
            .iter()
            .filter(|e| matches!(e, Effect::Warn(_)))
            .count(),
        1
    );
    assert_eq!(
        first
            .iter()
            .filter(|e| matches!(e, Effect::WalMeta { .. }))
            .count(),
        1
    );

    let second = retry_attach(&mut h);
    assert_eq!(
        second
            .iter()
            .filter(|e| matches!(e, Effect::Warn(_)))
            .count(),
        0
    );
    assert_eq!(
        second
            .iter()
            .filter(|e| matches!(e, Effect::WalMeta { .. }))
            .count(),
        0
    );

    // Python 基線（P5-T14 實作 recover 後）：`recover_session` 先清 `credentials_warned`，ATTACHED 分支再經
    // `_probe_existing_bridge` 重新解析帳密並告警一次——「手動 recover 後恰再告警一次」發生在 recover 本身，
    // 其後的 attach 又回到去重（0 次）。兩者合計仍恰 1 次 Warn＋1 次 WalMeta。
    let (_, recover_effects) = h.rpc_full(SessionRequest::Recover {
        timeout_s: 1.0,
        force: false,
    });
    let third = retry_attach(&mut h);
    let warns = |effects: &[Effect]| {
        effects
            .iter()
            .filter(|e| matches!(e, Effect::Warn(_)))
            .count()
    };
    let metas = |effects: &[Effect]| {
        effects
            .iter()
            .filter(|e| matches!(e, Effect::WalMeta { .. }))
            .count()
    };
    assert_eq!(warns(&recover_effects), 1, "手動 recover 應再告警一次");
    assert_eq!(metas(&recover_effects), 1);
    assert_eq!(warns(&third), 0, "recover 已告警，之後 attach 恢復去重");
    assert_eq!(metas(&third), 0);
    assert_eq!(warns(&recover_effects) + warns(&third), 1);
    assert_eq!(metas(&recover_effects) + metas(&third), 1);
}

// ---------------------------------------------------------------------------
// 審查 F15（High，0a187c1）：attach 登入中收到 boot banner（quiet arm 令 fsm_gen 前進）後，該次 run 自己的
// LoginFinished 仍須照 Python 套用（`_attach_by_id` 的 attach thread 照常寫終態）：NotReady → ATTACHED、
// Ready → READY（confirm_ready）、Failed → drain 到 DETACHED；不得永久停在 ATTACHING、SessionAttach 回 ok。
// ---------------------------------------------------------------------------

fn attach_login_then_banner(h: &mut CoreHarness) -> (u64, u64) {
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let effs = h.bridge_ok();
    let run_gen = effs
        .iter()
        .find_map(|e| match e {
            Effect::StartLogin { fsm_gen, .. } => Some(*fsm_gen),
            _ => None,
        })
        .expect("bridge 開成後應發出 StartLogin");
    h.rx(b"U-Boot 2024.01\r\n");
    assert_ne!(h.core.generations().fsm_gen, run_gen, "banner 應 arm quiet 並使 fsm_gen 前進");
    (h.epoch(), run_gen)
}

#[test]
fn banner_during_attach_login_settles_attached() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let (epoch, run_gen) = attach_login_then_banner(&mut h);
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: run_gen,
        result: LoginOutcome::NotReady("PROMPT_UNAVAILABLE".into()),
        after_rx_seq: None,
    });
    assert_eq!(h.core.state(), SessionState::Attached);
    let public = h.public();
    assert_eq!(pget(&public, "last_error"), PyValue::from("PROMPT_UNAVAILABLE"));
    assert_ne!(h.core.generations().bridge_generation, 0, "bridge 已採用");
    // 再一次 SessionAttach 不得在 ATTACHING 回 ok:true（走 ATTACHED 再探）。
    let (resp, effs) = h.rpc_full(SessionRequest::SessionAttach);
    if let Ok(resp) = resp {
        let session = pget(&resp, "session");
        if let PyValue::Dict(s) = session {
            assert_ne!(pget(&s, "state"), PyValue::from("ATTACHING"));
        }
    }
    let _ = effs;
}

#[test]
fn banner_during_attach_login_ready_result_applied() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let (epoch, run_gen) = attach_login_then_banner(&mut h);
    h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: run_gen,
        result: LoginOutcome::Ready,
        after_rx_seq: None,
    });
    assert_eq!(h.core.state(), SessionState::Ready);
    assert_eq!(pget(&h.public(), "boot_quiet_remaining_s"), PyValue::Null, "confirm_ready 清 quiet");
}

#[test]
fn banner_during_attach_login_failed_drains_detached() {
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let (epoch, run_gen) = attach_login_then_banner(&mut h);
    let effs = h.feed(ControlEvent::LoginFinished {
        epoch,
        fsm_gen: run_gen,
        result: LoginOutcome::Failed("SHELL_PROMPT_TIMEOUT".into()),
        after_rx_seq: None,
    });
    let (ep, stop) = begin_drain(&effs);
    h.feed(ControlEvent::WorkerStopped(receipt(ep, stop.0)));
    assert_eq!(h.core.state(), SessionState::Detached);
}

#[test]
fn recover_during_pending_attach_returns_reattach() {
    // 審查 F29（8b8a40f）：Python `recover_session` 在 `bridge is None and by_id in devices` 時一律
    // `{ok:true, recovering:false, action:REATTACH}`；`_spawn_attach` 對進行中的 attach 不重複開檔。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let effs = h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    assert!(effs.iter().any(|e| matches!(e, Effect::OpenBridge { .. })));
    let (resp, effs) = h.rpc_full(SessionRequest::Recover {
        timeout_s: 2.0,
        force: false,
    });
    let resp = resp.expect("RPC");
    assert_eq!(pget(&resp, "ok"), PyValue::Bool(true), "{resp:?}");
    assert_eq!(pget(&resp, "action"), PyValue::from("REATTACH"));
    assert_eq!(pget(&resp, "recovering"), PyValue::Bool(false));
    assert!(
        !effs.iter().any(|e| matches!(e, Effect::OpenBridge { .. })),
        "attach 進行中不得再發 OpenBridge"
    );
    assert_eq!(h.core.state(), SessionState::Attaching);
}

#[test]
fn rx_window_resets_per_bridge() {
    // 審查 F51（f20bdd0）：Python 每次 attach 新建 `UARTBridge`（`_rx_window` 空、`_rx_total_bytes=0`）。
    // t=0 上一條連線收 20000 bytes，clear 後 1 s 內重接、新連線零 RX → 公開速率兩欄皆 0（不沿用上一條連線）。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    let now = h.clock.mono_ns();
    h.rx.append(&vec![b'x'; 20_000], now, h.epoch(), 1);
    assert!(pget(&h.public(), "rx_bytes_last_10s").as_i64().is_some_and(|n| n >= 20_000));
    let (_resp, effs) = h.rpc_deferred(SessionRequest::Clear);
    let (ep, stop) = begin_drain(&effs);
    let effs = h.feed(ControlEvent::WorkerStopped(receipt(ep, stop.0)));
    assert!(has_reattach(&effs), "clear 後交 manager 再解析");
    let effs = h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    assert!(effs.iter().any(|e| matches!(e, Effect::OpenBridge { .. })), "clear 後應重新開檔");
    h.advance(Duration::from_secs(1));
    let mut opened = h.opened_bridge(REAL_PATH);
    opened.rx = sw_core::uart_bridge::RxShared::new(131_072, h.clock.mono_ns());
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Ok(opened),
    });
    let public = h.public();
    assert_eq!(pget(&public, "rx_bytes_last_10s"), PyValue::from(0), "{public:?}");
    assert_eq!(pget(&public, "rx_rate_bps"), PyValue::from(0));
}

fn has_reattach(effs: &[Effect]) -> bool {
    effs.iter().any(|e| matches!(e, Effect::Reattach { .. }))
}

#[test]
fn clear_without_bridge_with_device_reports_attaching_and_reattaches() {
    // 審查 F61（High，73634db）：Python `clear_session` 無 bridge 分支：裝置在線 → 回應前活 session 即
    // `state:"ATTACHING"`、`last_error:null`，再 `_spawn_attach`（開埠前走 `_reresolve_profile_on_reattach`）。
    // 具體輸入：開埠 AccessDenied（ATTACH_FAILED:PermissionError、DETACHED、裝置仍在）→ `session.clear`。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    h.feed(ControlEvent::BridgeOpened {
        epoch_hint: 0,
        result: Err(OpenFailure::AccessDenied(HolderReport {
            pids: Vec::new(),
            holder_unknown: true,
            status: sw_pal::proc_scan::HolderScanStatus::Unknown,
        })),
    });
    assert_eq!(pget(&h.public(), "state"), PyValue::from("DETACHED"));
    let (rx, effs) = h.rpc_deferred(SessionRequest::Clear);
    let resp = rx.try_recv().expect("clear 應立即回覆").expect("ok");
    let session = match resp.get("session") {
        Some(PyValue::Dict(d)) => d.clone(),
        other => panic!("缺 session：{other:?}"),
    };
    assert_eq!(session.get("state"), Some(&PyValue::from("ATTACHING")));
    assert_eq!(session.get("last_error"), Some(&PyValue::Null));
    assert!(has_reattach(&effs), "需交 manager 再解析後 attach");
    assert!(!has_open_bridge(&effs), "再解析前不得直接開埠");
    // manager 再解析後送 DeviceAttached：ATTACHING（等待再 attach）仍須接受並開埠。
    let effs = h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    assert!(has_open_bridge(&effs), "DeviceAttached 後應開埠");
}

#[test]
fn clear_without_bridge_without_device_stays_detached_cleared() {
    // Python `clear_session` 無 bridge 且裝置不在：`state:"DETACHED"`、`last_error:"CLEARED"`，不 `_spawn_attach`。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    let (rx, effs) = h.rpc_deferred(SessionRequest::Clear);
    assert!(rx.try_recv().expect("立即回覆").is_ok());
    let s = h.public();
    assert_eq!(pget(&s, "state"), PyValue::from("DETACHED"));
    assert_eq!(pget(&s, "last_error"), PyValue::from("CLEARED"));
    assert!(!has_reattach(&effs) && !has_open_bridge(&effs));
}

#[test]
fn boot_quiet_survives_clear_drain_and_blocks_reattach_probe() {
    // 審查 F63（High，73634db）：Python `boot_quiet_until`／`ready_reconfirm_pending` 掛在 SessionRuntime，
    // drain 收尾（`_complete_drain` 只呼叫 `reset_reprobe_progress`）不清；新 bridge 由 `_push_tx_gate` 帶回 quiet。
    // 具體輸入：READY shell session 收 `U-Boot 2024.01` → quiet armed → `session.clear` → 重新 attach。
    // Python：重新 attach 期間 `boot_quiet_remaining_s` 非 null、不送登入 probe（ATTACHED＋PROMPT_UNAVAILABLE）。
    let mut h = CoreHarness::new(SHELL_PROFILE);
    h.to_ready();
    h.rx(b"U-Boot 2024.01\r\n");
    assert_ne!(pget(&h.public(), "boot_quiet_remaining_s"), PyValue::Null);
    let (_rx, _) = h.rpc_deferred(SessionRequest::Clear);
    let (ep, stop) = h.pending_stop();
    h.feed(ControlEvent::WorkerStopped(receipt(ep, stop.0)));
    assert_ne!(
        pget(&h.public(), "boot_quiet_remaining_s"),
        PyValue::Null,
        "drain 不得清 boot quiet（manager 再解析以此判斷不偵測）"
    );
    assert_eq!(pget(&h.public(), "ready_reconfirm_pending"), PyValue::Bool(true));
    h.feed(ControlEvent::DeviceAttached {
        by_id: DEVICE_BY_ID.into(),
        real_path: REAL_PATH.into(),
    });
    let effs = h.bridge_ok();
    assert!(
        !effs.iter().any(|e| matches!(
            e,
            Effect::StartLogin { plan, .. } if !matches!(plan, sw_core::login_fsm::LoginPlan::None)
        )),
        "quiet 中重新 attach 不得送登入 probe"
    );
    assert_eq!(h.core.state(), SessionState::Attached);
    assert_eq!(pget(&h.public(), "last_error"), PyValue::from("PROMPT_UNAVAILABLE"));
    assert!(h.fence.with(|f| f.quiet_active), "新 bridge 的 TX fence 須帶回 quiet");
}
