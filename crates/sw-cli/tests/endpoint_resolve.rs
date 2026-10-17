//! P3-T12 紅燈測試：endpoint 解析（#108 dangling fallback、SC-09 config 探索）與 `--bench` state 讀取。
//!
//! 期望值依 Python 基線 `sw_core/cli.py:658-835`、`1115-1188` 與 `docs/rust-refactor/plans/04-stage1a-rust-cli.md` Task 12 表格。
//!
//! 替身配置：
//! - env、fs 為本檔內的 `SysEnv`／`FsRead` 實作（需攜帶資料，`sw_pal::fakes` 的 `FakeEnv`／`MemFs` 目前為骨架）；
//! - 連線器與 `ClientPlatform` 組裝來自 `sw_pal::fakes::fake_platform`（Task 9）；
//! - `alive` 探測依 script 順序消費：先探 config socket，再探 canonical（與 Python 呼叫順序一致）。
//!
//! 錯誤碼以 YAML 字串經 `ErrorCode::parse` 取得，不依賴生成的 variant 名稱。

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use sw_cli::ctx::{CliCtx, TextIo};
use sw_cli::endpoint_resolve::{
    endpoint_alive, local_default_endpoint, print_endpoint_resolution_error,
    resolve_endpoint_info, rpc_backend_is_win, EndpointArgs, EndpointResolutionError, ResolveFailure,
    ResolvedEndpoint,
};
use sw_pal::fakes::{fake_platform, FakeConn, ScriptedConnector};
use sw_pal::{
    BackendKind, ClientPlatform, FsRead, PalError, PalErrorKind, PalPathFlavor, PalResult, SysEnv,
};
use sw_proto::constants::SYSTEM_SOCKET;
use sw_proto::error_codes::ErrorCode;
use sw_proto::pypath::{HomeLookup, PathFlavor};
use sw_proto::runtime_paths::{EnvSnapshot, NativeTraits, RuntimePaths};

/// 測試用預設路徑（由 `World::new` 的 env 決定）。
const SOCKET: &str = "/sw/run/serialwrapd.sock";
const USER_CFG: &str = "/sw/config/config.yaml";
const BENCH_STATE: &str = "/sw/state/benches.state.json";
const DEFAULT_SYSTEM_DIR: &str = "/etc/serialwrap";

// ---------------------------------------------------------------------------
// 替身
// ---------------------------------------------------------------------------

struct TestEnv {
    vars: BTreeMap<String, String>,
    system_config_dir: String,
}

impl SysEnv for TestEnv {
    fn var(&self, k: &str) -> Option<String> {
        self.vars.get(k).cloned()
    }
    fn vars(&self) -> Vec<(String, String)> {
        self.vars.iter().map(|(k, v)| (k.clone(), v.clone())).collect()
    }
    fn system_config_dir(&self) -> String {
        self.system_config_dir.clone()
    }
    fn current_user_home(&self) -> Option<String> {
        Some("/home/t".to_string())
    }
    fn user_home(&self, _user: &str) -> Option<String> {
        None
    }
    fn current_exe(&self) -> PalResult<PathBuf> {
        Ok(PathBuf::from("/usr/bin/serialwrap"))
    }
    fn realpath(&self, p: &Path) -> PalResult<PathBuf> {
        Ok(p.to_path_buf())
    }
    fn terminal_columns(&self) -> Option<u16> {
        None
    }
    fn stdout_is_terminal(&self) -> bool {
        false
    }
    fn errno_name(&self, _code: i32) -> Option<&'static str> {
        None
    }
    fn pid(&self) -> u32 {
        4242
    }
}

struct TestFs {
    files: BTreeMap<PathBuf, Result<Vec<u8>, PalError>>,
    dirs: BTreeSet<PathBuf>,
}

fn not_found() -> PalError {
    PalError {
        kind: PalErrorKind::NotFound,
        os_code: Some(2),
        winerror: None,
        strerror: "No such file or directory".to_string(),
        py_message: "[Errno 2] No such file or directory".to_string(),
    }
}

fn is_a_directory() -> PalError {
    PalError {
        kind: PalErrorKind::Io,
        os_code: Some(21),
        winerror: None,
        strerror: "Is a directory".to_string(),
        py_message: "[Errno 21] Is a directory".to_string(),
    }
}

impl FsRead for TestFs {
    fn read(&self, p: &Path) -> PalResult<Vec<u8>> {
        if self.dirs.contains(p) {
            return Err(is_a_directory());
        }
        match self.files.get(p) {
            Some(Ok(bytes)) => Ok(bytes.clone()),
            Some(Err(e)) => Err(e.clone()),
            None => Err(not_found()),
        }
    }
    fn exists(&self, p: &Path) -> bool {
        self.files.contains_key(p) || self.dirs.contains(p)
    }
    fn is_dir(&self, p: &Path) -> bool {
        self.dirs.contains(p)
    }
    fn list_dir(&self, _p: &Path) -> PalResult<Vec<String>> {
        Err(not_found())
    }
}

struct TestHome;

impl HomeLookup for TestHome {
    fn current_user_home(&self) -> Option<String> {
        Some("/home/t".to_string())
    }
    fn user_home(&self, _user: &str) -> Option<String> {
        None
    }
}

fn probe_dead() -> FakeConn {
    FakeConn::Refuse(PalError {
        kind: PalErrorKind::ConnectionRefused,
        os_code: Some(111),
        winerror: None,
        strerror: "Connection refused".to_string(),
        py_message: "[Errno 111] Connection refused".to_string(),
    })
}

fn probe_alive() -> FakeConn {
    FakeConn::Stream(Vec::new())
}

// ---------------------------------------------------------------------------
// 測試世界與執行器
// ---------------------------------------------------------------------------

struct World {
    env: BTreeMap<String, String>,
    files: BTreeMap<PathBuf, Result<Vec<u8>, PalError>>,
    dirs: BTreeSet<PathBuf>,
    probes: Vec<FakeConn>,
    af_unix: bool,
    system_config_dir: String,
}

impl World {
    /// 預設：目錄變數指向 `/sw/*`，無 config、無 bench state，無探測 script，AF_UNIX 可用。
    fn new() -> Self {
        let mut env = BTreeMap::new();
        env.insert("SERIALWRAP_RUN_DIR".to_string(), "/sw/run".to_string());
        env.insert("SERIALWRAP_STATE_DIR".to_string(), "/sw/state".to_string());
        env.insert("SERIALWRAP_CONFIG_DIR".to_string(), "/sw/config".to_string());
        World {
            env,
            files: BTreeMap::new(),
            dirs: BTreeSet::new(),
            probes: Vec::new(),
            af_unix: true,
            system_config_dir: DEFAULT_SYSTEM_DIR.to_string(),
        }
    }

    fn env(mut self, k: &str, v: &str) -> Self {
        self.env.insert(k.to_string(), v.to_string());
        self
    }

    fn file(mut self, path: &str, text: &str) -> Self {
        self.files.insert(PathBuf::from(path), Ok(text.as_bytes().to_vec()));
        self
    }

    fn file_bytes(mut self, path: &str, bytes: &[u8]) -> Self {
        self.files.insert(PathBuf::from(path), Ok(bytes.to_vec()));
        self
    }

    fn dir(mut self, path: &str) -> Self {
        self.dirs.insert(PathBuf::from(path));
        self
    }

    fn probes(mut self, script: Vec<FakeConn>) -> Self {
        self.probes = script;
        self
    }

    fn no_af_unix(mut self) -> Self {
        self.af_unix = false;
        self
    }

    fn system_dir(mut self, dir: &str) -> Self {
        self.system_config_dir = dir.to_string();
        self
    }
}

struct Run<R> {
    value: R,
    stdout: String,
    stderr: String,
    conn: Arc<ScriptedConnector>,
}

/// 以 `World` 組出 `ClientPlatform` 與 `CliCtx`，執行 `body` 後回收輸出。
fn run<R>(w: World, body: impl FnOnce(&mut CliCtx<'_>) -> R) -> Run<R> {
    let (mut p, conn, _clock): (ClientPlatform, Arc<ScriptedConnector>, _) = fake_platform(w.probes);
    let env_snap = EnvSnapshot::from_pairs(w.env.clone());
    let env_dyn: Arc<dyn SysEnv> = Arc::new(TestEnv {
        vars: w.env.clone(),
        system_config_dir: w.system_config_dir.clone(),
    });
    let fs_dyn: Arc<dyn sw_pal::fs_ops::FsOps> = Arc::new(TestFs {
        files: w.files,
        dirs: w.dirs,
    });
    p.env = env_dyn;
    p.fs = fs_dyn;
    p.caps.af_unix = w.af_unix;
    p.caps.native_backend = BackendKind::Posix;
    p.caps.path_flavor = PalPathFlavor::Posix;

    let paths = match RuntimePaths::resolve(
        &env_snap,
        NativeTraits {
            default_endpoint_is_tcp: false,
            path_flavor: PathFlavor::Posix,
        },
        &TestHome,
    ) {
        Ok(paths) => paths,
        Err(_) => panic!("RuntimePaths::resolve 失敗（測試 env 非法）"),
    };

    let mut out: Vec<u8> = Vec::new();
    let mut err: Vec<u8> = Vec::new();
    let value = {
        let mut ctx = CliCtx {
            p: &p,
            env: env_snap,
            paths,
            io: TextIo::new(&mut out, &mut err, "\n"),
            verbose: 0,
            cli_version: sw_proto::version::version(),
        };
        body(&mut ctx)
    };
    Run {
        value,
        stdout: String::from_utf8(out).expect("stdout 應為 UTF-8"),
        stderr: String::from_utf8(err).expect("stderr 應為 UTF-8"),
        conn,
    }
}

/// 呼叫 `resolve_endpoint_info` 的簡寫。
fn resolve(
    w: World,
    endpoint: Option<&str>,
    socket: Option<&str>,
    bench: Option<&str>,
) -> Run<Result<ResolvedEndpoint, ResolveFailure>> {
    run(w, |ctx| {
        resolve_endpoint_info(
            ctx,
            &EndpointArgs {
                endpoint,
                socket,
                bench,
            },
        )
    })
}

fn code(s: &str) -> ErrorCode {
    ErrorCode::parse(s).unwrap_or_else(|| panic!("未知錯誤碼：{s}"))
}

fn ep(endpoint: &str, source: &'static str) -> Result<ResolvedEndpoint, ResolveFailure> {
    Ok(ResolvedEndpoint {
        endpoint: endpoint.to_string(),
        source,
    })
}

fn resolution_err(error_code: &str, message: &str) -> Result<ResolvedEndpoint, ResolveFailure> {
    Err(ResolveFailure::Resolution(EndpointResolutionError {
        code: code(error_code),
        message: message.to_string(),
    }))
}

/// `cli.py:778-783` 的 dangling fallback stderr（逐字）。
fn fallback_stderr(cfg_sock: &str, mode: &str, canonical: &str) -> String {
    format!(
        "serialwrap: config.yaml socket_path '{cfg_sock}' 不可連，依 supervision_mode={mode} 改用 '{canonical}'；如需修正請更新 config.yaml 或重跑 serialwrap setup。\n"
    )
}

const BENCH_NOT_REMEMBERED_MSG: &str = "bench \"lab\" 尚未記住 endpoint；請先執行 serialwrap connect lab";
const BENCH_CORRUPT_MSG: &str = "bench \"lab\" 的 endpoint 記憶損壞；請重新執行 serialwrap connect lab";

// ---------------------------------------------------------------------------
// 優先序：--endpoint > --socket > --bench > config.yaml > 預設
// ---------------------------------------------------------------------------

#[test]
fn endpoint_wins() {
    let r = resolve(World::new(), Some("tcp://h:1"), Some("/s"), None);
    assert_eq!(r.value, ep("tcp://h:1", "--endpoint"));
}

#[test]
fn empty_endpoint_falls_through() {
    let r = resolve(World::new(), Some(""), None, None);
    assert_eq!(r.value, ep(SOCKET, "default"));
}

#[test]
fn explicit_empty_socket() {
    let r = resolve(World::new(), None, Some(""), None);
    assert_eq!(r.value, ep("", "--socket"));
}

#[test]
fn socket_equal_default_is_still_explicit() {
    let w = World::new().file(USER_CFG, "socket_path: /cfg.sock\n");
    let r = resolve(w, None, Some(SOCKET), None);
    assert_eq!(r.value, ep(SOCKET, "--socket"));
}

// ---------------------------------------------------------------------------
// --bench（cli.py:1115-1188）
// ---------------------------------------------------------------------------

#[test]
fn bench_ok() {
    let w = World::new().file(BENCH_STATE, r#"{"lab":"tcp://127.0.0.1:7001"}"#);
    let r = resolve(w, None, None, Some("lab"));
    assert_eq!(r.value, ep("tcp://127.0.0.1:7001", "--bench"));
}

#[test]
fn bench_nested_format() {
    let w = World::new().file(BENCH_STATE, r#"{"benches":{"lab":" tcp://localhost:7001 "}}"#);
    let r = resolve(w, None, None, Some("lab"));
    assert_eq!(r.value, ep("tcp://localhost:7001", "--bench"));
}

#[test]
fn bench_missing_file() {
    let r = resolve(World::new(), None, None, Some("lab"));
    assert_eq!(
        r.value,
        resolution_err(
            "BENCH_ENDPOINT_NOT_REMEMBERED",
            "bench \"lab\" 尚未記住 endpoint；請先執行 serialwrap connect lab",
        )
    );
}

#[test]
fn bench_not_object() {
    let w = World::new().file(BENCH_STATE, "[1]");
    let r = resolve(w, None, None, Some("lab"));
    assert_eq!(
        r.value,
        resolution_err("INVALID_BENCH_STATE", "benches.state.json 必須是 object")
    );
}

#[test]
fn bench_benches_not_object() {
    let w = World::new().file(BENCH_STATE, r#"{"benches":[1]}"#);
    let r = resolve(w, None, None, Some("lab"));
    assert_eq!(
        r.value,
        resolution_err(
            "INVALID_BENCH_STATE",
            "benches.state.json 的 benches 欄位必須是 object",
        )
    );
}

#[test]
fn bench_bad_json() {
    let w = World::new().file(BENCH_STATE, "{");
    let r = resolve(w, None, None, Some("lab"));
    assert_eq!(
        r.value,
        resolution_err(
            "INVALID_BENCH_STATE",
            "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
        )
    );
}

#[test]
fn bench_non_loopback() {
    let w = World::new().file(BENCH_STATE, r#"{"lab":"tcp://10.0.0.1:7001"}"#);
    let r = resolve(w, None, None, Some("lab"));
    assert_eq!(r.value, resolution_err("INVALID_BENCH_STATE", BENCH_CORRUPT_MSG));
}

#[test]
fn bench_unix_value() {
    let w = World::new().file(BENCH_STATE, r#"{"lab":"/x.sock"}"#);
    let r = resolve(w, None, None, Some("lab"));
    assert_eq!(r.value, resolution_err("INVALID_BENCH_STATE", BENCH_CORRUPT_MSG));
}

#[test]
fn bench_state_is_dir() {
    let w = World::new().dir(BENCH_STATE);
    let r = resolve(w, None, None, Some("lab"));
    assert_eq!(
        r.value,
        resolution_err(
            "BENCH_STATE_IO_ERROR",
            "[Errno 21] Is a directory: '/sw/state/benches.state.json'",
        )
    );
}

#[test]
fn bench_bad_utf8() {
    let w = World::new().file_bytes(BENCH_STATE, &[0xff]);
    let r = resolve(w, None, None, Some("lab"));
    assert_eq!(
        r.value,
        resolution_err(
            "INVALID_BENCH_STATE",
            "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
        )
    );
}

// ---------------------------------------------------------------------------
// config.yaml 與 #108 dangling fallback
// ---------------------------------------------------------------------------

#[test]
fn config_alive() {
    let w = World::new()
        .file(USER_CFG, "socket_path: /cfg.sock\n")
        .probes(vec![probe_alive()]);
    let r = resolve(w, None, None, None);
    assert_eq!(r.value, ep("/cfg.sock", "config.yaml"));
}

#[test]
fn config_dangling_fallback() {
    let w = World::new()
        .file(USER_CFG, "socket_path: /cfg.sock\n")
        .probes(vec![probe_dead(), probe_alive()]);
    let r = resolve(w, None, None, None);
    assert_eq!(r.value, ep(SOCKET, "config.yaml -> canonical fallback"));
    assert_eq!(r.stderr, fallback_stderr("/cfg.sock", "on-demand", SOCKET));
}

#[test]
fn config_dangling_system() {
    let w = World::new()
        .file(
            USER_CFG,
            "socket_path: /cfg.sock\nsupervision_mode: systemd-system\n",
        )
        .probes(vec![probe_dead(), probe_alive()]);
    let r = resolve(w, None, None, None);
    assert_eq!(r.value, ep(SYSTEM_SOCKET, "config.yaml -> canonical fallback"));
    assert_eq!(
        r.stderr,
        fallback_stderr("/cfg.sock", "systemd-system", SYSTEM_SOCKET)
    );
}

#[test]
fn config_dangling_canonical_dead() {
    let w = World::new()
        .file(USER_CFG, "socket_path: /cfg.sock\n")
        .probes(vec![probe_dead(), probe_dead()]);
    let r = resolve(w, None, None, None);
    assert_eq!(r.value, ep("/cfg.sock", "config.yaml"));
    assert_eq!(r.stderr, "");
}

#[test]
fn config_dangling_same_value() {
    let w = World::new()
        .file(USER_CFG, &format!("socket_path: {SOCKET}\n"))
        .probes(vec![probe_dead()]);
    let r = resolve(w, None, None, None);
    assert_eq!(r.value, ep(SOCKET, "config.yaml"));
}

#[test]
fn config_tcp_not_probed_posix() {
    let w = World::new().file(USER_CFG, "socket_path: tcp://10.1.1.1:5\n");
    let r = resolve(w, None, None, None);
    assert_eq!(r.value, ep("tcp://10.1.1.1:5", "config.yaml"));
    assert_eq!(r.conn.connect_count(), 0);
}

#[test]
fn config_broken_yaml() {
    let w = World::new().file(USER_CFG, "a: [\n");
    let r = resolve(w, None, None, None);
    assert_eq!(r.value, ep(SOCKET, "default"));
}

#[test]
fn config_int_socket_path() {
    let w = World::new().file(USER_CFG, "socket_path: 5\n");
    let r = resolve(w, None, None, None);
    assert!(
        matches!(r.value, Err(ResolveFailure::Crash(_))),
        "非 str 的 truthy socket_path 應為 Crash，實際：{:?}",
        r.value
    );
}

#[test]
fn config_zero_socket_path() {
    let w = World::new().file(USER_CFG, "socket_path: 0\n");
    let r = resolve(w, None, None, None);
    assert_eq!(r.value, ep(SOCKET, "default"));
}

// ---------------------------------------------------------------------------
// SC-09：config.yaml 探索順序（SERIALWRAP_CONFIG > CONFIG_DIR > 系統檔 > CONFIG_DIR）
// ---------------------------------------------------------------------------

#[test]
fn sc09_explicit_config_env() {
    let w = World::new()
        .env("SERIALWRAP_CONFIG", "/sw/x.yaml")
        .file("/sw/x.yaml", "socket_path: /x.sock\n")
        .file(USER_CFG, "socket_path: /other.sock\n")
        .probes(vec![probe_alive()]);
    let r = resolve(w, None, None, None);
    assert_eq!(r.value, ep("/x.sock", "config.yaml"));
}

#[test]
fn sc09_explicit_missing_is_empty() {
    let w = World::new()
        .env("SERIALWRAP_CONFIG", "/sw/missing.yaml")
        .file(USER_CFG, "socket_path: /other.sock\n")
        .file("/etc/serialwrap/config.yaml", "socket_path: /other.sock\n");
    let r = resolve(w, None, None, None);
    assert_eq!(r.value, ep(SOCKET, "default"));
}

#[test]
fn sc09_explicit_blank_ignored() {
    let w = World::new()
        .env("SERIALWRAP_CONFIG", "  ")
        .file(USER_CFG, "socket_path: /u.sock\n")
        .probes(vec![probe_alive()]);
    let r = resolve(w, None, None, None);
    assert_eq!(r.value, ep("/u.sock", "config.yaml"));
}

#[test]
fn sc09_system_fallback() {
    let w = World::new()
        .system_dir("/sys-test/serialwrap")
        .file("/sys-test/serialwrap/config.yaml", "socket_path: /sys.sock\n")
        .probes(vec![probe_alive()]);
    let r = resolve(w, None, None, None);
    assert_eq!(r.value, ep("/sys.sock", "config.yaml"));
}

#[test]
fn sc09_user_beats_system() {
    let w = World::new()
        .file(USER_CFG, "socket_path: /u.sock\n")
        .file("/etc/serialwrap/config.yaml", "socket_path: /sys.sock\n")
        .probes(vec![probe_alive()]);
    let r = resolve(w, None, None, None);
    assert_eq!(r.value, ep("/u.sock", "config.yaml"));
}

// ---------------------------------------------------------------------------
// 後端選擇與預設 endpoint（Q-16、Q-17）
// ---------------------------------------------------------------------------

#[test]
fn win_backend_default_tcp() {
    let w = World::new().env("SERIALWRAP_RPC_BACKEND", "win");
    let r = run(w, |ctx| local_default_endpoint(ctx));
    assert_eq!(r.value, "tcp://127.0.0.1:48700");
}

#[test]
fn win_backend_custom_port() {
    let w = World::new()
        .env("SERIALWRAP_RPC_BACKEND", "win")
        .env("SERIALWRAP_TCP_PORT", "49001");
    let r = run(w, |ctx| local_default_endpoint(ctx));
    assert_eq!(r.value, "tcp://127.0.0.1:49001");
}

#[test]
fn posix_ignores_serialwrap_endpoint() {
    let w = World::new().env("SERIALWRAP_ENDPOINT", "tcp://x:1");
    let r = run(w, |ctx| local_default_endpoint(ctx));
    assert_eq!(r.value, SOCKET);
}

#[test]
fn bogus_backend_uses_platform() {
    let w = World::new().env("SERIALWRAP_RPC_BACKEND", "bsd");
    let r = run(w, |ctx| rpc_backend_is_win(ctx));
    assert!(!r.value);
}

#[test]
fn win_backend_probes_tcp() {
    let w = World::new()
        .env("SERIALWRAP_RPC_BACKEND", "win")
        .file(USER_CFG, "socket_path: tcp://127.0.0.1:1\n")
        .probes(vec![probe_dead(), probe_alive()]);
    let r = resolve(w, None, None, None);
    assert_eq!(
        r.value,
        ep("tcp://127.0.0.1:48700", "config.yaml -> canonical fallback")
    );
}

// ---------------------------------------------------------------------------
// endpoint_alive（01 §4.4，探測 0.2 s）
// ---------------------------------------------------------------------------

#[test]
fn no_af_unix_unix_dead() {
    let w = World::new()
        .no_af_unix()
        .file(USER_CFG, "socket_path: /cfg.sock\n");
    let r = run(w, |ctx| endpoint_alive(ctx, "/cfg.sock"));
    assert!(!r.value);
}

#[test]
fn empty_endpoint_alive_true() {
    let r = run(World::new(), |ctx| endpoint_alive(ctx, ""));
    assert!(r.value);
}

// ---------------------------------------------------------------------------
// 錯誤輸出（Q-02、Q-04）
// ---------------------------------------------------------------------------

#[test]
fn print_error_writes_stdout_json_and_stderr_line() {
    let e = EndpointResolutionError {
        code: code("BENCH_ENDPOINT_NOT_REMEMBERED"),
        message: BENCH_NOT_REMEMBERED_MSG.to_string(),
    };
    let r = run(World::new(), |ctx| {
        print_endpoint_resolution_error(ctx, &e, "session.list")
    });
    assert_eq!(r.value, 1);
    assert_eq!(
        r.stdout,
        concat!(
            r#"{"error_code":"BENCH_ENDPOINT_NOT_REMEMBERED","message":"bench \"lab\" 尚未記住 endpoint；請先執行 serialwrap connect lab","ok":false}"#,
            "\n"
        )
    );
    assert_eq!(
        r.stderr,
        "serialwrap: session.list failed: BENCH_ENDPOINT_NOT_REMEMBERED: bench \"lab\" 尚未記住 endpoint；請先執行 serialwrap connect lab\n"
    );
}

impl sw_pal::fs_ops::FsOps for TestFs {}
