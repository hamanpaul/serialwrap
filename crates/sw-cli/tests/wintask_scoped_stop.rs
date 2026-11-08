//! P8 Task 13：windows-task 實跑測試的正式停止路徑 harness（`sw_cli::wintask_harness`）memory_only 測試。
//!
//! 審查 P8X-e66ed8d-2：實跑測試（sw-pal `wintask_runtime.rs`）原本以自行重寫的停止序停止 daemon，正式
//! `graceful_stop` 回歸不會讓實跑案例失敗。改為以 harness 行程執行正式 `serialwrap service stop`，只在平台邊界
//! 把監管命令的工作名稱限定為 `sw-p8-test-<pid>`、RPC 限定為測試埠。本檔以替身證明：
//! - harness 執行的是正式路徑（`/DISABLE` → `daemon.stop` → 等待 → `/ENABLE`，正常結束時不 `/End`；RPC 不可達且工作
//!   Running 才 `/End`），而且所有監管命令都只指向測試工作；
//! - 繼承環境中的正式 config（on-demand）與正式埠（48700）完全看不到，48700 從未被連線；
//! - 不在允許範圍內的命令、工作名稱、endpoint 與要求一律拒絕，且不會送到底層。
//!
//! 外部 oracle：`FakeSysEnv` 實際收到的 argv（`TestEnv::calls`）與 `FakeRpcConnector` 實際收到的請求（`requests`）。
mod support;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use support::*;
use sw_cli::wintask_harness::*;
use sw_pal::traits::{EndpointSpec, RpcConnector, SockTimeout, SysEnv};
use sw_proto::pyjson::PyValue;

const TASK: &str = "sw-p8-test-4242";
const PORT: u16 = 50123;
const PID: u32 = 900;
const PROD_EP: &str = "tcp://127.0.0.1:48700";

/// sw-pal `tests/wintask_staging.rs` 的 `REPORT_JSON`（消費端以同一字串驗證解析）。
const REPORT_JSON: &str = r#"{"ran":[{"argv":["schtasks","/Change","/TN","sw-p8-test-4242","/DISABLE"],"rc":0},{"argv":["schtasks","/Change","/TN","sw-p8-test-4242","/ENABLE"],"rc":0}],"rc":0,"refused":[],"rpc":["health.status","daemon.stop","health.ping"],"stderr":"","stdout":"{\"action\":\"stop\",\"graceful\":true,\"mode\":\"windows-task\",\"ok\":true}\n"}"#;

fn ep() -> String {
    format!("tcp://127.0.0.1:{PORT}")
}

fn sv(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}

fn change(task: &str, switch: &str) -> Vec<String> {
    sv(&["schtasks", "/Change", "/TN", task, switch])
}

fn end(task: &str) -> Vec<String> {
    sv(&["schtasks", "/End", "/TN", task])
}

/// PAL `WindowsTaskCtl` 的工作狀態查詢 argv（`Get-ScheduledTask` 的 State）。
fn query(task: &str) -> Vec<String> {
    sv(&[
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        &format!("try {{ (Get-ScheduledTask -TaskName '{task}' -ErrorAction Stop).State.ToString() }} catch {{ if ($_.CategoryInfo.Category -eq 'ObjectNotFound') {{ 'NotFound' }} else {{ exit 3 }} }}"),
    ])
}

fn refs(argv: &[String]) -> Vec<&str> {
    argv.iter().map(String::as_str).collect()
}

fn dict(v: &PyValue) -> &sw_proto::pyjson::PyDict {
    match v {
        PyValue::Dict(d) => d,
        other => panic!("JSON 不是物件：{other:?}"),
    }
}

fn fbool(v: &PyValue, key: &str) -> bool {
    match dict(v).get(key) {
        Some(PyValue::Bool(b)) => *b,
        other => panic!("JSON 欄位 {key} 不是布林：{other:?}"),
    }
}

fn fstr(v: &PyValue, key: &str) -> String {
    match dict(v).get(key) {
        Some(PyValue::Str(s)) => s.clone(),
        other => panic!("JSON 欄位 {key} 不是字串：{other:?}"),
    }
}

/// 繼承環境模擬使用者的正式設定：`CONFIG_DIR/config.yaml` 為 on-demand、`SERIALWRAP_TCP_PORT` 為正式埠。
/// harness 若看得到其中任一項，`service stop` 會走 NO_SYSTEMD 或連到 48700，以下斷言即失敗。
fn base() -> TestEnv {
    TestEnv::windows()
        .with_config("supervision_mode: on-demand\n")
        .with_var("SERIALWRAP_TCP_PORT", "48700")
}

/// staging 變數：`SERIALWRAP_CONFIG` 指向 windows-task 的 CLI config，目錄沿用 TestEnv 的隔離暫存目錄。
fn request(t: &TestEnv) -> ScopedStopRequest {
    let state = t.state_dir();
    std::fs::create_dir_all(&state).expect("state dir");
    std::fs::create_dir_all(t.run_dir()).expect("run dir");
    let cfg = state.join("cli-config.yaml");
    std::fs::write(&cfg, "supervision_mode: windows-task\n").expect("cli config");
    let path = |p: std::path::PathBuf| p.display().to_string();
    ScopedStopRequest {
        task: TASK.into(),
        port: PORT,
        vars: vec![
            ("SERIALWRAP_CONFIG".into(), path(cfg)),
            ("SERIALWRAP_CONFIG_DIR".into(), path(t.config_dir())),
            ("SERIALWRAP_STATE_DIR".into(), path(t.state_dir())),
            ("SERIALWRAP_RUN_DIR".into(), path(t.run_dir())),
            ("SERIALWRAP_LOG_DIR".into(), path(t.log_dir())),
        ],
    }
}

/// 測試 daemon 收到 `daemon.stop` 後再存活 `delay`（假時鐘）才結束：結束時關閉監聽並使行程不存活。
fn exits_after_stop_rpc(t: &TestEnv, delay: Duration) {
    let ep = ep();
    t.rpc().respond(&ep, "daemon.stop", r#"{"ok":true,"stopping":true}"#);
    let rpc = Arc::clone(t.rpc());
    let clock = Arc::clone(t.clock());
    let asked_at: Mutex<Option<Duration>> = Mutex::new(None);
    t.procs().alive_while(
        PID,
        Arc::new(move || {
            if rpc.request_count(&ep, "daemon.stop") == 0 {
                return true;
            }
            let now = clock.elapsed();
            let at = *asked_at
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .get_or_insert(now);
            if now >= at + delay {
                if !rpc.is_stopped(&ep) {
                    rpc.stop_endpoint(&ep);
                }
                return false;
            }
            true
        }),
    );
}

#[test]
fn formal_stop_normal_exit_targets_only_test_task_and_never_ends() {
    let t = base()
        .with_daemon_alive_forever(&ep(), PID)
        .with_gone_ping_event();
    exits_after_stop_rpc(&t, Duration::from_secs(1));
    let report = scoped_service_stop(t.platform(), &request(&t)).expect("要求合法");
    assert_eq!(report.rc, 0, "{report:?}");
    let j = json(&report.stdout);
    assert!(fbool(&j, "ok"), "{}", report.stdout);
    assert!(fbool(&j, "graceful"), "{}", report.stdout);
    assert_eq!(fstr(&j, "mode"), "windows-task", "必須讀 staging 的 CLI config，不得讀到繼承的 on-demand");
    assert_eq!(fstr(&j, "action"), "stop");
    // 監管命令即正式 PAL 的 argv，只有工作名稱改為測試工作；daemon 正常結束時不 /End、不查詢工作狀態。
    let want = vec![change(TASK, "/DISABLE"), change(TASK, "/ENABLE")];
    assert_eq!(t.calls(), want, "底層實際收到的子行程");
    assert_eq!(
        report.log.ran,
        want.into_iter().map(|argv| (argv, 0)).collect::<Vec<_>>()
    );
    assert!(report.log.refused.is_empty(), "{:?}", report.log.refused);
    // RPC：daemon.stop 只送到測試埠且恰一次；正式埠從未被連線。
    assert!(report.log.rpc.iter().any(|m| m == "daemon.stop"), "{:?}", report.log.rpc);
    assert_eq!(t.rpc().request_count(&ep(), "daemon.stop"), 1);
    let requests = t.rpc().requests();
    assert!(!requests.is_empty());
    assert!(
        requests.iter().all(|(endpoint, _, _)| *endpoint == ep()),
        "所有 RPC 只送到測試埠：{requests:?}"
    );
    assert_eq!(t.rpc().request_count(PROD_EP, "daemon.stop"), 0);
}

#[test]
fn formal_stop_unreachable_running_task_ends_only_test_task() {
    let t = base()
        .with_rpc_refused(&ep())
        .with_command(&refs(&query(TASK)), 0, "Running\r\n", "")
        .with_command(&refs(&end(TASK)), 0, "", "");
    let report = scoped_service_stop(t.platform(), &request(&t)).expect("要求合法");
    assert_eq!(report.rc, 0, "{report:?}");
    let j = json(&report.stdout);
    assert!(fbool(&j, "ok"), "{}", report.stdout);
    assert!(!fbool(&j, "graceful"), "執行過 /End 即非 graceful：{}", report.stdout);
    // 正式路徑的固定名稱查詢與 /End 也只指向測試工作。
    assert_eq!(
        t.calls(),
        vec![change(TASK, "/DISABLE"), query(TASK), end(TASK), change(TASK, "/ENABLE")]
    );
    assert!(!report.log.rpc.iter().any(|m| m == "daemon.stop"), "RPC 不可達時沒有送出：{:?}", report.log.rpc);
    assert!(report.log.refused.is_empty(), "{:?}", report.log.refused);
    assert_eq!(t.rpc().request_count(PROD_EP, "daemon.stop"), 0);
}

#[test]
fn scoped_env_rewrites_production_name_and_refuses_everything_else() {
    let t = base();
    let log = Arc::new(Mutex::new(HarnessLog::default()));
    let vars = vec![("SERIALWRAP_CONFIG".to_string(), r"C:\stage\cli\config.yaml".to_string())];
    let env = ScopedEnv::new(t.sysenv(), TASK, vars, Arc::clone(&log)).expect("合法");
    // 正式 PAL 的固定名稱 argv → 測試工作（比對不分大小寫）；已是測試工作者原樣保留。
    assert_eq!(env.scope_argv(&change("serialwrap", "/DISABLE")), Ok(change(TASK, "/DISABLE")));
    assert_eq!(
        env.scope_argv(&sv(&["schtasks", "/end", "/tn", "SerialWrap"])),
        Ok(sv(&["schtasks", "/end", "/tn", TASK]))
    );
    assert_eq!(env.scope_argv(&query("serialwrap")), Ok(query(TASK)));
    assert_eq!(env.scope_argv(&change(TASK, "/ENABLE")), Ok(change(TASK, "/ENABLE")));
    assert_eq!(env.scope_argv(&sv(&["schtasks", "/Run", "/TN", "serialwrap"])), Ok(sv(&["schtasks", "/Run", "/TN", TASK])));
    let bad: Vec<Vec<String>> = vec![
        end("other-task"),
        end("sw-p8-test-1"),
        sv(&["schtasks", "/Query"]),
        sv(&["schtasks", "/Change", "/TN", "serialwrap", "/TN", TASK, "/DISABLE"]),
        sv(&["schtasks", "/Delete", "/TN", "serialwrap", "/F"]),
        sv(&["schtasks", "/Create", "/XML", r"C:\x.xml", "/TN", TASK, "/F"]),
        sv(&["sc.exe", "stop", "serialwrap"]),
        sv(&["taskkill", "/F", "/IM", "serialwrapd.exe"]),
        sv(&["cmd.exe", "/c", "schtasks /End /TN serialwrap"]),
        sv(&["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Stop-ScheduledTask -TaskName 'serialwrap'"]),
        query("other-task"),
    ];
    for argv in &bad {
        assert!(env.scope_argv(argv).is_err(), "必須拒絕：{argv:?}");
        assert!(env.run(argv, None).is_err(), "run 必須拒絕：{argv:?}");
        assert!(env.run_merged(argv, None).is_err(), "run_merged 必須拒絕：{argv:?}");
        assert!(env.run_raw(argv, None, None).is_err(), "run_raw 必須拒絕：{argv:?}");
    }
    assert!(t.calls().is_empty(), "被拒絕的命令不得送到底層：{:?}", t.calls());
    let log = log.lock().unwrap().clone();
    assert_eq!(log.refused.len(), bad.len() * 3, "{:?}", log.refused);
    assert!(log.ran.is_empty());
    // 允許的命令改寫後才送到底層，並記錄 rc。
    let ok = env.run(&change("serialwrap", "/ENABLE"), None).expect("允許");
    assert_eq!(ok.rc, 0);
    assert_eq!(t.calls(), vec![change(TASK, "/ENABLE")]);
    // 環境：繼承的 SERIALWRAP_* 一律隱藏，只看得到指定變數（Windows 不分大小寫）；其他變數照常。
    assert_eq!(env.var("SERIALWRAP_TCP_PORT"), None, "繼承的正式埠必須隱藏");
    assert_eq!(env.var("SERIALWRAP_CONFIG_DIR"), None, "繼承的正式 config 目錄必須隱藏");
    assert_eq!(env.var("serialwrap_config").as_deref(), Some(r"C:\stage\cli\config.yaml"));
    let serialwrap_vars: Vec<(String, String)> = env
        .vars()
        .into_iter()
        .filter(|(k, _)| k.to_ascii_uppercase().starts_with("SERIALWRAP_"))
        .collect();
    assert_eq!(serialwrap_vars, vec![("SERIALWRAP_CONFIG".to_string(), r"C:\stage\cli\config.yaml".to_string())]);
    assert_eq!(env.path_flavor(), t.sysenv().path_flavor(), "路徑方言沿用底層");
    assert!(ScopedEnv::new(t.sysenv(), "serialwrap", Vec::new(), Arc::new(Mutex::new(HarnessLog::default()))).is_err());
}

#[test]
fn scoped_rpc_only_connects_to_test_port() {
    let t = base().with_daemon_alive_forever(&ep(), PID);
    let log = Arc::new(Mutex::new(HarnessLog::default()));
    let rpc = ScopedRpc::new(Arc::clone(t.rpc()) as Arc<dyn RpcConnector>, PORT, Arc::clone(&log));
    let timeout = SockTimeout::Blocking(Duration::from_secs(1));
    let bad = [
        EndpointSpec::Tcp { host: "127.0.0.1".into(), port: 48700 },
        EndpointSpec::Tcp { host: "127.0.0.1".into(), port: PORT + 1 },
        EndpointSpec::Tcp { host: "localhost".into(), port: PORT },
        EndpointSpec::Tcp { host: "10.0.0.1".into(), port: PORT },
        EndpointSpec::Unix("/tmp/serialwrapd.sock".into()),
    ];
    for spec in &bad {
        assert!(rpc.connect(spec, timeout).is_err(), "必須拒絕：{spec:?}");
    }
    assert!(rpc.resolve_tcp("127.0.0.1", 48700).is_err());
    assert!(rpc.resolve_tcp("127.0.0.1", PORT).is_ok());
    assert!(t.rpc().requests().is_empty(), "被拒絕的 endpoint 不得送到底層");
    let mut stream = rpc
        .connect(&EndpointSpec::Tcp { host: "127.0.0.1".into(), port: PORT }, timeout)
        .expect("測試埠可連");
    stream
        .send_all(b"{\"id\":1,\"method\":\"health.ping\",\"params\":{}}\n")
        .expect("送出");
    let mut buf = [0u8; 256];
    assert!(stream.recv(&mut buf).expect("回應") > 0);
    let log = log.lock().unwrap().clone();
    assert_eq!(log.rpc, vec!["health.ping".to_string()]);
    assert_eq!(log.refused.len(), bad.len() + 1, "{:?}", log.refused);
    assert_eq!(t.rpc().request_count(&ep(), "health.ping"), 1);
}

#[test]
fn harness_rejects_unsafe_requests_without_running_anything() {
    let t = base().with_daemon_alive_forever(&ep(), PID);
    let good = request(&t);
    let with_task = |task: &str| ScopedStopRequest { task: task.into(), ..good.clone() };
    let with_port = |port: u16| ScopedStopRequest { port, ..good.clone() };
    let without = |key: &str| ScopedStopRequest {
        vars: good.vars.iter().filter(|(k, _)| k != key).cloned().collect(),
        ..good.clone()
    };
    let plus = |k: &str, v: &str| {
        let mut vars = good.vars.clone();
        vars.push((k.into(), v.into()));
        ScopedStopRequest { vars, ..good.clone() }
    };
    let cases: Vec<(&str, ScopedStopRequest)> = vec![
        ("正式工作", with_task("serialwrap")),
        ("正式工作（大小寫）", with_task("SerialWrap")),
        ("pid 非數字", with_task("sw-p8-test-abc")),
        ("缺 pid", with_task("sw-p8-test-")),
        ("前綴不符", with_task("sw-p8-tests-1")),
        ("正式埠", with_port(48700)),
        ("埠 0", with_port(0)),
        ("缺 SERIALWRAP_CONFIG", without("SERIALWRAP_CONFIG")),
        ("缺 SERIALWRAP_STATE_DIR", without("SERIALWRAP_STATE_DIR")),
        ("缺 SERIALWRAP_RUN_DIR", without("SERIALWRAP_RUN_DIR")),
        ("缺 SERIALWRAP_CONFIG_DIR", without("SERIALWRAP_CONFIG_DIR")),
        ("指定保留的 TCP_PORT", plus("SERIALWRAP_TCP_PORT", "50123")),
        ("指定保留的 SOCKET", plus("SERIALWRAP_SOCKET", "tcp://127.0.0.1:48700")),
        ("非 SERIALWRAP_ 變數", plus("PATH", r"C:\evil")),
        ("重複變數", plus("SERIALWRAP_CONFIG", r"C:\other.yaml")),
    ];
    for (why, req) in cases {
        assert!(scoped_service_stop(t.platform(), &req).is_err(), "{why} 必須拒絕：{req:?}");
    }
    assert!(t.calls().is_empty(), "拒絕的要求不得執行任何命令：{:?}", t.calls());
    assert!(t.rpc().requests().is_empty(), "拒絕的要求不得送出 RPC：{:?}", t.rpc().requests());
    for ok in ["sw-p8-test-1", "sw-p8-test-4242"] {
        assert_eq!(check_task_name(ok), Ok(()));
    }
}

#[test]
fn harness_args_and_report_json_contract() {
    let args = sv(&[
        "--task",
        TASK,
        "--port",
        "50123",
        "--var",
        r"SERIALWRAP_CONFIG=C:\stage\cli\config.yaml",
        "--var",
        r"SERIALWRAP_RUN_DIR=C:\stage\run=x",
    ]);
    assert_eq!(
        parse_harness_args(&args),
        Ok(ScopedStopRequest {
            task: TASK.into(),
            port: PORT,
            vars: vec![
                ("SERIALWRAP_CONFIG".into(), r"C:\stage\cli\config.yaml".into()),
                ("SERIALWRAP_RUN_DIR".into(), r"C:\stage\run=x".into()),
            ],
        })
    );
    let bad: Vec<Vec<String>> = vec![
        sv(&[]),
        sv(&["--task", TASK]),
        sv(&["--port", "50123"]),
        sv(&["--task", TASK, "--port", "x"]),
        sv(&["--task", TASK, "--port", "70000"]),
        sv(&["--task", TASK, "--port", "50123", "--bogus"]),
        sv(&["--task", TASK, "--port", "50123", "--var"]),
        sv(&["--task", TASK, "--port", "50123", "--var", "NOEQ"]),
        sv(&["--task", TASK, "--task", TASK, "--port", "50123"]),
        sv(&["--task", TASK, "--port", "50123", "--port", "50124"]),
    ];
    for argv in bad {
        assert!(parse_harness_args(&argv).is_err(), "必須拒絕：{argv:?}");
    }
    let report = ScopedStopReport {
        rc: 0,
        stdout: "{\"action\":\"stop\",\"graceful\":true,\"mode\":\"windows-task\",\"ok\":true}\n".into(),
        stderr: String::new(),
        log: HarnessLog {
            ran: vec![(change(TASK, "/DISABLE"), 0), (change(TASK, "/ENABLE"), 0)],
            rpc: sv(&["health.status", "daemon.stop", "health.ping"]),
            refused: Vec::new(),
        },
    };
    assert_eq!(report.to_json(), REPORT_JSON, "與 sw-pal 消費端共用的一行 JSON");
    assert!(!report.to_json().contains('\n'), "harness 輸出必須是一行");
}
