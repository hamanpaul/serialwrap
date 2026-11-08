//! `profile test` 的 regex 分類與未攔截例外順序回歸測試（P3-T15 審查修正）。
//!
//! - `(?L)` 是 Python `re.error`（回報於 `error` 欄位），不是未攔截的 ValueError。
//! - `(?a)(?u)` 才是未攔截的 ValueError（CLI 路徑 exit 1、stdout 空）。
//! - 五組檢查的求值順序與 Python dict 字面量相同（prompt → login → password → boot_done → bootloader）。

use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::Arc;

use sw_cli::commands::profile_cmd::profile_test_check;
use sw_pal::fakes::{FakeClock, FakeConn, FakeEnv, MemFs, RecordingProcess, ScriptedConnector};
use sw_pal::{
    BackendKind, ClientCaps, ClientPlatform, Clock, PalPathFlavor, RpcConnector, SkillPlatform,
};
use sw_proto::pyjson::CLI_STDOUT;

fn dumps(d: &sw_proto::pyjson::PyDict) -> String {
    sw_proto::pyjson::dumps(&d.clone().into(), &CLI_STDOUT)
}

#[test]
fn bad_inline_flag_l_is_reported_as_re_error() {
    assert_eq!(
        dumps(&profile_test_check("(?L)", "x")),
        r#"{"error":"bad inline flags: cannot use 'L' flag with a str pattern at position 3","matched":false,"matched_line":null,"pattern":"(?L)"}"#
    );
}

#[test]
fn ascii_and_unicode_flags_are_the_uncaught_value_error_case() {
    // 全函式 API 把未攔截例外也放進 error 欄位；CLI 路徑才會以 exit 1 結束（見下方 run 測試）。
    assert_eq!(
        dumps(&profile_test_check("(?a)(?u)", "x")),
        r#"{"error":"ASCII and UNICODE flags are incompatible","matched":false,"matched_line":null,"pattern":"(?a)(?u)"}"#
    );
}

fn platform(files: &[(&str, &[u8])], dirs: &[&str]) -> ClientPlatform {
    let fs = MemFs::default();
    {
        let mut map = fs.files.lock().unwrap_or_else(|e| e.into_inner());
        for (path, bytes) in files {
            map.insert(PathBuf::from(path), Ok(bytes.to_vec()));
        }
    }
    {
        let mut set = fs.dirs.lock().unwrap_or_else(|e| e.into_inner());
        for dir in dirs {
            set.insert(PathBuf::from(dir));
        }
    }
    let clock = Arc::new(FakeClock::default());
    let conn = Arc::new(ScriptedConnector::new(
        vec![FakeConn::Timeout],
        Arc::clone(&clock),
        true,
    ));
    ClientPlatform {
        env: Arc::new(FakeEnv::default()),
        fs: Arc::new(fs),
        clock: clock as Arc<dyn Clock>,
        rpc: conn as Arc<dyn RpcConnector>,
        process: Arc::new(RecordingProcess::default()),
        procs: sw_pal::fakes::FakeProcScan::new(Vec::new()),
        tunnel: sw_pal::fakes::FakeTunnel::new(false),
        service: sw_pal::fakes::FakeServiceCtl::new(Vec::new()),
        registry: sw_pal::fakes::FakeRegistry::new(Vec::new(), &[]),
        caps: ClientCaps {
            path_flavor: PalPathFlavor::Posix,
            af_unix: true,
            native_backend: BackendKind::Posix,
            default_skill: SkillPlatform::Linux,
            text_newline: "\n",
            exec_replace: true,
            on_demand_autostart: false,
            console_kind: sw_pal::ConsoleKind::Pty,
        },
    }
}

/// 以單一 YAML template 執行 `profile test`，回傳 (rc, stdout, stderr)。
fn run_profile(yaml: &str) -> (i32, String, String) {
    let p = platform(
        &[("/p/x.yaml", yaml.as_bytes()), ("/s.txt", b"boot\n=> \n")],
        &["/p"],
    );
    let argv: Vec<OsString> = [
        "serialwrap",
        "profile",
        "test",
        "--profile",
        "t",
        "--sample",
        "/s.txt",
        "--profile-dir",
        "/p",
    ]
    .iter()
    .map(OsString::from)
    .collect();
    let mut out = Vec::new();
    let mut err = Vec::new();
    let rc = sw_cli::run(argv, &p, &mut out, &mut err);
    (
        rc,
        String::from_utf8(out).unwrap(),
        String::from_utf8(err).unwrap(),
    )
}

#[test]
fn login_value_error_wins_over_later_bootloader_overflow() {
    // Python：prompt（`(?L)` 為 re.error，回報）→ login（ValueError，未攔截）先於 bootloader（OverflowError）拋出。
    let (rc, out, err) = run_profile(
        "profiles:\n  t:\n    platform: shell\n    prompt_regex: \"(?L)\"\n    login_regex: \"(?a)(?u)\"\n    password_regex: \"x\"\n    bootloader_prompts:\n      - \"a{4294967296}\"\n",
    );
    assert_eq!(rc, 1);
    assert_eq!(out, "");
    assert!(
        err.contains("ASCII and UNICODE flags are incompatible"),
        "err={err}"
    );
    assert!(!err.contains("repetition"), "err={err}");
}

#[test]
fn prompt_overflow_wins_over_later_login_value_error() {
    // Python：prompt 的 OverflowError 先於 login 的 ValueError 拋出。
    let (rc, out, err) = run_profile(
        "profiles:\n  t:\n    platform: shell\n    prompt_regex: \"a{4294967296}\"\n    login_regex: \"(?a)(?u)\"\n    password_regex: \"x\"\n",
    );
    assert_eq!(rc, 1);
    assert_eq!(out, "");
    assert!(
        err.contains("the repetition number is too large"),
        "err={err}"
    );
}

#[test]
fn reported_re_error_keeps_exit_zero_and_error_field() {
    let (rc, out, _) = run_profile(
        "profiles:\n  t:\n    platform: shell\n    prompt_regex: \"(?L)\"\n    login_regex: \"boot\"\n    password_regex: \"x\"\n    bootloader_prompts:\n      - \"(?L)\"\n",
    );
    assert_eq!(rc, 0);
    assert!(
        out.contains(
            "\"error\":\"bad inline flags: cannot use 'L' flag with a str pattern at position 3\""
        ),
        "out={out}"
    );
    assert!(
        out.contains("\"login_regex\":{\"error\":null,\"matched\":true,\"matched_line\":\"boot\""),
        "out={out}"
    );
}

#[test]
fn boot_done_regex_is_reported_like_python() {
    // #234：Python `cli.py` 在 password_regex 之後檢查 boot_done_regex；期望 stdout 取自 Python 基線實測。
    let (rc, out, err) = run_profile(
        "profiles:\n  t:\n    platform: shell\n    prompt_regex: \"x\"\n    login_regex: \"x\"\n    password_regex: \"x\"\n    boot_done_regex: \"oot\"\n",
    );
    assert_eq!((rc, err.as_str()), (0, ""));
    assert_eq!(
        out.trim_end(),
        "{\"checks\":{\"boot_done_regex\":{\"error\":null,\"matched\":true,\"matched_line\":\"boot\",\"pattern\":\"oot\"},\
\"bootloader_prompts\":[],\
\"login_regex\":{\"error\":null,\"matched\":false,\"matched_line\":null,\"pattern\":\"x\"},\
\"password_regex\":{\"error\":null,\"matched\":false,\"matched_line\":null,\"pattern\":\"x\"},\
\"prompt_regex\":{\"error\":null,\"matched\":false,\"matched_line\":null,\"pattern\":\"x\"}},\
\"ok\":true,\"platform\":\"shell\",\"profile\":\"t\",\"profile_dir\":\"/p\",\"sample\":\"/s.txt\"}"
    );
}

#[test]
fn boot_done_regex_unset_reports_empty_pattern() {
    let (rc, out, _) = run_profile("profiles:\n  t:\n    platform: shell\n");
    assert_eq!(rc, 0);
    assert!(
        out.contains("\"boot_done_regex\":{\"error\":null,\"matched\":false,\"matched_line\":null,\"pattern\":\"\"}"),
        "out={out}"
    );
}

#[test]
fn boot_done_value_error_wins_over_later_bootloader_overflow() {
    // Python：boot_done（ValueError，未攔截）先於 bootloader（OverflowError）拋出。
    let (rc, out, err) = run_profile(
        "profiles:\n  t:\n    platform: shell\n    prompt_regex: \"x\"\n    login_regex: \"x\"\n    password_regex: \"x\"\n    boot_done_regex: \"(?a)(?u)\"\n    bootloader_prompts:\n      - \"a{4294967296}\"\n",
    );
    assert_eq!((rc, out.as_str()), (1, ""));
    assert!(err.contains("ASCII and UNICODE flags are incompatible"), "err={err}");
    assert!(!err.contains("repetition"), "err={err}");
}

#[test]
fn password_value_error_wins_over_later_boot_done_overflow() {
    // Python：password（ValueError）先於 boot_done（OverflowError）拋出。
    let (rc, out, err) = run_profile(
        "profiles:\n  t:\n    platform: shell\n    prompt_regex: \"x\"\n    login_regex: \"x\"\n    password_regex: \"(?a)(?u)\"\n    boot_done_regex: \"a{4294967296}\"\n",
    );
    assert_eq!((rc, out.as_str()), (1, ""));
    assert!(err.contains("ASCII and UNICODE flags are incompatible"), "err={err}");
    assert!(!err.contains("repetition"), "err={err}");
}

#[test]
fn class_range_error_precedes_unterminated_and_later_escape() {
    // S1X-b-1：CPython 讀到 `a-b` 當下即檢查範圍；`bad character range` 先於其後的未封閉與跳脫錯誤，
    // `-` 後直接 EOF 仍為 unterminated（Python 3.12.3 `profile test` 實測文字）。
    let cases = [
        ("[z-a", "bad character range z-a at position 1"),
        ("[z-ab", "bad character range z-a at position 1"),
        (r"[z-a\A]", "bad character range z-a at position 1"),
        (r"[\d-z", r"bad character range \\d-z at position 1"),
        ("[z-", "unterminated character set at position 0"),
    ];
    for (pattern, error) in cases {
        let yaml = format!(
            "profiles:\n  t:\n    platform: shell\n    prompt_regex: '{pattern}'\n    login_regex: \"x\"\n    password_regex: \"x\"\n"
        );
        let (rc, out, err) = run_profile(&yaml);
        assert_eq!((rc, err.as_str()), (0, ""), "pattern={pattern} out={out}");
        let want = format!(
            "\"prompt_regex\":{{\"error\":\"{error}\",\"matched\":false,\"matched_line\":null,\"pattern\":\"{}\"}}",
            pattern.replace('\\', "\\\\")
        );
        assert!(
            out.contains(&want),
            "pattern={pattern}\nout={out}\nwant={want}"
        );
    }
}

#[test]
fn trailing_lone_backslash_error_precedes_earlier_token_errors() {
    // S1-2a65438-2、S1X-b-X1：CPython tokenizer 取走倒數第二個 token 時即預讀到結尾單獨的 `\`，
    // `bad escape (end of pattern)` 優先於該 token 之後才判定的錯誤（含 `(?x)` 註解內讀到結尾而原本通過者）；
    // 之前已判定者（`++\`）與偶數個反斜線結尾者不變（Python 3.12.3 `profile test` 實測文字）。
    let cases = [
        (r"[z-a\", "bad escape (end of pattern) at position 4"),
        (r"[\d-z\", "bad escape (end of pattern) at position 5"),
        (r"[z\A\", "bad escape (end of pattern) at position 4"),
        (r"a\8\", "bad escape (end of pattern) at position 3"),
        (r"(?P<\", "bad escape (end of pattern) at position 4"),
        (r"(?x)a#c\", "bad escape (end of pattern) at position 7"),
        (r"a{2,1}\", "bad escape (end of pattern) at position 6"),
        (r"++\", "nothing to repeat at position 0"),
        (r"[z-a\\]", "bad character range z-a at position 1"),
    ];
    for (pattern, error) in cases {
        let yaml = format!(
            "profiles:\n  t:\n    platform: shell\n    prompt_regex: '{pattern}'\n    login_regex: \"x\"\n    password_regex: \"x\"\n"
        );
        let (rc, out, err) = run_profile(&yaml);
        assert_eq!((rc, err.as_str()), (0, ""), "pattern={pattern} out={out}");
        let want = format!(
            "\"prompt_regex\":{{\"error\":\"{error}\",\"matched\":false,\"matched_line\":null,\"pattern\":\"{}\"}}",
            pattern.replace('\\', "\\\\")
        );
        assert!(
            out.contains(&want),
            "pattern={pattern}\nout={out}\nwant={want}"
        );
    }
}
