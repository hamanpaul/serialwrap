//! P5-T19：`serialwrapd` 行程層測試（啟動順序、有序關機、exit code、隔離 allowlist、panic 隔離）。
//!
//! 真 daemon 案例以 `CARGO_BIN_EXE_serialwrapd` 實際起行程；in-process 案例以 `run_with_hooks` 與 trace 驗證順序。
//! 紅燈階段：`daemon_main::run`／`run_with_hooks` 為 `unimplemented!`，故案例於執行期失敗（非編譯錯誤）。
#![cfg(feature = "test-fakes")]

mod support;

use std::fs;
use std::path::{Path, PathBuf};
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
    // Python 基線實測（P5-T19 修正預期值）：空 profile dir 先寫 `no profiles loaded`（load_profiles 早於取鎖），
    // 再寫 `DAEMON_ALREADY_RUNNING`；stderr 全文逐字比對。
    assert_eq!(out.stderr, "serialwrapd: no profiles loaded\nserialwrapd: DAEMON_ALREADY_RUNNING\n");
    rpc_once(&env.socket, STOP);
    assert_eq!(a.wait(LONG).code(), Some(0));
}

#[test]
fn unreadable_state_json_exits_3_and_releases_lock() {
    let env = isolated_env();
    fs::create_dir_all(env.state_dir.join("state.json")).unwrap(); // 讀取失敗
    let out = run_to_exit(&env, &env.vars, &[]);
    assert_eq!(out.code, Some(3));
    // Python 基線實測（P5-T19 修正預期值）：`no profiles loaded` 先於 state.json 讀取失敗（load_profiles 早於取鎖與 state 載入）。
    assert!(
        out.stderr.starts_with("serialwrapd: no profiles loaded\nserialwrapd: 無法讀取既有 state.json（"),
        "{}",
        out.stderr
    );
    assert_eq!(out.stderr.lines().count(), 2, "{}", out.stderr);
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
    // argparse 以 `COLUMNS`（未設時 80）決定 usage 換行寬度；固定為 80 使結果與外層 shell 無關。
    let mut vars = env.vars.clone();
    vars.insert("COLUMNS".to_string(), "80".to_string());
    let out = run_to_exit(&env, &vars, &["--bogus"]);
    assert_eq!(out.code, Some(2));
    // Python 3.12 argparse 實測（P5-T19 修正預期值）：usage 長 82 字元 > 寬度 78，於 `[--lock LOCK]` 前換行並以 19 空白縮排。
    assert_eq!(
        out.stderr,
        "usage: serialwrapd [-h] [--profile-dir PROFILE_DIR] [--socket SOCKET]\n                   [--lock LOCK]\n\
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
    // 審查 F81：root 下 chmod 無效＝證據不足，必須 FAIL（不得 return 計為 PASS）。
    assert!(!is_root(), "證據不足：root 下 chmod 無效，寫入失敗路徑須以非 root 執行");
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
        // 動態 session 於 attach worker 完成 PROBE 後才建立（與 Python `_attach_by_id_dynamic` 相同），輪詢至 3 個 session。
        let mut rows: Vec<PyValue> = Vec::new();
        let deadline = Instant::now() + LONG;
        while Instant::now() < deadline {
            rows = session_list(&socket).as_array().cloned().unwrap_or_default();
            if rows.len() == 3 {
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }
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
        // P5-T19 修正：q2／q3 原為背靠背並行送出，admission 順序不定；間隔 150 ms 使 q2 先 admission，flushed 依序比對。
        thread::sleep(Duration::from_millis(150));
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

    // 前景命令在停機前不會完成（假 DUT 的 `sleep 20` 於 20 s 後才回 prompt，等同 BlockUntilCancel），並排隊兩筆。
    // P5-T19 修正：原預寫的 `blk-never-returns` 會被假 DUT 立即回 prompt 而完成，排隊命令隨即執行，停機時沒有可沖刷的命令。
    let s1 = socket.clone();
    let fg = thread::spawn(move || submit(&s1, "COM0", "sleep 20"));
    thread::sleep(Duration::from_millis(300));
    let queued: Vec<_> = (0..2)
        .map(|i| {
            let s = socket.clone();
            thread::spawn(move || submit(&s, "COM0", &format!("queued-{i}")))
        })
        .collect();
    thread::sleep(Duration::from_millis(300));
    let queued_ids: Vec<String> = queued
        .into_iter()
        .map(|h| h.join().expect("queued submit")["cmd_id"].as_str().unwrap_or_default().to_string())
        .collect();
    // 停機前最後一次 command.get：排隊命令仍為 accepted（未開始），停機 drain 才會以 FLUSHED_BY_SHUTDOWN 終結。
    for id in &queued_ids {
        assert!(!id.is_empty(), "排隊命令應取得 cmd_id");
        assert_eq!(command_get(&socket, id)["status"], "accepted", "SIGTERM 前排隊命令 {id} 應尚未開始");
    }

    let status = Command::new("kill")
        .arg("-TERM")
        .arg(d.pid().to_string())
        .status()
        .expect("送出 SIGTERM");
    assert!(status.success());
    assert_eq!(d.wait(LONG).code(), Some(0), "SIGTERM 後應 exit 0");
    let _ = fg.join();
    // exit 0＝drain 已收斂（未收斂為 exit 1，見 shutdown_with_stalled_drain_exits_1_with_diagnostic），排隊命令已被沖刷。
    // P5-T19 修正：原預寫斷言「WAL 含 FLUSHED_BY_SHUTDOWN」與 Python 基線不符——Python `flush_session`／
    // `unregister_session` 只改 command 紀錄、不寫 WAL；沖刷結果改由停機前的 command.get（上方）與 in-process
    // `sigterm_graceful_exit_0_and_flushes_queue` 的 trace（flushed 列出兩筆 FLUSHED_BY_SHUTDOWN）驗證。
    let last = last_wal_line(&env);
    assert!(pyjson::loads(&last).is_ok(), "WAL 最後一筆不完整：{last}");
    assert!(!env.socket.exists(), "有序停機後 socket 檔應已移除");

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
    // 審查 F81：root 下 chmod 無效＝證據不足，必須 FAIL（不得 return 計為 PASS）。
    assert!(!is_root(), "證據不足：root 下 chmod 無效，唯讀 WAL 路徑須以非 root 執行（P5-T19 Review Focus 3）");
    let mut env = isolated_env_with_pty_target();
    chmod(&env.wal_dir, "555");
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);

    let h = health_status(&env.socket);
    assert!(h["wal"]["healthy"] == false, "{h:?}");
    // 審查 F81 非 root 實跑揭露：計畫寫 `dir_unwritable`，但 Python `WalWriter.append` 第一筆寫入失敗即設
    // `_unhealthy_reason = "write_failed"`（sw_core/wal.py append 失敗分支），attach 時假 DUT 的 `login: ` RX 即觸發；
    // 依 Python 斷言 write_failed（`dir_unwritable` 只在尚無任何 append 前可見，屬計畫指定的 Rust-only 值，見 F93）。
    let socket = env.socket.clone();
    wait_until("unhealthy_reason == write_failed", SHORT, || {
        health_status(&socket)["wal"]["unhealthy_reason"] == "write_failed"
    });

    // 登入 probe 不送：假 DUT 收到 0 bytes。審查 F81 非 root 實跑揭露計畫寫的「停 ATTACHED＋PROMPT_UNAVAILABLE」
    // 與 Python 不符：Python login TX 於 WAL 不健康時拋 `WalUnavailableError`（`_send_locked` 的 AUTO_TX／system:*
    // 分支），`_login_tx_refused` 只吞 quiet／stale_login_gen → 例外穿出 `_attach_by_id` → DETACHED＋
    // `ATTACH_FAILED:WalUnavailableError`（session_manager.py attach 的 except 分支）。依 Python 斷言。
    wait_until("COM0 ATTACH_FAILED:WalUnavailableError", LONG, || {
        let s = get_state(&socket, "COM0");
        s["state"] == "DETACHED" && s["last_error"] == "ATTACH_FAILED:WalUnavailableError"
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
    // 審查 F81：root 下 chmod 無效＝證據不足，必須 FAIL（不得 return 計為 PASS）。
    assert!(!is_root(), "證據不足：root 下 chmod 無效，WAL 故障路徑須以非 root 執行（P5-T19 Review Focus 3）");
    let mut env = isolated_env_with_pty_target();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_ready(&env, "COM0");

    // READY 之後：刪除既有檔再把 WAL 目錄改唯讀（重建失敗）。審查 F81：先 chmod 555 再刪檔在非 root 下必 EACCES。
    fs::remove_file(env.wal_dir.join("raw.wal.ndjson")).expect("刪除 raw.wal.ndjson");
    chmod(&env.wal_dir, "555");
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

/// WAL 中 `system:recover` 的 TX `payload_b64`（依寫入順序）。
fn recover_tx_payloads(env: &IsolatedEnv) -> Vec<String> {
    let wal = fs::read_to_string(env.wal_dir.join("raw.wal.ndjson")).unwrap_or_default();
    wal.lines()
        .filter_map(|line| pyjson::loads(line).ok())
        .filter(|row| {
            row["dir"] == "TX" && row["source"].as_str().is_some_and(|s| s.contains("recover"))
        })
        .filter_map(|row| row["payload_b64"].as_str().map(str::to_string))
        .collect()
}

#[test]
fn daemon_stop_during_force_recover_with_client_gone() {
    // P5-T19 修正（與計畫 Review Focus 5 衝突，依 OD-14 #1 以 Python 行為為準，待擁有者確認；審查 F87）：
    // Python 基線實測（`tests/contract_blackbox/slice21/test_safety_core.py::
    // test_pipelined_stop_after_client_gone_not_processed`，Python 輪 2/2 PASS）——同連線 pipelining
    // `session.recover --force`＋`daemon.stop` 後 client 立即斷線：recover handler 照常跑完（READY 路徑
    // `_recover_after_failure`，假 DUT 對 Ctrl-C 立即回 prompt → WAL 恰一筆 `system:recover` 的 `\x03`），回應寫入失敗後
    // 連線關閉，管線中的 `daemon.stop` 不被處理；daemon 持續運作，之後由新連線 `daemon.stop` 才有序停機（exit 0）。
    let env = isolated_env_with_pty_target();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_ready(&env, "COM0");

    let payload = format!(
        "{}\n{}\n",
        r#"{"id":3,"method":"session.recover","params":{"selector":"COM0","force":true}}"#,
        STOP
    );
    let sent = Instant::now();
    send_and_drop(&env.socket, &payload); // 送出後立即斷線

    // 收斂以救援 TX 實際寫入 WAL 為準（不以狀態判斷：COM0 起始即 READY）。
    wait_until("救援 TX 寫入 WAL", LONG, || !recover_tx_payloads(&env).is_empty());
    thread::sleep(Duration::from_secs(1));
    assert_eq!(recover_tx_payloads(&env), vec!["Aw==".to_string()], "救援 TX 應恰為 \\x03");
    // 超過 force recover 上限（10 s）＋餘裕：管線中的 stop 若於 handler 結束後才被處理，此時行程已結束。
    if let Some(rest) = Duration::from_secs(11).checked_sub(sent.elapsed()) {
        thread::sleep(rest);
    }

    // 管線中的 daemon.stop 未被處理：daemon 仍存活、socket 保留，新連線 health.status 回 ok。
    assert!(d.try_alive(), "回應寫入失敗後連線關閉，管線中的 daemon.stop 不得被處理：{}", d.stderr());
    assert!(env.socket.exists(), "管線中的 daemon.stop 不得移除 socket");
    assert!(health_status(&env.socket)["ok"] == true, "daemon 應仍可服務新連線");
    let last = last_wal_line(&env);
    assert!(pyjson::loads(&last).is_ok(), "WAL 最後一行不完整：{last}");

    // 新連線 daemon.stop → 有序停機。
    let started = Instant::now();
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(Duration::from_secs(15)).code(), Some(0));
    assert!(started.elapsed() <= Duration::from_secs(15));
    assert!(!env.socket.exists(), "有序停機後 socket 檔應已移除");
}

#[test]
fn daemon_stop_from_second_connection_during_force_recover() {
    // 審查 F87（Opus 修法）：force recover handler 進行中（假 DUT 暫停回應 8 s）、client 已斷線，由第二條連線送
    // `daemon.stop`：關機等 handler 收斂後才 drain（Python `server.stop()` 先於 `service.stop()`），exit 0、
    // 救援 TX 已在 WAL、WAL 每一行皆為完整 JSON、socket 移除。
    // Python 基線：`test_safety_core.py::test_daemon_stop_from_second_connection_during_force_recover`（shell DUT，
    // 2/2 PASS，stop→exit 約 8.2 s）。本 rig 的假 DUT 為登入型：`\x04` 登出後 reattach 停在 login，handler 依
    // Python `_force_recover` 10 × 1 s 逾時（約 14 s）才回 FORCE_RECOVER_TIMEOUT——上限以 SERVER_STOP_BUDGET（20 s）
    // ＋drain 計，不以計畫假設的「約 10 s」。
    let mut env = isolated_env_with_pty_target();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_ready(&env, "COM0");
    env.target_pause(8.0);
    send_and_drop(
        &env.socket,
        &format!(
            "{}\n",
            r#"{"id":3,"method":"session.recover","params":{"selector":"COM0","force":true}}"#
        ),
    );
    wait_until("救援 TX 寫入 WAL", SHORT, || !recover_tx_payloads(&env).is_empty());
    let started = Instant::now();
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(Duration::from_secs(30)).code(), Some(0), "{}", d.stderr());
    assert!(started.elapsed() <= Duration::from_secs(25), "{:?}", started.elapsed());
    assert!(
        !d.stderr().contains("shutdown incomplete"),
        "handler 收斂後的關機不得報 shutdown incomplete：{}",
        d.stderr()
    );
    let wal = fs::read_to_string(env.wal_dir.join("raw.wal.ndjson")).expect("讀取 WAL");
    for line in wal.lines() {
        assert!(pyjson::loads(line).is_ok(), "WAL 行不完整：{line}");
    }
    assert!(!env.socket.exists(), "有序停機後 socket 檔應已移除");
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

    // (2) SERIALWRAP_RUN_DIR 等於預設路徑。P5-T19 修正：Python `default_isolation_paths()` 以「三個隔離變數皆未設」
    // 計算預設值，RUN 預設為 `$XDG_STATE_HOME/serialwrap/run`（XDG_RUNTIME_DIR 為空），與 SERIALWRAP_STATE_DIR 無關；
    // 原預寫的 `<SERIALWRAP_STATE_DIR>/run` 不是預設值（Python 基線實測不拒絕、daemon 照常啟動）。
    let default_run = PathBuf::from(&env.vars["XDG_STATE_HOME"]).join("serialwrap").join("run");
    let mut vars = env.vars.clone();
    vars.insert("SERIALWRAP_RUN_DIR".to_string(), default_run.display().to_string());
    vars.insert(ALLOWLIST.to_string(), "/x/by-id/a".to_string());
    let out = run_to_exit(&env, &vars, &[]);
    assert_eq!(out.code, Some(2), "{}", out.stderr);
    assert_eq!(
        out.stderr,
        "serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: not an isolated instance: SERIALWRAP_RUN_DIR equals default\n"
    );
    // (2b) 審查 F79：Python `_isolation_reason` 先 `.strip()` 再正規化——前後空白的預設路徑同樣判定為預設。
    let mut vars = env.vars.clone();
    vars.insert(
        "SERIALWRAP_RUN_DIR".to_string(),
        format!("  {} ", default_run.display()),
    );
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

#[test]
fn backend_env_checked_where_python_selects_it() {
    // 審查 F78（Medium，3938032）：Python 在第一次用到各後端時才檢查（argparse、SC-19 allowlist、
    // ensure_runtime_dirs、load_profiles 之後）：DEVICE 於 `no profiles loaded` 之後、LOCK 於 `_build_lock`、
    // RPC 於 `_build_server`。Rust 原本在 argv 解析前一次檢查三者。
    let env = isolated_env();

    // (1) RPC 後端非法＋未知參數：Python argparse 先行 → rc 2、usage。
    let mut vars = env.vars.clone();
    vars.insert("SERIALWRAP_RPC_BACKEND".to_string(), "bogus".to_string());
    let out = run_to_exit(&env, &vars, &["--bogus"]);
    assert_eq!(out.code, Some(2), "{}", out.stderr);
    assert!(out.stderr.starts_with("usage: serialwrapd"), "{}", out.stderr);

    // (2) LOCK 後端非法＋非隔離＋allowlist：SC-19 先行 → rc 2、TEST_DEVICE_ALLOWLIST_REJECTED。
    let mut vars = non_isolated_vars(&env);
    vars.insert("SERIALWRAP_LOCK_BACKEND".to_string(), "bogus".to_string());
    vars.insert(ALLOWLIST.to_string(), "/x/by-id/a".to_string());
    let out = run_to_exit(&env, &vars, &[]);
    assert_eq!(out.code, Some(2), "{}", out.stderr);
    assert!(out.stderr.contains("TEST_DEVICE_ALLOWLIST_REJECTED"), "{}", out.stderr);

    // (3) DEVICE 後端非法：目錄已建立、`no profiles loaded` 已印出後才失敗（exit 1）。
    let env3 = isolated_env();
    let wal_dir = env3.wal_dir.clone();
    let _ = fs::remove_dir_all(&wal_dir);
    let mut vars = env3.vars.clone();
    vars.insert("SERIALWRAP_DEVICE_BACKEND".to_string(), "bogus".to_string());
    let out = run_to_exit(&env3, &vars, &[]);
    assert_eq!(out.code, Some(1), "{}", out.stderr);
    assert!(
        out.stderr.starts_with("serialwrapd: no profiles loaded\n"),
        "{}",
        out.stderr
    );
    assert!(wal_dir.is_dir(), "ensure_runtime_dirs 應已建立 WAL 目錄");
}

// ---------------------------------------------------------------------------
// parity-amendments-2026-10-26 追加（P5-T19：#46、#47、#3／#4／#68、#72）
// ---------------------------------------------------------------------------

/// 非隔離實例：`SERIALWRAP_STATE_DIR` 以空字串 pop（daemon 端 strip 後視同未設）。
fn non_isolated_vars(env: &IsolatedEnv) -> std::collections::BTreeMap<String, String> {
    let mut vars = env.vars.clone();
    vars.insert("SERIALWRAP_STATE_DIR".to_string(), String::new());
    vars
}

/// `SERIALWRAP_STATE_DIR` 未設時 daemon 使用的預設 state 目錄（`$XDG_STATE_HOME/serialwrap`）。
fn default_state_dir(env: &IsolatedEnv) -> PathBuf {
    PathBuf::from(&env.vars["XDG_STATE_HOME"]).join("serialwrap")
}

/// #46：正式實例設 allowlist 被拒 → exit 2，且 STATE／RUN／WAL 目錄皆未建立、lock 檔不存在（daemon.py:230-239）。
#[test]
fn allowlist_rejected_before_runtime_dirs_and_lock() {
    let env = isolated_env();
    let fresh_run = env.root.join("fresh-run");
    let fresh_wal = env.root.join("fresh-wal");
    let mut vars = non_isolated_vars(&env);
    vars.insert("SERIALWRAP_RUN_DIR".to_string(), fresh_run.display().to_string());
    vars.insert("SERIALWRAP_WAL_DIR".to_string(), fresh_wal.display().to_string());
    vars.insert(ALLOWLIST.to_string(), "/x/by-id/a".to_string());
    let out = run_to_exit(&env, &vars, &[]);
    assert_eq!(out.code, Some(2), "{}", out.stderr);
    assert_eq!(
        out.stderr,
        "serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: not an isolated instance: SERIALWRAP_STATE_DIR unset\n"
    );
    assert!(!default_state_dir(&env).exists(), "被拒時不得建立 STATE_DIR");
    assert!(!fresh_run.exists(), "被拒時不得建立 RUN_DIR");
    assert!(!fresh_wal.exists(), "被拒時不得建立 WAL_DIR");
    assert!(!fresh_run.join("serialwrapd.lock").exists(), "被拒時不得取鎖／建立 lock 檔");
    assert!(!env.config_dir.join("config.yaml").exists(), "被拒時不得寫入 config.yaml");
}

/// `contracts/golden/allowlist_unreadable_config.tsv` 一列（Python 基線實測）。
struct UnreadableRow {
    case_id: String,
    config: Vec<u8>,
    code: i32,
    stderr: String,
}

fn hex_bytes(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).expect("golden hex"))
        .collect()
}

fn unreadable_rows() -> Vec<UnreadableRow> {
    let path = serde_free::repo_file("contracts/golden/allowlist_unreadable_config.tsv");
    fs::read_to_string(path)
        .expect("讀取 golden")
        .lines()
        .filter(|l| !l.is_empty())
        .map(|l| {
            let cols: Vec<&str> = l.split('\t').collect();
            assert_eq!(cols.len(), 4, "golden 欄位數：{l}");
            UnreadableRow {
                case_id: cols[0].to_string(),
                config: hex_bytes(cols[1]),
                code: cols[2].parse().expect("exit code"),
                stderr: String::from_utf8(hex_bytes(cols[3])).expect("stderr UTF-8"),
            }
        })
        .collect()
}

/// #47：隔離實例＋不可讀／不可解析 config.yaml → exit 2、`invalid config.yaml: <Python 例外類別名>`（逐列對 golden）。
#[test]
fn unreadable_config_isolated_fail_closed() {
    let rows = unreadable_rows();
    assert!(rows.len() >= 12, "golden 列數不足：{}", rows.len());
    for row in rows {
        let env = isolated_env();
        let cfg = env.config_dir.join("config.yaml");
        fs::write(&cfg, &row.config).unwrap();
        let out = if row.case_id == "permission_denied" {
            chmod(&cfg, "000");
            if is_root() {
                // root 讀檔不受 mode 限制：以 setpriv 降為 nobody 執行（與 golden 產生器相同）；無 setpriv 則證據不足。
                if !Path::new("/usr/bin/setpriv").exists() {
                    // 審查 F81：證據不足必須 FAIL（不得 continue 計為 PASS）。
                    panic!("證據不足：permission_denied 列需非 root 或 /usr/bin/setpriv");
                }
                let o = Command::new("/usr/bin/setpriv")
                    .args(["--reuid=65534", "--regid=65534", "--clear-groups", DAEMON_BIN])
                    .envs(&env.vars)
                    .stdin(std::process::Stdio::null())
                    .output()
                    .expect("setpriv 執行 daemon");
                Output {
                    code: o.status.code(),
                    stderr: String::from_utf8_lossy(&o.stderr).into_owned(),
                }
            } else {
                run_to_exit(&env, &env.vars, &[])
            }
        } else {
            run_to_exit(&env, &env.vars, &[])
        };
        assert_eq!(out.code, Some(row.code), "{}：{}", row.case_id, out.stderr);
        assert_eq!(out.stderr, row.stderr, "{}", row.case_id);
        assert!(!env.run_dir.join("serialwrapd.lock").exists(), "{}：被拒時不得取鎖", row.case_id);
    }
}

/// #47：非隔離實例＋壞 config.yaml → 照常啟動、`health.status` 不輸出 `test_device_allowlist`；
/// endpoint 寫入時重新讀取同一份壞檔失敗，只寫非致命 stderr（Python `_write_config_endpoint`）。
#[test]
fn unreadable_config_non_isolated_unchanged() {
    let env = isolated_env();
    fs::write(env.config_dir.join("config.yaml"), b"a:\n\tb: 1\n").unwrap();
    let vars = non_isolated_vars(&env);
    let mut d = DaemonProc::spawn_with(&env, &vars, &[]);
    wait_socket(&env.socket, SHORT);
    let h = health_status(&env.socket);
    assert!(h["ok"] == true, "{h:?}");
    assert!(h["test_device_allowlist"] == PyValue::Null, "非隔離實例不得輸出 test_device_allowlist：{h:?}");
    d.wait_stderr_contains("serialwrapd: 寫入 config.yaml endpoint 失敗（非致命）: ", SHORT);
    assert!(!d.stderr().contains("TEST_DEVICE_ALLOWLIST_REJECTED"), "{}", d.stderr());
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

/// #47：env allowlist 非空 → 不採用壞 config，依 env 判定（隔離實例照常啟動、source=env）。
#[test]
fn unreadable_config_env_allowlist_wins() {
    let env = isolated_env();
    fs::write(env.config_dir.join("config.yaml"), b"a: [1, 2\n").unwrap();
    let mut vars = env.vars.clone();
    vars.insert(ALLOWLIST.to_string(), "/x/by-id/a".to_string());
    let mut d = DaemonProc::spawn_with(&env, &vars, &[]);
    wait_socket(&env.socket, SHORT);
    let h = health_status(&env.socket);
    assert!(h["test_device_allowlist"]["enabled"] == true, "{h:?}");
    assert_eq!(h["test_device_allowlist"]["source"], "env");
    assert_eq!(h["test_device_allowlist"]["coms"], pyjson::loads(r#"["/x/by-id/a"]"#).unwrap());
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

const RELEASE: &str = r#"{"id":7,"method":"device.release","params":{"selector":"COM0"}}"#;

/// #3／#68：產品 build 讀 `SERIALWRAP_TEST_KILL_AFTER_SAVE=1`；隔離實例於 release 持久化 commit 後、回覆前 exit 137，
/// 重啟後仍 RELEASED；非隔離實例只寫凍結的 WARNING（`contracts/daemon_logging.yaml`）、不終止。
#[test]
fn release_kill_hook_env_isolated_only() {
    // 隔離實例：commit 後被殺。
    let env = isolated_env_with_pty_target();
    let mut vars = env.vars.clone();
    vars.insert("SERIALWRAP_TEST_KILL_AFTER_SAVE".to_string(), "1".to_string());
    let mut d = DaemonProc::spawn_with(&env, &vars, &[]);
    wait_ready(&env, "COM0");
    let r = rpc_once(&env.socket, RELEASE);
    assert!(r.is_empty(), "commit 後、回覆前即終止，不得收到回覆：{r}");
    assert_eq!(d.wait(LONG).code(), Some(137), "{}", d.stderr());
    assert!(
        d.stderr().contains("SERIALWRAP_TEST_KILL_AFTER_SAVE=1：state 已落盤，於回應前以 os._exit(137) 模擬程序被殺（隔離實例）"),
        "{}",
        d.stderr()
    );
    let state = fs::read_to_string(env.state_dir.join("state.json")).expect("state.json 已落盤");
    assert!(state.contains("\"released\"") && state.contains(TARGET_BY_ID), "{state}");
    let mut d2 = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);
    assert_eq!(get_state(&env.socket, "COM0")["state"], "RELEASED");
    rpc_once(&env.socket, STOP);
    assert_eq!(d2.wait(LONG).code(), Some(0));

    // 非隔離實例：只寫 WARNING、不終止，release 正常回覆。
    let env = isolated_env_with_pty_target();
    let mut vars = non_isolated_vars(&env);
    vars.insert("SERIALWRAP_TEST_KILL_AFTER_SAVE".to_string(), "1".to_string());
    let mut d = DaemonProc::spawn_with(&env, &vars, &[]);
    wait_ready(&env, "COM0");
    let r = pyjson::loads(&rpc_once(&env.socket, RELEASE)).unwrap_or(PyValue::Null);
    assert!(r["ok"] == true, "{r:?}");
    d.wait_stderr_contains(
        "SERIALWRAP_TEST_KILL_AFTER_SAVE=1 已忽略：not an isolated instance: SERIALWRAP_STATE_DIR unset（非隔離實例不得終止行程）\n",
        SHORT,
    );
    assert!(!d.stderr().contains("os._exit(137)"), "{}", d.stderr());
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

/// #4／#68：產品 build 讀 `SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE=<dir>`；隔離實例在 commit 前寫 `<dir>/arrived`
/// （`pid=<pid>\n`）並輪詢 `<dir>/go`，期間 state.json 不變；非隔離實例只寫 WARNING、不暫停。
#[test]
fn release_pause_hook_env_isolated_only() {
    let env = isolated_env_with_pty_target();
    let pause = env.root.join("pause");
    let mut vars = env.vars.clone();
    vars.insert("SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE".to_string(), pause.display().to_string());
    let mut d = DaemonProc::spawn_with(&env, &vars, &[]);
    wait_ready(&env, "COM0");
    let state_path = env.state_dir.join("state.json");
    let before = fs::read(&state_path).ok();
    let socket = env.socket.clone();
    let release = thread::spawn(move || rpc_once(&socket, RELEASE));
    let arrived = pause.join("arrived");
    wait_until("arrived 標記", LONG, || arrived.exists());
    assert_eq!(fs::read_to_string(&arrived).unwrap(), format!("pid={}\n", d.pid()));
    thread::sleep(Duration::from_millis(300));
    assert!(!release.is_finished(), "go 標記出現前 release 不得回覆");
    assert_eq!(fs::read(&state_path).ok(), before, "commit 前暫停：state.json 不得改變");
    fs::write(pause.join("go"), b"").unwrap();
    let r = pyjson::loads(&release.join().unwrap()).unwrap_or(PyValue::Null);
    assert!(r["ok"] == true, "{r:?}");
    assert_eq!(get_state(&env.socket, "COM0")["state"], "RELEASED");
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));

    let env = isolated_env_with_pty_target();
    let pause = env.root.join("pause");
    let mut vars = non_isolated_vars(&env);
    vars.insert("SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE".to_string(), pause.display().to_string());
    let mut d = DaemonProc::spawn_with(&env, &vars, &[]);
    wait_ready(&env, "COM0");
    let r = pyjson::loads(&rpc_once(&env.socket, RELEASE)).unwrap_or(PyValue::Null);
    assert!(r["ok"] == true, "{r:?}");
    assert!(!pause.join("arrived").exists(), "非隔離實例不得暫停");
    d.wait_stderr_contains(
        "SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE 已忽略：not an isolated instance: SERIALWRAP_STATE_DIR unset（非隔離實例不得暫停）\n",
        SHORT,
    );
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

/// #72（POSIX）：SIGTERM／SIGINT → 有序停機（RPC 停、drain、WAL close、socket 檔移除、exit 0），與 Python `daemon.py:293-301` 相同。
/// Windows `SetConsoleCtrlHandler`（`windows_console_ctrl_triggers_orderly_stop`）為 LOCAL-WINDOWS-TODO。
#[test]
fn sigterm_sigint_orderly_stop() {
    for sig in ["-TERM", "-INT"] {
        let env = isolated_env_with_pty_target();
        let mut d = DaemonProc::spawn(&env, &[]);
        wait_ready(&env, "COM0");
        let status = Command::new("kill").arg(sig).arg(d.pid().to_string()).status().expect("送出訊號");
        assert!(status.success());
        assert_eq!(d.wait(LONG).code(), Some(0), "{sig}：{}", d.stderr());
        assert!(!env.socket.exists(), "{sig}：socket 檔應已移除");
        let last = last_wal_line(&env);
        assert!(pyjson::loads(&last).is_ok(), "{sig}：WAL 最後一筆不完整：{last}");
        assert!(pty_can_open_exclusively(&env.pty_slave), "{sig}：tty 應已釋放");
        // 鎖已釋放：新 daemon 可取得。
        let mut d2 = DaemonProc::spawn(&env, &[]);
        wait_socket(&env.socket, SHORT);
        rpc_once(&env.socket, STOP);
        assert_eq!(d2.wait(LONG).code(), Some(0), "{sig}");
    }
}
