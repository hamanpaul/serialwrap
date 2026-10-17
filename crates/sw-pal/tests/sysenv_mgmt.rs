//! P4-T2：PAL `SysEnv` 管理面方法的紅燈測試（實作前應全數以 `not implemented: P4-T2` 失敗）。
//!
//! 執行：`cargo test -p sw-pal --features test-fakes --test sysenv_mgmt`。
//! 會改動行程環境（PATH／USER／PATHEXT）或會以 PATH 解析子行程的案例，全部持有 `ENV_LOCK`，
//! 避免平行測試互相污染；`EnvRestore` 在案例結束（含 panic）時還原被改動的變數。
use std::ffi::OsString;
use std::sync::{Mutex, MutexGuard};
use std::time::Duration;

use sw_pal::native_client;
use sw_pal::traits::PalErrorKind;

static ENV_LOCK: Mutex<()> = Mutex::new(());

fn env_guard() -> MutexGuard<'static, ()> {
    ENV_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// 案例結束時還原指定環境變數（含 panic 路徑）。
struct EnvRestore {
    saved: Vec<(&'static str, Option<OsString>)>,
}

impl EnvRestore {
    fn capture(keys: &[&'static str]) -> Self {
        Self {
            saved: keys
                .iter()
                .map(|key| (*key, std::env::var_os(key)))
                .collect(),
        }
    }
}

impl Drop for EnvRestore {
    fn drop(&mut self) {
        for (key, value) in &self.saved {
            match value {
                Some(value) => std::env::set_var(key, value),
                None => std::env::remove_var(key),
            }
        }
    }
}

fn argv(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|part| part.to_string()).collect()
}

/// Python 3.12 `subprocess.run(text=True)` 基準（編譯期嵌入，不依賴執行時工作目錄）。
const GOLDEN_RUN_DECODE: &str = include_str!("golden/run_decode.ndjson");

/// golden 檔中指定 case 的整行。
fn golden_line(case: &str) -> String {
    let needle = format!("\"case\":\"{case}\"");
    GOLDEN_RUN_DECODE
        .lines()
        .find(|line| line.contains(&needle))
        .unwrap_or_else(|| panic!("golden 缺少 case {case}"))
        .to_string()
}

/// 取出 ndjson 物件中字串欄位的解碼結果；欄位為 null 或不存在時回傳 None。
fn golden_str(line: &str, key: &str) -> Option<String> {
    let marker = format!("\"{key}\":\"");
    let start = line.find(&marker)? + marker.len();
    let mut out = String::new();
    let mut chars = line[start..].chars();
    while let Some(c) = chars.next() {
        match c {
            '"' => return Some(out),
            '\\' => match chars.next()? {
                'n' => out.push('\n'),
                'r' => out.push('\r'),
                't' => out.push('\t'),
                'b' => out.push('\u{8}'),
                'f' => out.push('\u{c}'),
                'u' => {
                    let hex: String = (0..4).map(|_| chars.next()).collect::<Option<String>>()?;
                    out.push(char::from_u32(u32::from_str_radix(&hex, 16).ok()?)?);
                }
                other => out.push(other),
            },
            other => out.push(other),
        }
    }
    None
}

#[cfg(unix)]
fn write_exe(dir: &std::path::Path, name: &str, mode: u32) {
    use std::os::unix::fs::PermissionsExt;
    let path = dir.join(name);
    std::fs::write(&path, b"#!/bin/sh\n").unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
}

// ── which／which_all ──────────────────────────────────────────────

#[cfg(unix)]
#[test]
fn which_all_dedups_in_path_order() {
    let _g = env_guard();
    let _restore = EnvRestore::capture(&["PATH"]);
    let root = tempfile::tempdir().unwrap();
    let a = root.path().join("a");
    let b = root.path().join("b");
    std::fs::create_dir_all(&a).unwrap();
    std::fs::create_dir_all(&b).unwrap();
    write_exe(&a, "serialwrap", 0o755);
    write_exe(&b, "serialwrap", 0o755);
    let path = std::env::join_paths([&a, &b, &a]).unwrap();
    std::env::set_var("PATH", path);

    let got = native_client().env.which_all("serialwrap");
    assert_eq!(got, vec![a.join("serialwrap"), b.join("serialwrap")]);
}

#[cfg(unix)]
#[test]
fn which_all_skips_non_executable_posix() {
    let _g = env_guard();
    let _restore = EnvRestore::capture(&["PATH"]);
    let root = tempfile::tempdir().unwrap();
    let a = root.path().join("a");
    let b = root.path().join("b");
    std::fs::create_dir_all(&a).unwrap();
    std::fs::create_dir_all(&b).unwrap();
    write_exe(&a, "serialwrap", 0o755);
    write_exe(&b, "serialwrap", 0o644);
    let path = std::env::join_paths([&a, &b]).unwrap();
    std::env::set_var("PATH", path);

    let got = native_client().env.which_all("serialwrap");
    assert_eq!(got, vec![a.join("serialwrap")]);
}

#[test]
fn which_all_empty_path_returns_empty() {
    let _g = env_guard();
    let _restore = EnvRestore::capture(&["PATH"]);
    std::env::set_var("PATH", "");

    assert!(native_client().env.which_all("serialwrap").is_empty());
}

#[cfg(windows)]
#[test]
fn which_all_pathext_windows() {
    let _g = env_guard();
    let _restore = EnvRestore::capture(&["PATH", "PATHEXT"]);
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("serialwrap.exe"), b"MZ").unwrap();
    std::env::set_var("PATH", dir.path());
    std::env::set_var("PATHEXT", ".EXE;.CMD");

    let expected = vec![dir.path().join("serialwrap.exe")];
    assert_eq!(native_client().env.which_all("serialwrap"), expected);
    // name 已含副檔名時不得再加副檔名（不得產生 serialwrap.exe.EXE）。
    assert_eq!(native_client().env.which_all("serialwrap.exe"), expected);
}

// ── run／run_raw ──────────────────────────────────────────────────

#[test]
fn run_timeout_returns_minus1_timeout() {
    let _g = env_guard();
    #[cfg(unix)]
    let cmd = argv(&["sleep", "5"]);
    #[cfg(windows)]
    let cmd = argv(&["ping", "-n", "6", "127.0.0.1"]);

    let out = native_client()
        .env
        .run(&cmd, Some(Duration::from_millis(200)))
        .unwrap();
    assert_eq!(out.rc, -1);
    assert_eq!(out.stdout, "");
    assert_eq!(out.stderr, "TIMEOUT");
}

#[test]
fn run_missing_exe_is_notfound_with_py_message() {
    let _g = env_guard();
    let err = native_client()
        .env
        .run(&argv(&["no-such-exe-x"]), None)
        .expect_err("不存在的執行檔應回 Err");
    assert_eq!(err.kind, PalErrorKind::NotFound);
    #[cfg(unix)]
    assert_eq!(
        err.py_message,
        "[Errno 2] No such file or directory: 'no-such-exe-x'"
    );
    // Windows：FormatMessageW 的訊息依系統語系，只核對前綴。
    #[cfg(windows)]
    assert!(
        err.py_message.starts_with("[WinError 2] "),
        "{}",
        err.py_message
    );
}

#[cfg(unix)]
#[test]
fn run_universal_newlines_matches_python_golden() {
    let _g = env_guard();
    let line = golden_line("universal_newlines");
    let want = golden_str(&line, "stdout").expect("golden stdout");

    let out = native_client()
        .env
        .run(&argv(&["printf", r"a\r\nb\rc\n"]), None)
        .unwrap();
    assert_eq!(out.stdout, want);
}

#[cfg(unix)]
#[test]
fn run_invalid_utf8_is_decode_error_like_python() {
    let _g = env_guard();
    let line = golden_line("invalid_utf8");
    let want = golden_str(&line, "decode_error").expect("golden decode_error");

    let err = native_client()
        .env
        .run(&argv(&["printf", r"\xff"]), None)
        .expect_err("非法 UTF-8 應為解碼錯誤");
    assert_eq!(err.kind, PalErrorKind::Io);
    assert_eq!(err.py_message, want);
}

#[cfg(windows)]
#[test]
fn run_windows_acp_decode_golden() {
    let _g = env_guard();
    let line = golden_line("windows_acp_echo");
    let want = golden_str(&line, "stdout").expect("golden stdout");

    let out = native_client()
        .env
        .run(&argv(&["cmd", "/c", "echo 中文"]), None)
        .unwrap();
    assert_eq!(out.stdout, want);
}

#[cfg(unix)]
#[test]
fn run_raw_replaces_env() {
    let _g = env_guard();
    let env: Vec<(OsString, OsString)> = vec![(OsString::from("A"), OsString::from("1"))];

    let out = native_client()
        .env
        .run_raw(&argv(&["env"]), Some(env.as_slice()), None)
        .unwrap();
    let text = String::from_utf8(out.stdout).unwrap();
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(lines, vec!["A=1"]);
}

#[cfg(unix)]
#[test]
fn run_raw_preserves_nul_bytes_and_crlf() {
    let _g = env_guard();

    let out = native_client()
        .env
        .run_raw(&argv(&["printf", "a\\0b\\r\\n"]), None, None)
        .unwrap();
    assert_eq!(out.stdout, b"a\0b\r\n".to_vec());
}

// ── user_identity ─────────────────────────────────────────────────

#[cfg(unix)]
#[test]
fn user_identity_posix_prefers_user_env() {
    let _g = env_guard();
    let _restore = EnvRestore::capture(&["USER"]);
    std::env::set_var("USER", "alice");

    assert_eq!(native_client().env.user_identity(), "alice");
}

#[cfg(windows)]
#[test]
fn user_identity_windows_domain_backslash_name() {
    let _g = env_guard();
    let identity = native_client().env.user_identity();
    assert!(identity.contains('\\'), "{identity}");
    let whoami = native_client()
        .env
        .run(&argv(&["whoami"]), None)
        .unwrap()
        .stdout;
    assert!(
        identity.eq_ignore_ascii_case(whoami.trim()),
        "{identity} vs {whoami}"
    );
}

// ── has_systemd（POSIX 測試建構子；需 test-fakes）────────────────

#[cfg(all(unix, feature = "test-fakes"))]
#[test]
fn has_systemd_false_when_dir_absent() {
    use sw_pal::sysenv::posix::SysEnvPosix;
    use sw_pal::SysEnv;

    let root = tempfile::tempdir().unwrap();
    let env = SysEnvPosix::with_root(root.path().to_path_buf());
    assert!(!env.has_systemd());

    std::fs::create_dir_all(root.path().join("run/systemd/system")).unwrap();
    assert!(env.has_systemd());
}
