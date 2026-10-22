//! `event` 十個子指令的 RPC 參數（wire 位元組）回歸測試（P3-T15 審查修正）。
//!
//! 期望的請求行取自 Python 3.12.3 基線（`sw_core/cli.py:_dispatch_event`）以假 daemon 擷取的實際 wire。
//! 每個案例都經由 `sw_cli::run` 走完整分派，並斷言送出的位元組與 `timeout` 只有一次連線。

use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::Arc;

use sw_pal::fakes::{
    FakeClock, FakeConn, FakeEnv, MemFs, RecordingProcess, ScriptedConnector, StreamStep,
};
use sw_pal::{
    BackendKind, ClientCaps, ClientPlatform, Clock, PalPathFlavor, RpcConnector, SkillPlatform,
};

fn platform(files: &[(&str, Vec<u8>)]) -> (ClientPlatform, Arc<ScriptedConnector>) {
    let fs = MemFs::default();
    {
        let mut map = fs.files.lock().unwrap_or_else(|e| e.into_inner());
        for (path, bytes) in files {
            map.insert(PathBuf::from(path), Ok(bytes.clone()));
        }
    }
    let clock = Arc::new(FakeClock::default());
    let script = vec![FakeConn::stream(vec![StreamStep::Reply(
        b"{\"ok\":true}\n".to_vec(),
    )])];
    let conn = Arc::new(ScriptedConnector::new(script, Arc::clone(&clock), true));
    let p = ClientPlatform {
        env: Arc::new(FakeEnv::default()),
        fs: Arc::new(fs),
        clock: Arc::clone(&clock) as Arc<dyn Clock>,
        rpc: Arc::clone(&conn) as Arc<dyn RpcConnector>,
        process: Arc::new(RecordingProcess::default()),
        procs: sw_pal::fakes::FakeProcScan::new(Vec::new()),
        tunnel: sw_pal::fakes::FakeTunnel::new(false),
        service: sw_pal::fakes::FakeServiceCtl::new(Vec::new()),
        registry: sw_pal::fakes::FakeRegistry::new(Vec::new(), &[]),
        serial: sw_pal::fakes::serial::FakeSerialBackend::new(),
        uart_excl: std::sync::Arc::new(sw_pal::fakes::serial::FakeUartExclusive { report: Default::default() }),
        devices: sw_pal::fakes::platform::unconfigured_devices(),
        instance_lock: std::sync::Arc::new(sw_pal::fakes::platform::Unconfigured("instance_lock")),
        wal_fs: sw_pal::fakes::wal_fs::FakeWalFs::new(),
        rpc_listen: std::sync::Arc::new(sw_pal::fakes::platform::Unconfigured("rpc_listen")),
        console: std::sync::Arc::new(sw_pal::fakes::platform::Unconfigured("console")),
        signals: std::sync::Arc::new(sw_pal::fakes::platform::Unconfigured("signals")),
        caps: ClientCaps {
            path_flavor: PalPathFlavor::Posix,
            af_unix: true,
            native_backend: BackendKind::Posix,
            default_skill: SkillPlatform::Linux,
            text_newline: "\n",
            exec_replace: true,
            on_demand_autostart: false,
            console_kind: sw_pal::ConsoleKind::Pty,
            native_device: sw_pal::DeviceBackendKind::Posix,
        },
    };
    (p, conn)
}

/// 執行 `serialwrap --socket=/s.sock <parts...>`，回傳送出的請求行（不含換行）與 exit code。
fn wire_of(parts: &[&str]) -> (Vec<String>, i32) {
    let (p, conn) = platform(&[]);
    let mut argv = vec![
        OsString::from("serialwrap"),
        OsString::from("--socket=/s.sock"),
    ];
    argv.extend(parts.iter().map(OsString::from));
    let mut out = Vec::new();
    let mut err = Vec::new();
    let rc = sw_cli::run(argv, &p, &mut out, &mut err);
    let sent = conn
        .sent()
        .into_iter()
        .map(|bytes| {
            let text = String::from_utf8(bytes).unwrap();
            text.strip_suffix('\n').unwrap().to_string()
        })
        .collect();
    (sent, rc)
}

fn assert_wire(parts: &[&str], expected: &str) {
    let (sent, rc) = wire_of(parts);
    assert_eq!(rc, 0, "argv={parts:?}");
    assert_eq!(sent, vec![expected.to_string()], "argv={parts:?}");
}

#[test]
fn rm_sends_rule_id() {
    assert_wire(
        &["event", "rm", "rid1"],
        r#"{"id":1,"method":"event.rule_delete","params":{"rule_id":"rid1"}}"#,
    );
}

#[test]
fn show_sends_rule_id() {
    assert_wire(
        &["event", "show", "rid1"],
        r#"{"id":1,"method":"event.rule_get","params":{"rule_id":"rid1"}}"#,
    );
}

#[test]
fn list_sends_selector_and_owner_with_nulls_when_absent() {
    assert_wire(
        &["event", "list"],
        r#"{"id":1,"method":"event.rule_list","params":{"selector":null,"owner":null}}"#,
    );
    assert_wire(
        &["event", "list", "--selector", "COM0", "--owner", "alice"],
        r#"{"id":1,"method":"event.rule_list","params":{"selector":"COM0","owner":"alice"}}"#,
    );
}

#[test]
fn enable_and_disable_send_selector() {
    assert_wire(
        &["event", "enable", "--selector", "COM0"],
        r#"{"id":1,"method":"event.com_enable","params":{"selector":"COM0"}}"#,
    );
    assert_wire(
        &["event", "disable", "--selector", "COM0"],
        r#"{"id":1,"method":"event.com_disable","params":{"selector":"COM0"}}"#,
    );
}

#[test]
fn status_sends_selector_or_null() {
    assert_wire(
        &["event", "status"],
        r#"{"id":1,"method":"event.com_status","params":{"selector":null}}"#,
    );
    assert_wire(
        &["event", "status", "--selector", "COM1"],
        r#"{"id":1,"method":"event.com_status","params":{"selector":"COM1"}}"#,
    );
}

#[test]
fn reset_sends_rule_id_or_selector_with_the_other_null() {
    assert_wire(
        &["event", "reset", "--rule-id", "r9"],
        r#"{"id":1,"method":"event.reset","params":{"rule_id":"r9","selector":null}}"#,
    );
    assert_wire(
        &["event", "reset", "--selector", "COM2"],
        r#"{"id":1,"method":"event.reset","params":{"rule_id":null,"selector":"COM2"}}"#,
    );
}

#[test]
fn reload_sends_empty_params() {
    assert_wire(
        &["event", "reload"],
        r#"{"id":1,"method":"event.reload","params":{}}"#,
    );
}

#[test]
fn tail_defaults_and_explicit_values() {
    assert_wire(
        &["event", "tail"],
        r#"{"id":1,"method":"event.tail","params":{"rule_id":null,"selector":null,"n":50,"since_ts":null}}"#,
    );
    assert_wire(
        &[
            "event",
            "tail",
            "--rule-id",
            "r3",
            "--selector",
            "COM0",
            "-n",
            "7",
            "--since",
            "1234",
        ],
        r#"{"id":1,"method":"event.tail","params":{"rule_id":"r3","selector":"COM0","n":7,"since_ts":1234}}"#,
    );
    assert_wire(
        &["event", "tail", "-n", "0"],
        r#"{"id":1,"method":"event.tail","params":{"rule_id":null,"selector":null,"n":0,"since_ts":null}}"#,
    );
    assert_wire(
        &["event", "tail", "--since", "5"],
        r#"{"id":1,"method":"event.tail","params":{"rule_id":null,"selector":null,"n":50,"since_ts":5}}"#,
    );
}

#[test]
fn rm_keeps_valid_utf8_argv_unchanged() {
    assert_wire(
        &["event", "rm", "ünï"],
        r#"{"id":1,"method":"event.rule_delete","params":{"rule_id":"ünï"}}"#,
    );
}
