//! `serialwrapd` daemon 本體組裝（arch §6.1、計畫 P5-T19）。
//!
//! 對照 Python `sw_core/daemon.py`（`build_parser`、`_run_async`、`_resolve_test_allowlist`、`_load_config_data`、
//! `_write_config_endpoint`）與 `sw_core/test_allowlist.py`、`sw_core/session_manager.py` 的 Stage 0 測試鉤子：
//!
//! - 啟動順序：argv → SC-19 allowlist 判定（零副作用，早於目錄、profile 與鎖）→ `ensure_runtime_dirs` →
//!   `load_profiles` → 實例鎖 → `state.json` 載入 → SessionManager → 初始掃描 → rank → update → bootstrap attach →
//!   watcher 啟動（DF-14）→ RPC bind → 寫 `config.yaml` 的 `socket_path`（SC-09，非致命）。
//! - 有序關機（SC-11）：停 server（等進行中的 handler）→ stopping gate → 停 watcher → drain 全部 session
//!   （含在途 attach，排隊命令 `FLUSHED_BY_SHUTDOWN`）→ WAL flush＋sync＋close → 釋放鎖；未收斂回 exit 1 並寫診斷。
//! - exit code：0（正常停機、`-h`）、1（未攔截 panic、停機未收斂、環境錯誤）、2（argv 錯誤、allowlist 拒絕、鎖衝突）、
//!   3（既有 `state.json` 讀取失敗）。

use std::collections::HashMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use sw_core::arbiter::CommandRegistry;
use sw_core::session_manager::{ManagerDeps, PersistObserver, SessionManager};
use sw_core::state_store::{load_state_with_warn, StateStore};
use sw_core::wal::{WalConfig, WalWriter};
use sw_pal::traits::{EndpointSpec, FsOps, InstanceLockError, ListenOptions, PalError, PalResult, SysEnv};
use sw_pal::{
    BackendKind, Capabilities, ConsoleKind, DeviceBackendKind, DeviceSelection, DeviceWatcher,
    PalPathFlavor, Platform, SkillPlatform,
};
use sw_proto::config_types::{load_profiles, ConfigError, ProfileFs, ProfileTemplate, RuntimeConfig};
use sw_proto::endpoint::Endpoint;
use sw_proto::pycompat::{decode_utf8_strict, py_strip, PyOsError};
use sw_proto::pyfmt::py_repr_str;
use sw_proto::pypath::{self, HomeLookup, PathFlavor};
use sw_proto::runtime_paths::{resolve_daemon_dirs, DaemonDirs, EnvSnapshot, NativeTraits, PathsError, RuntimePaths};

use crate::framing::RpcServer;
use crate::service::{Service, ServiceDeps, ServiceHandler};

pub use crate::service::TestAllowlist;

/// argparse `prog`。
const PROG: &str = "serialwrapd";
/// argparse `description`。
const DESCRIPTION: &str = "serialwrap daemon";
/// SC-19 隔離判定的三個目錄變數（依序檢查）。
const ISOLATION_ENV_VARS: [&str; 3] = ["SERIALWRAP_STATE_DIR", "SERIALWRAP_RUN_DIR", "SERIALWRAP_CONFIG_DIR"];
/// OD-11 測試裝置 allowlist 的 env。
const ENV_TEST_DEVICE_ALLOWLIST: &str = "SERIALWRAP_TEST_DEVICE_ALLOWLIST";
/// `test_allowlist.INVALID_CONFIG_REASON`。
const INVALID_CONFIG_REASON: &str = "invalid windows.test_device_allowlist";
/// Stage 0 測試鉤子（#3）：release 持久化落盤後、回覆前 `exit(137)`。
const ENV_TEST_KILL_AFTER_SAVE: &str = "SERIALWRAP_TEST_KILL_AFTER_SAVE";
/// Stage 0 測試鉤子（#4）：release 持久化 commit 前的暫停目錄。
const ENV_TEST_PAUSE_BEFORE_RELEASE_SAVE: &str = "SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE";
/// RPC server 停止時等待進行中 handler 的上限（SC-11 ①）。Python `server.stop()`（`wait_closed`）無上限地等
/// handler 收斂；Rust 以上限換取 SC-11 的 exit 1 診斷，但上限須涵蓋最長的有界 handler：READY 路徑的
/// `session.recover --force`＝救援鍵（`\x03`／`\x04` 各等 prompt，約 4 s）→ `_force_recover`（clear drain ≤3 s＋
/// 10 × 1 s 等 READY）≈ 17 s（審查 F87 Python 實測：登入型 DUT 不回 READY 時 handler 約 14 s 才回
/// FORCE_RECOVER_TIMEOUT，Python exit 0；舊值 10 s 使 Rust exit 1）。
const SERVER_STOP_BUDGET: Duration = Duration::from_secs(20);
/// drain 全部 session 的共用期限（Python `shutdown_drain(total_timeout_s=5.0)`）。
const DRAIN_BUDGET: Duration = Duration::from_secs(5);
/// DeviceWatcher 輪詢間隔（Python `DeviceWatcher(interval_s=1.0)`）。
const WATCHER_POLL: Duration = Duration::from_secs(1);
/// `parse_args` 以此 exit code 表示 `-h`（help 寫 stdout、exit 0）。
const HELP_EXIT: i32 = 0;

/// `parse_args` 的結果（Task 19 Produces）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DaemonArgs {
    pub profile_dir: PathBuf,
    pub socket: String,
    pub lock: PathBuf,
}

// ---------------------------------------------------------------------------
// argparse 等價（`build_parser()`：`--profile-dir`／`--socket`／`--lock`，含 `-h`）
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq)]
enum Opt {
    Help,
    ProfileDir,
    Socket,
    Lock,
}

const LONG_OPTS: [(&str, Opt); 4] = [
    ("--help", Opt::Help),
    ("--profile-dir", Opt::ProfileDir),
    ("--socket", Opt::Socket),
    ("--lock", Opt::Lock),
];

fn opt_display(o: Opt) -> &'static str {
    match o {
        Opt::Help => "-h/--help",
        Opt::ProfileDir => "--profile-dir",
        Opt::Socket => "--socket",
        Opt::Lock => "--lock",
    }
}

/// argparse `_negative_number_matcher`：`^-\d+$|^-\d*\.\d+$`。
fn looks_negative_number(s: &str) -> bool {
    let Some(rest) = s.strip_prefix('-') else {
        return false;
    };
    let digits = |t: &str| t.bytes().all(|b| b.is_ascii_digit());
    if !rest.is_empty() && digits(rest) {
        return true;
    }
    match rest.split_once('.') {
        Some((a, b)) => digits(a) && !b.is_empty() && digits(b),
        None => false,
    }
}

/// argparse `_parse_optional`：此 arg 是否為選項樣式（'O'）。
fn is_optional_like(arg: &str) -> bool {
    !(arg.is_empty() || !arg.starts_with('-') || arg == "-" || looks_negative_number(arg) || arg.contains(' '))
}

enum Match {
    Found(Opt, Option<String>),
    Ambiguous(String),
    Unknown,
}

/// 精確或唯一前綴比對（argparse `allow_abbrev=True`）；回 `(選項, explicit_arg)`。
fn match_option(arg: &str) -> Match {
    if arg == "-h" {
        return Match::Found(Opt::Help, None);
    }
    if let Some((_, o)) = LONG_OPTS.iter().find(|(s, _)| *s == arg) {
        return Match::Found(*o, None);
    }
    if let Some((name, value)) = arg.split_once('=') {
        if name == "-h" {
            return Match::Found(Opt::Help, Some(value.to_string()));
        }
        if let Some((_, o)) = LONG_OPTS.iter().find(|(s, _)| *s == name) {
            return Match::Found(*o, Some(value.to_string()));
        }
    }
    if arg.starts_with("--") {
        let (prefix, explicit) = match arg.split_once('=') {
            Some((p, v)) => (p, Some(v.to_string())),
            None => (arg, None),
        };
        let hits: Vec<&(&str, Opt)> = LONG_OPTS.iter().filter(|(s, _)| s.starts_with(prefix)).collect();
        return match hits.len() {
            0 => Match::Unknown,
            1 => Match::Found(hits[0].1, explicit),
            _ => {
                let names: Vec<&str> = hits.iter().map(|(s, _)| *s).collect();
                Match::Ambiguous(format!("ambiguous option: {prefix} could match {}", names.join(", ")))
            }
        };
    }
    Match::Unknown
}

/// `shutil.get_terminal_size().columns - 2`（`COLUMNS` 優先，其次 stdout 終端寬度，最後 80）。
fn help_width(env: &dyn SysEnv) -> usize {
    let cols = env
        .var("COLUMNS")
        .and_then(|v| v.trim().parse::<i64>().ok())
        .filter(|c| *c > 0)
        .map(|c| c as usize)
        .or_else(|| env.terminal_columns().map(usize::from).filter(|c| *c > 0))
        .unwrap_or(80);
    cols.saturating_sub(2)
}

/// argparse `HelpFormatter._format_usage`（含 `usage: ` 前綴與結尾換行）。
fn format_usage(width: usize) -> String {
    let prefix = "usage: ";
    let parts = ["[-h]", "[--profile-dir PROFILE_DIR]", "[--socket SOCKET]", "[--lock LOCK]"];
    let one_line = format!("{PROG} {}", parts.join(" "));
    if prefix.len() + one_line.len() <= width {
        return format!("{prefix}{one_line}\n");
    }
    let get_lines = |items: &[&str], indent: &str, with_prefix: bool| -> Vec<String> {
        let mut lines = Vec::new();
        let mut line: Vec<&str> = Vec::new();
        let indented = indent.len();
        let mut line_len = if with_prefix { prefix.len() - 1 } else { indented - 1 };
        for part in items {
            if line_len + 1 + part.len() > width && !line.is_empty() {
                lines.push(format!("{indent}{}", line.join(" ")));
                line.clear();
                line_len = indented - 1;
            }
            line.push(part);
            line_len += part.len() + 1;
        }
        if !line.is_empty() {
            lines.push(format!("{indent}{}", line.join(" ")));
        }
        if with_prefix {
            if let Some(first) = lines.first_mut() {
                *first = first[indent.len()..].to_string();
            }
        }
        lines
    };
    let lines: Vec<String> = if (prefix.len() + PROG.len()) as f64 <= 0.75 * width as f64 {
        let indent = " ".repeat(prefix.len() + PROG.len() + 1);
        let mut items = vec![PROG];
        items.extend_from_slice(&parts);
        get_lines(&items, &indent, true)
    } else {
        let indent = " ".repeat(prefix.len());
        let mut lines = vec![PROG.to_string()];
        lines.extend(get_lines(&parts, &indent, false));
        lines
    };
    format!("{prefix}{}\n", lines.join("\n"))
}

/// `textwrap.wrap(text, width)` 的單段簡化版（以空白斷詞）。
fn wrap_words(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut lines = Vec::new();
    let mut cur = String::new();
    for word in text.split_whitespace() {
        if !cur.is_empty() && cur.len() + 1 + word.len() > width {
            lines.push(std::mem::take(&mut cur));
        }
        if !cur.is_empty() {
            cur.push(' ');
        }
        cur.push_str(word);
    }
    if !cur.is_empty() {
        lines.push(cur);
    }
    lines
}

/// `parser.format_help()`（usage、description、options 區段）。
fn format_help(width: usize) -> String {
    let mut out = format_usage(width);
    out.push('\n');
    for line in wrap_words(DESCRIPTION, width.max(11)) {
        out.push_str(&line);
        out.push('\n');
    }
    out.push_str("\noptions:\n");
    let max_help_position = 24usize.min(width.saturating_sub(20).max(4));
    let action_max = "--profile-dir PROFILE_DIR".len() + 2;
    let help_position = (action_max + 2).min(max_help_position);
    let help_lines = wrap_words("show this help message and exit", width.saturating_sub(help_position).max(11));
    let invocation = "  -h, --help";
    if invocation.len() + 2 <= help_position {
        out.push_str(&format!("{invocation:<help_position$}{}\n", help_lines[0]));
    } else {
        out.push_str(&format!("{invocation}\n{}{}\n", " ".repeat(help_position), help_lines[0]));
    }
    for line in &help_lines[1..] {
        out.push_str(&format!("{}{line}\n", " ".repeat(help_position)));
    }
    out.push_str("  --profile-dir PROFILE_DIR\n  --socket SOCKET\n  --lock LOCK\n");
    out
}

fn usage_error(width: usize, msg: &str) -> (i32, String) {
    (2, format!("{}{PROG}: error: {msg}\n", format_usage(width)))
}

/// 解析 daemon 的 argv（Task 19 Produces）；錯誤回 `(exit code, stderr 訊息)`。
///
/// `-h`／`--help` 回 `(0, help 文字)`，呼叫端寫 stdout 並以 0 結束（argparse 行為）。
/// 預設值：`--profile-dir` = `PROFILE_DIR`、`--socket` = `DEFAULT_ENDPOINT`、`--lock` = `LOCK_PATH`。
/// usage 換行寬度為 argparse 預設的 78（`COLUMNS` 未設、非終端）；`run` 另依環境計算寬度。
pub fn parse_args(argv: &[String], paths: &RuntimePaths) -> Result<DaemonArgs, (i32, String)> {
    parse_args_width(argv, paths, 78)
}

fn parse_args_width(argv: &[String], paths: &RuntimePaths, width: usize) -> Result<DaemonArgs, (i32, String)> {
    let mut profile_dir = paths.profile_dir.clone();
    let mut socket = paths.default_endpoint.clone();
    let mut lock = paths.lock_path.clone();
    let mut extras: Vec<String> = Vec::new();
    let args: &[String] = argv.get(1..).unwrap_or(&[]);
    let mut i = 0;
    let mut only_positional = false;
    while i < args.len() {
        let arg = &args[i];
        i += 1;
        if only_positional {
            extras.push(arg.clone());
            continue;
        }
        if arg == "--" {
            only_positional = true;
            continue;
        }
        if !is_optional_like(arg) {
            extras.push(arg.clone());
            continue;
        }
        let (opt, explicit) = match match_option(arg) {
            Match::Found(o, e) => (o, e),
            Match::Ambiguous(msg) => return Err(usage_error(width, &msg)),
            Match::Unknown => {
                extras.push(arg.clone());
                continue;
            }
        };
        if opt == Opt::Help {
            if let Some(e) = explicit {
                return Err(usage_error(
                    width,
                    &format!("argument -h/--help: ignored explicit argument {}", py_repr_str(&e)),
                ));
            }
            return Err((HELP_EXIT, format_help(width)));
        }
        let value = match explicit {
            Some(v) => v,
            None => match args.get(i) {
                Some(v) if !is_optional_like(v) && v != "--" => {
                    i += 1;
                    v.clone()
                }
                _ => {
                    return Err(usage_error(
                        width,
                        &format!("argument {}: expected one argument", opt_display(opt)),
                    ))
                }
            },
        };
        match opt {
            Opt::ProfileDir => profile_dir = value,
            Opt::Socket => socket = value,
            Opt::Lock => lock = value,
            Opt::Help => {}
        }
    }
    if !extras.is_empty() {
        return Err(usage_error(width, &format!("unrecognized arguments: {}", extras.join(" "))));
    }
    Ok(DaemonArgs {
        profile_dir: PathBuf::from(profile_dir),
        socket,
        lock: PathBuf::from(lock),
    })
}

// ---------------------------------------------------------------------------
// 環境、路徑與 SC-19 allowlist
// ---------------------------------------------------------------------------

/// `RuntimePaths::resolve` 等的家目錄查詢（`SysEnv` 委派）。
struct SysEnvHome<'a>(&'a dyn SysEnv);

impl HomeLookup for SysEnvHome<'_> {
    fn current_user_home(&self) -> Option<String> {
        self.0.current_user_home()
    }
    fn user_home(&self, user: &str) -> Option<String> {
        self.0.user_home(user)
    }
}

fn flavor_of(caps: &Capabilities) -> PathFlavor {
    match caps.path_flavor {
        PalPathFlavor::Posix => PathFlavor::Posix,
        PalPathFlavor::Windows => PathFlavor::Windows,
    }
}

fn native_traits(caps: &Capabilities) -> NativeTraits {
    NativeTraits {
        default_endpoint_is_tcp: caps.native_backend == BackendKind::Win,
        path_flavor: flavor_of(caps),
    }
}

/// 環境快照：Windows 路徑風格採大小寫不敏感（`os.environ`）。
fn snapshot_of(caps: &Capabilities, pairs: Vec<(String, String)>) -> EnvSnapshot {
    match caps.path_flavor {
        PalPathFlavor::Windows => EnvSnapshot::from_pairs_fold_upper(pairs),
        PalPathFlavor::Posix => EnvSnapshot::from_pairs(pairs),
    }
}

/// 解析 `RuntimePaths`（GAP-X1：`SERIALWRAP_TCP_PORT` 非整數 → `Err`，訊息與 CLI 相同）。
fn resolve_paths(p: &Platform) -> Result<RuntimePaths, String> {
    let env = snapshot_of(&p.caps, p.env.vars());
    RuntimePaths::resolve(&env, native_traits(&p.caps), &SysEnvHome(&*p.env)).map_err(|e| match e {
        PathsError::InvalidTcpPort { raw } => {
            format!("serialwrap: invalid SERIALWRAP_TCP_PORT: {}\n", py_repr_str(&raw))
        }
    })
}

/// `default_isolation_paths()`：移除三個隔離變數後解析的預設路徑（HOME／XDG 照舊，每次重新讀取）。
fn isolation_defaults(env: &dyn SysEnv, caps: &Capabilities) -> Option<RuntimePaths> {
    let pairs: Vec<(String, String)> = env
        .vars()
        .into_iter()
        .filter(|(k, _)| {
            let key = match caps.path_flavor {
                PalPathFlavor::Windows => k.to_uppercase(),
                PalPathFlavor::Posix => k.clone(),
            };
            !ISOLATION_ENV_VARS.contains(&key.as_str())
        })
        .collect();
    RuntimePaths::resolve(&snapshot_of(caps, pairs), native_traits(caps), &SysEnvHome(env)).ok()
}

/// `os.path.normcase(os.path.abspath(os.path.expanduser(v)))`。
fn normalize_path(f: PathFlavor, raw: &str, env: &EnvSnapshot, home: &dyn HomeLookup) -> String {
    let expanded = pypath::expanduser(f, raw, env, home);
    let abs = if pypath::isabs(f, &expanded) {
        expanded
    } else {
        let cwd = std::env::current_dir()
            .map(|c| c.to_string_lossy().into_owned())
            .unwrap_or_default();
        pypath::join(f, &cwd, &[&expanded])
    };
    let norm = pypath::normpath(f, &abs);
    match f {
        PathFlavor::Windows => norm.replace('/', "\\").to_lowercase(),
        PathFlavor::Posix => norm,
    }
}

/// `isolation_reason`：依 STATE、RUN、CONFIG 順序回報第一個不符隔離條件的項目；全部符合回 None。
fn isolation_reason(env: &dyn SysEnv, caps: &Capabilities, defaults: &RuntimePaths) -> Option<String> {
    let snap = snapshot_of(caps, env.vars());
    let home = SysEnvHome(env);
    let f = flavor_of(caps);
    for var in ISOLATION_ENV_VARS {
        let raw = snap.get(var).unwrap_or("");
        if py_strip(raw).is_empty() {
            return Some(format!("not an isolated instance: {var} unset"));
        }
        let default = match var {
            "SERIALWRAP_STATE_DIR" => &defaults.state_dir,
            "SERIALWRAP_RUN_DIR" => &defaults.run_dir,
            _ => &defaults.config_dir,
        };
        // Python `_isolation_reason`：`raw = (env.get(var) or "").strip()` 後才正規化（審查 F79）。
        if normalize_path(f, py_strip(raw), &snap, &home) == normalize_path(f, default, &snap, &home) {
            return Some(format!("not an isolated instance: {var} equals default"));
        }
    }
    None
}

/// env allowlist：有非空白內容即採用（逗號分隔、strip、去空、保序去重；解析後為空亦不回退 config）。
fn env_allowlist(env: &dyn SysEnv) -> Option<Vec<String>> {
    let raw = env.var(ENV_TEST_DEVICE_ALLOWLIST)?;
    if py_strip(&raw).is_empty() {
        return None;
    }
    let mut coms: Vec<String> = Vec::new();
    for item in raw.split(',') {
        let item = py_strip(item).to_string();
        if !item.is_empty() && !coms.contains(&item) {
            coms.push(item);
        }
    }
    Some(coms)
}

fn allowlist_decision(enabled: bool, coms: Vec<String>, source: &'static str, reason: Option<String>) -> TestAllowlist {
    TestAllowlist {
        enabled,
        coms,
        source,
        rejected_reason: reason,
    }
}

fn resolve_allowlist_with_caps(
    env: &dyn SysEnv,
    caps: &Capabilities,
    cfg: Option<&RuntimeConfig>,
    defaults: &RuntimePaths,
) -> TestAllowlist {
    let (source, coms) = match env_allowlist(env) {
        Some(coms) => ("env", coms),
        None => match cfg.map_or(Ok(None), RuntimeConfig::windows_test_device_allowlist) {
            Err(()) => {
                return allowlist_decision(false, Vec::new(), "config", Some(INVALID_CONFIG_REASON.to_string()))
            }
            Ok(None) => return allowlist_decision(false, Vec::new(), "none", None),
            Ok(Some(coms)) => ("config", coms),
        },
    };
    if coms.is_empty() {
        return allowlist_decision(false, Vec::new(), "none", None);
    }
    match isolation_reason(env, caps, defaults) {
        Some(reason) => allowlist_decision(false, Vec::new(), source, Some(reason)),
        None => allowlist_decision(true, coms, source, None),
    }
}

/// `SERIALWRAP_TEST_DEVICE_ALLOWLIST`（env 優先）與 `windows.test_device_allowlist`（config）的解析（SC-19，P1 T13 規格 1～3 逐字）。
///
/// 路徑風格以 `defaults` 的分隔字元推斷（含 `\` → Windows，否則 POSIX）。
pub fn resolve_test_allowlist(env: &dyn SysEnv, cfg: &RuntimeConfig, defaults: &RuntimePaths) -> TestAllowlist {
    resolve_allowlist_with_caps(env, &caps_for_defaults(defaults), Some(cfg), defaults)
}

fn caps_for_defaults(defaults: &RuntimePaths) -> Capabilities {
    let win = defaults.state_dir.contains('\\');
    Capabilities {
        path_flavor: if win { PalPathFlavor::Windows } else { PalPathFlavor::Posix },
        af_unix: !win,
        native_backend: if win { BackendKind::Win } else { BackendKind::Posix },
        default_skill: if win { SkillPlatform::Windows } else { SkillPlatform::Linux },
        text_newline: if win { "\r\n" } else { "\n" },
        exec_replace: !win,
        on_demand_autostart: win,
        console_kind: if win { ConsoleKind::TcpTelnet } else { ConsoleKind::Pty },
        native_device: if win { DeviceBackendKind::Win } else { DeviceBackendKind::Posix },
        // P6：與 `sw_pal::native()` 的平台值一致（Windows 宣告 com0com console 可用）。
        com0com_console: win,
    }
}

/// `decide_on_unreadable_config`（`test_allowlist.py:131-150`，parity #47）。
fn decide_on_unreadable_config(env: &dyn SysEnv, caps: &Capabilities, defaults: &RuntimePaths, error: &str) -> TestAllowlist {
    if env_allowlist(env).is_some() {
        return resolve_allowlist_with_caps(env, caps, None, defaults);
    }
    if isolation_reason(env, caps, defaults).is_some() {
        return allowlist_decision(false, Vec::new(), "none", None);
    }
    allowlist_decision(false, Vec::new(), "config", Some(format!("invalid config.yaml: {error}")))
}

/// 系統唯讀設定檔路徑（`SYSTEM_CONFIG_PATH`；`SERIALWRAP_TEST_SYSTEM_CONFIG_PATH` 存在即採用，與 Python 相同含空字串）。
fn system_config_path(env: &EnvSnapshot, p: &Platform) -> String {
    match env.get("SERIALWRAP_TEST_SYSTEM_CONFIG_PATH") {
        Some(v) => v.to_string(),
        None => pypath::join(flavor_of(&p.caps), &p.env.system_config_dir(), &["config.yaml"]),
    }
}

/// SC-09：config 讀取路徑（`resolve_config_path(for_write=False)`：`SERIALWRAP_CONFIG` 原樣勝出，即使不存在 →
/// `CONFIG_DIR/config.yaml`（存在時）→ 系統檔（存在時）→ `CONFIG_DIR/config.yaml`）。
fn config_read_path(env: &EnvSnapshot, paths: &RuntimePaths, p: &Platform) -> String {
    let f = flavor_of(&p.caps);
    if let Some(v) = env.get("SERIALWRAP_CONFIG") {
        if !py_strip(v).is_empty() {
            return pypath::expanduser(f, v, env, &SysEnvHome(&*p.env));
        }
    }
    if p.fs.exists(Path::new(&paths.config_yaml)) {
        return paths.config_yaml.clone();
    }
    let system = system_config_path(env, p);
    if p.fs.exists(Path::new(&system)) {
        return system;
    }
    paths.config_yaml.clone()
}

/// SC-09：config 寫入路徑（`SERIALWRAP_CONFIG`（strip 非空、expanduser）> `CONFIG_DIR/config.yaml`；永不寫系統檔）。
fn config_write_path(env: &EnvSnapshot, paths: &RuntimePaths, f: PathFlavor, home: &dyn HomeLookup) -> String {
    if let Some(v) = env.get("SERIALWRAP_CONFIG") {
        if !py_strip(v).is_empty() {
            return pypath::expanduser(f, v, env, home);
        }
    }
    paths.config_yaml.clone()
}

/// `str(OSError)`：有 errno／winerror 時附上 `: '<filename>'`。
fn os_error_text(e: &PalError, path: &str) -> String {
    if e.os_code.is_some() || e.winerror.is_some() {
        format!("{}: {}", e.py_message, py_repr_str(path))
    } else {
        e.py_message.clone()
    }
}

/// OSError 的 Python 例外類別名（補 `py_exc_name` 未涵蓋的 EISDIR／ENOTDIR）。
fn os_exc_name(e: &PalError) -> &'static str {
    match e.os_code {
        Some(21) => "IsADirectoryError",
        Some(20) => "NotADirectoryError",
        _ => e.py_exc_name(),
    }
}

/// YAML 解析錯誤 → PyYAML 例外類別名（對照表 `contracts/golden/allowlist_unreadable_config.tsv` 由 Python 基線實測）。
fn yaml_exc_name(m: &str) -> &'static str {
    // 審查 F80：PyYAML 的 timestamp regex 不符 → `None.groupdict()` 的 AttributeError；未知 bool 值 → `bool_values[...]`
    // 的 KeyError；重複 anchor → ComposerError；不相容／重複的 %YAML 指令 → ParserError（Python 基線實測，見 golden）。
    if m.contains("recursion limit exceeded") {
        "RecursionError"
    } else if m.contains("unacceptable character") {
        "ReaderError"
    } else if m.contains("invalid timestamp format") {
        "AttributeError"
    } else if m.contains("invalid bool value") {
        "KeyError"
    } else if m.contains("incompatible or duplicate YAML directive") {
        "ParserError"
    } else if m.contains("expected a single document")
        || m.contains("found unknown anchor")
        || m.contains("found undefined alias")
        || m.contains("found duplicate anchor")
        || m.contains("alias expansion limit")
    {
        "ComposerError"
    } else if m.contains("could not determine a constructor")
        || m.contains("found unhashable key")
        || m.contains("expected a mapping")
        || m.contains("expected a single mapping item")
        || m.contains("base64")
        || m.contains("invalid padding")
    {
        "ConstructorError"
    } else if m.starts_with("invalid ") || m.ends_with(" out of range") {
        "ValueError"
    } else if m.contains("while parsing") || m.contains("did not find expected") {
        "ParserError"
    } else {
        "ScannerError"
    }
}

/// `_load_config_data()`：缺檔（或非一般檔）→ `(空, None)`；存在但讀取／UTF-8／YAML 失敗 → `(空, Some(<Python 例外類別名>))`。
fn load_config_for_allowlist(path: &str, fs: &dyn FsOps) -> (RuntimeConfig, Option<String>) {
    let empty = || RuntimeConfig::from_text(None).expect("空 config 必可建立");
    let p = Path::new(path);
    if !fs.exists(p) || fs.is_dir(p) {
        return (empty(), None);
    }
    let bytes = match fs.read(p) {
        Ok(b) => b,
        Err(e) => return (empty(), Some(os_exc_name(&e).to_string())),
    };
    let Ok(text) = decode_utf8_strict(&bytes) else {
        return (empty(), Some("UnicodeDecodeError".to_string()));
    };
    match RuntimeConfig::from_text(Some(&text)) {
        Ok(cfg) => (cfg, None),
        Err(ConfigError::Yaml(e)) => (empty(), Some(yaml_exc_name(&e.message).to_string())),
        Err(_) => (empty(), Some("ValueError".to_string())),
    }
}

/// `SERIALWRAP_{RPC,LOCK,DEVICE}_BACKEND` 合法性檢查（02 §2.1：不 strip、`or "auto"`、`.lower()`）；
/// 非法值或非本 build 的原生後端 → `Err`（Python 為 ValueError traceback，exit 1；訊息非契約，G5）。
/// 單一變數的檢查；呼叫點對齊 Python 第一次選擇該後端的位置（審查 F78）：DEVICE 於 `load_profiles`／
/// `no profiles loaded` 之後（`select_device_backend()`）、LOCK 於取鎖前（`_build_lock`）、RPC 於建 server 前
/// （`_build_server`，SessionManager 建構之後、任何 attach 之前）。
fn check_backend_env(env: &dyn SysEnv, caps: &Capabilities, name: &str) -> Result<(), String> {
    let native_win = caps.native_backend == BackendKind::Win;
    let raw = env.var(name).filter(|v| !v.is_empty()).unwrap_or_else(|| "auto".to_string());
    let mode = raw.to_lowercase();
    let is_win = match mode.as_str() {
        "posix" | "unix" | "termios" => false,
        "win" | "windows" | "win32" => true,
        "auto" => native_win,
        _ => return Err(format!("unsupported backend: {}", py_repr_str(&mode))),
    };
    if is_win != native_win {
        return Err(format!("backend {} is not available in this build", py_repr_str(&raw)));
    }
    Ok(())
}

/// CONFIG／STATE／RUN／WAL／EVENTS／EVENTS_RUNTIME 目錄建立（`ensure_runtime_dirs()`）。
fn ensure_runtime_dirs(paths: &RuntimePaths, dirs: &DaemonDirs, fs: &dyn FsOps) -> PalResult<()> {
    for d in [
        &paths.config_dir,
        &paths.state_dir,
        &paths.run_dir,
        &paths.wal_dir,
        &dirs.events_dir,
        &dirs.events_runtime_dir,
    ] {
        fs.mkdirs(Path::new(d))?;
    }
    Ok(())
}

/// Windows 原生：無 passthrough template 時追加 `windows-default-passthrough`（`ready_probe=""`，同受 SC-35）。
fn inject_windows_passthrough(templates: &mut Vec<ProfileTemplate>) {
    if templates.iter().any(|t| t.platform == "passthrough") {
        return;
    }
    templates.push(windows_default_passthrough());
}

/// `ProfileTemplate(profile_name="windows-default-passthrough", platform="passthrough", ready_probe="")`；
/// 其餘欄位取 `load_profiles` 對最小 template 的解析結果（與 dataclass 預設一致，避免重複硬寫預設）。
fn windows_default_passthrough() -> ProfileTemplate {
    struct OneFile;
    impl ProfileFs for OneFile {
        fn flavor(&self) -> PathFlavor {
            PathFlavor::Posix
        }
        fn is_dir(&self, p: &str) -> bool {
            p == "/builtin"
        }
        fn list_dir(&self, _p: &str) -> Result<Vec<String>, PyOsError> {
            Ok(vec!["builtin.yaml".to_string()])
        }
        fn read_bytes(&self, _p: &str) -> Result<Vec<u8>, PyOsError> {
            Ok(b"profiles:\n  windows-default-passthrough:\n    platform: passthrough\n    ready_probe: ''\n".to_vec())
        }
        fn expanduser(&self, p: &str) -> String {
            p.to_string()
        }
    }
    load_profiles("/builtin", &OneFile)
        .ok()
        .and_then(|l| l.templates.into_iter().next())
        .expect("內建 passthrough template 必可解析")
}

/// RPC listener 選項（基線 SC-08 撤回：mode `0o660`、`ensure_dir_mode` None；`SERIALWRAP_SOCKET_GROUP` 非空 → group）。
fn listen_options(env: &dyn SysEnv) -> ListenOptions {
    let group = env
        .var("SERIALWRAP_SOCKET_GROUP")
        .map(|g| py_strip(&g).to_string())
        .filter(|g| !g.is_empty());
    ListenOptions {
        mode: 0o660,
        group,
        ensure_dir_mode: None,
    }
}

/// sw-proto → sw-pal 的唯一端點轉換點（sw-pal 不依賴 sw-proto）。
fn to_endpoint_spec(e: &Endpoint) -> EndpointSpec {
    match e {
        Endpoint::Unix(path) => EndpointSpec::Unix(sw_pal::os_string_from_bytes(path.as_bytes())),
        Endpoint::Tcp { host, port } => EndpointSpec::Tcp {
            host: host.clone(),
            port: *port,
        },
    }
}

/// `load_profiles` 的檔案系統（經 PAL `FsOps`）。
struct DaemonProfileFs<'a> {
    fs: &'a dyn FsOps,
    flavor: PathFlavor,
    env: &'a EnvSnapshot,
    home: SysEnvHome<'a>,
}

fn pal_os_error(e: PalError, filename: &str) -> PyOsError {
    PyOsError {
        errno: e.os_code.map(i64::from),
        winerror: e.winerror.map(i64::from),
        strerror: Some(e.strerror),
        filename: Some(filename.to_string()),
        plain: None,
    }
}

impl ProfileFs for DaemonProfileFs<'_> {
    fn flavor(&self) -> PathFlavor {
        self.flavor
    }
    fn is_dir(&self, p: &str) -> bool {
        self.fs.is_dir(Path::new(p))
    }
    fn list_dir(&self, p: &str) -> Result<Vec<String>, PyOsError> {
        self.fs.list_dir(Path::new(p)).map_err(|e| pal_os_error(e, p))
    }
    fn read_bytes(&self, p: &str) -> Result<Vec<u8>, PyOsError> {
        self.fs.read(Path::new(p)).map_err(|e| pal_os_error(e, p))
    }
    fn expanduser(&self, p: &str) -> String {
        pypath::expanduser(self.flavor, p, self.env, &self.home)
    }
}

// ---------------------------------------------------------------------------
// Stage 0 測試鉤子（parity #3／#4／#68：產品 build 亦讀 env，僅隔離實例生效）
// ---------------------------------------------------------------------------

/// `_test_kill_after_save_hook`／`_test_release_persist_point` 的 Rust 承接（`session_manager.py:8010-8077`）。
struct Stage0Hooks {
    kill: bool,
    pause_dir: Option<String>,
    /// `isolation_reason(os.environ, default_isolation_paths())`（None＝隔離實例）。
    isolation: Option<String>,
    warn: Arc<dyn Fn(String) + Send + Sync>,
    pid: u32,
    fs: Arc<dyn FsOps>,
    /// test-fakes `DaemonHooks` 的 release commit 前後掛鉤（產品為 None）。
    before_release: Option<Arc<dyn Fn() + Send + Sync>>,
    after_release: Option<Arc<dyn Fn() + Send + Sync>>,
}

impl Stage0Hooks {
    /// `_test_kill_after_save_hook(release_kill_point)`。
    fn kill_after_save(&self, release_kill_point: bool) {
        if !self.kill {
            return;
        }
        if let Some(reason) = &self.isolation {
            (self.warn)(format!("SERIALWRAP_TEST_KILL_AFTER_SAVE=1 已忽略：{reason}（非隔離實例不得終止行程）"));
            return;
        }
        if !release_kill_point {
            return;
        }
        (self.warn)(
            "SERIALWRAP_TEST_KILL_AFTER_SAVE=1：state 已落盤，於回應前以 os._exit(137) 模擬程序被殺（隔離實例）"
                .to_string(),
        );
        std::process::exit(137);
    }

    /// `_test_release_persist_point()`：commit 前暫停點（寫 `arrived`、每 50 ms 輪詢 `go`、上限 600 s）。
    /// 建目錄或寫 `arrived` 失敗即回 `Err`（Python 例外穿出，release 持久化失敗；審查 F85）。
    fn release_persist_point(&self) -> PalResult<()> {
        let Some(raw) = &self.pause_dir else {
            return Ok(());
        };
        if let Some(reason) = &self.isolation {
            (self.warn)(format!("SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE 已忽略：{reason}（非隔離實例不得暫停）"));
            return Ok(());
        }
        let dir = Path::new(raw);
        // RPC 的 EXCEPTION message 為 Python `str(OSError)`：附上失敗的路徑（`os.makedirs` 為第一個不存在層級；
        // 審查 3afdc4f #4）。
        self.fs.mkdirs(dir).map_err(|e| {
            let mut failed = dir;
            while let Some(parent) = failed.parent().filter(|p| !p.as_os_str().is_empty() && !self.fs.exists(p)) {
                failed = parent;
            }
            with_filename(e, failed)
        })?;
        let arrived = dir.join("arrived");
        self.fs
            .write_file(&arrived, format!("pid={}\n", self.pid).as_bytes())
            .map_err(|e| with_filename(e, &arrived))?;
        let go = dir.join("go");
        let deadline = Instant::now() + Duration::from_secs(600);
        while !self.fs.exists(&go) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(50));
        }
        Ok(())
    }
}

/// `str(OSError)` 附檔名：`[Errno N] msg: '<path>'`（無 errno／winerror 時不附）。
fn with_filename(mut e: PalError, path: &Path) -> PalError {
    if e.os_code.is_some() || e.winerror.is_some() {
        e.py_message = format!("{}: {}", e.py_message, py_repr_str(&path.to_string_lossy()));
    }
    e
}

impl PersistObserver for Stage0Hooks {
    fn before_release_commit(&self) -> PalResult<()> {
        self.release_persist_point()?;
        if let Some(h) = &self.before_release {
            h();
        }
        Ok(())
    }
    fn after_commit(&self, release: bool) {
        if release {
            if let Some(h) = &self.after_release {
                h();
            }
        }
        self.kill_after_save(release);
    }
}

// ---------------------------------------------------------------------------
// 觀測階段與掛鉤
// ---------------------------------------------------------------------------

/// 啟動／停機階段（恆編譯；test-fakes 下轉為 `TraceEvent`）。產品 build 不讀取欄位（trace 掛鉤恆為 None）。
#[allow(dead_code)]
enum Stage {
    InitialScan,
    Rank,
    UpdateDevices,
    BootstrapAttach,
    WatcherStart,
    Bound,
    EndpointWritten,
    ServerStopped,
    ShutdownGate,
    WatcherStopped,
    SessionsDrained { flushed: Vec<(String, String)>, stalled: Vec<String> },
    WalShutdown { ok: bool },
    LockReleased,
}

type Hook = Arc<dyn Fn() + Send + Sync>;

/// 私有掛鉤（恆編譯、產品全為 None；test-fakes 的 `run_with_hooks` 由 `DaemonHooks` 轉入）。
#[derive(Default)]
struct Hooks {
    before_release_commit: Option<Hook>,
    after_release_commit: Option<Hook>,
    trace: Option<Box<dyn Fn(Stage) + Send + Sync>>,
}

impl Hooks {
    fn trace(&self, s: Stage) {
        if let Some(t) = &self.trace {
            t(s);
        }
    }
}

/// 產品入口：解析 argv、組裝 daemon、回傳 exit code（0／1／2／3）。未攔截 panic → 1（arch §6.1）。
pub fn run(argv: Vec<String>, platform: Platform) -> i32 {
    run_guarded(argv, platform, Hooks::default())
}

fn run_guarded(argv: Vec<String>, platform: Platform, hooks: Hooks) -> i32 {
    catch_unwind(AssertUnwindSafe(|| run_inner(argv, platform, &hooks))).unwrap_or(1)
}

fn run_inner(argv: Vec<String>, p: Platform, hooks: &Hooks) -> i32 {
    // main.rs 已在建立任何 thread 之前呼叫 p.signals.install()。
    let env = snapshot_of(&p.caps, p.env.vars());
    let paths = match resolve_paths(&p) {
        Ok(x) => x,
        Err(msg) => {
            eprint!("{msg}");
            return 1;
        }
    };
    let dirs = resolve_daemon_dirs(&env);
    let args = match parse_args_width(&argv, &paths, help_width(&*p.env)) {
        Ok(a) => a,
        Err((HELP_EXIT, help)) => {
            print!("{help}");
            return 0;
        }
        Err((code, err)) => {
            eprint!("{err}");
            return code;
        }
    };

    // SC-19：allowlist 判定必須最先執行——早於 ensure_runtime_dirs、profiles 載入與 lock（daemon.py:230-239，零副作用）（#46）。
    let Some(iso_defaults) = isolation_defaults(&*p.env, &p.caps) else {
        eprint!("{PROG}: 無法解析預設隔離路徑\n");
        return 1;
    };
    let (cfg, cfg_error) = load_config_for_allowlist(&config_read_path(&env, &paths, &p), &*p.fs); // #47
    let allow = match &cfg_error {
        None => resolve_allowlist_with_caps(&*p.env, &p.caps, Some(&cfg), &iso_defaults),
        Some(err) => decide_on_unreadable_config(&*p.env, &p.caps, &iso_defaults, err),
    };
    if let Some(reason) = &allow.rejected_reason {
        eprint!("{PROG}: TEST_DEVICE_ALLOWLIST_REJECTED: {reason}\n"); // 未建目錄、未取鎖
        return 2;
    }

    if let Err(e) = ensure_runtime_dirs(&paths, &dirs, &*p.fs) {
        eprintln!("{}", e.py_message);
        return 1;
    }
    let profile_fs = DaemonProfileFs {
        fs: &*p.fs,
        flavor: flavor_of(&p.caps),
        env: &env,
        home: SysEnvHome(&*p.env),
    };
    let mut load = match load_profiles(&args.profile_dir.to_string_lossy(), &profile_fs) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("{e}");
            return 1;
        }
    };
    if load.profiles.is_empty() && load.templates.is_empty() {
        eprint!("{PROG}: no profiles loaded\n");
    }
    if let Err(msg) = check_backend_env(&*p.env, &p.caps, "SERIALWRAP_DEVICE_BACKEND") {
        eprintln!("{PROG}: {msg}");
        return 1;
    }
    if p.caps.native_device == DeviceBackendKind::Win {
        inject_windows_passthrough(&mut load.templates);
    }
    let ep = match sw_proto::endpoint::parse_endpoint(&args.socket) {
        Ok(e) => to_endpoint_spec(&e),
        Err(e) => {
            eprintln!("{PROG}: {}", e.0);
            return 1;
        }
    };
    if let Err(msg) = check_backend_env(&*p.env, &p.caps, "SERIALWRAP_LOCK_BACKEND") {
        eprintln!("{PROG}: {msg}");
        return 1;
    }
    let lock = match p.instance_lock.acquire(&args.lock, &ep) {
        Ok(g) => g,
        Err(InstanceLockError::AlreadyRunning) => {
            eprint!("{PROG}: DAEMON_ALREADY_RUNNING\n");
            return 2;
        }
        Err(InstanceLockError::SocketUnavailable) => {
            eprint!("{PROG}: SOCKET_UNAVAILABLE\n");
            return 2;
        }
        Err(InstanceLockError::Io(e)) => {
            eprintln!("{}", e.py_message);
            return 1;
        }
    };

    // logging：WARNING 一律以 `%(message)s` 寫 stderr（GAP-G-01）。
    let warn: Arc<dyn Fn(String) + Send + Sync> = Arc::new(|m: String| eprintln!("{m}"));
    let state_path = PathBuf::from(&paths.state_dir).join("state.json");
    let loaded = match load_state_with_warn(&state_path, &*p.fs, &|m| eprintln!("{m}")) {
        Ok((s, _backup)) => s,
        Err(e) => {
            eprint!("{}", e.stderr_text());
            lock.release();
            return e.exit_code();
        }
    };
    let wal = Arc::new(WalWriter::open(
        WalConfig {
            dir: PathBuf::from(&paths.wal_dir),
            rotate_bytes: sw_proto::constants::DEFAULT_WAL_ROTATE_BYTES,
        },
        p.wal_fs.clone(),
        p.clock.clone(),
    ));
    let registry = CommandRegistry::new();
    let explicit_targets = load.profiles.len();
    let stage0 = Arc::new(Stage0Hooks {
        kill: p.env.var(ENV_TEST_KILL_AFTER_SAVE).is_some_and(|v| py_strip(&v) == "1"),
        pause_dir: p
            .env
            .var(ENV_TEST_PAUSE_BEFORE_RELEASE_SAVE)
            .map(|v| py_strip(&v).to_string())
            .filter(|v| !v.is_empty()),
        isolation: isolation_reason(&*p.env, &p.caps, &iso_defaults),
        warn: warn.clone(),
        pid: p.env.pid(),
        fs: p.fs.clone(),
        before_release: hooks.before_release_commit.clone(),
        after_release: hooks.after_release_commit.clone(),
    });
    let manager = SessionManager::new(ManagerDeps {
        serial: p.serial.clone(),
        uart_excl: p.uart_excl.clone(),
        console: p.console.clone(),
        caps: p.caps,
        wal: wal.clone(),
        registry: registry.clone(),
        state: StateStore::spawn(state_path, p.fs.clone()),
        loaded,
        load,
        clock: p.clock.clone(),
        env: p.env.clone(),
        fs: p.fs.clone(),
        warn: warn.clone(),
    });
    if let Some(msg) = manager.startup_error() {
        // Python `SessionManager.__init__` 的 `_save_state` 拋出未捕捉例外（孤立 surrogate 的 UnicodeEncodeError）：
        // 尚未掃描／attach、state.json 不動、rc 1（審查 47c7b25 #1）。
        eprint!("{}\n{msg}\n", sw_core::state_store::PY_TRACEBACK_HEAD);
        manager.begin_shutdown();
        let _ = manager.shutdown(DRAIN_BUDGET);
        let _ = wal.shutdown();
        lock.release();
        return 1;
    }
    // `SessionManager::new` 已完成建構子尾端的啟動存檔（Python `_save_state()`，其內呼叫一次 kill 鉤子(False)）。
    stage0.kill_after_save(false);
    manager.set_persist_observer(stage0);
    if let Err(msg) = check_backend_env(&*p.env, &p.caps, "SERIALWRAP_RPC_BACKEND") {
        // Python `_build_server` 於 service 建構後、`service.start()` 前拋出：尚未掃描／attach。
        eprintln!("{PROG}: {msg}");
        manager.begin_shutdown();
        let _ = manager.shutdown(DRAIN_BUDGET);
        let _ = wal.shutdown();
        lock.release();
        return 1;
    }

    let mut src = (p.devices)(DeviceSelection {
        by_id_dir: PathBuf::from(&dirs.by_id_dir),
        by_path_dir: PathBuf::from(&dirs.by_path_dir),
        exclude_coms: {
            // D-12：com0com Console-side pair 永不被列舉成受管 UART。
            let mut coms = cfg.windows_exclude_coms();
            for port in serial_console_excludes(&cfg, &p.caps, &|m: String| warn(m)) {
                if !coms.contains(&port) {
                    coms.push(port);
                }
            }
            coms
        },
        test_allowlist: allow.enabled.then(|| allow.coms.clone()),
    });
    hooks.trace(Stage::InitialScan);
    let initial = DeviceWatcher::initial_scan(&mut *src).unwrap_or_default(); // DF-14：先掃描
    hooks.trace(Stage::Rank);
    manager.prepare_dynamic_rank(&initial.iter().map(|(k, _)| k.clone()).collect::<Vec<_>>());
    hooks.trace(Stage::UpdateDevices);
    manager.update_devices(&initial);
    hooks.trace(Stage::BootstrapAttach);
    manager.bootstrap_attach(); // P1 DF-14：bootstrap 在 watcher.start 之前
    hooks.trace(Stage::WatcherStart);
    let watcher = DeviceWatcher::start_with_baseline(src, WATCHER_POLL, manager.watcher_sender(), p.clock.clone(), initial);
    let svc = Arc::new(Service::new(ServiceDeps {
        manager: manager.clone(),
        wal: wal.clone(),
        registry: registry.clone(),
        procs: p.procs.clone(),
        version: sw_proto::version::version().to_string(),
        started_at: manager.now_iso(),
        explicit_target_count: explicit_targets,
        test_device_allowlist: allow.enabled.then(|| allow.clone()),
        pid: p.env.pid(),
    }));
    let listener = match p.rpc_listen.bind(&ep, &listen_options(&*p.env)) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("{}", e.py_message);
            manager.begin_shutdown();
            watcher.stop();
            let _ = manager.shutdown(DRAIN_BUDGET);
            let _ = wal.shutdown();
            lock.release();
            return 1;
        }
    };
    let (stop_tx, stop_rx) = crossbeam_channel::bounded(1);
    let server = RpcServer::start(listener, Arc::new(ServiceHandler::new(svc.clone(), stop_tx)));
    hooks.trace(Stage::Bound);
    write_endpoint(&env, &paths, &p, &args.socket);
    hooks.trace(Stage::EndpointWritten);

    p.signals.wait_stop(stop_rx);

    let mut incomplete: Vec<String> = Vec::new();
    svc.set_running(false);
    if !server.stop(SERVER_STOP_BUDGET) {
        incomplete.push("rpc handlers did not converge".to_string()); // SC-11 ① 停 server（等進行中的 handler 回應）
    }
    hooks.trace(Stage::ServerStopped);
    manager.begin_shutdown(); // ①b stopping gate（R1-F2）：之後熱插／bootstrap／recover 不再 open
    hooks.trace(Stage::ShutdownGate);
    watcher.stop(); // ② 停 watcher（drain 前，避免 drain 中觸發 attach）
    hooks.trace(Stage::WatcherStopped);
    let before: HashMap<String, Option<String>> = registry.error_codes().into_iter().collect();
    let drained = manager.shutdown(DRAIN_BUDGET); // ③ drain 全部 session（含在途 attach）、FLUSHED_BY_SHUTDOWN
    let flushed: Vec<(String, String)> = registry
        .error_codes()
        .into_iter()
        .filter_map(|(cmd_id, code)| {
            let code = code?;
            let newly = before.get(&cmd_id).is_none_or(|c| c.as_deref() != Some(code.as_str()));
            (code == "FLUSHED_BY_SHUTDOWN" && newly).then_some((cmd_id, code))
        })
        .collect();
    let stalled: Vec<String> = drained.iter().filter(|(_, ok)| !ok).map(|(s, _)| s.clone()).collect();
    hooks.trace(Stage::SessionsDrained {
        flushed,
        stalled: stalled.clone(),
    });
    if !stalled.is_empty() {
        // Python `service.stop()` 的 ERROR 文字（非優雅停止）。
        eprintln!(
            "serialwrapd: 有序關機逾時，未收斂 session={}（非優雅停止，SC-11 不計 PASS）",
            stalled.join(",")
        );
        incomplete.push(format!("drain stalled: {}", stalled.join(",")));
    }
    let wal_result = wal.shutdown(); // ④ WAL flush＋sync＋close
    hooks.trace(Stage::WalShutdown { ok: wal_result.is_ok() });
    if let Err(e) = wal_result {
        incomplete.push(format!("WAL sync failed: {}", e.py_message));
    }
    lock.release(); // ⑤
    hooks.trace(Stage::LockReleased);
    if incomplete.is_empty() {
        0
    } else {
        eprint!("{PROG}: shutdown incomplete: {}\n", incomplete.join("; "));
        1
    }
}

/// `_write_config_endpoint(endpoint)`：重新讀取 SC-09 讀取目標、更新 `socket_path`、寫入 SC-09 寫入目標
/// （mkdir parents、`yaml.safe_dump` 等價、非原子 KEEP）；失敗只寫 stderr（非致命）。
fn write_endpoint(env: &EnvSnapshot, paths: &RuntimePaths, p: &Platform, endpoint: &str) {
    let home = SysEnvHome(&*p.env);
    let read_path = config_read_path(env, paths, p);
    let write_path = config_write_path(env, paths, flavor_of(&p.caps), &home);
    let result = (|| -> Result<(), String> {
        let rp = Path::new(&read_path);
        let mut cfg = if p.fs.exists(rp) {
            let bytes = p.fs.read(rp).map_err(|e| os_error_text(&e, &read_path))?;
            let text = decode_utf8_strict(&bytes).map_err(|e| e.to_string())?;
            RuntimeConfig::from_text(Some(&text)).map_err(|e| e.to_string())?
        } else {
            RuntimeConfig::from_text(None).map_err(|e| e.to_string())?
        };
        cfg.set_socket(endpoint);
        let wp = PathBuf::from(&write_path);
        if let Some(parent) = wp.parent().filter(|d| !d.as_os_str().is_empty()) {
            p.fs.mkdirs(parent)
                .map_err(|e| os_error_text(&e, &parent.to_string_lossy()))?;
        }
        let text = cfg.to_yaml().replace('\n', p.caps.text_newline);
        p.fs.write_file(&wp, text.as_bytes()).map_err(|e| os_error_text(&e, &write_path))
    })();
    if let Err(msg) = result {
        eprint!("{PROG}: 寫入 config.yaml endpoint 失敗（非致命）: {msg}\n");
    }
}

// ---------------------------------------------------------------------------
// test-fakes：in-process 執行與觀測
// ---------------------------------------------------------------------------

/// 啟動／停機順序的觀測事件（僅 `test-fakes` 編譯；產品不產生副作用）。
///
/// 計畫原列的變體表缺 `ShutdownGate`，但 `sigterm_graceful_exit_0_and_flushes_queue` 的停機序列斷言需要它
/// （stopping gate 先於 watcher 停止，R1-F2），故於此補上。
#[cfg(feature = "test-fakes")]
#[derive(Debug, Clone, PartialEq)]
pub enum TraceEvent {
    InitialScan,
    Rank,
    UpdateDevices,
    BootstrapAttach,
    WatcherStart,
    Bound,
    EndpointWritten,
    ServerStopped,
    ShutdownGate,
    WatcherStopped,
    SessionsDrained { flushed: Vec<(String, String)>, stalled: Vec<String> },
    WalShutdown { ok: bool },
    LockReleased,
}

#[cfg(feature = "test-fakes")]
impl From<Stage> for TraceEvent {
    fn from(s: Stage) -> TraceEvent {
        match s {
            Stage::InitialScan => TraceEvent::InitialScan,
            Stage::Rank => TraceEvent::Rank,
            Stage::UpdateDevices => TraceEvent::UpdateDevices,
            Stage::BootstrapAttach => TraceEvent::BootstrapAttach,
            Stage::WatcherStart => TraceEvent::WatcherStart,
            Stage::Bound => TraceEvent::Bound,
            Stage::EndpointWritten => TraceEvent::EndpointWritten,
            Stage::ServerStopped => TraceEvent::ServerStopped,
            Stage::ShutdownGate => TraceEvent::ShutdownGate,
            Stage::WatcherStopped => TraceEvent::WatcherStopped,
            Stage::SessionsDrained { flushed, stalled } => TraceEvent::SessionsDrained { flushed, stalled },
            Stage::WalShutdown { ok } => TraceEvent::WalShutdown { ok },
            Stage::LockReleased => TraceEvent::LockReleased,
        }
    }
}

/// 測試掛鉤（僅 `test-fakes` 編譯）。
///
/// 兩個 commit hook 掛在 **release 持久化**的 commit 前後（與 Python Stage 0 鉤子 `_save_release_state` 同一點），
/// 而非 `StateStore` 的每一次 commit：Rust 與 Python 一樣在啟動時（建構子尾端）就寫一次 `state.json`，
/// 若掛在每次 commit，`restart_after_commit` 的子程序會在收到 release 請求前就於啟動存檔中止。
#[cfg(feature = "test-fakes")]
pub struct DaemonHooks {
    pub before_state_commit: Option<Box<dyn Fn() + Send + Sync>>,
    pub after_state_commit: Option<Box<dyn Fn() + Send + Sync>>,
    pub trace: Option<Box<dyn Fn(TraceEvent) + Send + Sync>>,
}

/// in-process 執行 daemon（僅 `test-fakes` 編譯）；語意與 `run` 相同（含未攔截 panic → 1），另接受測試掛鉤。
#[cfg(feature = "test-fakes")]
pub fn run_with_hooks(argv: Vec<String>, platform: Platform, hooks: DaemonHooks) -> i32 {
    let DaemonHooks {
        before_state_commit,
        after_state_commit,
        trace,
    } = hooks;
    let inner = Hooks {
        before_release_commit: before_state_commit.map(Arc::from),
        after_release_commit: after_state_commit.map(Arc::from),
        trace: trace.map(|t| -> Box<dyn Fn(Stage) + Send + Sync> { Box::new(move |s| t(TraceEvent::from(s))) }),
    };
    run_guarded(argv, platform, inner)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn usage_wraps_like_argparse() {
        // Python 3.12 實測（COLUMNS 未設＝80；COLUMNS=40）。
        assert_eq!(
            format_usage(78),
            "usage: serialwrapd [-h] [--profile-dir PROFILE_DIR] [--socket SOCKET]\n                   [--lock LOCK]\n"
        );
        assert_eq!(
            format_usage(38),
            "usage: serialwrapd [-h]\n                   [--profile-dir PROFILE_DIR]\n                   [--socket SOCKET]\n                   [--lock LOCK]\n"
        );
    }

    #[test]
    fn help_matches_argparse_at_default_width() {
        assert_eq!(
            format_help(78),
            "usage: serialwrapd [-h] [--profile-dir PROFILE_DIR] [--socket SOCKET]\n                   [--lock LOCK]\n\nserialwrap daemon\n\noptions:\n  -h, --help            show this help message and exit\n  --profile-dir PROFILE_DIR\n  --socket SOCKET\n  --lock LOCK\n"
        );
    }

    #[test]
    fn yaml_exception_names_follow_pyyaml_classes() {
        assert_eq!(yaml_exc_name("recursion limit exceeded"), "RecursionError");
        assert_eq!(yaml_exc_name("month out of range"), "ValueError");
        assert_eq!(yaml_exc_name("while parsing a flow sequence, expected ',' or ']'"), "ParserError");
        assert_eq!(yaml_exc_name("found character '\\t' that cannot start any token"), "ScannerError");
    }

    #[test]
    fn negative_numbers_are_values_not_options() {
        assert!(!is_optional_like("-1"));
        assert!(!is_optional_like("-.5"));
        assert!(is_optional_like("-x"));
        assert!(!is_optional_like("-"));
        assert!(!is_optional_like("-a b"));
    }
}

/// P6 D-12：`windows.serial_console` 的 console／peer 埠（只在 `caps.com0com_console` 時生效）。
pub fn serial_console_excludes(cfg: &RuntimeConfig, caps: &Capabilities, warn: &dyn Fn(String)) -> Vec<String> {
    let Some(parsed) = cfg.windows_serial_console() else {
        return Vec::new();
    };
    if !caps.com0com_console {
        warn("windows.serial_console 在此平台不支援，已忽略".to_string());
        return Vec::new();
    }
    let Ok(map) = parsed else {
        warn("windows.serial_console 格式不符（須為 <by_id>: {console_port, peer_port} 的 mapping），已忽略".to_string());
        return Vec::new();
    };
    let mut out: Vec<String> = Vec::new();
    for pair in map.0.values() {
        for port in [&pair.console_port, &pair.peer_port] {
            if !out.contains(port) {
                out.push(port.clone());
            }
        }
    }
    out
}
