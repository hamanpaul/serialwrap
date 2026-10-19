//! P4-T14 終端類承接（`session console-attach`／`console-detach` 的 RPC 參數、45 s 逾時下限、Windows
//! `console_endpoint` 原樣輸出）與委派集合清空（`DELEGATED_GROUPS`）的紅燈測試。
//!
//! 紅燈階段（TDD）：
//! - `delegated_groups_empty` 在 P3 委派集合仍為 7 群組時必須以斷言失敗（計畫 Task 14 步驟 3 尚未實作）。
//! - 承接類案例（console-attach 組包、逾時、passthrough）與 `delegate_mechanism_still_guards_recursion` 已在
//!   既有 P3 實作上實測 PASS；這類結果不得單獨當作紅燈證據，須附突變檢查（計畫 §0.1）。
//!
//! 替身：`TestEnv`（P4-T1，`tests/support/mod.rs`）與 `sw_pal::fakes`；本檔只使用既有公開 API，不引入新 stub。
//! 規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 14；盤點 `inventory/01-cli.md` §6.11。
mod support;

use std::ffi::OsString;
use std::sync::Arc;

use sw_cli::ctx::{CliCtx, TextIo};
use sw_cli::delegate::{run_delegate, DELEGATED_GROUPS};
use sw_pal::fakes::{fake_platform, FakeConn, FakeEnv, RecordingProcess, StreamStep::Reply};
use sw_pal::{ProcessCtl, SysEnv};
use sw_proto::pyjson::{dumps, RPC_WIRE};
use sw_proto::pypath::{HomeLookup, PathFlavor};
use sw_proto::runtime_paths::{EnvSnapshot, NativeTraits, RuntimePaths};
use support::{rpc_params, TestEnv};

/// `session.console_attach` 的成功回應（POSIX 形狀：vtty 為 PTY 路徑）。
const OK_ATTACH_POSIX: &str = r#"{"ok":true,"client_id":"c1","vtty":"/dev/pts/9"}"#;

/// `RuntimePaths::resolve` 所需的家目錄替身（與 `endpoint_resolve.rs` 相同用法）。
struct HomeStub;

impl HomeLookup for HomeStub {
    fn current_user_home(&self) -> Option<String> {
        Some("/home/t".to_string())
    }
    fn user_home(&self, _user: &str) -> Option<String> {
        None
    }
}

/// `session console-attach --label ""`：空字串 label 視為未提供，params 不得含 `label` 鍵（Q-24 `truthy_text`）。
#[test]
fn console_attach_label_omitted_when_empty() {
    let t = TestEnv::posix();
    let ep = t.default_socket();
    let t = t.with_rpc_ok(&ep, "session.console_attach", OK_ATTACH_POSIX);

    let (rc, _out, err) = t.run(&["session", "console-attach", "--selector", "COM0", "--label", ""]);

    assert_eq!(rc, 0, "stderr={err}");
    let params = rpc_params(&t, "session.console_attach");
    assert_eq!(dumps(&params, &RPC_WIRE), r#"{"selector":"COM0"}"#);
}

/// 無 `--timeout` 時 `session console-attach` 的 socket 逾時為 45.0 s（長逾時下限，計畫 Task 14 行為表）。
#[test]
fn console_attach_long_timeout_floor() {
    let (p, conn, _clock) = fake_platform(vec![FakeConn::stream(vec![Reply(
        OK_ATTACH_POSIX.as_bytes().to_vec(),
    )])]);
    let argv: Vec<OsString> = [
        "serialwrap",
        "session",
        "console-attach",
        "--selector",
        "COM0",
    ]
    .iter()
    .map(OsString::from)
    .collect();
    let mut out: Vec<u8> = Vec::new();
    let mut err: Vec<u8> = Vec::new();

    let rc = sw_cli::run(argv, &p, &mut out, &mut err);

    assert_eq!(rc, 0, "stderr={}", String::from_utf8_lossy(&err));
    assert_eq!(conn.timeouts_s(), vec![45.0]);
}

/// Windows daemon 回傳的 `console_endpoint` 原樣輸出（CLI 只傳遞，不改寫）。
#[test]
fn console_attach_windows_endpoint_passthrough() {
    let t = TestEnv::windows();
    let t = t.with_rpc_ok(
        TestEnv::win_endpoint(),
        "session.console_attach",
        r#"{"ok":true,"client_id":"c1","vtty":"","console_endpoint":"127.0.0.1:50123"}"#,
    );

    let (rc, out, err) = t.run(&["session", "console-attach", "--selector", "COM0"]);

    assert_eq!(rc, 0, "stderr={err}");
    assert!(
        out.contains(r#""console_endpoint":"127.0.0.1:50123""#),
        "stdout 應原樣含 console_endpoint：{out}"
    );
}

/// 委派集合在 Stage 1B 後必須為空（`[&str; 0]`）；P3 仍為 7 群組，本案例於紅燈階段失敗。
#[test]
fn delegated_groups_empty() {
    assert!(
        DELEGATED_GROUPS.is_empty(),
        "委派集合應已清空（Stage 1B），實際為 {DELEGATED_GROUPS:?}"
    );
}

/// 委派機制保留供緊急回退：`SERIALWRAP_DELEGATE_DEPTH=2` 時 `run_delegate` 必須拒絕執行並回報遞迴。
///
/// 集合清空後 `sw_cli::run` 不會再路由到委派，因此直接以群組名 `doctor` 呼叫 `run_delegate`（與
/// `DELEGATED_GROUPS` 內容無關），確認防護本身仍在。
#[test]
fn delegate_mechanism_still_guards_recursion() {
    let (mut p, _conn, _clock) = fake_platform(vec![]);
    let env = Arc::new(FakeEnv::default());
    env.vars
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .insert("SERIALWRAP_DELEGATE_DEPTH".to_string(), "2".to_string());
    let process = Arc::new(RecordingProcess::default());
    p.env = Arc::clone(&env) as Arc<dyn SysEnv>;
    p.process = Arc::clone(&process) as Arc<dyn ProcessCtl>;

    let env_snap = EnvSnapshot::from_pairs(vec![(
        "SERIALWRAP_DELEGATE_DEPTH".to_string(),
        "2".to_string(),
    )]);
    let paths = RuntimePaths::resolve(
        &env_snap,
        NativeTraits {
            default_endpoint_is_tcp: false,
            path_flavor: PathFlavor::Posix,
        },
        &HomeStub,
    )
    .expect("測試 env 應可解析路徑");
    let mut out: Vec<u8> = Vec::new();
    let mut err: Vec<u8> = Vec::new();

    let rc = {
        let mut ctx = CliCtx {
            p: &p,
            env: env_snap,
            paths,
            io: TextIo::new(&mut out, &mut err, "\n"),
            verbose: 0,
            cli_version: sw_proto::version::version(),
        };
        run_delegate(&mut ctx, &[OsString::from("doctor")], "doctor")
    };

    assert_eq!(rc, 1);
    assert!(
        String::from_utf8_lossy(&err).contains("delegation recursion detected"),
        "stderr 應含遞迴防護訊息：{}",
        String::from_utf8_lossy(&err)
    );
    let calls = process
        .calls
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    assert!(calls.is_empty(), "遞迴防護命中時不得執行子行程");
}
