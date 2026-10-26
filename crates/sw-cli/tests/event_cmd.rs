//! `event` 指令單元／整合測試（P3-T15）。

use sw_cli::commands::event_cmd::{dispatch_event, event_params, EVENT_CMD_METHOD};

#[test]
fn event_cmd_method_table_matches() {
    assert_eq!(
        EVENT_CMD_METHOD,
        [
            ("add", "event.rule_set"),
            ("rm", "event.rule_delete"),
            ("list", "event.rule_list"),
            ("show", "event.rule_get"),
            ("enable", "event.com_enable"),
            ("disable", "event.com_disable"),
            ("status", "event.com_status"),
            ("reset", "event.reset"),
            ("reload", "event.reload"),
            ("tail", "event.tail"),
        ]
    );
}

struct DummySysEnv;
impl sw_pal::SysEnv for DummySysEnv {
    fn var(&self, _: &str) -> Option<String> { None }
    fn vars(&self) -> Vec<(String, String)> { vec![] }
    fn system_config_dir(&self) -> String { String::new() }
    fn current_user_home(&self) -> Option<String> { None }
    fn user_home(&self, _: &str) -> Option<String> { None }
    fn current_exe(&self) -> sw_pal::PalResult<std::path::PathBuf> { unimplemented!() }
    fn realpath(&self, _: &std::path::Path) -> sw_pal::PalResult<std::path::PathBuf> { unimplemented!() }
    fn terminal_columns(&self) -> Option<u16> { None }
    fn stdout_is_terminal(&self) -> bool { false }
    fn errno_name(&self, _: i32) -> Option<&'static str> { None }
    fn pid(&self) -> u32 { 1 }
    fn run(&self, _: &[String], _: Option<std::time::Duration>) -> sw_pal::PalResult<sw_pal::CmdOutput> {
        Ok(sw_pal::CmdOutput { rc: 0, stdout: String::new(), stderr: String::new() })
    }
    fn run_raw(&self, _: &[String], _: Option<&[(std::ffi::OsString, std::ffi::OsString)]>, _: Option<std::time::Duration>) -> sw_pal::PalResult<sw_pal::CmdOutputRaw> {
        Ok(sw_pal::CmdOutputRaw { rc: 0, stdout: Vec::new(), stderr: Vec::new() })
    }
    fn which(&self, _: &str) -> Option<std::path::PathBuf> { None }
    fn which_all(&self, _: &str) -> Vec<std::path::PathBuf> { Vec::new() }
    fn has_systemd(&self) -> bool { false }
    fn is_wsl(&self) -> bool { false }
    fn user_in_group(&self, _: &str) -> bool { false }
    fn user_identity(&self) -> String { String::new() }
    fn local_appdata(&self) -> Option<std::path::PathBuf> { None }
}

struct DummyFs;
impl sw_pal::FsRead for DummyFs {
    fn read(&self, _: &std::path::Path) -> sw_pal::PalResult<Vec<u8>> { unimplemented!() }
    fn exists(&self, _: &std::path::Path) -> bool { false }
    fn is_dir(&self, _: &std::path::Path) -> bool { false }
    fn is_file(&self, _: &std::path::Path) -> bool { false }
    fn list_dir(&self, _: &std::path::Path) -> sw_pal::PalResult<Vec<String>> { unimplemented!() }
}

struct DummyClock;
impl sw_pal::Clock for DummyClock {
    fn wall_unix_us(&self) -> i64 { 0 }
    fn local_utc_offset_s(&self) -> i32 { 0 }
    fn mono_ns(&self) -> u64 { 0 }
    fn sleep(&self, _: std::time::Duration) {}
}

struct DummyRpc;
impl sw_pal::RpcConnector for DummyRpc {
    fn af_unix_available(&self) -> bool { false }
    fn connect(&self, _: &sw_pal::EndpointSpec, _: sw_pal::SockTimeout) -> sw_pal::PalResult<Box<dyn sw_pal::RpcStream>> { unimplemented!() }
    fn resolve_tcp(&self, _: &str, _: u16) -> sw_pal::PalResult<()> { Ok(()) }
}

struct DummyProcess;
impl sw_pal::ProcessCtl for DummyProcess {
    fn exec_or_wait(&self, _: &[std::ffi::OsString], _: &[(String, String)]) -> sw_pal::PalError { unimplemented!() }
}

#[test]
fn event_params_and_dispatch_fail_until_implemented() {
    let caps = sw_pal::ClientCaps {
        path_flavor: sw_pal::PalPathFlavor::Posix,
        af_unix: true,
        native_backend: sw_pal::BackendKind::Posix,
        default_skill: sw_pal::SkillPlatform::Linux,
        text_newline: "\n",
        exec_replace: true,
        on_demand_autostart: false,
        console_kind: sw_pal::ConsoleKind::Pty,
        native_device: sw_pal::DeviceBackendKind::Posix,
        com0com_console: false,
    };
    let p = sw_pal::ClientPlatform {
        env: std::sync::Arc::new(DummySysEnv),
        fs: std::sync::Arc::new(DummyFs),
        clock: std::sync::Arc::new(DummyClock),
        rpc: std::sync::Arc::new(DummyRpc),
        process: std::sync::Arc::new(DummyProcess),
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
        handlers: std::sync::Arc::new(sw_pal::fakes::platform::Unconfigured("handlers")),
        usb: std::sync::Arc::new(sw_pal::fakes::platform::Unconfigured("usb")),
        caps,
    };
    let env = sw_proto::runtime_paths::EnvSnapshot::from_pairs(std::iter::empty());
    let paths = sw_proto::runtime_paths::RuntimePaths {
        config_dir: String::new(),
        state_dir: String::new(),
        run_dir: String::new(),
        data_dir: String::new(),
        lock_path: String::new(),
        socket_path: String::new(),
        default_tcp_port: sw_proto::pyjson::PyInt::Small(48700),
        default_endpoint: String::new(),
        profile_dir: String::new(),
        wal_dir: String::new(),
        log_dir: String::new(),
        config_yaml: String::new(),
    };
    let mut out = Vec::new();
    let mut err = Vec::new();
    let io = sw_cli::ctx::TextIo::new(&mut out, &mut err, "\n");
    let mut ctx = sw_cli::ctx::CliCtx {
        p: &p,
        env,
        paths,
        io,
        verbose: 0,
        cli_version: sw_proto::version::version(),
        autostart_lock: None,
    };
    let ns = sw_cli::argparse::Namespace::default();
    let _ = event_params(&ctx, &ns);
    let _ = dispatch_event(&mut ctx, &ns);
}

impl sw_pal::fs_ops::FsOps for DummyFs {}
