//! `skill` 指令單元／整合測試（P3-T15）。
mod common;

use sw_cli::assets;
use sw_cli::commands::skill_cmd::run_skill;

#[test]
fn skill_assets_match_fs() {
    let root = common::data_root();
    let linux_skill = std::fs::read(root.join("sw_core/assets/skill/SKILL.md"))
        .expect("read SKILL.md");
    assert_eq!(assets::SKILL_MD.as_bytes(), linux_skill);

    let win_skill = std::fs::read(root.join("sw_core/assets/skill/SKILL_WINDOWS.md"))
        .expect("read SKILL_WINDOWS.md");
    assert_eq!(assets::SKILL_WINDOWS_MD.as_bytes(), win_skill);
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
}

struct DummyFs;
impl sw_pal::FsRead for DummyFs {
    fn read(&self, _: &std::path::Path) -> sw_pal::PalResult<Vec<u8>> { unimplemented!() }
    fn exists(&self, _: &std::path::Path) -> bool { false }
    fn is_dir(&self, _: &std::path::Path) -> bool { false }
    fn list_dir(&self, _: &std::path::Path) -> sw_pal::PalResult<Vec<String>> { unimplemented!() }
}

struct DummyClock;
impl sw_pal::Clock for DummyClock {
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
fn skill_default_windows_outputs_skill_windows() {
    let caps = sw_pal::ClientCaps {
        path_flavor: sw_pal::PalPathFlavor::Windows,
        af_unix: false,
        native_backend: sw_pal::BackendKind::Win,
        default_skill: sw_pal::SkillPlatform::Windows,
        text_newline: "\r\n",
        exec_replace: false,
    };
    let p = sw_pal::ClientPlatform {
        env: std::sync::Arc::new(DummySysEnv),
        fs: std::sync::Arc::new(DummyFs),
        clock: std::sync::Arc::new(DummyClock),
        rpc: std::sync::Arc::new(DummyRpc),
        process: std::sync::Arc::new(DummyProcess),
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
    let io = sw_cli::ctx::TextIo::new(&mut out, &mut err, "\r\n");
    let mut ctx = sw_cli::ctx::CliCtx {
        p: &p,
        env,
        paths,
        io,
        verbose: 0,
    };
    let ns = sw_cli::argparse::Namespace::default();
    let rc = run_skill(&mut ctx, &ns);
    assert_eq!(rc, 0);
    assert_eq!(String::from_utf8(out).unwrap(), assets::SKILL_WINDOWS_MD);
}
