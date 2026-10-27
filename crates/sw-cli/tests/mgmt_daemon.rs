//! P4-T7 `daemon start／stop／status／restart`、多開預檢、SC-18 首次需求啟動與 SC-20 重啟的失敗測試（SC-10、SC-17、SC-18、SC-20、DF-05）。
//!
//! 紅燈階段（TDD）的實際失敗來源（已於本 Task 的紅燈執行結果記錄）：
//! - `daemon` 群組目前仍在 `delegate::DELEGATED_GROUPS`（Task 7 實作步驟才移除，且 `delegate_route`、`cli_spec_schema`
//!   既有測試以此為前提）。因此經 `t.run` 的案例會走委派路徑，在測試替身中回 rc 1（`delegation target unavailable`）或
//!   sentinel 的 rc 97，而非進入 `daemon_cmd::run_daemon_*` 的 `todo!`。這是「原生 daemon 行為缺席」的紅燈。
//! - 直接呼叫的 API 案例（`wait_daemon_gone`）與 `graceful_stop` 相關案例以 `not implemented: P4-T7` panic 失敗。
//! - 需要 P4-T7／T12 替身模型（autostart.lock 腳本、重啟循環）者，於 `tests/support/mod.rs` 以 `todo!` 明示（見各案例註解）。
//!
//! 替身與 `TestEnv` 由 P4-T1（`tests/support/mod.rs`）提供；本檔只使用 plan 記載的 API。
//! 規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 7；盤點 `inventory/07-platform-mgmt.md` §5、§6。
mod support;

use std::path::PathBuf;
use std::time::Duration;

use support::*;
use sw_cli::graceful_stop::{wait_daemon_gone, DaemonIdentity};
use sw_pal::proc_scan::{is_serialwrapd_argv, extract_socket_arg, ArgvFlavor, DaemonProc, ProcIdentity};
use sw_pal::PalErrorKind;
use sw_proto::pyjson::{PyInt, PyValue};

/// `health.ping` 與 `health.status` 的成功回應（pid 與版本欄位即可）。
const PING_OK: &str = r#"{"ok":true,"pid":4321,"version":"0.4.0"}"#;
/// `session list` 的成功回應（與 P3 client 形狀一致）。
const SESSION_LIST_OK: &str = r#"{"id":1,"ok":true,"sessions":[],"version":"0.4.0"}"#;
/// `env -0` 的 bash 呼叫（與 P4-T7 `load_daemon_env` 的 POSIX 路徑逐字一致）。
const ENV_SOURCE_SCRIPT: &str = "set -a && source \"$1\" >/dev/null && env -0";
/// 只含 daemon launcher 的 release manifest（`components.daemon.launcher.path` 相對 manifest 目錄）。
const LAUNCHER_MANIFEST: &str = r#"{"schema_version":1,"bundle_version":"test","stage":"rust-1b-test","target":"x86_64-unknown-linux-gnu","git_revision":"test","components":{"cli":{"implementation":"rust","version":"test","path":"bin/serialwrap","sha256":"0000000000000000000000000000000000000000000000000000000000000000"},"python_cli_delegate":{"entry":["py/bin/python3","-I","delegate_sentinel.py"]},"daemon":{"launcher":{"path":"bin/serialwrapd"}}}}"#;

fn argv(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}

/// `PyValue::Dict` 的欄位（測試輔助；缺欄位即 panic）。
fn field<'a>(v: &'a PyValue, key: &str) -> &'a PyValue {
    match v {
        PyValue::Dict(d) => d
            .get(key)
            .unwrap_or_else(|| panic!("JSON 欄位 {key} 不存在")),
        _ => panic!("JSON 不是物件"),
    }
}

fn has_key(v: &PyValue, key: &str) -> bool {
    match v {
        PyValue::Dict(d) => d.get(key).is_some(),
        _ => false,
    }
}

fn object_len(v: &PyValue) -> usize {
    match v {
        PyValue::Dict(d) => d.0.len(),
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

fn fint(v: &PyValue, key: &str) -> i64 {
    match field(v, key) {
        PyValue::Int(PyInt::Small(n)) => i64::try_from(*n).expect("整數超出 i64"),
        _ => panic!("JSON 欄位 {key} 不是整數"),
    }
}

fn fstr_list(v: &PyValue, key: &str) -> Vec<String> {
    match field(v, key) {
        PyValue::List(items) => items
            .iter()
            .map(|item| match item {
                PyValue::Str(s) => s.clone(),
                _ => panic!("JSON 陣列 {key} 含非字串"),
            })
            .collect(),
        _ => panic!("JSON 欄位 {key} 不是陣列"),
    }
}

/// 第一次 spawn 的 argv（無 spawn 即 panic）。
fn first_spawn_argv(t: &TestEnv) -> Vec<String> {
    t.process()
        .spawns()
        .first()
        .map(|s| s.0.clone())
        .expect("spawn 應已發生")
}

/// 第一次 spawn 的環境中某鍵的原始位元組。
fn first_spawn_env(t: &TestEnv, key: &str) -> Option<Vec<u8>> {
    let spawns = t.process().spawns();
    let (_, env, _) = spawns.first()?;
    env.iter()
        .find(|(k, _)| k.as_encoded_bytes() == key.as_bytes())
        .map(|(_, v)| v.as_encoded_bytes().to_vec())
}

fn is_bash_call(call: &[String]) -> bool {
    call.first().map(String::as_str) == Some("bash")
}

fn rpc_count(t: &TestEnv, method: &str) -> usize {
    t.rpc()
        .requests()
        .iter()
        .filter(|r| r.1 == method)
        .count()
}

fn daemon_proc(pid: u32, socket: Option<&str>) -> DaemonProc {
    DaemonProc {
        pid,
        socket: socket.map(str::to_string),
        tree: vec![ProcIdentity {
            pid,
            start_ticks: None,
        }],
    }
}

/// 同一個 `TestEnv` 上取得 `default_socket()` 後再交給建構鏈（`with_*` 會消耗 `self`）。
fn posix_with_socket() -> (TestEnv, String) {
    let t = TestEnv::posix();
    let sock = t.default_socket();
    (t, sock)
}

#[test]
fn start_refuses_other_daemon_on_different_socket_without_stderr_mirror() {
    let t = TestEnv::posix()
        .with_procs(vec![DaemonProc { pid: 4242, socket: Some("/run/other.sock".into()), tree: vec![] }])
        .with_process();
    let (rc, out, err) = t.run(&["--socket", "/tmp/x/serialwrapd.sock", "daemon", "start"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(fstr(&j, "error_code"), "DAEMON_ALREADY_RUNNING_ELSEWHERE");
    assert_eq!(fint(&j, "existing_pid"), 4242);
    assert_eq!(fstr(&j, "target_socket"), "/tmp/x/serialwrapd.sock");
    assert_eq!(
        fstr(&j, "message"),
        "偵測到另一個 serialwrapd（pid=4242）綁在 /run/other.sock，與本次 daemon start 目標 /tmp/x/serialwrapd.sock 不同；為避免同一批裝置被兩個 daemon 同時開啟（two-reader），預設拒絕再 spawn 一個。"
    );
    assert_eq!(
        fstr(&j, "hint"),
        "改用 --socket /run/other.sock 指向現有 daemon；確認後要強制另起才加 --force-spawn"
    );
    assert_eq!(err, "");
    assert!(t.process().spawns().is_empty());
}

#[test]
fn start_treats_normalized_same_socket_as_no_conflict() {
    // 目標 `unix://…/../x/…` 與既有實例 `/tmp/x/…` 經 SC-10 正規化後相同 → 無衝突，照常 spawn。
    let target = "unix:///tmp/x/../x/serialwrapd.sock";
    let t = TestEnv::posix()
        .with_procs(vec![daemon_proc(7, Some("/tmp/x/serialwrapd.sock"))])
        .with_process()
        .with_manifest_launcher()
        .with_rpc_after_spawn(target, &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["--socket", target, "daemon", "start"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(
        first_spawn_argv(&t)[0],
        t.bundle_dir().join("bin/serialwrapd").display().to_string()
    );
}

#[test]
fn start_conflict_normalization_expands_user_home() {
    let t0 = TestEnv::posix();
    let home = t0.home();
    // POSIX 旗標以 `/` 接上（Python `posixpath`）；Windows 主機模擬時 `Path::join` 會用 `\\`，與 `~/x.sock` 展開結果不同形。
    let target = format!("{}/x.sock", home.display());
    let t = t0
        .with_var("HOME", &home.display().to_string())
        .with_procs(vec![daemon_proc(4242, Some("~/x.sock"))])
        .with_process();
    let (rc, out, _) = t.run(&["--socket", &target, "daemon", "start"]);
    assert_eq!(rc, 2);
    assert_eq!(
        fstr(&json(&out), "error_code"),
        "DAEMON_BINARY_NOT_FOUND",
        "同一個展開後 endpoint 不應被誤判為另一個 daemon"
    );
}

#[test]
fn autostart_only_when_request_never_sent() {
    // 第一次 session.list 連線被拒（請求從未送出）→ 自動啟動後重送一次。
    // 註：計畫原文未含 `with_process`／`with_rpc_after_spawn`；自動啟動需要 spawn 與 spawn 後的 ping 才能成立。
    let t = TestEnv::windows()
        .with_rpc_refused_then_ok(TestEnv::win_endpoint(), "session.list", SESSION_LIST_OK)
        .with_manifest_launcher()
        .with_process()
        .with_rpc_after_spawn(TestEnv::win_endpoint(), &["health.ping", "health.status"]);
    let (rc, out, err) = t.run(&["session", "list"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(out, format!("{SESSION_LIST_OK}\r\n"));
    assert!(err.starts_with(
        "serialwrap: on-demand：預設 endpoint tcp://127.0.0.1:48700 沒有 daemon，已自動啟動（pid="
    ));
    // 連線被拒的第一次嘗試不計入 requests（請求未送出）；重送的那一次為唯一的 session.list 請求。
    assert_eq!(rpc_count(&t, "session.list"), 1);
    assert_eq!(t.process().spawn_count(), 1);
}

#[test]
fn no_autostart_or_resend_after_send_phase_failure() {
    // command.submit 已送出後才失敗（send 之後）→ 不自動啟動、不重送。
    // 註：計畫原文未含 `with_rpc_ok`；`fail_after_send` 需要 endpoint 已存活（替身規則）。
    let t = TestEnv::windows()
        .with_rpc_ok(TestEnv::win_endpoint(), "command.submit", r#"{"ok":true}"#)
        .with_rpc_fail_after_send(TestEnv::win_endpoint(), "command.submit")
        .with_manifest_launcher()
        .with_process();
    let (rc, _, _) = t.run(&["cmd", "submit", "--selector", "COM0", "--cmd", "reboot"]);
    assert_eq!(rc, 2);
    assert!(t.process().spawns().is_empty());
    assert_eq!(rpc_count(&t, "command.submit"), 1);
}

#[test]
fn restart_on_demand_output_matches_p1_shape() {
    // 舊 daemon（pid 4321）停止後由新實例（pid 9000）接手。
    let (t0, sock) = posix_with_socket();
    let t = t0
        .with_process()
        .with_manifest_launcher()
        .with_restart_cycle(&sock, 4321, 9000);
    let (rc, out, _) = t.run(&["daemon", "restart"]);
    assert_eq!(rc, 0, "{out}");
    let j = json(&out);
    assert!(fbool(&j, "restarted") && fbool(&j, "stopped") && fbool(&j, "ok"));
    assert!(fbool(field(&j, "start"), "ok"));
    assert!(!has_key(&j, "stopped_pid"));
}

#[test]
fn start_endpoint_rejected_posix() {
    let t = TestEnv::posix();
    let (rc, out, err) = t.run(&["--endpoint", "tcp://1.2.3.4:1", "daemon", "start"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(fstr(&j, "error_code"), "REMOTE_NOT_SUPPORTED");
    assert_eq!(fstr(&j, "message"), "--endpoint 不支援 daemon start（daemon 只能在本機啟動）");
    assert!(err.contains("daemon start"), "stderr 須帶 context daemon start：{err}");
}

#[test]
fn start_endpoint_loopback_allowed_windows() {
    let t = TestEnv::windows()
        .with_manifest_launcher()
        .with_process()
        .with_rpc_after_spawn("tcp://127.0.0.1:48701", &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["--endpoint", "tcp://127.0.0.1:48701", "daemon", "start"]);
    assert_eq!(rc, 0, "{out}");
    let cmd = first_spawn_argv(&t);
    let pos = cmd.iter().position(|a| a == "--socket").expect("cmd 須含 --socket");
    assert_eq!(cmd[pos + 1], "tcp://127.0.0.1:48701");
}

#[test]
fn start_endpoint_socket_mismatch_invalid_args() {
    // Windows loopback endpoint 可達到 mismatch 檢查；POSIX 會先拒絕 endpoint。
    let t = TestEnv::windows();
    let (rc, out, _) = t.run(&["--endpoint", "tcp://127.0.0.1:48701", "--socket", "tcp://127.0.0.1:48702", "daemon", "start"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(fstr(&j, "error_code"), "INVALID_ARGS");
    assert_eq!(
        fstr(&j, "message"),
        "--endpoint 與 --socket 不一致；daemon start 請擇一指定 bind 位址"
    );
}

#[test]
fn start_posix_endpoint_rejection_precedes_socket_mismatch() {
    let t = TestEnv::posix();
    let (rc, out, _) = t.run(&["--endpoint", "unix:///tmp/a.sock", "--socket", "/tmp/b.sock", "daemon", "start"]);
    assert_eq!(rc, 2);
    assert_eq!(fstr(&json(&out), "error_code"), "REMOTE_NOT_SUPPORTED");
}

#[test]
fn start_routes_systemd_user_to_service() {
    let t = TestEnv::posix()
        .with_config("supervision_mode: systemd-user\n")
        .with_systemd(true)
        .with_real_service();
    let (_, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(fstr(&json(&out), "_routed_to"), "service start");
    assert_eq!(t.calls(), vec![argv(&["systemctl", "--user", "start", "serialwrap"])]);
}

#[test]
fn start_routes_windows_task_to_schtasks() {
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_real_service();
    let (_, _, _) = t.run(&["daemon", "start"]);
    assert_eq!(t.calls(), vec![argv(&["schtasks", "/Run", "/TN", "serialwrap"])]);
}

#[test]
fn start_unknown_mode_routes_no_systemd() {
    let t = TestEnv::posix()
        .with_config("supervision_mode: foo\n")
        .with_real_service();
    let (_, out, _) = t.run(&["daemon", "start"]);
    let j = json(&out);
    assert_eq!(fstr(&j, "error_code"), "NO_SYSTEMD");
    assert_eq!(fstr(&j, "_routed_to"), "service start");
}

#[test]
fn start_preserves_non_string_supervision_mode_json_type() {
    let t = TestEnv::posix()
        .with_config("supervision_mode: [a, b]\n")
        .with_real_service();
    let (_, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(
        field(&json(&out), "mode"),
        &PyValue::List(vec![PyValue::Str("a".into()), PyValue::Str("b".into())])
    );
}

#[test]
fn start_already_running() {
    let (t0, sock) = posix_with_socket();
    let t = t0.with_process().with_rpc_ok(&sock, "health.ping", PING_OK);
    let (rc, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 0, "{out}");
    let j = json(&out);
    assert!(fbool(&j, "already_running"));
    assert!(fbool(&j, "ok"));
    assert_eq!(fstr(&j, "socket"), sock);
    assert_eq!(t.process().spawn_count(), 0);
    assert_eq!(t.rpc().timeouts_s(), vec![0.5]);
}

#[test]
fn start_conflict_unknown_socket_hint() {
    let t = TestEnv::posix()
        .with_procs(vec![daemon_proc(4242, None)])
        .with_process();
    let (rc, out, _) = t.run(&["--socket", "/tmp/x/serialwrapd.sock", "daemon", "start"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(fstr(&j, "error_code"), "DAEMON_ALREADY_RUNNING_ELSEWHERE");
    assert!(matches!(field(&j, "existing_socket"), PyValue::Null));
    assert!(
        fstr(&j, "message").contains("未知（無法從 /proc 讀出 --socket，可能是舊版 daemon）"),
        "message 須顯示未知 socket"
    );
    assert!(
        fstr(&j, "hint").starts_with("先用 `serialwrap daemon status`"),
        "socket 未知時 hint 不得建議 --socket"
    );
}

#[test]
fn start_conflict_detects_module_form_daemon() {
    // Task 4 規則 4：`python -I -m sw_core.daemon --socket <p>` 形式辨識為 serialwrapd 實例，socket 由 argv 取出。
    let parts = argv(&["/usr/bin/python3", "-I", "-m", "sw_core.daemon", "--socket", "/run/o.sock"]);
    assert!(is_serialwrapd_argv(&parts, ArgvFlavor::Posix));
    assert_eq!(extract_socket_arg(&parts).as_deref(), Some("/run/o.sock"));

    // 同一形式的實例經 ProcScan 回傳後，多開預檢必須判為衝突。
    let t = TestEnv::posix()
        .with_procs(vec![daemon_proc(5151, Some("/run/o.sock"))])
        .with_process();
    let (rc, out, _) = t.run(&["--socket", "/tmp/x/serialwrapd.sock", "daemon", "start"]);
    assert_eq!(rc, 2);
    assert_eq!(fstr(&json(&out), "error_code"), "DAEMON_ALREADY_RUNNING_ELSEWHERE");
}

#[test]
fn start_force_spawn_skips_precheck() {
    let t = TestEnv::posix()
        .with_procs(vec![daemon_proc(4242, Some("/run/other.sock"))])
        .with_process()
        .with_manifest_launcher()
        .with_rpc_after_spawn("/tmp/x/serialwrapd.sock", &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["--socket", "/tmp/x/serialwrapd.sock", "daemon", "start", "--force-spawn"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(t.process().spawn_count(), 1);
}

#[test]
fn start_scan_failure_is_no_conflict() {
    let t = TestEnv::posix()
        .with_process()
        .with_manifest_launcher()
        .with_rpc_after_spawn("/tmp/x/serialwrapd.sock", &["health.ping", "health.status"]);
    t.procs().fail();
    let (rc, out, _) = t.run(&["--socket", "/tmp/x/serialwrapd.sock", "daemon", "start"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(t.process().spawn_count(), 1);
}

#[test]
fn start_locate_order_sibling_then_runtime() {
    // 步驟 2：無 launcher、同層 serialwrapd 存在 → 同層。
    let (t0, sock) = posix_with_socket();
    let sibling = t0.bundle_dir().join("bin").join("serialwrapd");
    t0.write_file(&sibling, b"sibling");
    let t = t0
        .with_process()
        .with_rpc_after_spawn(&sock, &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(first_spawn_argv(&t)[0], sibling.display().to_string());

    // 步驟 3：兩者皆無、manifest 的 Python runtime 存在 → [runtime, -I, -B, -m, sw_core.daemon]。
    let (t0, sock) = posix_with_socket();
    let t = t0
        .with_manifest_runtime_ok("3.12")
        .with_process()
        .with_rpc_after_spawn(&sock, &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 0, "{out}");
    let cmd = first_spawn_argv(&t);
    assert_eq!(cmd[0], t.bundle_dir().join("py/bin/python3").display().to_string());
    assert_eq!(&cmd[1..5], &argv(&["-I", "-B", "-m", "sw_core.daemon"])[..]);
}

#[test]
fn start_locate_honors_release_manifest_env() {
    // SERIALWRAP_RELEASE_MANIFEST 指向另一個 bundle → 採用該 bundle 的 launcher。
    let (t0, sock) = posix_with_socket();
    let other: PathBuf = t0.home().join("other-bundle");
    t0.write_file(&other.join("bin").join("serialwrapd"), b"other");
    t0.write_file(&other.join("release-manifest.json"), LAUNCHER_MANIFEST.as_bytes());
    let manifest = other.join("release-manifest.json").display().to_string();
    let t = t0
        .with_var("SERIALWRAP_RELEASE_MANIFEST", &manifest)
        .with_process()
        .with_rpc_after_spawn(&sock, &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(first_spawn_argv(&t)[0], other.join("bin/serialwrapd").display().to_string());
}

#[test]
fn start_manifest_relative_path_returns_absolute_launcher() {
    fn relative_path(from: &std::path::Path, to: &std::path::Path) -> PathBuf {
        let from = from.components().collect::<Vec<_>>();
        let to = to.components().collect::<Vec<_>>();
        let common = from.iter().zip(&to).take_while(|(a, b)| a == b).count();
        let mut relative = PathBuf::new();
        for _ in common..from.len() {
            relative.push("..");
        }
        for component in to.iter().skip(common) {
            relative.push(component.as_os_str());
        }
        relative
    }

    let t = TestEnv::posix().with_manifest_launcher();
    let manifest = t.bundle_dir().join("release-manifest.json");
    let cwd = std::env::current_dir().expect("current directory");
    let relative = relative_path(&cwd, &manifest);
    assert!(!relative.is_absolute());
    let t = t.with_var("SERIALWRAP_RELEASE_MANIFEST", &relative.display().to_string());
    let argv = t.with_cli_ctx(|ctx| {
        sw_cli::commands::daemon_cmd::locate_daemon_argv(ctx).expect("manifest launcher")
    });
    assert!(PathBuf::from(&argv[0]).is_absolute(), "launcher path 必須正規化為絕對路徑：{argv:?}");
}

#[test]
fn start_windows_python_launcher_uses_manifest_runtime() {
    let t = TestEnv::windows().with_process();
    let launcher = t.bundle_dir().join("serialwrapd.py");
    let runtime = t.bundle_dir().join("py").join("python.exe");
    let manifest = t.bundle_dir().join("release-manifest.json");
    let body = r#"{"schema_version":1,"bundle_version":"test","stage":"rust-1b-test","target":"x86_64-pc-windows-gnu","git_revision":"test","components":{"cli":{"implementation":"rust","version":"test","path":"bin/serialwrap.exe","sha256":"0000000000000000000000000000000000000000000000000000000000000000"},"python_cli_delegate":{"entry":["py/python.exe","-I","delegate_sentinel.py"]},"daemon":{"launcher":{"path":"serialwrapd.py"},"runtime":{"path":"py/python.exe","python_version":"3.12"}}}}"#;
    t.write_file(&launcher, b"python daemon shim");
    t.write_file(&runtime, b"python runtime");
    t.write_file(&manifest, body.as_bytes());

    let argv = t.with_cli_ctx(|ctx| {
        sw_cli::commands::daemon_cmd::locate_daemon_argv(ctx).expect("Windows Python launcher")
    });
    let expected_path = |path: &PathBuf| path.to_string_lossy().replace('/', "\\");
    assert_eq!(
        argv,
        vec![
            expected_path(&runtime),
            "-I".to_string(),
            "-B".to_string(),
            expected_path(&launcher),
        ]
    );
}

#[test]
fn start_windows_python_launcher_without_runtime_does_not_fall_through() {
    // manifest 宣告 `.py` launcher 卻缺 runtime 時必須 fail-closed；
    // 不得改走 CLI 同層 serialwrapd.exe，否則會繞過 manifest 的版本配對。
    let t = TestEnv::windows().with_process();
    let launcher = t.bundle_dir().join("serialwrapd.py");
    let manifest = t.bundle_dir().join("release-manifest.json");
    let body = r#"{"schema_version":1,"bundle_version":"test","stage":"rust-1b-test","target":"x86_64-pc-windows-gnu","git_revision":"test","components":{"cli":{"implementation":"rust","version":"test","path":"bin/serialwrap.exe","sha256":"0000000000000000000000000000000000000000000000000000000000000000"},"python_cli_delegate":{"entry":["py/python.exe","-I","delegate_sentinel.py"]},"daemon":{"launcher":{"path":"serialwrapd.py"},"runtime":{"path":"py/python.exe","python_version":"3.12"}}}}"#;
    t.write_file(&launcher, b"python daemon shim");
    t.write_file(&manifest, body.as_bytes());
    let sibling = t.with_cli_ctx(|ctx| {
        let exe = ctx.p.env.current_exe().expect("current exe");
        exe.parent().expect("exe directory").join("serialwrapd.exe")
    });
    t.write_file(&sibling, b"sibling daemon");

    let expected_path = |path: &PathBuf| path.to_string_lossy().replace('/', "\\");
    let located = t.with_cli_ctx(|ctx| sw_cli::commands::daemon_cmd::locate_daemon_argv(ctx));
    match located {
        Err(sw_cli::commands::daemon_cmd::DaemonLocateError::ManifestRuntimeMissing {
            launcher: ref found_launcher,
            runtime: ref found_runtime,
        }) => {
            assert_eq!(found_launcher, &expected_path(&launcher));
            assert_eq!(
                found_runtime.as_deref(),
                Some(expected_path(&t.bundle_dir().join("py").join("python.exe")).as_str())
            );
        }
        other => {
            panic!("缺 manifest runtime 時不得落到同層或其他候選，且須指出缺 runtime：{other:?}")
        }
    }

    // 使用者訊息須指出缺的是 manifest 宣告的 runtime，而非泛稱找不到 daemon。
    let (rc, out, _err) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(fstr(&j, "error_code"), "DAEMON_BINARY_NOT_FOUND");
    let message = fstr(&j, "message");
    assert!(
        message.contains("runtime") && message.contains("python.exe"),
        "訊息須帶 manifest runtime 路徑：{message}"
    );
}

#[test]
fn start_binary_not_found() {
    let t = TestEnv::posix().with_process();
    let (rc, out, err) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(fstr(&j, "error_code"), "DAEMON_BINARY_NOT_FOUND");
    assert_eq!(
        fstr(&j, "message"),
        "找不到 serialwrapd（release manifest 的 launcher、CLI 同層 serialwrapd、manifest 的 Python runtime 皆不存在）；請確認發行包完整"
    );
    assert!(err.contains("daemon start"), "stderr 鏡像須帶 context daemon start：{err}");
}

#[test]
fn start_env_files_default_list() {
    // 未設 SERIALWRAP_DAEMON_ENV_FILE：嘗試 ~/OPI.env 與 <profile_dir>/OPI.env，兩者皆不存在 → 不呼叫 bash、無 env_files。
    let (t0, sock) = posix_with_socket();
    let t = t0
        .with_manifest_launcher()
        .with_process()
        .with_rpc_after_spawn(&sock, &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 0, "{out}");
    assert!(!t.calls().iter().any(|c| is_bash_call(c)), "不存在的 env 檔不得呼叫 bash");
    assert!(!has_key(&json(&out), "env_files"));
}

#[test]
fn start_env_file_source_failure() {
    let t0 = TestEnv::posix();
    let env_file = t0.home().join("OPI.env");
    t0.write_file(&env_file, b"A=1\n");
    let path = env_file.display().to_string();
    let sock = t0.default_socket();
    let t = t0
        .with_var("SERIALWRAP_DAEMON_ENV_FILE", &path)
        .with_command(&["bash", "-lc", ENV_SOURCE_SCRIPT, "serialwrap", &path], 1, "", "boom")
        .with_manifest_launcher()
        .with_process()
        .with_rpc_after_spawn(&sock, &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(fstr(&j, "error_code"), "ENV_FILE_SOURCE_FAILED");
    assert_eq!(fstr(&j, "message"), "boom");
    assert_eq!(fstr(&j, "env_file"), path);
    assert_eq!(fstr_list(&j, "env_files"), vec![path.clone()]);
    assert!(t.process().spawns().is_empty());
}

#[test]
fn start_env_file_non_utf8_value_preserved() {
    let t0 = TestEnv::posix();
    let env_file = t0.home().join("OPI.env");
    t0.write_file(&env_file, b"B=x\n");
    let path = env_file.display().to_string();
    let sock = t0.default_socket();
    // `env -0` 輸出含非 UTF-8 位元組：整個取代 env 時 B 必須保持原始 bytes（不得變成 U+FFFD）。
    let t = t0
        .with_var("SERIALWRAP_DAEMON_ENV_FILE", &path)
        .with_command_raw(
            &["bash", "-lc", ENV_SOURCE_SCRIPT, "serialwrap", &path],
            0,
            b"PATH=/usr/bin\0B=\xff\xfe\0",
            b"",
        )
        .with_manifest_launcher()
        .with_process()
        .with_rpc_after_spawn(&sock, &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 0, "{out}");
    let expected = sw_pal::os_string_from_bytes(&[0xff, 0xfe]);
    assert_eq!(
        first_spawn_env(&t, "B"),
        Some(expected.as_encoded_bytes().to_vec())
    );
}

#[test]
fn start_env_file_win_simple_parse() {
    // Windows：`_parse_env_file_simple`（export 前綴、註解、去成對引號），不呼叫 bash。
    let t0 = TestEnv::windows();
    let env_file = t0.home().join("OPI.env");
    t0.write_file(&env_file, b"export A=\"1\"\n# c\nB=2\n");
    let path = env_file.display().to_string();
    let t = t0
        .with_var("SERIALWRAP_DAEMON_ENV_FILE", &path)
        .with_manifest_launcher()
        .with_process()
        .with_rpc_after_spawn(TestEnv::win_endpoint(), &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(first_spawn_env(&t, "A"), Some(b"1".to_vec()));
    assert_eq!(first_spawn_env(&t, "B"), Some(b"2".to_vec()));
    assert!(!t.calls().iter().any(|c| is_bash_call(c)), "Windows 簡易解析不得呼叫 bash");
}

#[test]
fn start_foreground_returns_child_rc() {
    let t = TestEnv::posix().with_manifest_launcher().with_process();
    t.process().foreground_rc(3);
    let (rc, out, _) = t.run(&["daemon", "start", "--foreground"]);
    assert_eq!(rc, 3);
    assert_eq!(out, "");
}

#[test]
fn start_daemon_exited() {
    let t = TestEnv::posix().with_manifest_launcher().with_spawn_exits(2);
    let (rc, out, err) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 2);
    let j = json(&out);
    assert_eq!(fstr(&j, "error_code"), "DAEMON_EXITED");
    assert!(fbool(&j, "ok") == false);
    assert_eq!(fint(&j, "returncode"), 2);
    assert!(fint(&j, "pid") > 0);
    assert_eq!(object_len(&j), 4, "DAEMON_EXITED 不應額外帶 message");
    assert_eq!(
        out,
        format!("{{\"error_code\":\"DAEMON_EXITED\",\"ok\":false,\"pid\":{},\"returncode\":2}}\n", fint(&j, "pid"))
    );
    assert_eq!(err, "serialwrap: daemon start failed: DAEMON_EXITED\n");
}

#[test]
fn start_not_ready_posix_15_attempts() {
    let t = TestEnv::posix().with_manifest_launcher().with_process();
    let (rc, out, err) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 2);
    assert_eq!(fstr(&json(&out), "error_code"), "DAEMON_NOT_READY");
    assert_eq!(object_len(&json(&out)), 3, "DAEMON_NOT_READY 不應額外帶 message");
    assert_eq!(
        out,
        format!("{{\"error_code\":\"DAEMON_NOT_READY\",\"ok\":false,\"pid\":{}}}\n", fint(&json(&out), "pid"))
    );
    assert_eq!(err, "serialwrap: daemon start failed: DAEMON_NOT_READY\n");
    let sleeps = t.clock().sleeps_s();
    assert_eq!(sleeps.len(), 15);
    assert!(sleeps.iter().all(|s| (s - 0.2).abs() < 1e-9));
}

#[test]
fn start_not_ready_windows_50_attempts() {
    let t = TestEnv::windows().with_manifest_launcher().with_process();
    let (rc, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 2);
    assert_eq!(fstr(&json(&out), "error_code"), "DAEMON_NOT_READY");
    let sleeps = t.clock().sleeps_s();
    assert_eq!(sleeps.len(), 50);
    assert!(sleeps.iter().all(|s| (s - 0.2).abs() < 1e-9));
}

#[test]
fn start_success_includes_warnings_and_env_files() {
    let t0 = TestEnv::posix();
    let env_file = t0.home().join("OPI.env");
    t0.write_file(&env_file, b"A=1\n");
    let path = env_file.display().to_string();
    let sock = t0.default_socket();
    let t = t0
        .with_var("SERIALWRAP_DAEMON_ENV_FILE", &path)
        .with_command_raw(&["bash", "-lc", ENV_SOURCE_SCRIPT, "serialwrap", &path], 0, b"A=1\0", b"")
        .with_manifest_launcher()
        .with_process()
        .with_rpc_after_spawn(&sock, &["health.ping", "health.status"]);
    t.rpc().respond(
        &t.isolated_endpoint(&sock),
        "health.status",
        r#"{"ok":true,"pid":9000,"version":"0.4.0","warnings":["no_profiles_loaded"]}"#,
    );
    let (rc, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 0, "{out}");
    let j = json(&out);
    assert!(fbool(&j, "ok"));
    assert_eq!(fstr_list(&j, "warnings"), vec!["no_profiles_loaded".to_string()]);
    assert_eq!(fstr_list(&j, "env_files"), vec![path]);
    assert_eq!(t.rpc().timeouts_s(), vec![0.5, 0.5, 1.0]);
}

#[test]
fn start_success_omits_empty_warnings() {
    let t0 = TestEnv::posix();
    let sock = t0.default_socket();
    let t = t0
        .with_manifest_launcher()
        .with_process()
        .with_rpc_after_spawn(&sock, &["health.ping", "health.status"]);
    t.rpc().respond(
        &t.isolated_endpoint(&sock),
        "health.status",
        r#"{"ok":true,"pid":9000,"version":"0.4.0","warnings":[]}"#,
    );
    let (rc, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 0, "{out}");
    assert!(!has_key(&json(&out), "warnings"));
}

#[test]
fn stop_routes_supervised_unless_explicit_target() {
    // (a) systemd-user 且無 --socket → systemctl stop（不觸發 Restart=）。
    let t = TestEnv::posix()
        .with_config("supervision_mode: systemd-user\n")
        .with_systemd(true)
        .with_real_service();
    let (_, _, _) = t.run(&["daemon", "stop"]);
    assert_eq!(t.calls(), vec![argv(&["systemctl", "--user", "stop", "serialwrap"])]);

    // (b) windows-task 且無 --socket → graceful_stop：/DISABLE → daemon.stop → /ENABLE。
    let t = TestEnv::windows()
        .with_config("supervision_mode: windows-task\n")
        .with_real_service()
        .with_daemon_alive_then_gone(TestEnv::win_endpoint(), 900, 3);
    let (_, _, _) = t.run(&["daemon", "stop"]);
    let o = t.event_order();
    assert!(o.index_of("run:schtasks /Change /TN serialwrap /DISABLE") < o.index_of("rpc:daemon.stop"));
    assert!(o.index_of("rpc:daemon.stop") < o.index_of("run:schtasks /Change /TN serialwrap /ENABLE"));

    // (c) 帶 --socket（明確目標）→ 只送 RPC daemon.stop，不經監管。
    let t = TestEnv::posix()
        .with_config("supervision_mode: systemd-user\n")
        .with_systemd(true)
        .with_real_service()
        .with_rpc_ok("/tmp/x.sock", "daemon.stop", r#"{"ok":true,"stopping":true}"#);
    let (_, _, _) = t.run(&["--socket", "/tmp/x.sock", "daemon", "stop"]);
    assert_eq!(rpc_count(&t, "daemon.stop"), 1);
    assert!(t.calls().is_empty(), "明確目標不得呼叫監管命令");
}

#[test]
fn stop_empty_endpoint_routes_systemd_like_python() {
    let t = TestEnv::posix()
        .with_config("supervision_mode: systemd-user\n")
        .with_systemd(true)
        .with_real_service();
    let (_, _, _) = t.run(&["--endpoint", "", "daemon", "stop"]);
    assert_eq!(t.calls(), vec![argv(&["systemctl", "--user", "stop", "serialwrap"])]);
}

#[test]
fn stop_unknown_systemd_prefix_preserves_no_systemd_route() {
    let t = TestEnv::posix()
        .with_config("supervision_mode: systemd-foo\n")
        .with_real_service();
    let (rc, out, _) = t.run(&["daemon", "stop"]);
    assert_eq!(rc, 2);
    assert_eq!(fstr(&json(&out), "error_code"), "NO_SYSTEMD");
    assert_eq!(fstr(&json(&out), "_routed_to"), "service stop");
    assert_eq!(rpc_count(&t, "daemon.stop"), 0);
    assert!(t.calls().is_empty());
}

#[test]
fn stop_rpc_timeout_fixed_2s_no_retries() {
    // `--retries 3 --timeout 9`：daemon stop 的 RPC 固定 2.0 s 且不重試。
    let (t0, sock) = posix_with_socket();
    let t = t0.with_rpc_ok(&sock, "daemon.stop", r#"{"ok":true,"stopping":true}"#);
    let (_, _, _) = t.run(&["--retries", "3", "--timeout", "9", "--socket", &sock, "daemon", "stop"]);
    assert_eq!(rpc_count(&t, "daemon.stop"), 1);
    assert_eq!(t.rpc().timeouts_s(), vec![2.0]);
}

#[test]
fn stop_bench_unremembered_exit1() {
    let t = TestEnv::posix();
    let (rc, out, _) = t.run(&["--bench", "B9", "daemon", "stop"]);
    assert_eq!(rc, 1);
    assert_eq!(fstr(&json(&out), "error_code"), "BENCH_ENDPOINT_NOT_REMEMBERED");
}

#[test]
fn restart_supervised_systemd_equals_service_restart() {
    let mk = || {
        TestEnv::posix()
            .with_config("supervision_mode: systemd-user\n")
            .with_systemd(true)
            .with_real_service()
    };
    let a = mk();
    let (rc_a, out_a, err_a) = a.run(&["daemon", "restart"]);
    let b = mk();
    let (rc_b, out_b, err_b) = b.run(&["service", "restart"]);
    assert_eq!(a.calls(), vec![argv(&["systemctl", "--user", "restart", "serialwrap"])]);
    assert_eq!(rc_a, rc_b);
    assert_eq!(out_a, out_b);
    assert_eq!(err_a, err_b);
}

#[test]
fn restart_on_demand_stop_wait_start() {
    // ping ok → daemon.stop → 舊實例結束（identity 消失）→ spawn；daemon.stop 必須早於 spawn。
    let (t0, sock) = posix_with_socket();
    let t = t0
        .with_process()
        .with_manifest_launcher()
        .with_restart_cycle(&sock, 4321, 9000);
    let (rc, out, _) = t.run(&["daemon", "restart"]);
    assert_eq!(rc, 0, "{out}");
    let o = t.event_order();
    assert!(o.index_of("rpc:daemon.stop") < o.index_of("spawn:serialwrapd"));
}

#[test]
fn restart_on_demand_timeout_is_p1_shape() {
    // 停止後 ping 持續 ok 10 s → TIMEOUT（P1 形狀）、exit 1、不 spawn。
    let (t0, sock) = posix_with_socket();
    let t = t0
        .with_daemon_alive_forever(&sock, 4321)
        .with_rpc_ok(&sock, "daemon.stop", r#"{"ok":true,"stopping":true}"#)
        .with_process();
    let (rc, out, _) = t.run(&["daemon", "restart"]);
    assert_eq!(rc, 1);
    assert_eq!(out, "{\"error_code\":\"TIMEOUT\",\"ok\":false,\"phase\":\"restart_wait_old_exit\"}\n");
    assert_eq!(t.process().spawn_count(), 0);
    assert!(t.clock().elapsed() >= Duration::from_secs(10));
}

#[test]
fn restart_on_demand_not_running_wraps_start() {
    // ping 失敗 → 不送 daemon.stop，直接 spawn；輸出 stopped:false、restarted:true、start:{…}。
    let (t0, sock) = posix_with_socket();
    let t = t0
        .with_process()
        .with_manifest_launcher()
        .with_rpc_after_spawn(&sock, &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["daemon", "restart"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(rpc_count(&t, "daemon.stop"), 0);
    assert_eq!(t.process().spawn_count(), 1);
    let j = json(&out);
    assert!(fbool(&j, "restarted"));
    assert!(!fbool(&j, "stopped"));
    assert!(fbool(field(&j, "start"), "ok"));
}

#[test]
fn restart_waits_for_process_when_ping_is_already_down() {
    let (t0, sock) = posix_with_socket();
    let t = t0
        .with_process()
        .with_manifest_launcher()
        .with_procs(vec![daemon_proc(4321, Some(&sock))])
        .with_rpc_refused(&sock);
    let (rc, out, _) = t.run(&["daemon", "restart"]);
    assert_eq!(rc, 1);
    assert_eq!(out, "{\"error_code\":\"TIMEOUT\",\"ok\":false,\"phase\":\"restart_wait_old_exit\"}\n");
    assert_eq!(t.process().spawn_count(), 0, "舊行程仍存活時不得啟動新 daemon");
    assert!(t.clock().elapsed() >= Duration::from_secs(10));
}

#[test]
fn restart_keeps_nested_start_stderr() {
    let t0 = TestEnv::posix();
    let env_file = t0.home().join("OPI.env");
    t0.write_file(&env_file, b"A=1\n");
    let env_path = env_file.display().to_string();
    let sock = t0.default_socket();
    let t = t0
        .with_var("SERIALWRAP_DAEMON_ENV_FILE", &env_path)
        .with_command_raw(&["bash", "-lc", ENV_SOURCE_SCRIPT, "serialwrap", &env_path], 1, b"", b"boom\n")
        .with_manifest_launcher()
        .with_process()
        .with_rpc_after_spawn(&sock, &["health.ping"]);
    let (rc, _, err) = t.run(&["daemon", "restart"]);
    assert_eq!(rc, 2);
    assert!(err.lines().any(|line| line == "serialwrap: daemon start failed: ENV_FILE_SOURCE_FAILED: boom"), "巢狀 start stderr 遺失：{err}");
}

#[test]
fn wait_gone_zombie_counts_as_gone() {
    // pid_alive 為 false（殭屍行程依 Task 4 規則視為不存活）且 ping 被拒 → 立即 true，不等滿 10 s。
    let t = TestEnv::posix();
    let id = DaemonIdentity {
        pid: Some(4321),
        tree: vec![ProcIdentity { pid: 4321, start_ticks: None }],
        known: true,
    };
    let gone = t.with_cli_ctx(|ctx| {
        wait_daemon_gone(ctx, &t.default_socket(), &id, Duration::from_secs(10))
    });
    assert!(gone);
    assert!(t.clock().elapsed() < Duration::from_secs(10));
}

#[test]
fn wait_gone_launcher_child_still_alive_blocks() {
    // Windows：launcher（100）已死、其 Python 子行程（200）仍活 → 不視為結束，逾時回 false。
    let t = TestEnv::windows().with_procs(vec![daemon_proc(200, None)]);
    let id = DaemonIdentity {
        pid: Some(100),
        tree: vec![
            ProcIdentity { pid: 100, start_ticks: None },
            ProcIdentity { pid: 200, start_ticks: None },
        ],
        known: true,
    };
    let gone = t.with_cli_ctx(|ctx| {
        wait_daemon_gone(ctx, TestEnv::win_endpoint(), &id, Duration::from_secs(10))
    });
    assert!(!gone);
    assert!(t.clock().elapsed() >= Duration::from_secs(10));
}

#[test]
fn autostart_disabled_on_posix_caps() {
    let t = TestEnv::posix().with_manifest_launcher().with_process();
    let (rc, out, _) = t.run(&["session", "list"]);
    assert_eq!(rc, 2);
    assert_eq!(fstr(&json(&out), "error_code"), "SOCKET_ERROR");
    assert!(t.process().spawns().is_empty());
}

#[test]
fn autostart_not_for_explicit_endpoint() {
    let t = TestEnv::windows().with_manifest_launcher().with_process();
    let (rc, _, _) = t.run(&["--endpoint", "tcp://127.0.0.1:48700", "session", "list"]);
    assert_eq!(rc, 2);
    assert!(t.process().spawns().is_empty());
}

#[test]
fn autostart_not_for_daemon_status() {
    let t = TestEnv::windows().with_manifest_launcher().with_process();
    let (rc, out, _) = t.run(&["daemon", "status"]);
    assert_eq!(rc, 2);
    assert_eq!(fstr(&json(&out), "error_code"), "SOCKET_ERROR");
    assert!(t.process().spawns().is_empty());
    assert_eq!(t.rpc().connect_count(TestEnv::win_endpoint()), 1, "只連線一次（health.status），不得探測或啟動");
}

#[test]
fn autostart_not_for_daemon_stop() {
    let t = TestEnv::windows().with_manifest_launcher().with_process();
    let (_, _, _) = t.run(&["daemon", "stop"]);
    assert!(t.process().spawns().is_empty());
}

#[test]
fn autostart_not_for_timeout() {
    // 連線成功但 recv 逾時 → 不 spawn、不重送。
    let t = TestEnv::windows()
        .with_rpc_ok(TestEnv::win_endpoint(), "session.list", SESSION_LIST_OK)
        .with_rpc_recv_timeout(TestEnv::win_endpoint(), "session.list")
        .with_manifest_launcher()
        .with_process();
    let (rc, _, _) = t.run(&["session", "list"]);
    assert_eq!(rc, 2);
    assert!(t.process().spawns().is_empty());
    assert_eq!(rpc_count(&t, "session.list"), 1);
}

#[test]
fn autostart_permission_denied_no_resend() {
    let t = TestEnv::windows()
        .with_rpc_refused_kind(TestEnv::win_endpoint(), PalErrorKind::PermissionDenied)
        .with_manifest_launcher()
        .with_process();
    let (rc, out, _) = t.run(&["session", "list"]);
    assert_eq!(rc, 2);
    assert_eq!(fstr(&json(&out), "error_code"), "SOCKET_ERROR");
    assert!(t.process().spawns().is_empty());
    assert_eq!(rpc_count(&t, "session.list"), 0, "PermissionDenied 不得自動啟動或重送");
}

#[test]
fn autostart_connect_timeout_no_resend() {
    let t = TestEnv::windows()
        .with_rpc_refused_kind(TestEnv::win_endpoint(), PalErrorKind::TimedOut)
        .with_manifest_launcher()
        .with_process();
    let (rc, out, _) = t.run(&["session", "list"]);
    assert_eq!(rc, 2);
    assert_eq!(fstr(&json(&out), "error_code"), "TIMEOUT");
    assert!(t.process().spawns().is_empty());
    assert_eq!(rpc_count(&t, "session.list"), 0, "連線被拒的請求未送出，不得重送");
}

#[test]
fn autostart_concurrent_second_cli_reuses_daemon() {
    // 贏家持有 autostart.lock，在釋放前（3 s）讓 ping 轉 ok；第二個 CLI 取得鎖後判定 AlreadyUp。
    // 註：需要 FakeClock 感知的 autostart.lock 持有者替身（support 的 with_autostart_lock_holder，P4-T7 todo）。
    let t = TestEnv::windows()
        .with_autostart_lock_holder(Some(3.0))
        .with_rpc_refused_then_ok(TestEnv::win_endpoint(), "session.list", SESSION_LIST_OK)
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", PING_OK)
        .with_manifest_launcher()
        .with_process();
    let (rc, out, err) = t.run(&["session", "list"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(out, format!("{SESSION_LIST_OK}\r\n"));
    assert!(t.process().spawns().is_empty(), "AlreadyUp 不得 spawn");
    assert_eq!(rpc_count(&t, "session.list"), 1, "重送 1 次");
    assert!(!err.contains("已自動啟動"));
}

#[test]
fn autostart_loser_daemon_exited_but_healthy_is_success() {
    // spawn 回 DAEMON_EXITED（被實例鎖擋）但再探 ping 已健康 → 重送成功、exit 0、無失敗訊息。
    let t = TestEnv::windows()
        .with_spawn_exits(1)
        .with_manifest_launcher()
        .with_rpc_refused_then_ok(TestEnv::win_endpoint(), "session.list", SESSION_LIST_OK)
        .with_rpc_after_spawn(TestEnv::win_endpoint(), &["health.ping", "health.status"]);
    let (rc, out, err) = t.run(&["session", "list"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(out, format!("{SESSION_LIST_OK}\r\n"));
    assert!(!err.contains("自動啟動失敗"));
}

#[test]
fn autostart_nested_start_already_running_without_pid_is_reused() {
    let endpoint = TestEnv::win_endpoint();
    let t = TestEnv::windows()
        .with_manifest_launcher()
        .with_process()
        .with_rpc_ok(endpoint, "session.list", SESSION_LIST_OK);
    t.rpc().refuse_first(endpoint, 1);
    t.rpc().respond_seq(
        &t.isolated_endpoint(endpoint),
        "health.ping",
        &[r#"{"ok":false}"#, PING_OK],
    );
    let (rc, out, err) = t.run(&["session", "list"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(out, format!("{SESSION_LIST_OK}\r\n"));
    assert_eq!(t.process().spawn_count(), 0, "巢狀 start 的 already_running 不得回報 pid=0");
    assert!(!err.contains("已自動啟動"));
}

#[test]
fn autostart_failure_reports_stderr() {
    // spawn 後 DAEMON_EXITED 且再探仍失敗 → 原 SOCKET_ERROR 回應照印，stderr 另起一行說明失敗碼。
    let t = TestEnv::windows()
        .with_spawn_exits(1)
        .with_manifest_launcher();
    let (rc, out, err) = t.run(&["session", "list"]);
    assert_eq!(rc, 2);
    assert_eq!(fstr(&json(&out), "error_code"), "SOCKET_ERROR");
    assert!(
        err.lines().any(|line| line == "serialwrap: on-demand 自動啟動失敗: DAEMON_EXITED"),
        "stderr 缺少失敗行：{err}"
    );
}

#[test]
fn autostart_suppressed_while_install_journal_present() {
    // R5-F1：install-journal.json 存在（update／rollback 進行中，半切換入口）→ 不 spawn、不重送，原回應照印。
    // 註：計畫的 `with_journal_step` 為 P4-T12 todo；此處直接寫入存在性即足以驗證 SC-18 的閘門，改為直接寫檔。
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_manifest_launcher()
        .with_process();
    t.write_file(
        &t.install_root().join("install-journal.json"),
        br#"{"step":"backup_complete"}"#,
    );
    let before = t.journal_bytes();
    let (rc, out, err) = t.run(&["session", "list"]);
    assert_eq!(rc, 2);
    assert_eq!(fstr(&json(&out), "error_code"), "SOCKET_ERROR");
    assert!(t.process().spawns().is_empty());
    assert_eq!(rpc_count(&t, "session.list"), 0, "不得重送");
    let paused = "serialwrap: on-demand 自動啟動暫停：更新／回滾進行中（install-journal.json 存在），請待 serialwrap setup 完成";
    assert_eq!(err.lines().filter(|line| *line == paused).count(), 1);
    assert!(!err.contains("自動啟動失敗"));
    assert_eq!(t.journal_bytes(), before, "journal 位元組不得被修改");

    // 同情境移除 journal 後：spawn 1 次、重送 1 次。
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_manifest_launcher()
        .with_process()
        .with_rpc_refused_then_ok(TestEnv::win_endpoint(), "session.list", SESSION_LIST_OK)
        .with_rpc_after_spawn(TestEnv::win_endpoint(), &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["session", "list"]);
    assert_eq!(rc, 0, "{out}");
    assert_eq!(t.process().spawn_count(), 1);
    assert_eq!(rpc_count(&t, "session.list"), 1);
}

#[test]
fn autostart_rechecks_install_journal_after_waiting_for_lock() {
    let t = TestEnv::windows()
        .with_install_root()
        .with_installed("0.4.0")
        .with_autostart_lock_holder_creating_journal(3.0)
        .with_manifest_launcher()
        .with_process();
    let (rc, out, err) = t.run(&["session", "list"]);
    assert_eq!(rc, 2);
    assert_eq!(fstr(&json(&out), "error_code"), "SOCKET_ERROR");
    assert!(t.process().spawns().is_empty(), "journal 在等待鎖時出現後不得 spawn");
    assert_eq!(rpc_count(&t, "session.list"), 0, "不得重送未送出的請求");
    assert_eq!(
        err.lines().filter(|line| line.contains("install-journal.json 存在")).count(),
        1
    );
    assert!(t.journal_bytes().is_some(), "測試替身須於鎖釋放時建立 journal");
}

#[test]
fn autostart_waits_on_update_hold_lock() {
    // 另一 handle 持有 autostart.lock 且 15 s 內不釋放 → Failed(TIMEOUT)：無 spawn、原回應照印。
    // 註：需要 FakeClock 感知的 autostart.lock 持有者替身（P4-T7 todo）。
    let t = TestEnv::windows()
        .with_autostart_lock_holder(None)
        .with_manifest_launcher()
        .with_process();
    let (rc, out, err) = t.run(&["session", "list"]);
    assert_eq!(rc, 2);
    assert_eq!(fstr(&json(&out), "error_code"), "SOCKET_ERROR");
    assert!(t.process().spawns().is_empty());
    assert!(err.lines().any(|line| line == "serialwrap: on-demand 自動啟動失敗: TIMEOUT"));

    // 持鎖者在第 3 s 讓 ping 轉 ok 並釋放 → AlreadyUp、無 spawn、重送 1 次。
    let t = TestEnv::windows()
        .with_autostart_lock_holder(Some(3.0))
        .with_rpc_refused_then_ok(TestEnv::win_endpoint(), "session.list", SESSION_LIST_OK)
        .with_rpc_ok(TestEnv::win_endpoint(), "health.ping", PING_OK)
        .with_manifest_launcher()
        .with_process();
    let (rc, out, _) = t.run(&["session", "list"]);
    assert_eq!(rc, 0, "{out}");
    assert!(t.process().spawns().is_empty());
    assert_eq!(rpc_count(&t, "session.list"), 1);
}
