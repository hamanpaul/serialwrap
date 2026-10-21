//! P9-T16 審查 F1：退役守門的端到端測試（真實 `CliCtx` 與 `daemon start`，不經純函式替身）。
//!
//! - 竄改 manifest（退役標記＋注入 `components.daemon.runtime`，且 runtime 檔實際存在）：不得 spawn 任何 daemon，
//!   回 `DAEMON_BINARY_NOT_FOUND`（exit 2）。
//! - 合法退役 manifest 無 launcher：`DAEMON_BINARY_NOT_FOUND`，即使 runtime 檔存在。
//! - 合法退役 manifest 有 launcher：argv 只含 launcher，不含 `-I`／`sw_core.daemon`。
//! - 同層 serialwrapd 的平台後綴：POSIX 不命中 `.exe`，Windows 不命中無後綴檔、命中 `.exe`。
//!
//! 夾具沿用 P9 的 `tests/fixtures/`；P4 的 `mgmt_daemon.rs` 已涵蓋未退役 manifest 的 DF-05 順序，本檔不重複。

mod support;

use support::*;
use sw_proto::pyjson::PyValue;

const RETIRED_RUST: &str = include_str!("fixtures/stage3_retired_rust.json");
const RETIRED_RUNTIME_INJECTED: &str =
    include_str!("fixtures/stage3_retired_but_runtime_injected.json");

fn first_spawn_argv(t: &TestEnv) -> Vec<String> {
    t.process()
        .spawns()
        .first()
        .map(|spawn| spawn.0.clone())
        .expect("spawn 應已發生")
}

/// `daemon start` 的失敗 JSON 之 `error_code`。
fn error_code(out: &str) -> String {
    match json(out) {
        PyValue::Dict(dict) => match dict.get("error_code") {
            Some(PyValue::Str(code)) => code.clone(),
            other => panic!("error_code 缺失或非字串：{other:?}"),
        },
        other => panic!("輸出不是 JSON 物件：{other:?}"),
    }
}

#[test]
fn retired_manifest_with_injected_runtime_never_spawns_python() {
    let t0 = TestEnv::posix();
    let sock = t0.default_socket();
    let bundle = t0.bundle_dir();
    // 竄改者同時注入 runtime 欄位與實際的 runtime 檔；無 launcher、無同層 serialwrapd。
    t0.write_file(
        &bundle.join("release-manifest.json"),
        RETIRED_RUNTIME_INJECTED.as_bytes(),
    );
    t0.write_file(&bundle.join("py").join("python"), b"runtime");
    let t = t0
        .with_process()
        .with_rpc_after_spawn(&sock, &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 2, "{out}");
    assert_eq!(error_code(&out), "DAEMON_BINARY_NOT_FOUND", "{out}");
    assert!(
        t.process().spawns().is_empty(),
        "退役 manifest 不得 spawn 任何 daemon"
    );
}

#[test]
fn valid_retired_manifest_without_launcher_is_binary_not_found_even_with_runtime_file() {
    let t0 = TestEnv::posix();
    let sock = t0.default_socket();
    let bundle = t0.bundle_dir();
    t0.write_file(
        &bundle.join("release-manifest.json"),
        RETIRED_RUST.as_bytes(),
    );
    t0.write_file(&bundle.join("py").join("bin").join("python3"), b"runtime");
    let t = t0
        .with_process()
        .with_rpc_after_spawn(&sock, &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 2, "{out}");
    assert_eq!(error_code(&out), "DAEMON_BINARY_NOT_FOUND", "{out}");
    assert!(
        t.process().spawns().is_empty(),
        "退役 manifest 不得 spawn 任何 daemon"
    );
}

#[test]
fn valid_retired_manifest_uses_launcher_step_one_without_python_flags() {
    let t0 = TestEnv::posix();
    let sock = t0.default_socket();
    let bundle = t0.bundle_dir();
    t0.write_file(
        &bundle.join("release-manifest.json"),
        RETIRED_RUST.as_bytes(),
    );
    t0.write_file(&bundle.join("bin").join("serialwrapd"), b"launcher");
    let t = t0
        .with_process()
        .with_rpc_after_spawn(&sock, &["health.ping", "health.status"]);
    let (rc, out, _) = t.run(&["daemon", "start"]);
    assert_eq!(rc, 0, "{out}");
    let argv = first_spawn_argv(&t);
    assert_eq!(
        argv[0],
        bundle.join("bin/serialwrapd").display().to_string(),
        "{argv:?}"
    );
    assert!(
        !argv
            .iter()
            .any(|arg| arg == "-I" || arg == "sw_core.daemon"),
        "退役 manifest 的 launcher 路徑不得帶 Python 旗標：{argv:?}"
    );
}

#[test]
fn sibling_lookup_rejects_wrong_platform_suffix_posix() {
    let t = TestEnv::posix();
    t.write_file(
        &t.bundle_dir().join("bin").join("serialwrapd.exe"),
        b"wrong platform",
    );
    let found = t.with_cli_ctx(|ctx| sw_cli::commands::daemon_cmd::locate_daemon_argv(ctx));
    assert!(found.is_err(), "POSIX 不得命中 serialwrapd.exe：{found:?}");
}

#[test]
fn sibling_lookup_rejects_wrong_platform_suffix_windows() {
    let t = TestEnv::windows();
    t.write_file(
        &t.bundle_dir().join("bin").join("serialwrapd"),
        b"wrong platform",
    );
    let found = t.with_cli_ctx(|ctx| sw_cli::commands::daemon_cmd::locate_daemon_argv(ctx));
    assert!(
        found.is_err(),
        "Windows 不得命中無 .exe 的 serialwrapd：{found:?}"
    );
}

#[test]
fn sibling_lookup_accepts_exe_on_windows() {
    let t = TestEnv::windows();
    let exe = t.bundle_dir().join("bin").join("serialwrapd.exe");
    t.write_file(&exe, b"sibling");
    let argv = t
        .with_cli_ctx(|ctx| sw_cli::commands::daemon_cmd::locate_daemon_argv(ctx))
        .expect("Windows 同層 serialwrapd.exe 應命中");
    assert_eq!(argv.len(), 1, "{argv:?}");
}
