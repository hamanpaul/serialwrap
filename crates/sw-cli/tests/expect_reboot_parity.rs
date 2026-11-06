//! `cmd submit --expect-reboot` 的 Python 3.12 CLI 基線位元組對照。

use std::ffi::OsString;
use std::sync::Arc;

use sw_pal::fakes::{FakeClock, FakeConn, FakeEnv, MemFs, RecordingProcess, ScriptedConnector};
use sw_pal::{
    BackendKind, ClientCaps, ClientPlatform, Clock, DeviceBackendKind, PalPathFlavor, RpcConnector,
    SkillPlatform,
};

fn platform() -> (ClientPlatform, Arc<ScriptedConnector>) {
    let clock = Arc::new(FakeClock::default());
    let conn = Arc::new(ScriptedConnector::new(
        vec![FakeConn::Timeout],
        Arc::clone(&clock),
        true,
    ));
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
            flash_endpoint: true,
        },
    };
    (p, conn)
}

#[test]
fn interactive_rejection_matches_python_cli_baseline_byte_for_byte() {
    let (p, conn) = platform();
    let argv = [
        "serialwrap",
        "--socket",
        "/tmp/serialwrap-s1m242-never-connect.sock",
        "cmd",
        "submit",
        "--selector",
        "COM0",
        "--cmd",
        "firstboot -y && reboot",
        "--mode",
        "interactive",
        "--expect-reboot",
    ]
    .iter()
    .map(OsString::from)
    .collect();
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let rc = sw_cli::run(argv, &p, &mut stdout, &mut stderr);

    // 預期 bytes 取自 Python 3.12 `python3 -m sw_core.cli` 基線輸出。
    let expected = concat!(
        "{\"error_code\":\"EXPECT_REBOOT_MODE_UNSUPPORTED\",",
        "\"hint\":\"--expect-reboot 只適用 line／background 模式；",
        "interactive 是 lease 按鍵通道，不走 reboot 路徑。\",",
        "\"ok\":false}\n"
    );
    assert_eq!(rc, 1);
    assert_eq!(stdout, expected.as_bytes());
    assert!(stderr.is_empty());
    assert!(conn.sent().is_empty(), "interactive 拒絕不得送出 RPC");
}
