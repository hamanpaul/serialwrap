//! P5-T19：`serialwrapd` 行程層測試（啟動順序、有序關機、exit code、隔離 allowlist、panic 隔離）。
//!
//! 真 daemon 案例以 `CARGO_BIN_EXE_serialwrapd` 實際起行程；in-process 案例以 `run_with_hooks` 與 trace 驗證順序。
//! 紅燈階段：`daemon_main::run`／`run_with_hooks` 為 `unimplemented!`，故案例於執行期失敗（非編譯錯誤）。
#![cfg(feature = "test-fakes")]

mod support;

use std::fs;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use support::*;
use sw_pal::traits::DeviceInfo;
use sw_proto::pyjson::{self, PyValue};

const ALLOWLIST: &str = "SERIALWRAP_TEST_DEVICE_ALLOWLIST";

fn device(by_id: &str, real_path: &str) -> (String, DeviceInfo) {
    (
        by_id.to_string(),
        DeviceInfo {
            by_id: by_id.to_string(),
            real_path: real_path.to_string(),
        },
    )
}

fn trace_hooks(sink: Arc<Mutex<Vec<TraceEvent>>>) -> DaemonHooks {
    DaemonHooks {
        before_state_commit: None,
        after_state_commit: None,
        trace: Some(Box::new(move |ev| sink.lock().unwrap().push(ev))),
    }
}

/// 每 100 ms 輪詢 `cond`，逾時 panic。
fn wait_until(what: &str, timeout: Duration, mut cond: impl FnMut() -> bool) {
    let deadline = Instant::now() + timeout;
    while !cond() {
        if Instant::now() >= deadline {
            panic!("{what} 未在 {timeout:?} 內成立");
        }
        thread::sleep(Duration::from_millis(100));
    }
}

/// 以 sw_pal 的 RPC 連線送出 `payload` 後立即斷線（client 在 handler 完成前離開）。
fn send_and_drop(socket: &std::path::Path, payload: &str) {
    let platform = sw_pal::native();
    let endpoint = sw_pal::traits::EndpointSpec::Unix(socket.as_os_str().to_owned());
    let mut stream = platform
        .rpc
        .connect(&endpoint, sw_pal::traits::SockTimeout::Blocking(SHORT))
        .expect("連線 daemon");
    stream.send_all(payload.as_bytes()).expect("送出 pipelined 請求");
}

fn last_wal_line(env: &IsolatedEnv) -> String {
    let wal = fs::read_to_string(env.wal_dir.join("raw.wal.ndjson")).expect("讀取 raw.wal.ndjson");
    wal.lines().last().unwrap_or("").to_string()
}

#[test]
fn second_daemon_same_lock_exits_2_with_stderr() {
    let env = isolated_env();
    let mut a = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);
    let out = run_to_exit(&env, &env.vars, &[]);
    assert_eq!(out.code, Some(2));
    assert_eq!(out.stderr, "serialwrapd: DAEMON_ALREADY_RUNNING\n");
    rpc_once(&env.socket, STOP);
    assert_eq!(a.wait(LONG).code(), Some(0));
}

#[test]
fn unreadable_state_json_exits_3_and_releases_lock() {
    let env = isolated_env();
    fs::create_dir_all(env.state_dir.join("state.json")).unwrap(); // 讀取失敗
    let out = run_to_exit(&env, &env.vars, &[]);
    assert_eq!(out.code, Some(3));
    assert!(out.stderr.starts_with("serialwrapd: 無法讀取既有 state.json（"), "{}", out.stderr);
    // 鎖已釋放：第二次啟動仍走到讀取失敗，而不是 DAEMON_ALREADY_RUNNING。
    let again = run_to_exit(&env, &env.vars, &[]);
    assert_eq!(again.code, Some(3), "{}", again.stderr);
}

#[test]
fn no_profiles_message_and_continue() {
    let env = isolated_env();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);
    d.wait_stderr_contains("serialwrapd: no profiles loaded\n", SHORT);
    assert!(rpc_value(&env.socket, "health.ping", "{}")["ok"] == true);
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

#[test]
fn unknown_arg_exit_2_usage() {
    let env = isolated_env();
    let out = run_to_exit(&env, &env.vars, &["--bogus"]);
    assert_eq!(out.code, Some(2));
    assert_eq!(
        out.stderr,
        "usage: serialwrapd [-h] [--profile-dir PROFILE_DIR] [--socket SOCKET] [--lock LOCK]\n\
         serialwrapd: error: unrecognized arguments: --bogus\n"
    );
}

#[test]
fn no_args_uses_env_paths() {
    let env = isolated_env();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);
    assert_eq!(env.socket, env.run_dir.join("serialwrapd.sock"));
    assert!(env.run_dir.join("serialwrapd.lock").exists(), "lock 應位於 $SERIALWRAP_RUN_DIR");
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

#[test]
fn endpoint_written_after_bind() {
    let env = isolated_env();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);
    let cfg = fs::read_to_string(env.config_dir.join("config.yaml")).expect("config.yaml 應於 bind 後寫入");
    let want = format!("socket_path: {}", env.socket.display());
    assert!(cfg.lines().any(|l| l == want), "{cfg}");
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));

    // config 目錄唯讀：寫入失敗為非致命，daemon 仍存活。
    if is_root() {
        eprintln!("skip: root 下 chmod 無效，寫入失敗路徑證據不足（非 PASS）");
        return;
    }
    let env2 = isolated_env();
    chmod(&env2.config_dir, "555");
    let mut d2 = DaemonProc::spawn(&env2, &[]);
    wait_socket(&env2.socket, SHORT);
    d2.wait_stderr_contains("serialwrapd: 寫入 config.yaml endpoint 失敗（非致命）: ", SHORT);
    assert!(rpc_value(&env2.socket, "health.ping", "{}")["ok"] == true, "寫入失敗不得殺死 daemon");
    rpc_once(&env2.socket, STOP);
    assert_eq!(d2.wait(LONG).code(), Some(0));
}

#[test]
fn serialwrap_config_env_read_and_write_target() {
    let env = isolated_env();

    // A：SERIALWRAP_CONFIG 原樣勝出（即使指向新檔）：讀此檔、endpoint 寫此檔、CONFIG_DIR/config.yaml 不建立。
    let alt = env.root.join("alt.yaml");
    fs::write(&alt, "windows:\n  exclude_coms: [COM3]\n  test_device_allowlist: [\"/x/by-id/b-alt\"]\n").unwrap();
    let mut vars = env.vars.clone();
    vars.insert("SERIALWRAP_CONFIG".to_string(), alt.display().to_string());
    let mut d = DaemonProc::spawn_with(&env, &vars, &[]);
    wait_socket(&env.socket, SHORT);
    let h = health_status(&env.socket);
    assert_eq!(h["test_device_allowlist"]["source"], "config");
    assert_eq!(h["test_device_allowlist"]["coms"][0], "/x/by-id/b-alt");
    let text = fs::read_to_string(&alt).unwrap();
    let want = format!("socket_path: {}", env.socket.display());
    assert!(text.lines().any(|l| l == want), "endpoint 應寫入 SERIALWRAP_CONFIG：{text}");
    assert!(!env.config_dir.join("config.yaml").exists(), "SERIALWRAP_CONFIG 生效時不得建立 CONFIG_DIR/config.yaml");
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));

    // B：未設 SERIALWRAP_CONFIG → CONFIG_DIR（不存在）→ 系統檔（唯讀 fallback）；永不寫入系統檔。
    let sys_yaml = "windows:\n  test_device_allowlist: [\"/sys/by-id/c\"]\n";
    fs::create_dir_all(env.system_config.parent().unwrap()).unwrap();
    fs::write(&env.system_config, sys_yaml).unwrap();
    let mut d2 = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);
    let h2 = health_status(&env.socket);
    assert_eq!(h2["test_device_allowlist"]["source"], "config");
    assert_eq!(h2["test_device_allowlist"]["coms"][0], "/sys/by-id/c");
    assert_eq!(fs::read_to_string(&env.system_config).unwrap(), sys_yaml, "系統檔不得被寫入");
    assert!(env.config_dir.join("config.yaml").exists(), "endpoint 應寫入 CONFIG_DIR/config.yaml");
    rpc_once(&env.socket, STOP);
    assert_eq!(d2.wait(LONG).code(), Some(0));
}

#[test]
fn startup_order_scan_rank_watch() {
    let env = isolated_env();
    let devices = vec![
        device("usb-SER-C-if00", "/dev/pts/12"),
        device("usb-SER-A-if00", "/dev/pts/10"),
        device("usb-SER-B-if00", "/dev/pts/11"),
    ];
    let sink = Arc::new(Mutex::new(Vec::new()));
    let platform = in_process_platform(&env.vars, devices, Fixture::None);
    let socket = env.socket.clone();
    let client = thread::spawn(move || {
        wait_socket(&socket, SHORT);
        let sessions = session_list(&socket);
        let rows: Vec<PyValue> = sessions.as_array().cloned().unwrap_or_default();
        rpc_once(&socket, STOP);
        rows
    });
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, trace_hooks(sink.clone()));
    let rows = client.join().expect("client thread");
    assert_eq!(code, 0);

    // COM 依 by-id 排序（#100）：usb-SER-A 必為 COM0。
    let a = rows
        .iter()
        .find(|s| s["device_by_id"] == "usb-SER-A-if00")
        .expect("應有 usb-SER-A-if00 的 session");
    assert_eq!(a["com"], "COM0");

    // DF-14：scan → rank → update → bootstrap_attach → watcher.start，之後才 bound 與寫 endpoint。
    let got: Vec<TraceEvent> = sink.lock().unwrap().iter().take(7).cloned().collect();
    assert_eq!(
        got,
        vec![
            TraceEvent::InitialScan,
            TraceEvent::Rank,
            TraceEvent::UpdateDevices,
            TraceEvent::BootstrapAttach,
            TraceEvent::WatcherStart,
            TraceEvent::Bound,
            TraceEvent::EndpointWritten,
        ]
    );
}

#[test]
fn sigterm_graceful_exit_0_and_flushes_queue() {
    let env = isolated_env();
    let sink = Arc::new(Mutex::new(Vec::new()));
    let platform = in_process_platform(
        &env.vars,
        vec![device("usb-SER-A-if00", "/dev/pts/10")],
        Fixture::ReadyShellBlockingWithQueue,
    );
    let socket = env.socket.clone();
    let client = thread::spawn(move || {
        wait_socket(&socket, SHORT);
        // 前景命令 BlockUntilCancel：RPC 不回應，直到停機取消。
        let s1 = socket.clone();
        let fg = thread::spawn(move || submit(&s1, "COM0", "blk"));
        thread::sleep(Duration::from_millis(300));
        // 另排隊兩筆。
        let s2 = socket.clone();
        let q2 = thread::spawn(move || submit(&s2, "COM0", "q2"));
        let s3 = socket.clone();
        let q3 = thread::spawn(move || submit(&s3, "COM0", "q3"));
        thread::sleep(Duration::from_millis(300));
        rpc_once(&socket, STOP);
        let _ = fg.join();
        let c2 = q2.join().expect("q2")["cmd_id"].as_str().unwrap_or_default().to_string();
        let c3 = q3.join().expect("q3")["cmd_id"].as_str().unwrap_or_default().to_string();
        (c2, c3)
    });
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, trace_hooks(sink.clone()));
    let (c2, c3) = client.join().expect("client thread");
    assert_eq!(code, 0);

    // 停機序列：server → stopping gate → watcher → drain（排隊命令 FLUSHED_BY_SHUTDOWN）→ WAL sync → 釋放鎖。
    let t = sink.lock().unwrap().clone();
    let tail = t.len().saturating_sub(6);
    assert_eq!(
        t[tail..].to_vec(),
        vec![
            TraceEvent::ServerStopped,
            TraceEvent::ShutdownGate,
            TraceEvent::WatcherStopped,
            TraceEvent::SessionsDrained {
                flushed: vec![
                    (c2, "FLUSHED_BY_SHUTDOWN".to_string()),
                    (c3, "FLUSHED_BY_SHUTDOWN".to_string()),
                ],
                stalled: vec![],
            },
            TraceEvent::WalShutdown { ok: true },
            TraceEvent::LockReleased,
        ]
    );
}

#[test]
fn real_sigterm_to_started_daemon_exits_0_after_flush() {
    let env = isolated_env_with_pty_target();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_ready(&env, "COM0");
    let socket = env.socket.clone();

    // 前景命令不被假 DUT 回應（等同 BlockUntilCancel），並排隊兩筆。
    let s1 = socket.clone();
    let fg = thread::spawn(move || submit(&s1, "COM0", "blk-never-returns"));
    thread::sleep(Duration::from_millis(300));
    let queued: Vec<_> = (0..2)
        .map(|i| {
            let s = socket.clone();
            thread::spawn(move || submit(&s, "COM0", &format!("queued-{i}")))
        })
        .collect();
    thread::sleep(Duration::from_millis(300));

    let status = Command::new("kill")
        .arg("-TERM")
        .arg(d.pid().to_string())
        .status()
        .expect("送出 SIGTERM");
    assert!(status.success());
    assert_eq!(d.wait(LONG).code(), Some(0), "SIGTERM 後應 exit 0");
    let _ = fg.join();
    for h in queued {
        let _ = h.join();
    }

    // 停機後 WAL 最後一筆完整，且排隊命令已寫入 FLUSHED_BY_SHUTDOWN。
    let last = last_wal_line(&env);
    assert!(pyjson::loads(&last).is_ok(), "WAL 最後一筆不完整：{last}");
    let wal = fs::read_to_string(env.wal_dir.join("raw.wal.ndjson")).unwrap();
    assert!(wal.contains("FLUSHED_BY_SHUTDOWN"), "排隊命令應被沖刷並寫入 WAL");

    // 鎖已釋放：新 daemon 可取得。
    let mut d2 = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);
    rpc_once(&env.socket, STOP);
    assert_eq!(d2.wait(LONG).code(), Some(0));
}

#[test]
fn shutdown_with_stalled_drain_exits_1_with_diagnostic() {
    if std::env::var("S21_CHILD").as_deref() == Ok("stall") {
        let vars = process_vars();
        let sink = Arc::new(Mutex::new(Vec::new()));
        let platform = in_process_platform(
            &vars,
            vec![device("usb-SER-A-if00", "/dev/pts/10")],
            Fixture::BlockForeverWriter,
        );
        let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, trace_hooks(sink));
        std::process::exit(code);
    }
    let env = isolated_env();
    let out = run_child_test("shutdown_with_stalled_drain_exits_1_with_diagnostic", "stall", &env);
    assert_eq!(out.code, Some(1), "不得宣告 graceful success：{}", out.stderr);
    assert!(
        out.stderr.contains("serialwrapd: shutdown incomplete: drain stalled: t:COM0"),
        "{}",
        out.stderr
    );
    // 鎖仍釋放：新 daemon 可啟動。
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

#[test]
fn shutdown_wal_sync_failure_exits_1() {
    if std::env::var("S21_CHILD").as_deref() == Ok("walsync") {
        let vars = process_vars();
        let platform = in_process_platform(
            &vars,
            vec![device("usb-SER-A-if00", "/dev/pts/10")],
            Fixture::WalSyncFailure,
        );
        let sink = Arc::new(Mutex::new(Vec::new()));
        let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, trace_hooks(sink));
        std::process::exit(code);
    }
    let env = isolated_env();
    let out = run_child_test("shutdown_wal_sync_failure_exits_1", "walsync", &env);
    assert_eq!(out.code, Some(1), "{}", out.stderr);
    assert!(
        out.stderr.contains("serialwrapd: shutdown incomplete: WAL sync failed: "),
        "{}",
        out.stderr
    );
}

#[test]
fn shutdown_releases_tty_before_exit() {
    let env = isolated_env_with_pty_target();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_ready(&env, "COM0");
    // 假 DUT（PTY master）全程由測試持有；daemon 停機後 slave 必須可被重新開啟。
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
    assert!(
        pty_can_open_exclusively(&env.pty_slave),
        "停機後 TIOCEXCL 應已解除（CloseToken 須執行 TIOCNXCL），slave={}",
        env.pty_slave.display()
    );
}

#[test]
fn wal_dir_readonly_at_start_blocks_login_probe() {
    if is_root() {
        eprintln!("skip: root 下 chmod 無效，唯讀 WAL 路徑證據不足（非 PASS；P5-T19 Review Focus 3）");
        return;
    }
    let mut env = isolated_env_with_pty_target();
    chmod(&env.wal_dir, "555");
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);

    let h = health_status(&env.socket);
    assert!(h["wal"]["healthy"] == false, "{h:?}");
    assert_eq!(h["wal"]["unhealthy_reason"], "dir_unwritable");

    // 登入 probe 不送：假 DUT 收到 0 bytes；session 停在 ATTACHED，last_error=PROMPT_UNAVAILABLE。
    let socket = env.socket.clone();
    wait_until("COM0 PROMPT_UNAVAILABLE", LONG, || {
        let s = get_state(&socket, "COM0");
        s["state"] == "ATTACHED" && s["last_error"] == "PROMPT_UNAVAILABLE"
    });
    assert!(env.target_rx().is_empty(), "WAL 不健康時不得送出登入 probe");

    // agent 命令：SESSION_NOT_READY，且仍 0 bytes。
    let r = submit(&env.socket, "COM0", "echo x");
    assert_eq!(r["error_code"], "SESSION_NOT_READY");
    assert!(env.target_rx().is_empty());

    chmod(&env.wal_dir, "755");
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

#[test]
fn wal_fault_after_ready_splits_agent_and_auto_tx() {
    if is_root() {
        eprintln!("skip: root 下 chmod 無效，WAL 故障路徑證據不足（非 PASS；P5-T19 Review Focus 3）");
        return;
    }
    let mut env = isolated_env_with_pty_target();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_ready(&env, "COM0");

    // READY 之後：WAL 目錄改唯讀並刪除既有檔（重建失敗）。
    chmod(&env.wal_dir, "555");
    fs::remove_file(env.wal_dir.join("raw.wal.ndjson")).expect("刪除 raw.wal.ndjson");
    let socket = env.socket.clone();
    wait_until("wal.healthy == false", SHORT, || {
        health_status(&socket)["wal"]["healthy"] == false
    });

    // agent 命令 TX 照常寫出，終態 done。
    let r = submit(&env.socket, "COM0", "echo x");
    let cmd_id = r["cmd_id"].as_str().unwrap_or_default().to_string();
    assert!(!cmd_id.is_empty(), "{r:?}");
    wait_until("command done", LONG, || command_get(&socket, &cmd_id)["status"] == "done");
    let rx = env.target_rx();
    assert!(rx.windows(7).any(|w| w == b"echo x\n"), "agent 命令應送出 echo x\\n");

    // 自動 TX（session self-test 的 probe）不得送出任何 bytes。
    let before = env.target_rx().len();
    let _ = rpc_value(&env.socket, "session.self_test", r#"{"selector":"COM0"}"#);
    let after = env.target_rx().len();
    assert_eq!(after, before, "WAL 不健康時 self-test probe 不得送出");

    chmod(&env.wal_dir, "755");
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

#[test]
fn daemon_stop_during_force_recover_with_client_gone() {
    let mut env = isolated_env_with_pty_target();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_ready(&env, "COM0");
    env.target_mute(); // DUT 不回 prompt：session.recover --force 會等待逾時

    let wal_before = fs::metadata(env.wal_dir.join("raw.wal.ndjson")).map(|m| m.len()).unwrap_or(0);
    let payload = format!(
        "{}\n{}\n",
        r#"{"id":3,"method":"session.recover","params":{"selector":"COM0","force":true}}"#,
        STOP
    );
    let started = Instant::now();
    send_and_drop(&env.socket, &payload); // 送出後立即斷線

    // handler 不因斷線取消；停機在 handler 收斂後進行，15 s 內 exit 0。
    let status = d.wait(Duration::from_secs(15));
    assert_eq!(status.code(), Some(0));
    assert!(started.elapsed() <= Duration::from_secs(15));

    // recover handler 的救援 TX 已寫入 WAL，且最後一行完整。
    let wal_after = fs::metadata(env.wal_dir.join("raw.wal.ndjson")).map(|m| m.len()).unwrap_or(0);
    assert!(wal_after > wal_before, "救援 TX 應已寫入 WAL");
    let last = last_wal_line(&env);
    assert!(pyjson::loads(&last).is_ok(), "WAL 最後一行不完整：{last}");
    // 註：「WAL close 發生在 handler 結束之後」的順序斷言需 trace，留待 in-process 變體（實作者補強）。
}

#[test]
fn allowlist_rejected_reasons_exact() {
    let env = isolated_env();

    // (1) 只設 allowlist，SERIALWRAP_STATE_DIR 未設。
    let mut vars = env.vars.clone();
    vars.insert("SERIALWRAP_STATE_DIR".to_string(), String::new());
    vars.insert(ALLOWLIST.to_string(), "/x/by-id/a".to_string());
    let out = run_to_exit(&env, &vars, &[]);
    assert_eq!(out.code, Some(2), "{}", out.stderr);
    assert_eq!(
        out.stderr,
        "serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: not an isolated instance: SERIALWRAP_STATE_DIR unset\n"
    );

    // (2) SERIALWRAP_RUN_DIR 等於預設路徑（<state>/run）。
    let default_run = env.state_dir.join("run");
    fs::create_dir_all(&default_run).unwrap();
    let mut vars = env.vars.clone();
    vars.insert("SERIALWRAP_RUN_DIR".to_string(), default_run.display().to_string());
    vars.insert(ALLOWLIST.to_string(), "/x/by-id/a".to_string());
    let out = run_to_exit(&env, &vars, &[]);
    assert_eq!(out.code, Some(2), "{}", out.stderr);
    assert_eq!(
        out.stderr,
        "serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: not an isolated instance: SERIALWRAP_RUN_DIR equals default\n"
    );

    // (3) config windows.test_device_allowlist 非 list。
    fs::write(
        env.config_dir.join("config.yaml"),
        "windows:\n  test_device_allowlist: \"COM7\"\n",
    )
    .unwrap();
    let mut vars = env.vars.clone();
    vars.insert(ALLOWLIST.to_string(), String::new());
    let out = run_to_exit(&env, &vars, &[]);
    assert_eq!(out.code, Some(2), "{}", out.stderr);
    assert_eq!(
        out.stderr,
        "serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: invalid windows.test_device_allowlist\n"
    );
}

#[test]
fn allowlist_env_overrides_config() {
    let env = isolated_env();
    fs::write(
        env.config_dir.join("config.yaml"),
        "windows:\n  test_device_allowlist: [\"/x/by-id/b\"]\n",
    )
    .unwrap();
    let mut vars = env.vars.clone();
    vars.insert(ALLOWLIST.to_string(), "/x/by-id/a".to_string());
    let mut d = DaemonProc::spawn_with(&env, &vars, &[]);
    wait_socket(&env.socket, SHORT);
    let h = health_status(&env.socket);
    assert!(h["test_device_allowlist"]["enabled"] == true, "{h:?}");
    assert_eq!(h["test_device_allowlist"]["source"], "env");
    let want_coms = pyjson::loads(r#"["/x/by-id/a"]"#).unwrap();
    assert_eq!(h["test_device_allowlist"]["coms"], want_coms);
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

#[test]
fn uncaught_panic_exit_1() {
    if std::env::var("S21_CHILD").as_deref() == Ok("panic") {
        let hooks = DaemonHooks {
            before_state_commit: None,
            after_state_commit: None,
            trace: Some(Box::new(|ev| {
                if ev == TraceEvent::InitialScan {
                    panic!("slice21 T19 injected panic");
                }
            })),
        };
        let code = run_with_hooks(vec!["serialwrapd".to_string()], sw_pal::native(), hooks);
        std::process::exit(code);
    }
    let env = isolated_env();
    let out = run_child_test("uncaught_panic_exit_1", "panic", &env);
    assert_eq!(out.code, Some(1), "未攔截 panic 必須 exit 1：{}", out.stderr);
    assert!(out.stderr.contains("slice21 T19 injected panic"), "{}", out.stderr);
}

#[test]
fn handler_panic_does_not_kill_daemon() {
    let env = isolated_env();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);
    // test-fakes 建置專用 method：handler 內 panic。
    let r = rpc_value(&env.socket, "test.panic", "{}");
    assert!(r["ok"] == false, "{r:?}");
    assert_eq!(r["error_code"], "EXCEPTION");
    assert!(r["message"].as_str().is_some(), "{r:?}");
    assert!(rpc_value(&env.socket, "health.ping", "{}")["ok"] == true, "handler panic 不得殺死 daemon");
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

#[test]
fn backend_env_invalid_exit_1() {
    let env = isolated_env();
    let mut vars = env.vars.clone();
    vars.insert("SERIALWRAP_RPC_BACKEND".to_string(), " posix".to_string()); // 不 strip（02 §2.1）
    let out = run_to_exit(&env, &vars, &[]);
    assert_eq!(out.code, Some(1), "{}", out.stderr);
}
