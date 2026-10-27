//! P7 Task 16-wire（08-slice23-login-events.md）：Event Engine 隨 daemon 啟停、`BridgeQueries for SessionManager`。
//!
//! 偏離計畫原文：計畫的 `shutdown_closes_event_intake_before_drain` 以 trace 新增 `EngineCloseIntake`／`EngineStop` 斷言；
//! P5 `daemon_process::sigterm_graceful_exit_0_and_flushes_queue` 鎖定停機 trace 末 6 筆，本 Phase 不新增 trace 事件，
//! 停止序改以行為斷言（close_intake 後不再 spawn、與 drain 共用期限的終止時間）。完整停止序權威為 P8 Task 12。
#![cfg(feature = "test-fakes")]
mod support;

use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

use support::*;
use sw_core::event_engine::BridgeQueries;
use sw_daemon::daemon_main::{run_with_hooks, DaemonHooks};
use sw_pal::traits::DeviceInfo;

fn device(by_id: &str, real_path: &str) -> (String, DeviceInfo) {
    (
        by_id.to_string(),
        DeviceInfo {
            by_id: by_id.to_string(),
            real_path: real_path.to_string(),
        },
    )
}

fn no_hooks() -> DaemonHooks {
    DaemonHooks {
        before_state_commit: None,
        after_state_commit: None,
        trace: None,
    }
}

fn events_log(env: &IsolatedEnv) -> PathBuf {
    PathBuf::from(&env.vars["SERIALWRAP_EVENTS_RUNTIME_DIR"]).join("events.ndjson")
}

fn write_rule(env: &IsolatedEnv, name: &str, handler: &str) {
    let dir = PathBuf::from(&env.vars["SERIALWRAP_EVENTS_DIR"]);
    std::fs::write(
        dir.join(format!("o.{name}.json")),
        format!(
            r#"{{"schema_version": 1, "owner": "o", "name": "{name}", "kind": "tool", "selectors": ["COM0"],
                "pattern": {{"kind": "contains", "value": "__READY__"}}, "handler": {handler}}}"#
        ),
    )
    .unwrap();
}

fn types(path: &Path) -> Vec<String> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .filter_map(|l| {
            let v = sw_proto::pyjson::loads(l).ok()?;
            v.get("type").and_then(|t| t.as_str()).map(str::to_string)
        })
        .collect()
}

fn wait_for(what: &str, limit: Duration, f: impl FnMut() -> bool) {
    assert!(poll(limit, f), "等待逾時：{what}");
}

/// 輪詢至條件成立或逾時；client thread 用此結果決定斷言，但一律送出 `daemon.stop`（避免 daemon 永不結束）。
fn poll(limit: Duration, mut f: impl FnMut() -> bool) -> bool {
    let end = Instant::now() + limit;
    while !f() {
        if Instant::now() >= end {
            return false;
        }
        thread::sleep(Duration::from_millis(20));
    }
    true
}

#[test]
fn engine_started_before_bootstrap_attach() {
    // Python `service.start()`：engine.start → add_rx_observer → scan／bootstrap（parity #121）；bootstrap attach 的
    // ready probe 回應（含 `__READY__<nonce>` 行）即被 engine 看到。
    let env = isolated_env();
    write_rule(&env, "boot", r#"{"exec": ["true"]}"#);
    let platform = in_process_platform(
        &env.vars,
        vec![device("usb-SER-A-if00", "/dev/pts/10")],
        Fixture::ReadyShellBlockingWithQueue,
    );
    let socket = env.socket.clone();
    let log = events_log(&env);
    let log2 = log.clone();
    let client = thread::spawn(move || {
        wait_socket(&socket, Duration::from_secs(10));
        let seen = poll(Duration::from_secs(10), || {
            types(&log2).iter().any(|t| t == "match_recorded")
        });
        rpc_once(&socket, STOP);
        seen
    });
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, no_hooks());
    assert!(
        client.join().unwrap(),
        "bootstrap attach 的 RX 未被 engine 看到"
    );
    assert_eq!(code, 0);
    let t = types(&log);
    assert_eq!(&t[..2], ["rule_loaded", "com_enabled"], "{t:?}");
    assert!(t.contains(&"match_recorded".to_string()), "{t:?}");
}

#[test]
fn shutdown_aborts_long_handler_within_budget() {
    // SC-11 細化：handler 與 drain 共用 5 s 期限，逾時終止整個 process group 後 100 ms 寬限內返回。
    let env = isolated_env();
    let marker = format!("swp7life{}", std::process::id());
    write_rule(
        &env,
        "long",
        &format!(r#"{{"exec": ["/bin/sh", "-c", "sleep 60; : {marker}"]}}"#),
    );
    let platform = in_process_platform(
        &env.vars,
        vec![device("usb-SER-A-if00", "/dev/pts/10")],
        Fixture::ReadyShellBlockingWithQueue,
    );
    let socket = env.socket.clone();
    let m2 = marker.clone();
    let client = thread::spawn(move || {
        wait_socket(&socket, Duration::from_secs(10));
        let running = poll(Duration::from_secs(10), || marker_alive(&m2));
        let t0 = Instant::now();
        rpc_once(&socket, STOP);
        (running, t0)
    });
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, no_hooks());
    let (running, stop_sent) = client.join().unwrap();
    assert!(running, "handler 未啟動");
    let took = stop_sent.elapsed();
    assert_eq!(code, 0, "aborted handler 已回收（unreaped 空）");
    assert!(took < Duration::from_millis(5600), "{took:?}");
    wait_for(
        "handler 行程樹已不存在",
        Duration::from_secs(5),
        || !marker_alive(&marker),
    );
    assert!(types(&events_log(&env)).contains(&"fire_timeout".to_string()));
}

fn marker_alive(marker: &str) -> bool {
    let Ok(dir) = std::fs::read_dir("/proc") else {
        return false;
    };
    dir.flatten().any(|e| {
        let cmd = std::fs::read(e.path().join("cmdline")).unwrap_or_default();
        let stat = std::fs::read_to_string(e.path().join("stat")).unwrap_or_default();
        let zombie = stat
            .rsplit_once(')')
            .is_some_and(|(_, rest)| rest.trim_start().starts_with('Z'));
        !zombie && String::from_utf8_lossy(&cmd).contains(marker)
    })
}

#[test]
fn bridge_queries_reflect_manager_state() {
    let rig = ServiceRig::two_shells();
    let q: &dyn BridgeQueries = &*rig.mgr;
    assert_eq!(q.known_coms(), ["COM0", "COM1"], "sorted(com)");
    assert_eq!(q.profile_for("COM0").as_deref(), Some("t"));
    assert_eq!(q.profile_for("COM9"), None);
    assert_eq!(q.active_cmd_id_for("COM0"), None);
    // 假 DUT 不回應此命令：前景等到 timeout_s（1 s）才結束。
    let sub = rig.call(
        "command.submit",
        r#"{"selector":"COM0","cmd":"sleep 9","timeout_s":1}"#,
    );
    assert_eq!(
        sub.get("ok"),
        Some(&sw_proto::pyjson::PyValue::Bool(true)),
        "{sub:?}"
    );
    wait_until("COM0 前景中", || {
        q.active_cmd_id_for("COM0").as_deref() == Some("foreground")
    });
    assert_eq!(q.active_cmd_id_for("COM1"), None, "閒置 com");
    assert_eq!(q.active_cmd_id_for("COM9"), None, "不存在的 com");
    // rig 為 FakeClock：推進虛擬時間讓 1 s 逾時到期。
    wait_until("前景結束", || {
        rig.clock.advance(Duration::from_millis(200));
        rig.mgr.on_tick();
        q.active_cmd_id_for("COM0").is_none()
    });
}
