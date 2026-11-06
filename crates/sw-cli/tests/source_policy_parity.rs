//! `--source` 與 `session interactive-open --owner` 的 Python CLI 位元組對齊。
//!
//! 預期輸出取自 Python 3.12 `python3 -m sw_core.cli`；測試使用假 RPC，保證保留來源
//! 在 CLI 預檢階段被拒絕，且不連 daemon。

use std::ffi::OsString;
use std::sync::Arc;

use sw_pal::fakes::{FakeClock, FakeConn, FakeEnv, MemFs, RecordingProcess, ScriptedConnector};
use sw_pal::{
    BackendKind, ClientCaps, ClientPlatform, Clock, DeviceBackendKind, PalPathFlavor, RpcConnector,
    SkillPlatform,
};

const SOURCE_HINT: &str = "`system` 與 `system:*` 是 daemon 內部保留的來源前綴（WAL 歸因與內部恢復旗標），外部呼叫不可使用。自動化請用 `agent:<名>`，人工操作請用 `human:<名>`；逾時行為差異見 skill「`--source` 的語意」。";

fn platform(script: Vec<FakeConn>) -> (ClientPlatform, Arc<ScriptedConnector>) {
    let clock = Arc::new(FakeClock::default());
    let conn = Arc::new(ScriptedConnector::new(script, Arc::clone(&clock), true));
    let p = ClientPlatform {
        env: Arc::new(FakeEnv::default()),
        fs: Arc::new(MemFs::default()),
        clock: clock as Arc<dyn Clock>,
        rpc: Arc::clone(&conn) as Arc<dyn RpcConnector>,
        process: Arc::new(RecordingProcess::default()),
        procs: sw_pal::fakes::FakeProcScan::new(Vec::new()),
        tunnel: sw_pal::fakes::FakeTunnel::new(false),
        service: sw_pal::fakes::FakeServiceCtl::new(Vec::new()),
        registry: sw_pal::fakes::FakeRegistry::new(Vec::new(), &[]),
        serial: sw_pal::fakes::serial::FakeSerialBackend::new(),
        uart_excl: Arc::new(sw_pal::fakes::serial::FakeUartExclusive { report: Default::default() }),
        devices: sw_pal::fakes::platform::unconfigured_devices(),
        instance_lock: Arc::new(sw_pal::fakes::platform::Unconfigured("instance_lock")),
        wal_fs: sw_pal::fakes::wal_fs::FakeWalFs::new(),
        rpc_listen: Arc::new(sw_pal::fakes::platform::Unconfigured("rpc_listen")),
        console: Arc::new(sw_pal::fakes::platform::Unconfigured("console")),
        signals: Arc::new(sw_pal::fakes::platform::Unconfigured("signals")),
        caps: ClientCaps {
            path_flavor: PalPathFlavor::Posix,
            af_unix: true,
            native_backend: BackendKind::Posix,
            default_skill: SkillPlatform::Linux,
            text_newline: "\n",
            exec_replace: true,
            on_demand_autostart: false,
            console_kind: sw_pal::ConsoleKind::Pty,
            native_device: DeviceBackendKind::Posix,
            com0com_console: false,
        },
    };
    (p, conn)
}

fn run(args: &[&str], script: Vec<FakeConn>) -> (i32, Vec<u8>, Vec<u8>, Arc<ScriptedConnector>) {
    let (p, conn) = platform(script);
    let mut argv = vec![
        "serialwrap",
        "--socket",
        "/tmp/serialwrap-s1m240-never-connect.sock",
    ];
    argv.extend_from_slice(args);
    let argv = argv.into_iter().map(OsString::from).collect();
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let rc = sw_cli::run(argv, &p, &mut stdout, &mut stderr);
    (rc, stdout, stderr, conn)
}

fn expected_reserved(field: &str, value: &str, context: &str) -> (Vec<u8>, Vec<u8>) {
    // Python 的 json.dumps 會跳脫保留原始值中的 U+001C/U+001F 控制字元。
    let value = value
        .replace('\u{001c}', "\\u001c")
        .replace('\u{001f}', "\\u001f");
    let stdout = format!(
        "{{\"error_code\":\"SOURCE_RESERVED\",\"field\":\"{field}\",\"hint\":\"{SOURCE_HINT}\",\"ok\":false,\"reserved_prefix\":\"system\",\"value\":\"{value}\"}}\n"
    );
    let stderr = format!("serialwrap: {context} failed: SOURCE_RESERVED（hint: {SOURCE_HINT}）\n");
    (stdout.into_bytes(), stderr.into_bytes())
}

#[test]
fn reserved_source_is_rejected_at_each_python_cli_entry_point() {
    let cases: &[(&[&str], &str, &str)] = &[
        (
            &[
                "device",
                "release",
                "--selector",
                "COM0",
                "--source",
                "System:recover",
            ],
            "source",
            "device.release",
        ),
        (
            &[
                "cmd",
                "submit",
                "--selector",
                "COM0",
                "--cmd",
                "true",
                "--source",
                "System:recover",
            ],
            "source",
            "command.submit",
        ),
        (
            &[
                "file",
                "push",
                "--selector",
                "COM0",
                "--local",
                "/tmp/unused",
                "--remote",
                "/tmp/remote",
                "--source",
                "System:recover",
            ],
            "source",
            "file.push",
        ),
        (
            &[
                "file",
                "pull",
                "--selector",
                "COM0",
                "--remote",
                "/tmp/remote",
                "--source",
                "System:recover",
            ],
            "source",
            "file.pull",
        ),
        (
            &[
                "session",
                "interactive-open",
                "--selector",
                "COM0",
                "--owner",
                "System:recover",
            ],
            "owner",
            "session.interactive_open",
        ),
    ];

    for (args, field, context) in cases {
        let (rc, stdout, stderr, conn) = run(args, vec![FakeConn::Timeout]);
        let (expected_stdout, expected_stderr) =
            expected_reserved(field, "System:recover", context);
        assert_eq!(rc, 1, "args={args:?}");
        assert_eq!(stdout, expected_stdout, "args={args:?}");
        assert_eq!(stderr, expected_stderr, "args={args:?}");
        assert!(conn.sent().is_empty(), "拒絕不得送出 RPC：{args:?}");
        assert_eq!(conn.connect_count(), 0, "拒絕不得連 daemon：{args:?}");
    }
}

#[test]
fn reserved_source_prefix_uses_python_casefold_and_preserves_raw_value() {
    for raw in ["ſystem:recover", "syﬅem:recover", "syﬆem:recover"] {
        let args = [
            "cmd",
            "submit",
            "--selector",
            "COM0",
            "--cmd",
            "true",
            "--source",
            raw,
        ];
        let (rc, stdout, stderr, conn) = run(&args, vec![FakeConn::Timeout]);
        let (expected_stdout, expected_stderr) = expected_reserved("source", raw, "command.submit");
        assert_eq!(rc, 1, "source={raw:?}");
        assert_eq!(stdout, expected_stdout, "source={raw:?}");
        assert_eq!(stderr, expected_stderr, "source={raw:?}");
        assert!(conn.sent().is_empty(), "source={raw:?}");
        assert_eq!(conn.connect_count(), 0, "source={raw:?}");
    }
}

#[test]
fn python_strip_controls_reject_reserved_sources_at_each_cli_entry_point() {
    let raw = "\u{001c}system:recover\u{001c}";
    let device_release = ["device", "release", "--selector", "COM0", "--source", raw];
    let cmd_submit = [
        "cmd",
        "submit",
        "--selector",
        "COM0",
        "--cmd",
        "true",
        "--source",
        raw,
    ];
    let file_push = [
        "file",
        "push",
        "--selector",
        "COM0",
        "--local",
        "/tmp/unused",
        "--remote",
        "/tmp/remote",
        "--source",
        raw,
    ];
    let file_pull = [
        "file",
        "pull",
        "--selector",
        "COM0",
        "--remote",
        "/tmp/remote",
        "--source",
        raw,
    ];
    let interactive_open = [
        "session",
        "interactive-open",
        "--selector",
        "COM0",
        "--owner",
        raw,
    ];
    let cases: &[(&[&str], &str, &str)] = &[
        (&device_release, "source", "device.release"),
        (&cmd_submit, "source", "command.submit"),
        (&file_push, "source", "file.push"),
        (&file_pull, "source", "file.pull"),
        (&interactive_open, "owner", "session.interactive_open"),
    ];

    for (args, field, context) in cases {
        let (rc, stdout, stderr, conn) = run(args, vec![FakeConn::Timeout]);
        let (expected_stdout, expected_stderr) = expected_reserved(field, raw, context);
        assert_eq!(rc, 1, "args={args:?}");
        assert_eq!(stdout, expected_stdout, "args={args:?}");
        assert_eq!(stderr, expected_stderr, "args={args:?}");
        assert!(conn.sent().is_empty(), "拒絕不得送出 RPC：{args:?}");
        assert_eq!(conn.connect_count(), 0, "拒絕不得連 daemon：{args:?}");
    }

    let submit_before_expect = [
        "cmd",
        "submit",
        "--selector",
        "COM0",
        "--cmd",
        "true",
        "--source",
        raw,
        "--expect-reboot",
        "--mode",
        "interactive",
    ];
    let (rc, stdout, stderr, conn) = run(&submit_before_expect, vec![FakeConn::Timeout]);
    let (expected_stdout, expected_stderr) = expected_reserved("source", raw, "command.submit");
    assert_eq!(rc, 1);
    assert_eq!(stdout, expected_stdout);
    assert_eq!(stderr, expected_stderr);
    assert!(conn.sent().is_empty());
    assert_eq!(conn.connect_count(), 0);
}

#[test]
fn source_policy_python_strip_handles_empty_input_and_bare_human() {
    use sw_cli::source_policy::check_source;

    let empty = check_source(
        Some("\u{001c}\u{001d}\u{001e}\u{001f}"),
        "agent",
        "source",
        true,
    );
    assert_eq!(empty.source, "agent");

    let raw = "\u{001c}Human:\u{001f}";
    let bare = check_source(Some(raw), "agent", "source", true);
    assert_eq!(bare.source, "human:cli");
    assert_eq!(bare.normalized_from.as_deref(), Some(raw));
    assert!(bare.is_bare_human());

    let owner = check_source(Some(raw), "agent", "owner", false);
    assert_eq!(owner.source, raw);
    assert!(owner.is_bare_human());
}

#[test]
fn source_check_matches_python_normalization_and_classification() {
    use sw_cli::source_policy::{
        SOURCE_CLASS_AGENT, SOURCE_CLASS_HUMAN, check_source, classify_source,
    };

    let bare = check_source(Some("Human:"), "agent", "source", true);
    assert_eq!(bare.source, "human:cli");
    assert_eq!(bare.normalized_from.as_deref(), Some("Human:"));
    assert!(bare.is_bare_human());
    assert_eq!(classify_source(&bare.source), SOURCE_CLASS_HUMAN);

    let owner = check_source(Some("Human"), "agent", "owner", false);
    assert_eq!(owner.source, "Human");
    assert!(owner.normalized_from.is_none());
    assert!(owner.is_bare_human());
    assert_eq!(classify_source(&owner.source), SOURCE_CLASS_AGENT);

    let padded_owner = check_source(Some("human : "), "agent", "owner", false);
    assert_eq!(padded_owner.source, "human : ");
    assert!(!padded_owner.is_bare_human());

    let explicit = check_source(Some("human:cli"), "agent", "source", true);
    assert_eq!(explicit.source, "human:cli");
    assert!(explicit.normalized_from.is_none());
    assert!(!explicit.is_bare_human());
    assert_eq!(classify_source(&explicit.source), SOURCE_CLASS_HUMAN);

    let defaulted = check_source(Some("  "), "agent", "source", true);
    assert_eq!(defaulted.source, "agent");
    assert_eq!(defaulted.raw_source.as_deref(), Some("  "));
}

#[test]
fn submit_checks_completion_then_source_then_expect_reboot() {
    let invalid_completion = [
        "cmd",
        "submit",
        "--selector",
        "COM0",
        "--cmd",
        "true",
        "--completion-regex",
        "(",
        "--source",
        "system:recover",
        "--expect-reboot",
        "--mode",
        "interactive",
    ];
    let (rc, stdout, stderr, conn) = run(&invalid_completion, vec![FakeConn::Timeout]);
    assert_eq!(rc, 1);
    assert_eq!(
        stdout,
        b"{\"error_code\":\"INVALID_COMPLETION_REGEX\",\"ok\":false}\n"
    );
    assert!(stderr.is_empty());
    assert!(conn.sent().is_empty());
    assert_eq!(conn.connect_count(), 0);

    let source_before_expect = [
        "cmd",
        "submit",
        "--selector",
        "COM0",
        "--cmd",
        "true",
        "--completion-regex",
        "DONE",
        "--source",
        "system:recover",
        "--expect-reboot",
        "--mode",
        "interactive",
    ];
    let (rc, stdout, stderr, conn) = run(&source_before_expect, vec![FakeConn::Timeout]);
    let (expected_stdout, expected_stderr) =
        expected_reserved("source", "system:recover", "command.submit");
    assert_eq!(rc, 1);
    assert_eq!(stdout, expected_stdout);
    assert_eq!(stderr, expected_stderr);
    assert!(conn.sent().is_empty());
    assert_eq!(conn.connect_count(), 0);
}

#[test]
fn bare_human_remains_accepted_and_is_normalized_by_the_daemon() {
    let reply = br#"{"ok":true,"source":"human:cli","source_normalized_from":"human"}"#;
    let args = [
        "cmd",
        "submit",
        "--selector",
        "COM0",
        "--cmd",
        "true",
        "--source",
        "human",
    ];
    let (rc, stdout, stderr, conn) = run(
        &args,
        vec![FakeConn::stream(vec![sw_pal::fakes::StreamStep::Reply(
            reply.to_vec(),
        )])],
    );
    assert_eq!(rc, 0);
    assert_eq!(
        stdout,
        reply.iter().copied().chain([b'\n']).collect::<Vec<_>>()
    );
    assert!(stderr.is_empty());
    let sent = conn.sent();
    assert_eq!(sent.len(), 1);
    let request = String::from_utf8(sent[0].clone()).expect("RPC JSON 為 UTF-8");
    assert!(
        request.contains("\"source\":\"human\""),
        "request={request}"
    );
}
