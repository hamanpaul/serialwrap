//! P5-T19：`serialwrapd` 行程層測試（啟動順序、有序關機、exit code、隔離 allowlist、panic 隔離）。
//!
//! 真 daemon 案例以 `CARGO_BIN_EXE_serialwrapd` 實際起行程；in-process 案例以 `run_with_hooks` 與 trace 驗證順序。
//! 紅燈階段：`daemon_main::run`／`run_with_hooks` 為 `unimplemented!`，故案例於執行期失敗（非編譯錯誤）。
#![cfg(feature = "test-fakes")]

mod support;

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use support::*;
use sw_pal::traits::DeviceInfo;
use sw_proto::pyjson::{self, PyValue};

const ALLOWLIST: &str = "SERIALWRAP_TEST_DEVICE_ALLOWLIST";

#[test]
fn windows_caps_support_env_allowlist_is_com250() {
    assert_eq!(
        support::test_device_allowlist_for_caps(sw_pal::fakes::windows_caps()),
        "COM250",
        "Windows 測試 daemon 的預設 allowlist 必須只允許不存在的 COM250"
    );
}

#[test]
fn windows_caps_isolated_env_uses_unique_loopback_tcp_endpoints() {
    let caps = sw_pal::fakes::windows_caps();
    let first = support::isolated_env_for_caps(caps);
    let second = support::isolated_env_for_caps(caps);

    for env in [&first, &second] {
        assert!(
            env.endpoint.starts_with("tcp://"),
            "Windows caps 必須配置 TCP endpoint：{}",
            env.endpoint
        );
        assert_eq!(env.vars["SERIALWRAP_ENDPOINT"], env.endpoint);
        let parsed = sw_proto::endpoint::parse_endpoint(&env.endpoint)
            .unwrap_or_else(|error| panic!("隔離 endpoint 無效：{} ({error:?})", env.endpoint));
        let sw_proto::endpoint::Endpoint::Tcp { host, port } = parsed else {
            panic!("Windows caps 必須使用 TCP loopback endpoint：{}", env.endpoint);
        };
        assert_eq!(host, "127.0.0.1", "測試 endpoint 僅可綁 loopback");
        assert_ne!(port, 48700, "測試 daemon 不得使用正式預設埠");
    }
    assert_ne!(first.endpoint, second.endpoint, "每個隔離環境都必須分配不同埠");

    let platform = in_process_platform(&first.vars, Vec::new(), Fixture::None);
    let endpoint = first.socket.clone();
    let client = spawn_stop_guarded_client(endpoint.clone(), move || {
        wait_socket(&endpoint, SHORT);
        let health = health_status(&endpoint);
        let sessions = rpc_value(&endpoint, "session.list", "{}")["sessions"].clone();
        rpc_once(&endpoint, STOP);
        (health, sessions)
    });
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, DaemonHooks::default());
    let (health, sessions) = client.join().expect("TCP endpoint client");
    assert_eq!(code, 0);
    assert_eq!(health["test_device_allowlist"]["coms"], pyjson::loads(r#"["COM250"]"#).unwrap());
    assert_eq!(sessions, pyjson::loads("[]").unwrap());
}

#[test]
fn windows_caps_daemon_launch_refuses_empty_allowlist() {
    let caps = sw_pal::fakes::windows_caps();
    let env = support::isolated_env_for_caps(caps);
    let mut vars = env.vars.clone();
    vars.insert(ALLOWLIST.to_string(), String::new());

    let error = match DaemonProc::try_spawn_with_caps(&env, &vars, &[], caps) {
        Err(error) => error,
        Ok(daemon) => {
            drop(daemon);
            panic!("Windows caps 下 allowlist 為空時，harness 不得啟動 serialwrapd");
        }
    };
    assert!(
        error.contains("SERIALWRAP_TEST_DEVICE_ALLOWLIST") && error.contains("COM250"),
        "拒絕原因須指出 Windows daemon 必須配置非空 allowlist：{error}"
    );
}

#[test]
fn windows_caps_allowlist_that_normalizes_empty_is_rejected() {
    let caps = sw_pal::fakes::windows_caps();
    for raw in [",", " , , "] {
        let env = support::isolated_env_for_caps(caps);
        let mut vars = env.vars.clone();
        vars.insert(ALLOWLIST.to_string(), raw.to_string());
        assert!(
            support::require_test_daemon_allowlist(caps, &vars).is_err(),
            "逗號分隔後沒有 COM 項目的 allowlist 必須拒絕：{raw:?}"
        );
    }
}

#[test]
fn windows_caps_explicit_missing_config_does_not_fall_back_to_allowlist() {
    let caps = sw_pal::fakes::windows_caps();
    let env = support::isolated_env_for_caps(caps);
    fs::write(
        env.config_dir.join("config.yaml"),
        "windows:\n  test_device_allowlist: [COM250]\n",
    )
    .expect("寫入隔離 fallback config");
    let mut vars = env.vars.clone();
    vars.insert(ALLOWLIST.to_string(), String::new());
    vars.insert(
        "SERIALWRAP_CONFIG".to_string(),
        env.root.join("missing.yaml").display().to_string(),
    );
    assert!(
        support::require_test_daemon_allowlist(caps, &vars).is_err(),
        "明確指定但不存在的 config 不得借用 config dir 的 allowlist"
    );
}

#[test]
fn in_process_client_panic_requests_daemon_stop() {
    let env = isolated_env();
    let endpoint = env.socket.clone();
    let platform = in_process_platform(&env.vars, Vec::new(), Fixture::None);
    let (done_tx, done_rx) = std::sync::mpsc::channel();
    let server = thread::spawn(move || {
        let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, DaemonHooks::default());
        let _ = done_tx.send(code);
    });
    let client_endpoint = endpoint.clone();
    let client = spawn_stop_guarded_client(client_endpoint.clone(), move || {
        wait_socket(&client_endpoint, SHORT);
        panic!("刻意觸發 client panic，驗證看門狗會送 daemon.stop");
    });

    let client_result = client.join();
    let code = match done_rx.recv_timeout(SHORT) {
        Ok(code) => code,
        Err(error) => {
            let _ = rpc_once(&endpoint, STOP);
            let _ = server.join();
            panic!("client panic 後 daemon 未自行停止：{error}");
        }
    };
    assert!(client_result.is_err(), "刻意觸發的 client panic 必須傳回 join");
    assert_eq!(code, 0, "看門狗送 STOP 後 daemon 應有序退出");
    assert!(!endpoint_is_serving(&endpoint), "看門狗停止後 endpoint 不應繼續服務");
    server.join().expect("daemon server thread");
}

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
        ..DaemonHooks::default()
    }
}

/// 以明確的 loopback TCP endpoint 呼叫 daemon；Windows RPC 後端不支援 Unix socket。
fn loopback_rpc_value(port: u16, method: &str, params: &str) -> PyValue {
    let Ok(mut stream) =
        TcpStream::connect_timeout(&format!("127.0.0.1:{port}").parse().unwrap(), SHORT)
    else {
        return PyValue::Null;
    };
    let _ = stream.set_read_timeout(Some(SHORT));
    let request = format!(r#"{{"id":1,"method":"{method}","params":{params}}}"#);
    if writeln!(stream, "{request}").is_err() {
        return PyValue::Null;
    }
    let mut response = String::new();
    if BufReader::new(stream).read_line(&mut response).is_err() {
        return PyValue::Null;
    }
    pyjson::loads(response.trim_end_matches(['\r', '\n'])).unwrap_or(PyValue::Null)
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
    let endpoint = support::endpoint_spec(socket);
    let mut stream = platform
        .rpc
        .connect(&endpoint, sw_pal::traits::SockTimeout::Blocking(SHORT))
        .expect("連線 daemon");
    stream
        .send_all(payload.as_bytes())
        .expect("送出 pipelined 請求");
    drop(stream);
}

fn last_wal_line(env: &IsolatedEnv) -> String {
    let wal = fs::read_to_string(env.wal_dir.join("raw.wal.ndjson")).expect("讀取 raw.wal.ndjson");
    wal.lines().last().unwrap_or("").to_string()
}

#[test]
fn config_wal_dir_is_used_by_daemon_startup_and_writes() {
    let mut env = isolated_env_with_pty_target();
    env.vars.insert("SERIALWRAP_WAL_DIR".to_string(), String::new());
    fs::write(
        env.config_dir.join("config.yaml"),
        "wal_dir: ../audit/custom-wal\n",
    )
    .expect("寫入 config.yaml");
    let reported_dir = env.config_dir.join("../audit/custom-wal");
    let expected_wal = env.root.join("audit/custom-wal/raw.wal.ndjson");

    let mut daemon = DaemonProc::spawn(&env, &[]);
    wait_ready(&env, "COM0");
    let health = health_status(&env.socket);
    assert_eq!(
        health["wal_path"],
        reported_dir.join("raw.wal.ndjson").display().to_string(),
        "health.status 必須回報 config wal_dir 實際使用的路徑"
    );
    let contents = fs::read_to_string(&expected_wal).unwrap_or_else(|error| {
        panic!(
            "config wal_dir 未被 daemon 實際寫入：{}（{error}，stderr={}）",
            expected_wal.display(),
            daemon.stderr()
        )
    });
    assert!(
        contents.lines().any(|line| pyjson::loads(line).is_ok()),
        "custom WAL 至少須包含一筆有效 JSON 紀錄"
    );

    rpc_once(&env.socket, STOP);
    assert_eq!(daemon.wait(LONG).code(), Some(0), "{}", daemon.stderr());
}

#[test]
fn system_config_wal_dir_stays_stable_after_endpoint_copy_and_restart() {
    let mut env = isolated_env_with_pty_target();
    env.vars.insert("SERIALWRAP_WAL_DIR".to_string(), String::new());
    fs::create_dir_all(env.system_config.parent().expect("system config parent"))
        .expect("建立 system config 目錄");
    fs::write(&env.system_config, "wal_dir: archive/wal\n").expect("寫入 system config");
    let expected_dir = env
        .system_config
        .parent()
        .expect("system config parent")
        .join("archive/wal");
    let expected_wal = expected_dir.join("raw.wal.ndjson");

    let mut first = DaemonProc::spawn(&env, &[]);
    wait_ready(&env, "COM0");
    assert_eq!(
        health_status(&env.socket)["wal_path"],
        expected_wal.display().to_string(),
        "第一次啟動必須以 system config 父目錄解析 wal_dir"
    );
    rpc_once(&env.socket, STOP);
    assert_eq!(first.wait(LONG).code(), Some(0), "{}", first.stderr());

    assert!(
        env.config_dir.join("config.yaml").is_file(),
        "daemon 綁定 endpoint 後必須複製 system config 到 user config"
    );
    let mut second = DaemonProc::spawn(&env, &[]);
    wait_ready(&env, "COM0");
    assert_eq!(
        health_status(&env.socket)["wal_path"],
        expected_wal.display().to_string(),
        "user config 複本重啟後仍須指向相同 WAL 目錄"
    );
    let contents = fs::read_to_string(&expected_wal).unwrap_or_else(|error| {
        panic!(
            "重啟後未繼續寫入原 WAL 目錄：{}（{error}，stderr={}）",
            expected_wal.display(),
            second.stderr()
        )
    });
    assert!(
        contents.lines().any(|line| pyjson::loads(line).is_ok()),
        "原 WAL 目錄必須保有有效 JSON 紀錄"
    );
    rpc_once(&env.socket, STOP);
    assert_eq!(second.wait(LONG).code(), Some(0), "{}", second.stderr());
}

#[test]
fn config_wal_dir_preserves_symlink_dotdot_filesystem_semantics() {
    let mut env = isolated_env_with_pty_target();
    env.vars.insert("SERIALWRAP_WAL_DIR".to_string(), String::new());
    let project = env.root.join("vault/project");
    fs::create_dir_all(&project).expect("建立 config 實體目錄");
    fs::write(project.join("config.yaml"), "wal_dir: ../wal\n").expect("寫入 config.yaml");
    let config_link = env.root.join("linked-config");
    let link_kind = sw_pal::native()
        .fs
        .symlink_or_copy_dir(&config_link, &project)
        .expect("建立 config 目錄 symlink／複本");
    env.vars.insert(
        "SERIALWRAP_CONFIG".to_string(),
        config_link.join("config.yaml").display().to_string(),
    );
    let expected_dir = match link_kind {
        sw_pal::fs_ops::LinkKind::Symlink => env.root.join("vault/wal"),
        sw_pal::fs_ops::LinkKind::Copied => env.root.join("wal"),
    };
    fs::create_dir_all(&expected_dir).expect("建立預期 WAL 目錄");
    let expected_wal = expected_dir.join("raw.wal.ndjson");

    let mut daemon = DaemonProc::spawn(&env, &[]);
    wait_ready(&env, "COM0");
    let health = health_status(&env.socket);
    let health_path = PathBuf::from(
        health["wal_path"]
            .as_str()
            .expect("health.status wal_path 必須是字串"),
    );
    let actual_dir = fs::canonicalize(
        health_path
            .parent()
            .expect("health.status wal_path 必須有父目錄"),
    )
    .unwrap_or_else(|error| panic!("無法解析 daemon WAL 目錄：{error}"));
    let expected_dir =
        fs::canonicalize(&expected_dir).unwrap_or_else(|error| panic!("無法解析預期 WAL 目錄：{error}"));
    assert_eq!(
        actual_dir, expected_dir,
        "wal_dir 的 `..` 必須由檔案系統在 symlink 解析後套用"
    );
    let contents = fs::read_to_string(&expected_wal).unwrap_or_else(|error| {
        panic!(
            "config wal_dir 未寫入預期實體目錄：{}（{error}，stderr={}）",
            expected_wal.display(),
            daemon.stderr()
        )
    });
    assert!(
        contents.lines().any(|line| pyjson::loads(line).is_ok()),
        "預期實體 WAL 目錄必須包含有效 JSON 紀錄"
    );
    rpc_once(&env.socket, STOP);
    assert_eq!(daemon.wait(LONG).code(), Some(0), "{}", daemon.stderr());
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
    assert_eq!(
        out.stderr,
        "serialwrapd: no profiles loaded\nserialwrapd: DAEMON_ALREADY_RUNNING\n"
    );
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
        out.stderr
            .starts_with("serialwrapd: no profiles loaded\nserialwrapd: 無法讀取既有 state.json（"),
        "{}",
        out.stderr
    );
    assert_eq!(out.stderr.lines().count(), 2, "{}", out.stderr);
    // 鎖已釋放：第二次啟動仍走到讀取失敗，而不是 DAEMON_ALREADY_RUNNING。
    let again = run_to_exit(&env, &env.vars, &[]);
    assert_eq!(again.code, Some(3), "{}", again.stderr);
}

#[cfg(all(test, unix))]
#[test]
fn unwritable_state_dir_startup_save_exits_1_before_attach() {
    // sol-backfill p5 #4：Python 基線實測（非 root、state 目錄 555）——`SessionManager.__init__` 的 `_save_state()`
    // 於 mkstemp 拋 `PermissionError: [Errno 13] Permission denied: '<state>/state.json.tmp.XXXX'`，traceback 後 rc 1，
    // 尚未 attach（假 DUT 收到 0 bytes）、state 目錄無新檔。
    assert!(
        !is_root(),
        "證據不足：root 下 chmod 無效，state 目錄唯讀路徑須以非 root 執行"
    );
    let mut env = isolated_env_with_pty_target();
    chmod(&env.state_dir, "555");
    let out = run_to_exit(&env, &env.vars, &[]);
    chmod(&env.state_dir, "755");
    assert_eq!(out.code, Some(1), "{}", out.stderr);
    assert!(
        out.stderr.contains("Traceback (most recent call last):"),
        "{}",
        out.stderr
    );
    let last = out.stderr.lines().last().unwrap_or_default();
    assert!(
        last.starts_with("PermissionError: [Errno 13] Permission denied"),
        "{}",
        out.stderr
    );
    assert!(
        env.target_rx().is_empty(),
        "啟動存檔失敗不得對 DUT 送出任何 bytes"
    );
    assert_eq!(
        fs::read_dir(&env.state_dir).unwrap().count(),
        0,
        "state 目錄不得留下檔案"
    );
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
fn serial_backend_env_warning_is_emitted_once() {
    // 指定非原生後端才會警告：POSIX 原生為 termios，Windows 原生為 pyserial。
    let non_native = match sw_pal::native().caps.native_backend {
        sw_pal::BackendKind::Posix => "pyserial",
        sw_pal::BackendKind::Win => "posix",
    };
    let env = isolated_env();
    let mut vars = env.vars.clone();
    vars.insert("SERIALWRAP_SERIAL_BACKEND".to_string(), non_native.to_string());
    let mut daemon = DaemonProc::spawn_with(&env, &vars, &[]);
    wait_socket(&env.socket, SHORT);

    let warning = format!(
        "serialwrapd: SERIALWRAP_SERIAL_BACKEND={non_native} 在 Rust build 無對應後端，改用原生後端"
    );
    let stderr = daemon.stderr();
    assert_eq!(
        stderr.lines().filter(|line| *line == warning.as_str()).count(),
        1,
        "{stderr}"
    );

    rpc_once(&env.socket, STOP);
    assert_eq!(daemon.wait(LONG).code(), Some(0), "{}", daemon.stderr());
}

#[test]
fn daemon_stop_reply_rejects_new_health_ping() {
    let env = isolated_env();
    let endpoint = env.socket.clone();
    let platform = in_process_platform(&env.vars, Vec::new(), Fixture::None);
    let (entered_tx, entered_rx) = std::sync::mpsc::channel();
    let (release_tx, release_rx) = crossbeam_channel::bounded(1);
    let hooks = DaemonHooks {
        before_server_stop_accept: Some(Box::new(move || {
            entered_tx.send(()).expect("通知停機前置鉤子已進入");
            release_rx.recv().expect("解除停機前置鉤子");
        })),
        ..DaemonHooks::default()
    };
    let server = thread::spawn(move || run_with_hooks(vec!["serialwrapd".into()], platform, hooks));
    wait_socket(&endpoint, SHORT);

    let stopped = rpc_value(&endpoint, "daemon.stop", "{}");
    assert_eq!(stopped["ok"], true, "daemon.stop 必須先回覆成功：{stopped:?}");
    entered_rx.recv_timeout(SHORT).expect("daemon 停機必須抵達 server.stop_accept 前的同步點");
    let ping = rpc_value(&endpoint, "health.ping", "{}");
    release_tx.send(()).expect("解除停機前置鉤子");
    let code = server.join().expect("daemon 行程執行緒");

    assert_eq!(ping, PyValue::Null, "daemon.stop 回覆後新的 RPC 必須無法連線：{ping:?}");
    assert_eq!(code, 0, "daemon.stop 應完成正常停止");
}

#[test]
fn windows_default_allowlist_health_has_no_dynamic_sessions() {
    let caps = sw_pal::native().caps;
    let env = isolated_env();
    assert_eq!(
        env.vars[ALLOWLIST],
        support::test_device_allowlist_for_caps(caps),
        "隔離環境的預設 allowlist 必須依 native PAL caps 選擇"
    );
    let probe = TcpListener::bind("127.0.0.1:0").expect("配置 loopback RPC port");
    let port = probe.local_addr().unwrap().port();
    drop(probe);
    let endpoint = format!("tcp://127.0.0.1:{port}");
    let mut d = DaemonProc::spawn(&env, &["--socket", &endpoint]);
    wait_until("loopback daemon RPC", SHORT, || {
        loopback_rpc_value(port, "health.ping", "{}")["ok"] == true
    });

    if caps.native_device == sw_pal::DeviceBackendKind::Win {
        let health = loopback_rpc_value(port, "health.status", "{}");
        assert!(
            health["test_device_allowlist"]["enabled"] == true,
            "{health:?}"
        );
        assert_eq!(health["test_device_allowlist"]["source"], "env");
        assert_eq!(
            health["test_device_allowlist"]["coms"],
            pyjson::loads(r#"["COM250"]"#).unwrap()
        );
        assert_eq!(
            loopback_rpc_value(port, "session.list", "{}")["sessions"],
            pyjson::loads("[]").unwrap()
        );
    }

    assert!(loopback_rpc_value(port, "daemon.stop", "{}")["ok"] == true);
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
fn bare_double_dash_is_unrecognized_exit_2() {
    // sol-backfill p5 #5：Python 3.12 argparse 實測——無 positional 的 parser 不消耗 `--`，留在 extras：
    // `serialwrapd --`、`--profile-dir x --`、`-- -h` 皆 rc 2、末行 `unrecognized arguments: ...`，不得啟動。
    for (args, tail) in [
        (vec!["--"], "--"),
        (vec!["--profile-dir", "x", "--"], "--"),
        (vec!["--", "-h"], "-- -h"),
    ] {
        let env = isolated_env();
        let mut vars = env.vars.clone();
        vars.insert("COLUMNS".to_string(), "80".to_string());
        let out = run_to_exit(&env, &vars, &args);
        assert_eq!(out.code, Some(2), "{args:?}: {}", out.stderr);
        assert!(
            out.stderr.ends_with(&format!(
                "serialwrapd: error: unrecognized arguments: {tail}\n"
            )),
            "{args:?}: {}",
            out.stderr
        );
        assert!(
            !endpoint_is_serving(&env.socket),
            "{args:?}：不得 bind RPC endpoint"
        );
        assert!(
            !env.state_dir.join("state.json").exists(),
            "{args:?}：不得寫 state.json"
        );
    }
}

#[test]
fn no_args_uses_env_paths() {
    let env = isolated_env();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);
    assert_eq!(env.vars["SERIALWRAP_ENDPOINT"], env.endpoint);
    if env.endpoint.starts_with("tcp://") {
        assert!(env.endpoint.starts_with("tcp://127.0.0.1:"));
    } else {
        assert_eq!(env.socket, env.run_dir.join("serialwrapd.sock"));
    }
    assert!(
        env.run_dir.join("serialwrapd.lock").exists(),
        "lock 應位於 $SERIALWRAP_RUN_DIR"
    );
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

#[test]
fn endpoint_is_written_after_bind() {
    let env = isolated_env();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);
    let cfg = fs::read_to_string(env.config_dir.join("config.yaml"))
        .expect("config.yaml 應於 bind 後寫入");
    let want = format!("socket_path: {}", env.socket.display());
    assert!(cfg.lines().any(|l| l == want), "{cfg}");
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

/// OD-95：設定目錄唯讀時端點寫入失敗不致命；保留既有 Linux L1 案例識別碼。
#[cfg(all(test, unix))]
#[test]
fn endpoint_written_after_bind() {
    // 審查 F81：root 下 chmod 無效＝證據不足，必須 FAIL（不得 return 計為 PASS）。
    assert!(
        !is_root(),
        "證據不足：root 下 chmod 無效，寫入失敗路徑須以非 root 執行"
    );
    let env2 = isolated_env();
    chmod(&env2.config_dir, "555");
    let mut d2 = DaemonProc::spawn(&env2, &[]);
    wait_socket(&env2.socket, SHORT);
    d2.wait_stderr_contains(
        "serialwrapd: 寫入 config.yaml endpoint 失敗（非致命）: ",
        SHORT,
    );
    assert!(
        rpc_value(&env2.socket, "health.ping", "{}")["ok"] == true,
        "寫入失敗不得殺死 daemon"
    );
    rpc_once(&env2.socket, STOP);
    assert_eq!(d2.wait(LONG).code(), Some(0));
}

#[test]
fn serialwrap_config_env_read_and_write_target() {
    let env = isolated_env();

    // A：SERIALWRAP_CONFIG 原樣勝出（即使指向新檔）：讀此檔、endpoint 寫此檔、CONFIG_DIR/config.yaml 不建立。
    let alt = env.root.join("alt.yaml");
    fs::write(
        &alt,
        "windows:\n  exclude_coms: [COM3]\n  test_device_allowlist: [\"/x/by-id/b-alt\"]\n",
    )
    .unwrap();
    let mut vars = env.vars.clone();
    vars.insert(ALLOWLIST.to_string(), String::new());
    vars.insert("SERIALWRAP_CONFIG".to_string(), alt.display().to_string());
    let started_a = Instant::now();
    let mut d = DaemonProc::spawn_with(&env, &vars, &[]);
    wait_socket(&env.socket, SHORT);
    eprintln!(
        "[gpz16] startup_ready_ms={} case=SERIALWRAP_CONFIG",
        started_a.elapsed().as_millis()
    );
    let h = health_status(&env.socket);
    assert_eq!(h["test_device_allowlist"]["source"], "config");
    assert_eq!(h["test_device_allowlist"]["coms"][0], "/x/by-id/b-alt");
    let text = fs::read_to_string(&alt).unwrap();
    let want = format!("socket_path: {}", env.socket.display());
    assert!(
        text.lines().any(|l| l == want),
        "endpoint 應寫入 SERIALWRAP_CONFIG：{text}"
    );
    assert!(
        !env.config_dir.join("config.yaml").exists(),
        "SERIALWRAP_CONFIG 生效時不得建立 CONFIG_DIR/config.yaml"
    );
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));

    // B：未設 SERIALWRAP_CONFIG → CONFIG_DIR（不存在）→ 系統檔（唯讀 fallback）；永不寫入系統檔。
    let sys_yaml = "windows:\n  test_device_allowlist: [\"/sys/by-id/c\"]\n";
    fs::create_dir_all(env.system_config.parent().unwrap()).unwrap();
    fs::write(&env.system_config, sys_yaml).unwrap();
    let mut vars2 = env.vars.clone();
    vars2.insert(ALLOWLIST.to_string(), String::new());
    let started_b = Instant::now();
    let mut d2 = DaemonProc::spawn_with(&env, &vars2, &[]);
    wait_socket(&env.socket, SHORT);
    eprintln!(
        "[gpz16] startup_ready_ms={} case=system-config-fallback",
        started_b.elapsed().as_millis()
    );
    let h2 = health_status(&env.socket);
    assert_eq!(h2["test_device_allowlist"]["source"], "config");
    assert_eq!(h2["test_device_allowlist"]["coms"][0], "/sys/by-id/c");
    assert_eq!(
        fs::read_to_string(&env.system_config).unwrap(),
        sys_yaml,
        "系統檔不得被寫入"
    );
    assert!(
        env.config_dir.join("config.yaml").exists(),
        "endpoint 應寫入 CONFIG_DIR/config.yaml"
    );
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
    let client = spawn_stop_guarded_client(socket.clone(), move || {
        wait_socket(&socket, SHORT);
        // 動態 session 於 attach worker 完成 PROBE 後才建立（與 Python `_attach_by_id_dynamic` 相同），輪詢至 3 個 session。
        let mut rows: Vec<PyValue> = Vec::new();
        let deadline = Instant::now() + LONG;
        while Instant::now() < deadline {
            rows = session_list(&socket)
                .as_array()
                .cloned()
                .unwrap_or_default();
            if rows.len() == 3 {
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }
        rpc_once(&socket, STOP);
        rows
    });
    let code = run_with_hooks(
        vec!["serialwrapd".to_string()],
        platform,
        trace_hooks(sink.clone()),
    );
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
    let client = spawn_stop_guarded_client(socket.clone(), move || {
        // client thread 不得在送出 STOP 前 panic（daemon 會一直等停止要求）：輪詢逾時只回 false，斷言留到主執行緒。
        let poll = |cond: &mut dyn FnMut() -> bool| {
            let deadline = Instant::now() + LONG;
            loop {
                if cond() {
                    return true;
                }
                if Instant::now() >= deadline {
                    return false;
                }
                thread::sleep(Duration::from_millis(50));
            }
        };
        let cmd_id = |r: &PyValue| r["cmd_id"].as_str().unwrap_or_default().to_string();
        wait_socket(&socket, SHORT);
        // 先等 READY：socket bind 時 COM0 可能仍在 ATTACHING（全集並行負載下實測），此時送 blk 會被拒為
        // SESSION_NOT_READY，q2 隨即成為執行中的前景命令而不在佇列（停機時被取消而非 FLUSHED_BY_SHUTDOWN）。
        let ready = poll(&mut || get_state(&socket, "COM0")["state"] == "READY");
        // 前景命令 BlockUntilCancel：writer 卡住直到停機取消（submit 於 admission 即回覆）。
        let blk = submit(&socket, "COM0", "blk");
        let c1 = cmd_id(&blk);
        // 等 worker 撿起 blk（status running），q2／q3 才是排在它之後、未開始的排隊命令。
        let running =
            !c1.is_empty() && poll(&mut || command_get(&socket, &c1)["status"] == "running");
        // 先等 q2 admission 回覆，再送 q3，固定 flush trace 的 admission 順序。
        let c2 = cmd_id(&submit(&socket, "COM0", "q2"));
        let c3 = cmd_id(&submit(&socket, "COM0", "q3"));
        // 讓 BlockUntilCancel 前景命令確實進入 writer，再送停機；保留原測試的排程窗口。
        thread::sleep(Duration::from_millis(300));
        // 停機前最後一次 command.get：排隊命令仍為 accepted（入列、未開始）。
        let queued: Vec<PyValue> = [&c2, &c3]
            .iter()
            .map(|id| command_get(&socket, id)["status"].clone())
            .collect();
        rpc_once(&socket, STOP);
        (ready, blk, running, c2, c3, queued)
    });
    let code = run_with_hooks(
        vec!["serialwrapd".to_string()],
        platform,
        trace_hooks(sink.clone()),
    );
    let (ready, blk, running, c2, c3, queued) = client.join().expect("client thread");
    assert!(ready, "COM0 未在 {LONG:?} 內進入 READY");
    assert_eq!(blk["ok"], true, "前景命令 blk 應被接受：{blk:?}");
    assert!(
        running,
        "前景命令 blk 未在 {LONG:?} 內進入 running：{blk:?}"
    );
    assert!(
        !c2.is_empty() && !c3.is_empty(),
        "排隊命令應取得 cmd_id：{c2:?} {c3:?}"
    );
    for (id, status) in [&c2, &c3].iter().zip(&queued) {
        assert_eq!(*status, "accepted", "STOP 前排隊命令 {id} 應尚未開始");
    }
    assert_eq!(code, 0);

    // 停機序列（P8 Task 12 停止序 1～7）：stop_accept → stopping gate → watcher → event intake → ttyMCU 端點 →
    // drain Phase 1 → join handler → Phase 2 → FLUSHED_BY_SHUTDOWN 收尾（排隊命令）→ engine.stop → WAL sync → 釋放鎖。
    let t = sink.lock().unwrap().clone();
    let tail = t.len().saturating_sub(12);
    assert_eq!(
        t[tail..].to_vec(),
        vec![
            TraceEvent::ServerStopAccept,
            TraceEvent::ShutdownGate,
            TraceEvent::WatcherStopped,
            TraceEvent::EngineIntakeClosed,
            TraceEvent::FlashStopped {
                joined: true,
                unlinked: true
            },
            TraceEvent::DrainPhase1,
            TraceEvent::HandlersJoined { pending: vec![] },
            TraceEvent::DrainPhase2 {
                stalled: vec![],
                drained: vec![("t:COM0".to_string(), true)],
            },
            TraceEvent::SessionsDrained {
                flushed: vec![
                    (c2, "FLUSHED_BY_SHUTDOWN".to_string()),
                    (c3, "FLUSHED_BY_SHUTDOWN".to_string()),
                ],
                stalled: vec![],
            },
            TraceEvent::EngineStopped { unreaped: vec![] },
            TraceEvent::WalShutdown { ok: true },
            TraceEvent::LockReleased,
        ]
    );
}

#[cfg(all(test, unix))]
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
        .map(|h| {
            h.join().expect("queued submit")["cmd_id"]
                .as_str()
                .unwrap_or_default()
                .to_string()
        })
        .collect();
    // 停機前最後一次 command.get：排隊命令仍為 accepted（未開始），停機 drain 才會以 FLUSHED_BY_SHUTDOWN 終結。
    for id in &queued_ids {
        assert!(!id.is_empty(), "排隊命令應取得 cmd_id");
        assert_eq!(
            command_get(&socket, id)["status"],
            "accepted",
            "SIGTERM 前排隊命令 {id} 應尚未開始"
        );
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
    assert!(!endpoint_is_serving(&env.socket), "有序停機後 RPC endpoint 應已關閉");

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
    let out = run_child_test(
        "shutdown_with_stalled_drain_exits_1_with_diagnostic",
        "stall",
        &env,
    );
    assert_eq!(
        out.code,
        Some(1),
        "不得宣告 graceful success：{}",
        out.stderr
    );
    assert!(
        out.stderr
            .contains("serialwrapd: shutdown incomplete: drain stalled: t:COM0"),
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
        out.stderr
            .contains("serialwrapd: shutdown incomplete: WAL sync failed: "),
        "{}",
        out.stderr
    );
}

#[cfg(all(test, unix))]
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

#[cfg(all(test, unix))]
#[test]
fn wal_dir_readonly_at_start_blocks_login_probe() {
    // 審查 F81：root 下 chmod 無效＝證據不足，必須 FAIL（不得 return 計為 PASS）。
    assert!(
        !is_root(),
        "證據不足：root 下 chmod 無效，唯讀 WAL 路徑須以非 root 執行（P5-T19 Review Focus 3）"
    );
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
    wait_until("unhealthy_reason == write_failed", LONG, || {
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

/// rulings/p5.md P6 SPEC-GAP #4（2026-10-29）：Rust daemon 以一般使用者執行時，鎖後 holder 掃描讀不到他人行程的
/// `/proc/<pid>/fd`（EACCES → `Permission`）不得使開埠 fail-closed。對齊 Python `serial_port.probe_holders`：
/// pids 空且 status 非 ok → 警告「無法確認 … 的外部 holder（status=…）」並照常開啟（`_set_holder_unknown`），
/// `health.status.foreign_holders` 以 `{device, holder_unknown:true}` 回報，session 照常 READY。保守判定只保留在
/// CLI 升級／回滾的安全檢查（`serialwrapd_tty_holders`，rulings/p7.md P7-71fe017-4）。
#[cfg(all(test, unix))]
#[test]
fn non_root_holder_scan_permission_does_not_block_open() {
    // root 可讀所有 /proc/<pid>/fd，無法覆蓋權限不足路徑＝證據不足，必須 FAIL（不得 return 計為 PASS）。
    assert!(
        !is_root(),
        "證據不足：須以非 root 執行（root 下 holder 掃描不會遇到 EACCES）"
    );
    let denied = fs::read_dir("/proc")
        .expect("讀 /proc")
        .filter_map(Result::ok)
        .filter(|e| e.file_name().to_string_lossy().bytes().all(|b| b.is_ascii_digit()))
        .any(|e| matches!(fs::read_dir(e.path().join("fd")), Err(err) if err.kind() == std::io::ErrorKind::PermissionDenied));
    assert!(
        denied,
        "證據不足：本機沒有 fd 目錄不可讀的他人行程，無法覆蓋權限不足路徑"
    );
    let env = isolated_env_with_pty_target();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);
    wait_ready(&env, "COM0");
    // 與 Python `serial_port.py` 逐字相同（`report.status`＝"permission"；daemon_logging.yaml uart_holder_scan_unknown）。
    d.wait_stderr_contains(
        "的外部 holder（status=permission），BC-04 僅保證 serialwrap 實例互斥",
        SHORT,
    );
    let h = health_status(&env.socket);
    let holders = format!("{:?}", h["foreign_holders"]);
    assert!(
        holders.contains("holder_unknown"),
        "foreign_holders 應回報 holder 未知的裝置：{holders}"
    );
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

#[cfg(all(test, unix))]
#[test]
fn wal_fault_after_ready_splits_agent_and_auto_tx() {
    // 審查 F81：root 下 chmod 無效＝證據不足，必須 FAIL（不得 return 計為 PASS）。
    assert!(
        !is_root(),
        "證據不足：root 下 chmod 無效，WAL 故障路徑須以非 root 執行（P5-T19 Review Focus 3）"
    );
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
    wait_until("command done", LONG, || {
        command_get(&socket, &cmd_id)["status"] == "done"
    });
    let rx = env.target_rx();
    assert!(
        rx.windows(7).any(|w| w == b"echo x\n"),
        "agent 命令應送出 echo x\\n"
    );

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
            row["dir"] == "TX"
                && row["source"]
                    .as_str()
                    .is_some_and(|s| s.contains("recover"))
        })
        .filter_map(|row| row["payload_b64"].as_str().map(str::to_string))
        .collect()
}

#[cfg(all(test, unix))]
#[test]
fn daemon_stop_during_force_recover_with_client_gone() {
    // P5-T19 修正（與計畫 Review Focus 5 衝突，依 OD-14 #1 以 Python 行為為準，待擁有者確認；審查 F87）：
    // Python 基線實測（`tests/contract_blackbox/slice21/test_safety_core.py::
    // test_pipelined_stop_after_client_gone_not_processed`，Python 輪 2/2 PASS）——同連線 pipelining
    // `session.recover --force`＋`daemon.stop` 後 client 立即斷線：recover handler 照常跑完（READY 路徑
    // `_recover_after_failure`，假 DUT 對 Ctrl-C 立即回 prompt → WAL 恰一筆 `system:recover` 的 `\x03`），回應寫入失敗後
    // 連線關閉，管線中的 `daemon.stop` 不被處理；daemon 持續運作，之後由新連線 `daemon.stop` 才有序停機（exit 0）。
    let mut env = isolated_env_with_pty_target();
    let mut d = DaemonProc::spawn(&env, &[]);
    wait_ready(&env, "COM0");

    let payload = format!(
        "{}\n{}\n",
        r#"{"id":3,"method":"session.recover","params":{"selector":"COM0","force":true}}"#, STOP
    );
    let sent = Instant::now();
    env.target_hold_reply();
    send_and_drop(&env.socket, &payload); // 送出後立即斷線
    wait_until(
        "假 DUT 收到 Ctrl-C（回應仍暫停）",
        Duration::from_millis(1500),
        || env.target_rx().contains(&0x03),
    );
    env.target_release_reply();

    // 收斂以救援 TX 實際寫入 WAL 為準（不以狀態判斷：COM0 起始即 READY）。
    wait_until("救援 TX 寫入 WAL", LONG, || {
        !recover_tx_payloads(&env).is_empty()
    });
    thread::sleep(Duration::from_secs(1));
    assert_eq!(
        recover_tx_payloads(&env),
        vec!["Aw==".to_string()],
        "救援 TX 應恰為 \\x03"
    );
    // 超過 force recover 上限（10 s）＋餘裕：管線中的 stop 若於 handler 結束後才被處理，此時行程已結束。
    if let Some(rest) = Duration::from_secs(11).checked_sub(sent.elapsed()) {
        thread::sleep(rest);
    }
    assert_eq!(
        recover_tx_payloads(&env),
        vec!["Aw==".to_string()],
        "READY prompt 後 CTRL-C 已結束 recover，不應再送 CTRL-D"
    );

    // 管線中的 daemon.stop 未被處理：daemon 仍存活、socket 保留，新連線 health.status 回 ok。
    assert!(
        d.try_alive(),
        "回應寫入失敗後連線關閉，管線中的 daemon.stop 不得被處理：{}",
        d.stderr()
    );
    assert!(endpoint_is_serving(&env.socket), "管線中的 daemon.stop 不得停止 RPC endpoint");
    assert!(
        health_status(&env.socket)["ok"] == true,
        "daemon 應仍可服務新連線"
    );
    let last = last_wal_line(&env);
    assert!(pyjson::loads(&last).is_ok(), "WAL 最後一行不完整：{last}");

    // 完成命令作為 session worker barrier：只有 recover handler 已結算，後續命令才會完成。
    let barrier = submit(&env.socket, "COM0", "echo barrier");
    let barrier_id = barrier["cmd_id"].as_str().unwrap_or_default().to_string();
    assert!(
        !barrier_id.is_empty(),
        "barrier command 未取得 cmd_id：{barrier:?}"
    );
    wait_until("recover 後 barrier command done", LONG, || {
        command_get(&env.socket, &barrier_id)["status"] == "done"
    });
    assert!(
        env.target_rx().windows(13).any(|w| w == b"echo barrier\n"),
        "recover handler 完成後 daemon 應能處理新命令"
    );
    assert_eq!(
        recover_tx_payloads(&env),
        vec!["Aw==".to_string()],
        "新連線的 barrier 不得補出救援 Ctrl-D"
    );

    // 新連線 daemon.stop → 有序停機。
    let started = Instant::now();
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(Duration::from_secs(15)).code(), Some(0));
    assert!(started.elapsed() <= Duration::from_secs(15));
    assert!(!endpoint_is_serving(&env.socket), "有序停機後 RPC endpoint 應已關閉");
}

#[cfg(all(test, unix))]
#[test]
fn daemon_stop_from_second_connection_during_force_recover() {
    // 審查 F87（Opus 修法）：force recover handler 進行中（假 DUT 暫停回應 8 s）、client 已斷線，由第二條連線送
    // `daemon.stop`：關機等 handler 收斂後才 drain（Python `server.stop()` 先於 `service.stop()`），exit 0、
    // 救援 TX 已在 WAL、WAL 每一行皆為完整 JSON、socket 移除。
    // Python 基線：`test_safety_core.py::test_daemon_stop_from_second_connection_during_force_recover`（shell DUT，
    // 2/2 PASS，stop→exit 約 8.2 s）。本 rig 的假 DUT 為登入型：`\x04` 登出後 reattach 停在 login，handler 依
    // Python `_force_recover` 10 × 1 s 逾時（約 14 s）才回 FORCE_RECOVER_TIMEOUT——上限以 SERVER_STOP_BUDGET（20 s）
    // ＋drain 計，不以計畫假設的「約 10 s」。
    // P8 Task 12 起 Rust 停止序改為 drain Phase 1 先取消 handler 再 join（共用 5 s 期限），不再先等 handler 自然
    // 結束；本案斷言（exit 0、不報 shutdown incomplete、≤ 25 s、WAL 完整、socket 移除）不變。
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
    wait_until("救援 TX 寫入 WAL", SHORT, || {
        !recover_tx_payloads(&env).is_empty()
    });
    let started = Instant::now();
    rpc_once(&env.socket, STOP);
    let code = d.wait(Duration::from_secs(30)).code();
    let elapsed = started.elapsed();
    eprintln!("daemon.stop → exit 耗時 {elapsed:?}（登入型 DUT，非 Python shell DUT 同情境）");
    assert_eq!(code, Some(0), "耗時 {elapsed:?}：{}", d.stderr());
    assert!(elapsed <= Duration::from_secs(25), "{elapsed:?}");
    assert!(
        !d.stderr().contains("shutdown incomplete"),
        "handler 收斂後的關機不得報 shutdown incomplete：{}",
        d.stderr()
    );
    let wal = fs::read_to_string(env.wal_dir.join("raw.wal.ndjson")).expect("讀取 WAL");
    for line in wal.lines() {
        assert!(pyjson::loads(line).is_ok(), "WAL 行不完整：{line}");
    }
    assert!(!endpoint_is_serving(&env.socket), "有序停機後 RPC endpoint 應已關閉");
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
    let default_run = PathBuf::from(&env.vars["XDG_STATE_HOME"])
        .join("serialwrap")
        .join("run");
    let mut vars = env.vars.clone();
    vars.insert(
        "SERIALWRAP_RUN_DIR".to_string(),
        default_run.display().to_string(),
    );
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
    // Python `_run_async` 在 allowlist 判定後才呼叫 `ensure_runtime_dirs()`；拒絕前不得建立預設 RUN_DIR。
    assert!(
        !default_run.exists(),
        "allowlist 拒絕前不得建立預設 RUN_DIR：{}",
        default_run.display()
    );

    // (3) config windows.test_device_allowlist 非 list。
    fs::write(
        env.config_dir.join("config.yaml"),
        "windows:\n  test_device_allowlist: \"COM7\"\n",
    )
    .unwrap();
    let mut vars = env.vars.clone();
    vars.insert(ALLOWLIST.to_string(), String::new());
    let out = run_allowlist_preflight_to_exit(&vars, &[]);
    assert_eq!(out.code, Some(2), "{}", out.stderr);
    assert_eq!(
        out.stderr,
        "serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: invalid windows.test_device_allowlist\n"
    );
    assert!(
        !env.run_dir.join("serialwrapd.lock").exists(),
        "格式錯誤 allowlist 必須在取鎖前拒絕"
    );
    assert!(
        !env.state_dir.join("state.json").exists(),
        "格式錯誤 allowlist 必須在載入或寫入狀態前拒絕"
    );
    assert!(
        !endpoint_is_serving(&env.socket),
        "格式錯誤 allowlist 必須在綁定 RPC endpoint 前拒絕"
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
            ..DaemonHooks::default()
        };
        let code = run_with_hooks(vec!["serialwrapd".to_string()], sw_pal::native(), hooks);
        std::process::exit(code);
    }
    let env = isolated_env();
    let out = run_child_test("uncaught_panic_exit_1", "panic", &env);
    assert_eq!(
        out.code,
        Some(1),
        "未攔截 panic 必須 exit 1：{}",
        out.stderr
    );
    assert!(
        out.stderr.contains("slice21 T19 injected panic"),
        "{}",
        out.stderr
    );
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
    assert!(
        rpc_value(&env.socket, "health.ping", "{}")["ok"] == true,
        "handler panic 不得殺死 daemon"
    );
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
    assert!(
        out.stderr.starts_with("usage: serialwrapd"),
        "{}",
        out.stderr
    );

    // (2) LOCK 後端非法＋非隔離＋allowlist：SC-19 先行 → rc 2、TEST_DEVICE_ALLOWLIST_REJECTED。
    let mut vars = non_isolated_vars(&env);
    vars.insert("SERIALWRAP_LOCK_BACKEND".to_string(), "bogus".to_string());
    vars.insert(ALLOWLIST.to_string(), "/x/by-id/a".to_string());
    let out = run_to_exit(&env, &vars, &[]);
    assert_eq!(out.code, Some(2), "{}", out.stderr);
    assert!(
        out.stderr.contains("TEST_DEVICE_ALLOWLIST_REJECTED"),
        "{}",
        out.stderr
    );

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
    vars.insert(
        "SERIALWRAP_RUN_DIR".to_string(),
        fresh_run.display().to_string(),
    );
    vars.insert(
        "SERIALWRAP_WAL_DIR".to_string(),
        fresh_wal.display().to_string(),
    );
    vars.insert(ALLOWLIST.to_string(), "/x/by-id/a".to_string());
    let out = run_to_exit(&env, &vars, &[]);
    assert_eq!(out.code, Some(2), "{}", out.stderr);
    assert_eq!(
        out.stderr,
        "serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: not an isolated instance: SERIALWRAP_STATE_DIR unset\n"
    );
    assert!(
        !default_state_dir(&env).exists(),
        "被拒時不得建立 STATE_DIR"
    );
    assert!(!fresh_run.exists(), "被拒時不得建立 RUN_DIR");
    assert!(!fresh_wal.exists(), "被拒時不得建立 WAL_DIR");
    assert!(
        !fresh_run.join("serialwrapd.lock").exists(),
        "被拒時不得取鎖／建立 lock 檔"
    );
    assert!(
        !env.config_dir.join("config.yaml").exists(),
        "被拒時不得寫入 config.yaml"
    );
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
    let caps = sw_pal::native().caps;
    if caps.native_device == sw_pal::DeviceBackendKind::Win {
        let env = isolated_env();
        let mut vars = env.vars.clone();
        vars.insert(ALLOWLIST.to_string(), String::new());
        let error = match DaemonProc::try_spawn_with_caps(&env, &vars, &[], caps) {
            Err(error) => error,
            Ok(daemon) => {
                drop(daemon);
                panic!("Windows caps 下沒有 allowlist 時，harness 不得啟動 serialwrapd");
            }
        };
        assert!(
            error.contains("SERIALWRAP_TEST_DEVICE_ALLOWLIST") && error.contains("COM250"),
            "Windows 空 allowlist 必須由 harness 明確拒絕：{error}"
        );
        return;
    }

    let rows = unreadable_rows();
    assert!(rows.len() >= 12, "golden 列數不足：{}", rows.len());
    for row in rows {
        let env = isolated_env();
        let cfg = env.config_dir.join("config.yaml");
        fs::write(&cfg, &row.config).unwrap();
        // 此案例驗證壞 config 的失敗行為，必須明確走 config fallback；Windows 隔離環境預設為 COM250。
        let mut vars = env.vars.clone();
        vars.insert(ALLOWLIST.to_string(), String::new());
        let out = if row.case_id == "permission_denied" {
            chmod(&cfg, "000");
            if is_root() {
                // root 讀檔不受 mode 限制：以 setpriv 降為 nobody 執行（與 golden 產生器相同）；無 setpriv 則證據不足。
                if !Path::new("/usr/bin/setpriv").exists() {
                    // 審查 F81：證據不足必須 FAIL（不得 continue 計為 PASS）。
                    panic!("證據不足：permission_denied 列需非 root 或 /usr/bin/setpriv");
                }
                let o = Command::new("/usr/bin/setpriv")
                    .args([
                        "--reuid=65534",
                        "--regid=65534",
                        "--clear-groups",
                        DAEMON_BIN,
                    ])
                    .envs(&vars)
                    .stdin(std::process::Stdio::null())
                    .output()
                    .expect("setpriv 執行 daemon");
                Output {
                    code: o.status.code(),
                    stderr: String::from_utf8_lossy(&o.stderr).into_owned(),
                }
            } else {
                run_to_exit(&env, &vars, &[])
            }
        } else {
            run_to_exit(&env, &vars, &[])
        };
        assert_eq!(out.code, Some(row.code), "{}：{}", row.case_id, out.stderr);
        assert_eq!(out.stderr, row.stderr, "{}", row.case_id);
        assert!(
            !env.run_dir.join("serialwrapd.lock").exists(),
            "{}：被拒時不得取鎖",
            row.case_id
        );
    }
}

/// #47：非隔離實例＋壞 config.yaml → 照常啟動；POSIX 不輸出 `test_device_allowlist`，Windows 以 COM250 allowlist 啟動；
/// endpoint 寫入時重新讀取同一份壞檔失敗，只寫非致命 stderr（Python `_write_config_endpoint`）。
#[test]
fn unreadable_config_non_isolated_unchanged() {
    let env = isolated_env();
    fs::write(env.config_dir.join("config.yaml"), b"a:\n\tb: 1\n").unwrap();
    let mut vars = non_isolated_vars(&env);
    if sw_pal::native().caps.native_device == sw_pal::DeviceBackendKind::Win {
        vars.insert(ALLOWLIST.to_string(), "COM250".to_string());
        let out = run_to_exit(&env, &vars, &[]);
        assert_eq!(out.code, Some(2), "{}", out.stderr);
        assert_eq!(
            out.stderr,
            "serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: not an isolated instance: SERIALWRAP_STATE_DIR unset\n"
        );
        assert!(
            !env.run_dir.join("serialwrapd.lock").exists(),
            "Windows 非隔離 allowlist 必須在取鎖前拒絕"
        );
        return;
    }

    vars.insert(ALLOWLIST.to_string(), String::new());
    let mut d = DaemonProc::spawn_with(&env, &vars, &[]);
    wait_socket(&env.socket, SHORT);
    let h = health_status(&env.socket);
    assert!(h["ok"] == true, "{h:?}");
    assert!(
        h["test_device_allowlist"] == PyValue::Null,
        "非隔離 POSIX 實例不得輸出 test_device_allowlist：{h:?}"
    );
    d.wait_stderr_contains(
        "serialwrapd: 寫入 config.yaml endpoint 失敗（非致命）: ",
        SHORT,
    );
    assert!(
        !d.stderr().contains("TEST_DEVICE_ALLOWLIST_REJECTED"),
        "{}",
        d.stderr()
    );
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
    assert_eq!(
        h["test_device_allowlist"]["coms"],
        pyjson::loads(r#"["/x/by-id/a"]"#).unwrap()
    );
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));
}

const RELEASE: &str = r#"{"id":7,"method":"device.release","params":{"selector":"COM0"}}"#;

/// #3／#68：產品 build 讀 `SERIALWRAP_TEST_KILL_AFTER_SAVE=1`；隔離實例於 release 持久化 commit 後、回覆前 exit 137，
/// 重啟後仍 RELEASED；非隔離實例只寫凍結的 WARNING（`contracts/daemon_logging.yaml`）、不終止。
#[cfg(all(test, unix))]
#[test]
fn release_kill_hook_env_isolated_only() {
    // 隔離實例：commit 後被殺。
    let env = isolated_env_with_pty_target();
    let mut vars = env.vars.clone();
    vars.insert(
        "SERIALWRAP_TEST_KILL_AFTER_SAVE".to_string(),
        "1".to_string(),
    );
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
    assert!(
        state.contains("\"released\"") && state.contains(TARGET_BY_ID),
        "{state}"
    );
    let mut d2 = DaemonProc::spawn(&env, &[]);
    wait_socket(&env.socket, SHORT);
    assert_eq!(get_state(&env.socket, "COM0")["state"], "RELEASED");
    rpc_once(&env.socket, STOP);
    assert_eq!(d2.wait(LONG).code(), Some(0));

    // 非隔離實例：只寫 WARNING、不終止，release 正常回覆。
    let env = isolated_env_with_pty_target();
    let mut vars = non_isolated_vars(&env);
    vars.insert(
        "SERIALWRAP_TEST_KILL_AFTER_SAVE".to_string(),
        "1".to_string(),
    );
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
#[cfg(all(test, unix))]
#[test]
fn release_pause_hook_env_isolated_only() {
    let env = isolated_env_with_pty_target();
    let pause = env.root.join("pause");
    let mut vars = env.vars.clone();
    vars.insert(
        "SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE".to_string(),
        pause.display().to_string(),
    );
    let mut d = DaemonProc::spawn_with(&env, &vars, &[]);
    wait_ready(&env, "COM0");
    let state_path = env.state_dir.join("state.json");
    let before = fs::read(&state_path).ok();
    let socket = env.socket.clone();
    let release = thread::spawn(move || rpc_once(&socket, RELEASE));
    let arrived = pause.join("arrived");
    wait_until("arrived 標記", LONG, || arrived.exists());
    assert_eq!(
        fs::read_to_string(&arrived).unwrap(),
        format!("pid={}\n", d.pid())
    );
    thread::sleep(Duration::from_millis(300));
    assert!(!release.is_finished(), "go 標記出現前 release 不得回覆");
    assert_eq!(
        fs::read(&state_path).ok(),
        before,
        "commit 前暫停：state.json 不得改變"
    );
    fs::write(pause.join("go"), b"").unwrap();
    let r = pyjson::loads(&release.join().unwrap()).unwrap_or(PyValue::Null);
    assert!(r["ok"] == true, "{r:?}");
    assert_eq!(get_state(&env.socket, "COM0")["state"], "RELEASED");
    rpc_once(&env.socket, STOP);
    assert_eq!(d.wait(LONG).code(), Some(0));

    let env = isolated_env_with_pty_target();
    let pause = env.root.join("pause");
    let mut vars = non_isolated_vars(&env);
    vars.insert(
        "SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE".to_string(),
        pause.display().to_string(),
    );
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
#[cfg(all(test, unix))]
#[test]
fn sigterm_sigint_orderly_stop() {
    for sig in ["-TERM", "-INT"] {
        let env = isolated_env_with_pty_target();
        let mut d = DaemonProc::spawn(&env, &[]);
        wait_ready(&env, "COM0");
        let status = Command::new("kill")
            .arg(sig)
            .arg(d.pid().to_string())
            .status()
            .expect("送出訊號");
        assert!(status.success());
        assert_eq!(d.wait(LONG).code(), Some(0), "{sig}：{}", d.stderr());
        assert!(!endpoint_is_serving(&env.socket), "{sig}：RPC endpoint 應已關閉");
        let last = last_wal_line(&env);
        assert!(
            pyjson::loads(&last).is_ok(),
            "{sig}：WAL 最後一筆不完整：{last}"
        );
        assert!(
            pty_can_open_exclusively(&env.pty_slave),
            "{sig}：tty 應已釋放"
        );
        // 鎖已釋放：新 daemon 可取得。
        let mut d2 = DaemonProc::spawn(&env, &[]);
        wait_socket(&env.socket, SHORT);
        rpc_once(&env.socket, STOP);
        assert_eq!(d2.wait(LONG).code(), Some(0), "{sig}");
    }
}

/// #72／A-94（Windows）：互動桌面送出 CTRL_C／CTRL_BREAK_EVENT 後走同一套有序停機序。
///
/// Python `sw_core/daemon.py:293-301` 只註冊 SIGINT／SIGTERM；Windows Rust PAL 的 console handler
/// 將 CTRL_C／CTRL_BREAK／CTRL_CLOSE 共用轉為 daemon stop channel（`crates/sw-pal/src/signal/windows.rs:51-59`）。
/// CTRL_CLOSE 只能由關閉 console 觸發，無法在共用互動桌面中安全合成；本測試以兩種可定向的 control event
/// 驗證完整 daemon 路徑，並由同一 handler 分支涵蓋 CTRL_CLOSE 的映射。
#[test]
#[ignore = "需要 Windows Terminal 互動桌面；由 win-interactive-l1.ps1 執行"]
#[allow(non_snake_case)] // 依 P3 能力節點規範使用 cap_<resource>__<name> 命名。
fn cap_interactive_desktop__windows_console_ctrl_triggers_orderly_stop() {
    if std::env::var_os("SW_S21_WINDOWS_CTRL_CHILD").is_some() {
        windows_ctrl_child_main();
    } else {
        windows_ctrl_parent();
    }
}

fn windows_ctrl_child_main() {
    use sw_daemon::daemon_main::{ENV_TEST_T13_WAL_RECORD, T13_WAL_TEST_PAYLOAD};
    sw_pal::test_support::enable_ctrl_c().expect("重新啟用子行程的 CTRL_C 接收");
    let mut env = isolated_env();
    env.vars.insert(
        "SERIALWRAP_TEST_DEVICE_ALLOWLIST".to_string(),
        "COM250".to_string(),
    );
    let wal_marker = env.root.join("t13-wal-marker");
    fs::write(&wal_marker, T13_WAL_TEST_PAYLOAD).expect("寫入隔離 WAL marker");
    env.vars.insert(
        ENV_TEST_T13_WAL_RECORD.to_string(),
        wal_marker.display().to_string(),
    );

    let trace = Arc::new(Mutex::new(Vec::<TraceEvent>::new()));
    let trace_for_hook = trace.clone();
    let ready_endpoint = env.endpoint.clone();
    let ready_file = PathBuf::from(
        std::env::var_os("SW_S21_WINDOWS_CTRL_READY_FILE").expect("子行程缺少 ready marker 路徑"),
    );
    let hooks = DaemonHooks {
        trace: Some(Box::new(move |event| {
            if matches!(event, TraceEvent::EndpointWritten) {
                fs::write(&ready_file, format!("{ready_endpoint}\n"))
                    .expect("發布 daemon endpoint ready marker");
            }
            trace_for_hook
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .push(event);
        })),
        ..DaemonHooks::default()
    };

    let platform = in_process_platform(&env.vars, Vec::new(), Fixture::None);
    platform
        .signals
        .install()
        .expect("註冊 Windows console control handler");
    let code = run_with_hooks(vec!["serialwrapd".to_string()], platform, hooks);
    assert_eq!(code, 0, "console control 有序停機應 exit 0");
    assert!(
        !endpoint_is_serving(&env.socket),
        "TCP RPC endpoint 在 stop accept／server stop 後應不再提供服務"
    );

    const EXPECTED_STEPS: [&str; 12] = [
        "server.stop_accept",
        "manager.begin_shutdown",
        "watcher.stop",
        "engine.close_intake",
        "flash.stop",
        "drain.phase1",
        "server.join_handlers",
        "drain.phase2",
        "flush_shutdown",
        "engine.stop",
        "wal.sync_close",
        "lock.release",
    ];
    let events = trace
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone();
    let steps = events
        .iter()
        .filter_map(TraceEvent::shutdown_step)
        .collect::<Vec<_>>();
    assert_eq!(steps, EXPECTED_STEPS, "停機步驟與 POSIX orderly-stop 對齊");
    assert!(
        events.iter().any(|event| matches!(
            event,
            TraceEvent::HandlersJoined { pending } if pending.is_empty()
        )),
        "RPC handlers 應完成 join"
    );
    assert!(
        events.iter().any(|event| matches!(
            event,
            TraceEvent::DrainPhase2 { stalled, drained } if stalled.is_empty() && drained.is_empty()
        )),
        "空 session 集合應完成 drain"
    );
    assert!(
        events.iter().any(|event| matches!(
            event,
            TraceEvent::SessionsDrained { flushed, stalled } if flushed.is_empty() && stalled.is_empty()
        )),
        "shutdown flush 不應留下未完成 session"
    );
    assert!(
        events
            .iter()
            .any(|event| matches!(event, TraceEvent::WalShutdown { ok: true })),
        "WAL sync／close 應成功"
    );
    let last = last_wal_line(&env);
    assert!(
        pyjson::loads(&last).is_ok(),
        "WAL 最後一筆應為完整 JSON：{last}"
    );

    // 能用同一組 endpoint／lock 再啟動並以 RPC 停止，證明 listener 與 singleton lock 均已釋放。
    let platform = in_process_platform(&env.vars, Vec::new(), Fixture::None);
    platform
        .signals
        .install()
        .expect("重啟測試 daemon 時 console handler 保持註冊");
    let endpoint = env.socket.clone();
    let client = spawn_stop_guarded_client(endpoint.clone(), move || {
        wait_socket(&endpoint, LONG);
        rpc_once(&endpoint, STOP)
    });
    let code = run_with_hooks(
        vec!["serialwrapd".to_string()],
        platform,
        DaemonHooks::default(),
    );
    let reply = client.join().expect("重啟 daemon stop RPC client");
    assert_eq!(code, 0, "WAL／lock 已釋放後 daemon 應可重啟");
    assert!(
        pyjson::loads(&reply).unwrap_or(PyValue::Null)["ok"] == true,
        "重啟 daemon 應可經 RPC 正常停止：{reply}"
    );
    assert!(
        !endpoint_is_serving(&env.socket),
        "第二次停止後 endpoint 也應關閉"
    );
}

struct WindowsCtrlChild(std::process::Child);

struct WindowsCtrlReadyFile(PathBuf);

impl Drop for WindowsCtrlReadyFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

impl Drop for WindowsCtrlChild {
    fn drop(&mut self) {
        if matches!(self.0.try_wait(), Ok(None)) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}

fn windows_ctrl_parent() {
    use std::process::{Command, Stdio};
    use std::time::Instant;
    use sw_pal::test_support::{
        has_interactive_console, send_console_control_event, spawn_console_process_group,
        ConsoleControlEvent,
    };

    assert!(
        has_interactive_console(),
        "測試必須從 Windows Terminal 互動 console 執行"
    );

    let run_event = |event: ConsoleControlEvent, name: &str| {
        let ready_file = std::env::temp_dir().join(format!(
            "sw21-console-ready-{}-{name}.txt",
            std::process::id()
        ));
        let _ = fs::remove_file(&ready_file);
        let _ready_guard = WindowsCtrlReadyFile(ready_file.clone());
        let mut command =
            Command::new(std::env::current_exe().expect("目前 daemon_process 測試執行檔"));
        command
            .args([
                "--ignored",
                "--exact",
                "cap_interactive_desktop__windows_console_ctrl_triggers_orderly_stop",
                "--nocapture",
                "--test-threads=1",
            ])
            .env("SW_S21_WINDOWS_CTRL_CHILD", "1")
            .env(
                "SW_S21_WINDOWS_CTRL_READY_FILE",
                ready_file.display().to_string(),
            )
            .stdin(Stdio::null());
        let mut child = WindowsCtrlChild(
            spawn_console_process_group(command).expect("啟動 Windows console-event daemon 子行程"),
        );

        let deadline = Instant::now() + LONG;
        let endpoint = loop {
            if let Ok(value) = fs::read_to_string(&ready_file) {
                let endpoint = value.trim();
                if !endpoint.is_empty() {
                    break PathBuf::from(endpoint);
                }
            }
            if let Some(status) = child.0.try_wait().expect("檢查 daemon 子行程 ready 狀態")
            {
                panic!("{name} daemon 子行程在回報 endpoint 前退出：{status}");
            }
            assert!(
                Instant::now() < deadline,
                "{name} daemon 子行程未回報已就緒 endpoint"
            );
            std::thread::sleep(Duration::from_millis(50));
        };
        wait_socket(&endpoint, LONG);
        let group_id = child.0.id();
        send_console_control_event(group_id, event)
            .unwrap_or_else(|error| panic!("送 {name} 到 daemon 專屬 process group 失敗：{error}"));

        let deadline = Instant::now() + LONG;
        let status = loop {
            if let Some(status) = child.0.try_wait().expect("等待 daemon console stop") {
                break status;
            }
            assert!(
                Instant::now() < deadline,
                "daemon 收到 {name} 後未完成有序停機"
            );
            std::thread::sleep(Duration::from_millis(50));
        };
        assert_eq!(status.code(), Some(0), "{name} 子行程 exit status");
    };

    run_event(ConsoleControlEvent::CtrlC, "CTRL_C_EVENT");
    run_event(ConsoleControlEvent::CtrlBreak, "CTRL_BREAK_EVENT");
}
