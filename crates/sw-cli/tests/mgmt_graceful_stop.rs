//! P4-T7 共用 `graceful_stop`（§12.1 步驟 3）與 windows-task 停止／重新啟動路徑的失敗測試（SC-10、SC-17、SC-18、SC-20；R1-F1、R5-F1）。
//!
//! 紅燈階段（TDD）：`sw_cli::graceful_stop` 的本體為 `todo!("P4-T7: …")`，`service stop|restart` 目前仍由
//! `DELEGATED_GROUPS` 委派（Python sentinel），因此 `t.run` 類案例以斷言不符失敗；直接呼叫 `graceful_stop_with` 的案例
//! 以 `not implemented: P4-T7` panic 失敗。R5-F1 拉起模型（`with_restart_on_failure` 等）與 identity 置換模型
//! 屬 `tests/support/mod.rs` 的 todo（P4-T12／P4-T7），於實作時替換。
//! 規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 7；盤點 `inventory/07-platform-mgmt.md` §5.3、§12。
mod support;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use support::*;
use sw_cli::graceful_stop::{
    graceful_stop_with, wait_handoff_gone, DaemonIdentity, HandoffError, HandoffWait, StopFailure,
    StopRpc, HANDOFF_EXIT_WAIT,
};
use sw_pal::fakes::real_fs;
use sw_pal::proc_scan::{DaemonProc, ProcIdentity};
use sw_pal::service_ctl::{HoldState, ServiceCtl, ServiceResult};
use sw_pal::traits::SupervisionMode;
use sw_proto::pyjson::PyValue;

/// `PyValue::Dict` 的欄位（測試輔助；缺欄位即 panic）。
fn field<'a>(v: &'a PyValue, key: &str) -> &'a PyValue {
    match v {
        PyValue::Dict(d) => d
            .get(key)
            .unwrap_or_else(|| panic!("JSON 欄位 {key} 不存在")),
        _ => panic!("JSON 不是物件"),
    }
}

fn fstr(v: &PyValue, key: &str) -> String {
    match field(v, key) {
        PyValue::Str(s) => s.clone(),
        _ => panic!("JSON 欄位 {key} 不是字串"),
    }
}

fn fbool(v: &PyValue, key: &str) -> bool {
    match field(v, key) {
        PyValue::Bool(b) => *b,
        _ => panic!("JSON 欄位 {key} 不是布林"),
    }
}

/// 以 `/tmp` 字面路徑表示的 POSIX endpoint（替身鍵；`isolated_endpoint` 會轉成 `unix://`）。
const POSIX_EP: &str = "/tmp/sw-t7/serialwrapd.sock";

/// 交接成功的回應行（token 為 32 hex）。
const GRANT: &str =
    r#"{"ok":true,"stopping":true,"handoff_token":"0123456789abcdef0123456789abcdef"}"#;
/// 交接被忙碌拒絕（`FLASHING_BUSY`＋daemon 原子判定的 busy 項目）。
const BUSY: &str = r#"{"ok":false,"error_code":"FLASHING_BUSY","busy":[{"com":"COM0","state":"FLASHING","ops":["flash"]}]}"#;

fn stop_count(t: &TestEnv, method: &str) -> usize {
    t.rpc().requests().iter().filter(|r| r.1 == method).count()
}

fn hard_stop_count(t: &TestEnv) -> usize {
    t.service()
        .actions()
        .iter()
        .filter(|a| a.1 == "hard_stop")
        .count()
}

/// SC-11 交接後的有序關機時間模型：`daemon.handoff_stop` grant 後實例在 `drain`（排空窗口，Python 基線
/// `HANDOFF_DRAIN_GRACE_S`）內仍回 ping，之後關閉監聽；行程再存活 `exit` 才結束（`None`＝永不結束）。
/// grant 時刻取 grant 送出後第一次行程存活查詢的假時鐘（其間 CLI 不推進時鐘）。
fn ordered_shutdown_after_grant(t: &TestEnv, ep: &str, pid: u32, drain: Duration, exit: Option<Duration>) {
    let endpoint = t.isolated_endpoint(ep);
    t.rpc().clear_stop_after_send(&endpoint, "daemon.handoff_stop");
    let rpc = Arc::clone(t.rpc());
    let clock = Arc::clone(t.clock());
    let granted_at: Mutex<Option<Duration>> = Mutex::new(None);
    t.procs().alive_while(
        pid,
        Arc::new(move || {
            if rpc.request_count(&endpoint, "daemon.handoff_stop") == 0 {
                return true;
            }
            let now = clock.elapsed();
            let at = *granted_at
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .get_or_insert(now);
            if now >= at + drain && !rpc.is_stopped(&endpoint) {
                rpc.stop_endpoint(&endpoint);
            }
            exit.map_or(true, |exit| now < at + drain + exit)
        }),
    );
}

fn on_demand_revert_handoff(t: &TestEnv) -> Result<sw_cli::graceful_stop::StopReport, StopFailure> {
    t.with_cli_ctx(|ctx| {
        graceful_stop_with(
            ctx,
            &SupervisionMode::OnDemand,
            POSIX_EP,
            false,
            true,
            "install_recover",
            StopRpc::Handoff { reason: "revert" },
        )
    })
}

fn daemon_record(pid: u32, socket: &str) -> DaemonProc {
    DaemonProc {
        pid,
        socket: Some(socket.to_string()),
        tree: vec![ProcIdentity {
            pid,
            start_ticks: Some(u64::from(pid)),
        }],
    }
}

#[test]
fn windows_task_stop_sends_daemon_stop_before_end() {
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_real_service()
        .with_daemon_alive_then_gone(TestEnv::win_endpoint(), 900, 4);
    let (rc, out, _) = t.run(&["service", "stop"]);
    assert_eq!(rc, 0, "{out}");
    let o = t.event_order();
    assert!(
        o.index_of("run:schtasks /Change /TN serialwrap /DISABLE") < o.index_of("rpc:daemon.stop")
    );
    assert!(
        !t.calls()
            .iter()
            .any(|c| c.get(1).map(String::as_str) == Some("/End")),
        "已正常結束不得硬殺"
    );
    // 「gone」判定：最後一次 health.ping（連線被拒）必須位於 daemon.stop 之後、/ENABLE 之前。
    let pings = o.count_prefix("rpc:health.ping");
    let last_ping = o.index_of_nth("rpc:health.ping", pings - 1);
    assert!(o.index_of("rpc:daemon.stop") < last_ping);
    assert!(last_ping < o.index_of("run:schtasks /Change /TN serialwrap /ENABLE"));
    assert!(fbool(&json(&out), "graceful"));
}

#[test]
fn windows_task_restart_aborts_when_daemon_never_exits() {
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_real_service()
        .with_daemon_alive_forever(TestEnv::win_endpoint(), 900);
    let (rc, out, _) = t.run(&["service", "restart"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(fstr(&j, "error_code"), "TIMEOUT");
    assert_eq!(fstr(&j, "phase"), "service_restart_wait_stop");
    assert!(
        t.calls()
            .iter()
            .any(|c| c.get(1).map(String::as_str) == Some("/End")),
        "逾時後才 /End"
    );
    assert!(!t
        .calls()
        .iter()
        .any(|c| c.get(1).map(String::as_str) == Some("/Run")));
    assert!(!t
        .calls()
        .iter()
        .any(|c| c.last().map(String::as_str) == Some("/ENABLE")));
    assert!(t.clock().elapsed() >= Duration::from_secs(15));
}

#[test]
fn graceful_handoff_holds_before_any_stop_rpc() {
    // R5-F1：交接前對每一種 mode 先 hold（systemd 持久 drop-in、windows disable、on-demand 持鎖＋journal）。
    let cases: [(SupervisionMode, bool); 5] = [
        (SupervisionMode::SystemdUser, false),
        (SupervisionMode::SystemdSystem, true),
        (SupervisionMode::WindowsTask, false),
        (SupervisionMode::WindowsService, true),
        (SupervisionMode::OnDemand, false),
    ];
    for (mode, with_sudo) in cases {
        let windows_mode = matches!(
            mode,
            SupervisionMode::WindowsTask | SupervisionMode::WindowsService
        );
        let ep = if windows_mode {
            TestEnv::win_endpoint()
        } else {
            POSIX_EP
        };
        let base = if windows_mode {
            TestEnv::windows()
        } else {
            TestEnv::posix()
        };
        let t = base
            .with_supervisor_state(mode.clone(), true, true)
            .with_idle_daemon(ep, 4242);
        let report = t
            .with_cli_ctx(|ctx| {
                graceful_stop_with(
                    ctx,
                    &mode,
                    ep,
                    with_sudo,
                    false,
                    "service_stop_wait_stop",
                    StopRpc::Handoff { reason: "update" },
                )
            })
            .unwrap_or_else(|e| panic!("graceful_stop_with 失敗：{e:?}"));
        let o = t.event_order();
        if mode == SupervisionMode::OnDemand {
            assert_eq!(o.count_prefix("service:hold_autostart"), 0);
        } else {
            assert!(
                o.index_of("service:hold_autostart") < o.index_of("rpc:daemon.handoff_stop"),
                "hold 必須先於第一個交接 RPC（mode={mode:?}）"
            );
        }
        assert_eq!(
            stop_count(&t, "daemon.stop"),
            0,
            "交接路徑不得送 daemon.stop（mode={mode:?}）"
        );
        if matches!(mode, SupervisionMode::OnDemand) {
            assert!(report.hold.is_none(), "on-demand 以 autostart.lock 表示 hold");
            assert_eq!(t.service().autostart_hold_state(&mode), HoldState::NotHeld);
            // on-demand：autostart.lock 於交接 RPC 前取得，返回後仍持有（另一 handle 的非阻塞取鎖必須失敗）。
            let lock = t.run_dir().join("autostart.lock");
            assert!(
                real_fs().file_lock_exclusive(&lock, false).is_err(),
                "on-demand 交接後 autostart.lock 必須仍被持有"
            );
        } else {
            assert!(
                report.hold.is_some(),
                "Handoff 回傳的 hold 必須保持生效（mode={mode:?}）"
            );
            assert_eq!(
                t.service().autostart_hold_state(&mode),
                HoldState::Held,
                "Handoff 不得自行 release（mode={mode:?}）"
            );
        }
    }

    // StopRpc::Plain＋systemd：不呼叫 hold_autostart（雙邊行為不變）。
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_daemon_alive_then_gone(POSIX_EP, 4242, 2);
    let report = t
        .with_cli_ctx(|ctx| {
            graceful_stop_with(
                ctx,
                &SupervisionMode::SystemdUser,
                POSIX_EP,
                false,
                false,
                "service_stop_wait_stop",
                StopRpc::Plain,
            )
        })
        .unwrap_or_else(|e| panic!("Plain 停止失敗：{e:?}"));
    assert!(report.hold.is_none());
    assert_eq!(t.event_order().count_prefix("service:hold_autostart"), 0);
}

#[test]
fn graceful_handoff_marks_first_link_when_identity_changes_during_hold() {
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_idle_daemon(POSIX_EP, 4242);
    t.procs().script(vec![
        vec![daemon_record(4242, POSIX_EP)],
        vec![daemon_record(9000, POSIX_EP)],
        vec![],
    ]);
    let report = t
        .with_cli_ctx(|ctx| {
            graceful_stop_with(
                ctx,
                &SupervisionMode::SystemdUser,
                POSIX_EP,
                false,
                false,
                "service_stop_wait_stop",
                StopRpc::Handoff { reason: "update" },
            )
        })
        .unwrap_or_else(|error| panic!("hold 前後 identity 改變後仍應交接目前實例：{error:?}"));
    assert_eq!(report.chain.len(), 1);
    assert_eq!(report.chain[0].pid, Some(9000));
    assert!(report.chain[0].relaunched);
}

#[test]
fn capture_identity_requires_process_scan_evidence() {
    let t = TestEnv::posix()
        .with_procs(vec![])
        .with_rpc_ok(POSIX_EP, "health.status", r#"{"ok":true,"pid":9000}"#);
    let identity = t.with_cli_ctx(|ctx| {
        sw_cli::commands::daemon_cmd::capture_daemon_identity(ctx, POSIX_EP)
    });
    assert!(!identity.known);
    assert!(identity.tree.is_empty());
    assert_eq!(identity.pid, None);
}

#[test]
fn windows_service_plain_stop_requires_sudo_before_sc_config() {
    let t = TestEnv::windows().with_real_service();
    let failure = t
        .with_cli_ctx(|ctx| {
            graceful_stop_with(
                ctx,
                &SupervisionMode::WindowsService,
                "tcp://127.0.0.1:48700",
                false,
                false,
                "service_stop_wait_stop",
                StopRpc::Plain,
            )
        })
        .expect_err("windows-service 停用缺管理員權限時須 fail closed");
    assert!(matches!(
        failure,
        StopFailure::Service(ServiceResult {
            error_code: Some("NEEDS_SUDO"),
            phase: Some("service_disable"),
            ..
        })
    ));
    assert!(t.calls().is_empty(), "缺權限時不得呼叫 sc.exe config");
}

#[test]
fn graceful_handoff_hold_failure_sends_no_stop() {
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_idle_daemon(POSIX_EP, 4242);
    // hold_autostart 的下一筆回應為失敗（R5-F1 失敗路徑注入）。
    t.service().respond(ServiceResult {
        ok: false,
        mode: "systemd-user".to_string(),
        action: "hold_autostart".to_string(),
        rc: Some(1),
        stdout: None,
        stderr: None,
        error_code: Some("INTERNAL_ERROR"),
        hint: None,
        phase: Some("autostart_hold"),
    });
    let r = t.with_cli_ctx(|ctx| {
        graceful_stop_with(
            ctx,
            &SupervisionMode::SystemdUser,
            POSIX_EP,
            false,
            false,
            "service_stop_wait_stop",
            StopRpc::Handoff { reason: "update" },
        )
    });
    assert!(
        matches!(r, Err(StopFailure::Hold(_))),
        "hold 失敗必須回 StopFailure::Hold"
    );
    assert_eq!(stop_count(&t, "daemon.handoff_stop"), 0);
    assert_eq!(stop_count(&t, "daemon.stop"), 0);
    assert_eq!(hard_stop_count(&t), 0);
}

#[test]
fn graceful_handoff_lock_timeout_reports_timeout() {
    let t = TestEnv::posix()
        .with_autostart_lock_holder(None)
        .with_supervisor_state(SupervisionMode::OnDemand, true, true)
        .with_idle_daemon(POSIX_EP, 4242);
    let failure = t
        .with_cli_ctx(|ctx| {
            graceful_stop_with(
                ctx,
                &SupervisionMode::OnDemand,
                POSIX_EP,
                false,
                false,
                "service_stop_wait_stop",
                StopRpc::Handoff { reason: "update" },
            )
        })
        .expect_err("autostart.lock 等待逾時必須拒絕交接");
    assert!(matches!(
        failure.cause(),
        StopFailure::Hold(result)
            if result.error_code == Some("TIMEOUT") && result.phase == Some("autostart_lock")
    ));
    assert_eq!(stop_count(&t, "daemon.handoff_stop"), 0);
    assert_eq!(hard_stop_count(&t), 0);
}

#[test]
fn graceful_handoff_rejects_incomplete_or_invalid_grants() {
    for response in [
        r#"{"ok":true,"handoff_token":"0123456789abcdef0123456789abcdef"}"#,
        r#"{"ok":true,"stopping":true,"handoff_token":""}"#,
        r#"{"ok":true,"stopping":true,"handoff_token":"0123456789ABCDEF0123456789ABCDEF"}"#,
        r#"{"ok":true,"stopping":true,"handoff_token":"0123456789abcdef0123456789abcde"}"#,
        r#"{"ok":"true","stopping":true,"handoff_token":"0123456789abcdef0123456789abcdef"}"#,
        r#"{"ok":true,"stopping":"true","handoff_token":"0123456789abcdef0123456789abcdef"}"#,
    ] {
        let t = TestEnv::posix()
            .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
            .with_idle_daemon(POSIX_EP, 4242);
        t.rpc().respond(&t.isolated_endpoint(POSIX_EP), "daemon.handoff_stop", response);
        let failure = t
            .with_cli_ctx(|ctx| {
                graceful_stop_with(
                    ctx,
                    &SupervisionMode::SystemdUser,
                    POSIX_EP,
                    false,
                    false,
                    "service_stop_wait_stop",
                    StopRpc::Handoff { reason: "update" },
                )
            })
            .expect_err("不完整 handoff grant 不可授權停止");
        assert!(matches!(
            failure.cause(),
            StopFailure::Handoff(HandoffError::Unconfirmed { code, .. }) if code == "INVALID_RESPONSE"
        ));
        assert_eq!(hard_stop_count(&t), 0);
        assert_eq!(stop_count(&t, "daemon.stop"), 0);
    }
}

#[test]
fn graceful_handoff_gone_requires_process_scan_confirmation() {
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_idle_daemon(POSIX_EP, 4242);
    // 舊實例消失後，endpoint 上出現一個尚未能 ping 的新實例。
    t.procs().script(vec![
        vec![daemon_record(4242, POSIX_EP)],
        vec![daemon_record(4242, POSIX_EP)],
        vec![daemon_record(9000, POSIX_EP)],
    ]);
    let result = t.with_cli_ctx(|ctx| {
        graceful_stop_with(
            ctx,
            &SupervisionMode::SystemdUser,
            POSIX_EP,
            false,
            false,
            "service_stop_wait_stop",
            StopRpc::Handoff { reason: "update" },
        )
    });
    assert!(result.is_err(), "新實例尚未完成 handoff 時不得回報已釋放");
    assert_eq!(hard_stop_count(&t), 0);
}

#[test]
fn graceful_handoff_process_scan_failure_is_not_released() {
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_idle_daemon(POSIX_EP, 4242);
    t.procs().fail();
    let result = t.with_cli_ctx(|ctx| {
        graceful_stop_with(
            ctx,
            &SupervisionMode::SystemdUser,
            POSIX_EP,
            false,
            false,
            "service_stop_wait_stop",
            StopRpc::Handoff { reason: "update" },
        )
    });
    assert!(result.is_err(), "ProcScan 失敗不能視為 endpoint 已釋放");
    assert_eq!(hard_stop_count(&t), 0);
}

#[test]
fn handoff_wait_does_not_call_any_pid_a_relaunch_when_identity_is_unknown() {
    let t = TestEnv::posix()
        .with_procs(vec![daemon_record(9000, POSIX_EP)])
        .with_rpc_ok(POSIX_EP, "health.ping", r#"{"ok":true}"#)
        .with_rpc_ok(POSIX_EP, "health.status", r#"{"ok":true,"pid":9000}"#);
    let outcome = t.with_cli_ctx(|ctx| {
        wait_handoff_gone(ctx, POSIX_EP, &DaemonIdentity::default(), Duration::ZERO)
    });
    assert_eq!(outcome, HandoffWait::TimedOut);
}

#[test]
fn graceful_handoff_rejection_returns_hold_context() {
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_idle_daemon(POSIX_EP, 4242);
    t.rpc()
        .respond(&t.isolated_endpoint(POSIX_EP), "daemon.handoff_stop", BUSY);
    let failure = t
        .with_cli_ctx(|ctx| {
            graceful_stop_with(
                ctx,
                &SupervisionMode::SystemdUser,
                POSIX_EP,
                false,
                false,
                "service_stop_wait_stop",
                StopRpc::Handoff { reason: "update" },
            )
        })
        .expect_err("忙碌交接必須回報失敗");
    assert!(matches!(
        failure.cause(),
        StopFailure::Handoff(HandoffError::Busy { code, .. }) if code == "FLASHING_BUSY"
    ));
    assert!(
        failure.hold().is_some(),
        "失敗必須保留可供呼叫端解除的 hold"
    );
    assert!(failure.chain().is_empty(), "首次交接拒絕時尚無交接鏈");
    assert_eq!(stop_count(&t, "daemon.stop"), 0);
    assert_eq!(hard_stop_count(&t), 0);
}

#[test]
fn graceful_handoff_relaunched_instance_rehandoff_no_hard_stop() {
    // R5-F1：交接 grant 後舊實例以 exit 1 結束，監管模型立即拉起 pid 9000（忽略 hold）。
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_supervisor_ignores_hold()
        .with_restart_on_failure()
        .with_daemon_exit_code_after_handoff(1)
        .with_idle_daemon(POSIX_EP, 4242);
    let r = t.with_cli_ctx(|ctx| {
        graceful_stop_with(
            ctx,
            &SupervisionMode::SystemdUser,
            POSIX_EP,
            false,
            false,
            "service_stop_wait_stop",
            StopRpc::Handoff { reason: "update" },
        )
    });
    let report = r.unwrap_or_else(|e| panic!("重新交接後應成功：{e:?}"));
    // 重新交接不得等滿 10 s 才偵測（wait_handoff_gone 立即返回 Relaunched）。
    assert!(t.clock().elapsed() < Duration::from_secs(10));
    assert_eq!(report.chain.len(), 2);
    assert_eq!(report.chain[1].pid, Some(9000));
    assert!(report.chain[1].relaunched);
    assert_ne!(report.chain[0].token, report.chain[1].token);
    assert_eq!(
        stop_count(&t, "daemon.handoff_stop"),
        2,
        "第 2 次交接送往 pid 9000"
    );
    assert_eq!(hard_stop_count(&t), 0);
}

#[test]
fn graceful_handoff_hold_prevents_restart_on_failure_relaunch() {
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_restart_on_failure()
        .with_daemon_exit_code_after_handoff(1)
        .with_idle_daemon(POSIX_EP, 4242);
    let report = t
        .with_cli_ctx(|ctx| {
            graceful_stop_with(
                ctx,
                &SupervisionMode::SystemdUser,
                POSIX_EP,
                false,
                false,
                "service_stop_wait_stop",
                StopRpc::Handoff { reason: "update" },
            )
        })
        .unwrap_or_else(|error| panic!("有效 hold 應阻止 restart-on-failure 重拉：{error:?}"));
    assert_eq!(report.chain.len(), 1);
    assert_eq!(stop_count(&t, "daemon.handoff_stop"), 1);
    assert_eq!(hard_stop_count(&t), 0);
    assert_eq!(t.service().hold_state_now(), HoldState::Held);
}

#[test]
fn graceful_handoff_hard_stop_only_same_identity() {
    // 情境 1：交接後 10 s 仍為同一 identity（4242）→ hard_stop 恰 1 次。
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_daemon_alive_forever(POSIX_EP, 4242);
    t.service().stop_rpc_on_hard_stop(
        t.rpc().clone(),
        &t.isolated_endpoint(POSIX_EP),
    );
    t.rpc()
        .respond(&t.isolated_endpoint(POSIX_EP), "daemon.handoff_stop", GRANT);
    let r = t.with_cli_ctx(|ctx| {
        graceful_stop_with(
            ctx,
            &SupervisionMode::SystemdUser,
            POSIX_EP,
            false,
            false,
            "service_stop_wait_stop",
            StopRpc::Handoff { reason: "update" },
        )
    });
    assert!(r.is_ok(), "同一 identity 的 hard_stop 路徑應成功返回");
    assert_eq!(hard_stop_count(&t), 1);

    // 情境 2：偵測到 5151 後先重新交接；取得 5151 的 grant 且仍為同一 identity 時可 hard_stop。
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_daemon_alive_forever(POSIX_EP, 4242)
        .with_identity_swap_after(10.0, 4242, 5151);
    t.service().stop_rpc_on_hard_stop(
        t.rpc().clone(),
        &t.isolated_endpoint(POSIX_EP),
    );
    t.rpc()
        .respond(&t.isolated_endpoint(POSIX_EP), "daemon.handoff_stop", GRANT);
    let r = t.with_cli_ctx(|ctx| {
        graceful_stop_with(
            ctx,
            &SupervisionMode::SystemdUser,
            POSIX_EP,
            false,
            false,
            "service_stop_wait_stop",
            StopRpc::Handoff { reason: "update" },
        )
    });
    let report = r.unwrap_or_else(|error| panic!("5151 取得新 grant 後可停止：{error:?}"));
    assert!(!report.graceful, "5151 超時後應沿用新 grant hard_stop");
    assert_eq!(report.chain.len(), 2, "逾時必須保留每個 identity 的交接鏈");
    assert_eq!(report.chain[1].pid, Some(5151));
    assert_eq!(
        stop_count(&t, "daemon.handoff_stop"),
        2,
        "必須對 5151 重新交接"
    );
    assert_eq!(hard_stop_count(&t), 1, "只 hard_stop 已取得最後 grant 的 5151");
}

#[test]
fn graceful_handoff_relaunch_loop_capped() {
    // 每次交接 grant 後都有新 pid 被拉起 → 第 MAX_HANDOFF_CHAIN 次之後回 RelaunchLoop。
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_supervisor_ignores_hold()
        .with_restart_on_failure()
        .with_daemon_exit_code_after_handoff(1)
        .with_idle_daemon(POSIX_EP, 4242)
        .with_new_daemon_handoff_pids(&[9000, 9001, 9002]);
    let r = t.with_cli_ctx(|ctx| {
        graceful_stop_with(
            ctx,
            &SupervisionMode::SystemdUser,
            POSIX_EP,
            false,
            false,
            "service_stop_wait_stop",
            StopRpc::Handoff { reason: "update" },
        )
    });
    let failure = r.expect_err("交接鏈超過上限時應停止");
    match failure.cause() {
        StopFailure::RelaunchLoop { pids } => assert_eq!(pids.len(), 3),
        other => panic!("RelaunchLoop 預期，實得 {other:?}"),
    }
    assert!(failure.hold().is_some());
    assert_eq!(failure.chain().len(), 3);
    assert_eq!(
        stop_count(&t, "daemon.handoff_stop"),
        3,
        "交接 RPC 恰 MAX_HANDOFF_CHAIN 次"
    );
    assert_eq!(hard_stop_count(&t), 0);
}

#[test]
fn graceful_handoff_relaunched_busy_is_refused() {
    // 被拉起的 pid 9000 交接回 FLASHING_BUSY → 不硬停、不送 daemon.stop，9000 仍存活。
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_idle_daemon(POSIX_EP, 4242)
        .with_supervisor_ignores_hold()
        .with_restart_on_failure()
        .with_daemon_exit_code_after_handoff(1);
    t.rpc().respond_seq(
        &t.isolated_endpoint(POSIX_EP),
        "daemon.handoff_stop",
        &[GRANT, BUSY],
    );
    let r = t.with_cli_ctx(|ctx| {
        graceful_stop_with(
            ctx,
            &SupervisionMode::SystemdUser,
            POSIX_EP,
            false,
            false,
            "service_stop_wait_stop",
            StopRpc::Handoff { reason: "update" },
        )
    });
    let failure = r.expect_err("重新啟動的 daemon 忙碌時應拒絕");
    match failure.cause() {
        StopFailure::Relaunched {
            pid: 9000,
            err: HandoffError::Busy { ref code, ref ops, ref busy },
        } => {
            assert_eq!(code, "FLASHING_BUSY");
            assert_eq!(ops, &vec!["flash".to_string()]);
            assert_eq!(busy.len(), 1);
            assert_eq!(fstr(&busy[0], "com"), "COM0");
        }
        other => panic!("Relaunched(9000, Busy FLASHING_BUSY) 預期，實得 {other:?}"),
    }
    assert!(failure.hold().is_some());
    assert_eq!(failure.chain().len(), 1);
    assert_eq!(hard_stop_count(&t), 0);
    assert_eq!(stop_count(&t, "daemon.stop"), 0);
}

#[test]
fn on_demand_handoff_waits_for_granted_instance_ordered_shutdown() {
    // S1 出口 A/B B 輪（2026-10-30，test_revert_new_daemon_push_not_aborted_l15）：grant 後 3 s 排空窗口內仍回 ping，
    // 之後關閉監聽；SC-11 有序關機（bridge drain、thread join、WAL fsync）使行程於 grant 後 12 s 才結束。
    // on-demand 沒有 hard_stop：舊實作自 grant 起固定 10 s 即回 Timeout（recover_journal 報 handoff_timeout）。
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::OnDemand, true, true)
        .with_idle_daemon(POSIX_EP, 4242);
    ordered_shutdown_after_grant(&t, POSIX_EP, 4242, Duration::from_secs(3), Some(Duration::from_secs(9)));
    let report = on_demand_revert_handoff(&t)
        .unwrap_or_else(|e| panic!("已凍結、正在有序關機的實例應等到行程結束：{e:?}"));
    assert!(report.graceful, "未 hard_stop 即結束");
    assert!(t.clock().elapsed() >= Duration::from_secs(12), "須等到行程結束：{:?}", t.clock().elapsed());
    assert_eq!(stop_count(&t, "daemon.handoff_stop"), 1, "不得對同一實例重送交接");
    assert_eq!(hard_stop_count(&t), 0);
}

#[test]
fn on_demand_handoff_exit_wait_is_bounded_from_listener_close() {
    // 監聽已關閉但行程永不結束：自監聽關閉起算 HANDOFF_EXIT_WAIT 後回 Timeout（有界、不硬停、不重送交接）。
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::OnDemand, true, true)
        .with_idle_daemon(POSIX_EP, 4242);
    ordered_shutdown_after_grant(&t, POSIX_EP, 4242, Duration::from_secs(3), None);
    let failure = on_demand_revert_handoff(&t).expect_err("行程永不結束時必須逾時");
    assert!(matches!(failure.cause(), StopFailure::Timeout { .. }), "{failure:?}");
    let elapsed = t.clock().elapsed();
    let floor = Duration::from_secs(3) + HANDOFF_EXIT_WAIT;
    assert!(elapsed >= floor && elapsed < floor + Duration::from_secs(2), "{elapsed:?}");
    assert_eq!(stop_count(&t, "daemon.handoff_stop"), 1);
    assert_eq!(hard_stop_count(&t), 0);
}

#[test]
fn on_demand_handoff_still_answering_keeps_grant_limit() {
    // grant 後一直回 ping（未進入停止序列）：仍以 grant 起算 10 s 回 Timeout，結束等待不延長此情形。
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::OnDemand, true, true)
        .with_daemon_alive_forever(POSIX_EP, 4242);
    let failure = on_demand_revert_handoff(&t).expect_err("仍在服務的實例必須逾時");
    assert!(matches!(failure.cause(), StopFailure::Timeout { .. }), "{failure:?}");
    let elapsed = t.clock().elapsed();
    assert!(elapsed >= Duration::from_secs(10) && elapsed < Duration::from_secs(12), "{elapsed:?}");
    assert_eq!(hard_stop_count(&t), 0);
}
