//! `sw-cli`：Stage 1A 資料面 CLI（P3-T1 骨架；P3-T10～P3-T16 承接實作）。
//!
//! 只依賴 `sw-proto`＋`sw-pal`；不得連結 `sw-core`（arch §1.3）。
//! 行為尚未實作的函式本體一律 `todo!("P3-T<n>: <name>")`。
#![allow(unused_variables)]

pub mod argparse;
pub mod argv;
pub mod assets;
pub mod bench_state;
pub mod cli_spec;
pub mod cli_trace;
pub mod client;
pub mod commands;
pub mod ctx;
pub mod delegate;
pub mod endpoint_resolve;
pub mod output;
pub mod remote_tunnel;
// P4-T1 空殼：以下模組的實作由 Stage 1B 各 Task 承接（見 docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md）。
pub mod doctor_cmd;
pub mod graceful_stop;
pub mod install_layout;
pub(crate) mod legacy_handoff;
pub mod pairing;
pub mod rollback;
pub mod setup_cmd;
pub mod state_migrate;
pub mod supervisor_defs;
mod tree_digest;
pub mod wslconf;

use std::any::Any;
use std::cell::Cell;
use std::ffi::OsString;
use std::io::{self, Write};
use std::panic::{self, AssertUnwindSafe};
use std::rc::Rc;

use argparse::{ArgValue, ParseResult};
use cli_spec::{root_spec, SpecDefaults};
use ctx::{stdout_writer, CliCtx, TextIo};
use sw_proto::error_codes::ErrorCode;
use sw_proto::pypath::{HomeLookup, PathFlavor};
use sw_proto::runtime_paths::{EnvSnapshot, NativeTraits, PathsError, RuntimePaths};

/// CLI 版本；bundle 以同一個編譯輸入配對 CLI 與 daemon。
pub const CLI_VERSION: &str = match option_env!("SW_BUNDLE_VERSION") {
    Some(version) => version,
    None => env!("CARGO_PKG_VERSION"),
};

/// `cli.py` `_JSON_BOUNDARY_GLOBAL_OPTIONS`（逐字）。
const JSON_BOUNDARY_GLOBAL_OPTIONS: [&str; 5] = [
    "--socket",
    "--endpoint",
    "--bench",
    "--timeout",
    "--retries",
];
/// `cli.py` `_JSON_BOUNDARY_COMMANDS`（逐字）。
const JSON_BOUNDARY_COMMANDS: [&str; 2] = ["connect", "benches"];

struct PlatformHome<'a>(&'a dyn sw_pal::SysEnv);

impl HomeLookup for PlatformHome<'_> {
    fn current_user_home(&self) -> Option<String> {
        self.0.current_user_home()
    }
    fn user_home(&self, user: &str) -> Option<String> {
        self.0.user_home(user)
    }
}

/// 環境快照：Windows（`PathFlavor` 為 Windows）採大小寫不敏感語意（`os.environ`）。
fn env_snapshot(p: &sw_pal::ClientPlatform) -> EnvSnapshot {
    let pairs = p.env.vars();
    match p.caps.path_flavor {
        sw_pal::PalPathFlavor::Windows => EnvSnapshot::from_pairs_fold_upper(pairs),
        sw_pal::PalPathFlavor::Posix => EnvSnapshot::from_pairs(pairs),
    }
}

fn path_flavor(p: &sw_pal::ClientPlatform) -> PathFlavor {
    match p.caps.path_flavor {
        sw_pal::PalPathFlavor::Posix => PathFlavor::Posix,
        sw_pal::PalPathFlavor::Windows => PathFlavor::Windows,
    }
}

/// 主流程（arch §7.1、計畫 P3-T14）：回傳 exit code；stdout／stderr 由呼叫端提供。
///
/// 1. 建立環境快照與路徑；`SERIALWRAP_TCP_PORT` 非法 → stderr 一行、回傳 1（早於委派判斷）。
/// 2. `delegate::classify` 在任何 UTF-8 轉換之前對原始 `OsString` 執行。
/// 3. 原生路徑：每個 argv 片段以 [`argv::encode`] 轉為可逆字串（非 UTF-8 位元組跳脫）後解析；
///    路徑型值於 PAL 邊界以 [`argv::decode`] 還原原始 bytes，非路徑值的跳脫於送出（Q-19 (b)）或輸出（Q-19 (c)）前攔截。
pub fn run(
    argv: Vec<OsString>,
    p: &sw_pal::ClientPlatform,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let io = TextIo::new(out, err, p.caps.text_newline);
    let env = env_snapshot(p);
    let home = PlatformHome(p.env.as_ref());
    let native = NativeTraits {
        default_endpoint_is_tcp: p.caps.native_backend == sw_pal::BackendKind::Win,
        path_flavor: path_flavor(p),
    };
    let paths = match RuntimePaths::resolve(&env, native, &home) {
        Ok(paths) => paths,
        Err(PathsError::InvalidTcpPort { raw }) => {
            let mut io = io;
            let _ = io.err_str(&format!(
                "serialwrap: invalid SERIALWRAP_TCP_PORT: {}\n",
                sw_proto::pyfmt::py_repr_str(&raw)
            ));
            return 1;
        }
    };
    let mut ctx = CliCtx {
        p,
        env,
        paths,
        io,
        verbose: 0,
        cli_version: CLI_VERSION,
        autostart_lock: None,
    };

    let tail: &[OsString] = argv.get(1..).unwrap_or(&[]);
    let delegate_route = if let Some(test_group) = ctx.env.get("SERIALWRAP_TEST_DELEGATE_GROUP") {
        const CANDIDATES: [&'static str; 8] = [
            "daemon", "service", "supervision-mode", "remote", "connect", "benches", "setup",
            "doctor",
        ];
        if let Some(&matched) = CANDIDATES.iter().find(|g| **g == test_group) {
            delegate::classify_with_groups(tail, &[matched])
        } else {
            delegate::classify(tail)
        }
    } else {
        delegate::classify(tail)
    };
    if let delegate::Route::Delegate { group } = delegate_route {
        return delegate::run_delegate(&mut ctx, tail, group);
    }

    let spec = root_spec(&SpecDefaults {
        version_line: format!("serialwrap {CLI_VERSION}"),
        profile_dir: ctx.paths.profile_dir.clone(),
        lock_path: ctx.paths.lock_path.clone(),
    });
    let tokens: Vec<String> = tail
        .iter()
        .map(|token| argv::encode(token.as_os_str()))
        .collect();
    let width = argparse::help_width(
        ctx.env.get(sw_proto::constants::ENV_COLUMNS),
        ctx.p.env.terminal_columns(),
    );
    let ns = match argparse::parse_args(&spec, &tokens, width) {
        ParseResult::Ok(ns) => ns,
        ParseResult::Exit(request) => {
            let _ = ctx.io.out_str(&request.stdout);
            let _ = ctx
                .io
                .err_str(&sw_proto::pyfmt::py_display_argv(&request.stderr));
            if request.code == 2 {
                if let Some(cmd) = json_boundary_command(&tokens) {
                    let resp = output::error_resp(ErrorCode::InvalidArgs, None);
                    let _ = output::print_json(&mut ctx.io, &resp);
                    let _ = output::mirror_err(&mut ctx.io, &resp, Some(cmd));
                    return 1;
                }
            }
            return request.code;
        }
        ParseResult::Delegated => {
            let group = delegate::DELEGATED_GROUPS
                .iter()
                .copied()
                .find(|group| tokens.iter().any(|token| token == group))
                .unwrap_or("");
            return delegate::run_delegate(&mut ctx, tail, group);
        }
    };
    ctx.verbose = match ns.get("verbose") {
        ArgValue::Count(count) => *count,
        _ => 0,
    };
    commands::dispatch(&mut ctx, &ns)
}

/// 組裝點（P3-T14）：建立 stdout（依 TTY）／stderr，以 [`run_process_with`] 包住 [`run`]。
///
/// - `run` 期間 stdout 或 stderr 寫入失敗 → 1；`run` 正常回傳後最終 flush 失敗 → 120（Q-20，對齊直譯器結束 flush）。
/// - panic 或未攔截的 Python 等價例外 → stderr 一行 `serialwrap: internal error: <訊息>`、回傳 1。
pub fn run_process(argv: Vec<OsString>, p: &sw_pal::ClientPlatform) -> i32 {
    let out = stdout_writer(p.env.stdout_is_terminal());
    let err: Box<dyn Write> = Box::new(io::stderr());
    run_process_with(&|out, err| run(argv.clone(), p, out, err), out, err)
}

/// `cli.py:2457-2473` `_json_boundary_command_from_argv` 逐字對照（Q-08）。
pub fn json_boundary_command(argv: &[String]) -> Option<&'static str> {
    let mut index = 0;
    while index < argv.len() {
        let token = argv[index].as_str();
        let (option, has_equals, value) = match token.split_once('=') {
            Some((option, value)) => (option, true, value),
            None => (token, false, ""),
        };
        if JSON_BOUNDARY_GLOBAL_OPTIONS.contains(&option) {
            if has_equals {
                if let Some(command) = JSON_BOUNDARY_COMMANDS.iter().find(|c| **c == value) {
                    return Some(*command);
                }
            }
            if let Some(next) = argv.get(index + 1) {
                if let Some(command) = JSON_BOUNDARY_COMMANDS.iter().find(|c| **c == next.as_str())
                {
                    return Some(*command);
                }
            }
            index += if has_equals { 1 } else { 2 };
            continue;
        }
        if token.starts_with('-') {
            index += 1;
            continue;
        }
        return JSON_BOUNDARY_COMMANDS
            .iter()
            .find(|c| **c == token)
            .copied();
    }
    None
}

/// 寫入計數包裝：記錄寫入／flush 失敗，讓「呼叫端忽略錯誤」的路徑仍得到 Python 的 exit code。
struct Tracked {
    inner: Box<dyn Write>,
    failed: Rc<Cell<bool>>,
}

impl Write for Tracked {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let result = self.inner.write(buf);
        if result.is_err() {
            self.failed.set(true);
        }
        result
    }

    fn write_all(&mut self, buf: &[u8]) -> io::Result<()> {
        let result = self.inner.write_all(buf);
        if result.is_err() {
            self.failed.set(true);
        }
        result
    }

    fn flush(&mut self) -> io::Result<()> {
        let result = self.inner.flush();
        if result.is_err() {
            self.failed.set(true);
        }
        result
    }
}

fn panic_message(payload: &(dyn Any + Send)) -> String {
    if let Some(text) = payload.downcast_ref::<&str>() {
        return (*text).to_string();
    }
    if let Some(text) = payload.downcast_ref::<String>() {
        return text.clone();
    }
    "unknown panic".to_string()
}

/// 測試入口：以閉包取代 `run`，驗 panic → 1、寫入失敗 → 1、最終 flush 失敗 → 120。
#[doc(hidden)]
pub fn run_process_with(
    f: &dyn Fn(&mut dyn Write, &mut dyn Write) -> i32,
    out: Box<dyn Write>,
    err: Box<dyn Write>,
) -> i32 {
    let run_failed = Rc::new(Cell::new(false));
    let mut out = Tracked {
        inner: out,
        failed: Rc::clone(&run_failed),
    };
    let mut err = Tracked {
        inner: err,
        failed: Rc::clone(&run_failed),
    };
    let outcome = panic::catch_unwind(AssertUnwindSafe(|| f(&mut out, &mut err)));
    let code = match outcome {
        Ok(_) if run_failed.get() => 1,
        Ok(code) => code,
        Err(payload) => {
            let message = panic_message(payload.as_ref());
            let _ = err.write_all(format!("serialwrap: internal error: {message}\n").as_bytes());
            1
        }
    };
    if out.flush().is_err() || err.flush().is_err() {
        return 120;
    }
    code
}
