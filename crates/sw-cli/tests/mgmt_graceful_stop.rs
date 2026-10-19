//! P4-T7 共用 `graceful_stop`（§12.1 步驟 3）與 windows-task 停止／重新啟動路徑的失敗測試（SC-10、SC-17、SC-18、SC-20；R1-F1、R5-F1）。
//!
//! 紅燈階段（TDD）：`sw_cli::graceful_stop` 的本體為 `todo!("P4-T7: …")`，`service stop|restart` 目前仍由
//! `DELEGATED_GROUPS` 委派（Python sentinel），因此 `t.run` 類案例以斷言不符失敗；直接呼叫 `graceful_stop_with` 的案例
//! 以 `not implemented: P4-T7` panic 失敗。R5-F1 拉起模型（`with_restart_on_failure` 等）與 identity 置換模型
//! 屬 `tests/support/mod.rs` 的 todo（P4-T12／P4-T7），於實作時替換。
//! 規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 7；盤點 `inventory/07-platform-mgmt.md` §5.3、§12。
mod support;

use std::time::Duration;

use support::*;
use sw_cli::graceful_stop::{graceful_stop_with, HandoffError, StopFailure, StopRpc};
use sw_pal::fakes::real_fs;
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
const GRANT: &str = r#"{"ok":true,"stopping":true,"handoff_token":"0123456789abcdef0123456789abcdef"}"#;
/// 交接被忙碌拒絕（`FLASHING_BUSY`＋`ops:["flash"]`）。
const BUSY: &str = r#"{"ok":false,"error_code":"FLASHING_BUSY","ops":["flash"]}"#;

fn stop_count(t: &TestEnv, method: &str) -> usize {
    t.rpc()
        .requests()
        .iter()
        .filter(|r| r.1 == method)
        .count()
}

fn hard_stop_count(t: &TestEnv) -> usize {
    t.service()
        .actions()
        .iter()
        .filter(|a| a.1 == "hard_stop")
        .count()
}

#[test]
fn windows_task_stop_sends_daemon_stop_before_end() {
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_real_service()
        .with_daemon_alive_then_gone(TestEnv::win_endpoint(), 900, 3);
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
        let ep = if windows_mode { TestEnv::win_endpoint() } else { POSIX_EP };
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
        assert!(
            o.index_of("service:hold_autostart") < o.index_of("rpc:daemon.handoff_stop"),
            "hold 必須先於第一個交接 RPC（mode={mode:?}）"
        );
        assert_eq!(stop_count(&t, "daemon.stop"), 0, "交接路徑不得送 daemon.stop（mode={mode:?}）");
        assert!(report.hold.is_some(), "Handoff 回傳的 hold 必須保持生效（mode={mode:?}）");
        assert_eq!(
            t.service().autostart_hold_state(&mode),
            HoldState::Held,
            "Handoff 不得自行 release（mode={mode:?}）"
        );
        if matches!(mode, SupervisionMode::OnDemand) {
            // on-demand：autostart.lock 於交接 RPC 前取得，返回後仍持有（另一 handle 的非阻塞取鎖必須失敗）。
            let lock = t.run_dir().join("autostart.lock");
            assert!(
                real_fs().file_lock_exclusive(&lock, false).is_err(),
                "on-demand 交接後 autostart.lock 必須仍被持有"
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
    assert!(matches!(r, Err(StopFailure::Hold(_))), "hold 失敗必須回 StopFailure::Hold");
    assert_eq!(stop_count(&t, "daemon.handoff_stop"), 0);
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
    assert_eq!(stop_count(&t, "daemon.handoff_stop"), 2, "第 2 次交接送往 pid 9000");
    assert_eq!(hard_stop_count(&t), 0);
}

#[test]
fn graceful_handoff_hard_stop_only_same_identity() {
    // 情境 1：交接後 10 s 仍為同一 identity（4242）→ hard_stop 恰 1 次。
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_daemon_alive_forever(POSIX_EP, 4242);
    t.rpc().respond(&t.isolated_endpoint(POSIX_EP), "daemon.handoff_stop", GRANT);
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

    // 情境 2：等待逾時後重新 capture 得到不同 identity（5151，start_ticks 不同）→ 不 hard_stop，對 5151 重新交接。
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_daemon_alive_forever(POSIX_EP, 4242)
        .with_identity_swap_after(10.0, 4242, 5151);
    t.rpc().respond(&t.isolated_endpoint(POSIX_EP), "daemon.handoff_stop", GRANT);
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
    assert!(r.is_err(), "5151 仍存活時不得視為已停止");
    assert_eq!(hard_stop_count(&t), 0, "identity 不同時不得 hard_stop");
    assert_eq!(stop_count(&t, "daemon.handoff_stop"), 2, "必須對 5151 重新交接");
}

#[test]
fn graceful_handoff_relaunch_loop_capped() {
    // 每次交接 grant 後都有新 pid 被拉起（4 次）→ 第 MAX_HANDOFF_CHAIN 次之後回 RelaunchLoop。
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
    match r {
        Err(StopFailure::RelaunchLoop { pids }) => assert_eq!(pids.len(), 3),
        other => panic!("RelaunchLoop 預期，實得 {other:?}"),
    }
    assert_eq!(stop_count(&t, "daemon.handoff_stop"), 3, "交接 RPC 恰 MAX_HANDOFF_CHAIN 次");
    assert_eq!(hard_stop_count(&t), 0);
}

#[test]
fn graceful_handoff_relaunched_busy_is_refused() {
    // 被拉起的 pid 9000 交接回 FLASHING_BUSY → 不硬停、不送 daemon.stop，9000 仍存活。
    let t = TestEnv::posix()
        .with_supervisor_state(SupervisionMode::SystemdUser, true, true)
        .with_idle_daemon(POSIX_EP, 4242)
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
    match r {
        Err(StopFailure::Relaunched {
            pid: 9000,
            err: HandoffError::Busy { ref code, ref ops },
        }) => {
            assert_eq!(code, "FLASHING_BUSY");
            assert_eq!(ops, &vec!["flash".to_string()]);
        }
        other => panic!("Relaunched(9000, Busy FLASHING_BUSY) 預期，實得 {other:?}"),
    }
    assert_eq!(hard_stop_count(&t), 0);
    assert_eq!(stop_count(&t, "daemon.stop"), 0);
}
