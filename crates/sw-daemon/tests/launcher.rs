//! `serialwrapd` launcher 測試（P4-T17；arch §6.1、§10.1、multi_open 規則 2）。
//!
//! - 平台分流只用 `sw_pal::native_client().caps.native_backend`（capability 值），本檔不使用 `cfg`
//!   （`check_cfg_boundary` 禁止 sw-daemon 的 tests 出現平台謂詞）。
//! - 原生 Windows 專用案例標 `#[ignore]`，於 Windows host 以 `-- --include-ignored` 執行（`win-test.sh` 的 `--` 之後）；
//!   Linux 上它們顯示為 ignored，不計為通過。
//! - 端到端案例把本測試的 `serialwrapd` 複製到暫存 bundle 佈局 `<root>/bin/serialwrapd`，manifest 放在
//!   `<root>/release-manifest.json`（launcher 以 `current_exe` 上兩層定位 manifest）。
//! - manifest 的 `entry` 相對路徑（不以 `-` 開頭、非絕對、含 `/`）以 manifest 目錄 join；絕對路徑與其餘元素原樣保留。

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use sw_daemon::launcher::{launcher_argv, LauncherError};
use sw_pal::proc_scan::{is_serialwrapd_argv, ArgvFlavor};
use sw_pal::BackendKind;
use sw_proto::release_manifest::parse_manifest;

const DAEMON_BIN: &str = env!("CARGO_BIN_EXE_serialwrapd");
const TAIL_UNICODE: &str = "/tmp/含 空白/p";

/// 暫存目錄（Drop 時刪除）；不新增 dev-dependency。
struct TempTree(PathBuf);

impl TempTree {
    fn new(tag: &str) -> TempTree {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let dir = std::env::temp_dir().join(format!(
            "swd-launcher-{tag}-{}-{nanos}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).expect("建立暫存目錄");
        TempTree(dir)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempTree {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn is_posix() -> bool {
    sw_pal::native_client().caps.native_backend == BackendKind::Posix
}

fn json_str(s: &str) -> String {
    let mut out = String::from("\"");
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// 最小合法 release manifest（arch §10.1；`components.python_cli_delegate.entry` 為必填）。
fn manifest_text(daemon_impl: &str, daemon_entry: &[&str]) -> String {
    let entry: Vec<String> = daemon_entry.iter().map(|s| json_str(s)).collect();
    format!(
        "{{\"schema_version\":1,\"bundle_version\":\"0.4.0\",\"stage\":\"1A\",\"target\":\"test\",\
\"git_revision\":\"{rev}\",\"components\":{{\
\"python_cli_delegate\":{{\"entry\":[\"py/bin/python3\",\"-I\",\"-B\",\"-m\",\"sw_core.cli\"]}},\
\"daemon\":{{\"implementation\":{impl_json},\"version\":\"0.4.0\",\"entry\":[{entry}]}}}}}}",
        rev = "a".repeat(40),
        impl_json = json_str(daemon_impl),
        entry = entry.join(","),
    )
}

/// 在 `<root>` 下建立 `bin/serialwrapd`（複製自本測試的 daemon binary）與 `release-manifest.json`。
fn stage_bundle(root: &Path, manifest: &str) -> PathBuf {
    let bin_dir = root.join("bin");
    std::fs::create_dir_all(&bin_dir).expect("建立 bin 目錄");
    let exe = bin_dir.join(format!("serialwrapd{}", std::env::consts::EXE_SUFFIX));
    std::fs::copy(DAEMON_BIN, &exe).expect("複製 serialwrapd 至暫存 bundle");
    std::fs::write(root.join("release-manifest.json"), manifest).expect("寫入 manifest");
    exe
}

fn powershell_path() -> String {
    let system_root = std::env::var("SystemRoot").unwrap_or_else(|_| "C:\\Windows".to_string());
    format!("{system_root}\\System32\\WindowsPowerShell\\v1.0\\powershell.exe")
}

/// 以 `tasklist /FI "PID eq <pid>"` 判定存活：輸出含該 pid 即存活（不使用 `os.kill(pid, 0)` 語意）。
fn tasklist_contains_pid(pid: u32) -> bool {
    let out = Command::new("tasklist")
        .args(["/FI", &format!("PID eq {pid}"), "/NH"])
        .output()
        .expect("執行 tasklist");
    String::from_utf8_lossy(&out.stdout).contains(&pid.to_string())
}

fn release_manifest_dir() -> PathBuf {
    std::env::temp_dir().join("swd-argv-root")
}

fn python_manifest() -> sw_proto::release_manifest::ReleaseManifest {
    parse_manifest(&manifest_text(
        "python",
        &["py/bin/python3", "-I", "-B", "py/serialwrapd.py"],
    ))
    .expect("python manifest 應可解析")
}

#[test]
fn launcher_argv_python_entry_absolute_and_tail_preserved() {
    let dir = release_manifest_dir();
    let m = python_manifest();
    let tail: Vec<OsString> = ["--socket", "/s", "--lock", "/l", "--profile-dir", TAIL_UNICODE]
        .iter()
        .map(|s| OsString::from(*s))
        .collect();

    let got = launcher_argv(&dir, &m, &tail).expect("python entry 應可組出 argv");

    let mut want: Vec<OsString> = vec![
        dir.join("py/bin/python3").into_os_string(),
        OsString::from("-I"),
        OsString::from("-B"),
        dir.join("py/serialwrapd.py").into_os_string(),
    ];
    want.extend(tail.iter().cloned());
    assert_eq!(got, want, "entry 相對路徑須轉為 manifest 目錄下的絕對路徑，argv 尾段原樣保留");
}

#[test]
fn launcher_argv_matches_multi_open_rule2() {
    let dir = release_manifest_dir();
    let m = python_manifest();
    let tail: Vec<OsString> = vec![OsString::from("--socket"), OsString::from("/s")];

    let got = launcher_argv(&dir, &m, &tail).expect("python entry 應可組出 argv");
    let parts: Vec<String> = got
        .iter()
        .map(|s| s.to_string_lossy().into_owned())
        .collect();

    assert!(
        is_serialwrapd_argv(&parts, ArgvFlavor::Posix),
        "launcher 組出的 argv 必須被 multi_open 規則 2（尾段 serialwrapd.py）辨識為 serialwrapd：{parts:?}"
    );
}

#[test]
fn launcher_rejects_rust_implementation_in_stage1() {
    let dir = release_manifest_dir();
    let rust_manifest = parse_manifest(&manifest_text("rust", &["bin/serialwrapd"]))
        .expect("rust manifest 應可解析");
    let err = launcher_argv(&dir, &rust_manifest, &[]).expect_err("Stage 1 不承接 implementation=rust");
    assert!(
        matches!(err, LauncherError::UnsupportedImplementation(_)),
        "錯誤種類必須為 UnsupportedImplementation：{err:?}"
    );

    // 端到端：stderr 恰一行 `serialwrapd: <原因>`、exit 2、stdout 不印任何 JSON。
    let root = TempTree::new("rust-impl");
    let exe = stage_bundle(root.path(), &manifest_text("rust", &["bin/serialwrapd"]));
    let out = Command::new(&exe)
        .stdin(Stdio::null())
        .output()
        .expect("執行 launcher");
    let stderr = String::from_utf8_lossy(&out.stderr).into_owned();
    assert_eq!(out.status.code(), Some(2), "exit code 必須為 2；stderr={stderr:?}");
    let lines: Vec<&str> = stderr.lines().collect();
    assert_eq!(lines.len(), 1, "stderr 必須恰一行：{stderr:?}");
    assert!(
        lines[0].starts_with("serialwrapd: "),
        "stderr 須以 `serialwrapd: ` 起頭：{stderr:?}"
    );
    assert!(
        out.stdout.is_empty(),
        "launcher 失敗不得在 stdout 印出 JSON：{:?}",
        String::from_utf8_lossy(&out.stdout)
    );
}

#[test]
fn launcher_posix_exec_replaces_process() {
    if !is_posix() {
        eprintln!("skip：POSIX execve 案例不適用於原生 Windows（以 capability 分流）");
        return;
    }
    // entry 為 `/bin/sh -c 'echo $$'`：execve 取代行程後，sh 的 pid 必須等於 launcher 的 pid。
    let root = TempTree::new("exec");
    let exe = stage_bundle(
        root.path(),
        &manifest_text("python", &["/bin/sh", "-c", "echo $$"]),
    );

    let child = Command::new(&exe)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("啟動 launcher");
    let launcher_pid = child.id();
    let out = child.wait_with_output().expect("等待 launcher 結束");
    let stderr = String::from_utf8_lossy(&out.stderr).into_owned();
    assert!(out.status.success(), "launcher 應成功結束；stderr={stderr:?}");

    let printed = String::from_utf8_lossy(&out.stdout).trim().to_string();
    let printed_pid: u32 = printed
        .parse()
        .unwrap_or_else(|_| panic!("stdout 應為 pid，實際 {printed:?}；stderr={stderr:?}"));
    assert_eq!(
        printed_pid, launcher_pid,
        "POSIX execve 應取代 launcher 行程：印出的 pid 必須等於 launcher 子行程 pid"
    );
}

#[test]
fn launcher_exit_code_passthrough() {
    let entry: Vec<String> = if is_posix() {
        vec!["/bin/sh".into(), "-c".into(), "exit 7".into()]
    } else {
        let comspec = std::env::var("ComSpec").unwrap_or_else(|_| "C:\\Windows\\System32\\cmd.exe".into());
        vec![comspec, "/c".into(), "exit 7".into()]
    };
    let refs: Vec<&str> = entry.iter().map(String::as_str).collect();
    let root = TempTree::new("exit7");
    let exe = stage_bundle(root.path(), &manifest_text("python", &refs));

    let out = Command::new(&exe)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .expect("執行 launcher");
    assert_eq!(
        out.status.code(),
        Some(7),
        "launcher 必須以子行程結束碼 7 結束；stderr={:?}",
        String::from_utf8_lossy(&out.stderr)
    );
}

#[test]
#[ignore = "原生 Windows 專用（Job Object kill-on-close）：於 Windows host 以 -- --include-ignored 執行"]
fn launcher_windows_parent_kill_kills_child() {
    let root = TempTree::new("winkill");
    let pid_file = root.path().join("child.pid");
    let ps = powershell_path();
    let script = format!(
        "Set-Content -LiteralPath '{}' -Value $PID; Start-Sleep -Seconds 600",
        pid_file.display()
    );
    let exe = stage_bundle(
        root.path(),
        &manifest_text("python", &[ps.as_str(), "-NoProfile", "-Command", script.as_str()]),
    );

    let mut child = Command::new(&exe)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("啟動 launcher");

    let deadline = Instant::now() + Duration::from_secs(30);
    let helper_pid: u32 = loop {
        if let Ok(text) = std::fs::read_to_string(&pid_file) {
            if let Ok(pid) = text.trim().parse::<u32>() {
                break pid;
            }
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            panic!("子行程 helper 未在 30 s 內寫出 pid");
        }
        std::thread::sleep(Duration::from_millis(200));
    };

    child.kill().expect("終止 launcher");
    let _ = child.wait();
    std::thread::sleep(Duration::from_secs(1));

    assert!(
        !tasklist_contains_pid(helper_pid),
        "launcher 被終止後 1 s 內 Python 子行程 {helper_pid} 仍存活（Job Object kill-on-close 未生效）"
    );
}

#[test]
#[ignore = "原生 Windows 專用（PE Subsystem 與無 console 啟動）：於 Windows host 以 -- --include-ignored 執行"]
fn launcher_windows_gui_subsystem() {
    // (a) PE Subsystem 必須為 Windows GUI（crate 屬性 windows_subsystem = "windows"）。
    let dump = Command::new("objdump")
        .arg("-p")
        .arg(DAEMON_BIN)
        .output()
        .expect("執行 objdump -p");
    let text = String::from_utf8_lossy(&dump.stdout).into_owned();
    assert!(
        text.lines()
            .any(|line| line.contains("Subsystem") && line.contains("Windows GUI")),
        "serialwrapd.exe 的 PE Subsystem 必須為 Windows GUI；objdump 輸出未含該欄位"
    );

    // (b) 從 windows-task 等價的無 console 情境啟動：子行程 GetConsoleWindow() 必須為 NULL（輸出 0）。
    let root = TempTree::new("wingui");
    let ps = powershell_path();
    let script = "$q=[char]34; $src='using System; using System.Runtime.InteropServices; public static class SwConsole { [DllImport(' + $q + 'kernel32.dll' + $q + ')] public static extern IntPtr GetConsoleWindow(); }'; Add-Type -TypeDefinition $src; [SwConsole]::GetConsoleWindow().ToInt64()";
    let exe = stage_bundle(
        root.path(),
        &manifest_text("python", &[ps.as_str(), "-NoProfile", "-Command", script]),
    );
    let out = Command::new(&exe)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .expect("執行 launcher");
    assert_eq!(
        String::from_utf8_lossy(&out.stdout).trim(),
        "0",
        "無 console 啟動時子行程 GetConsoleWindow() 必須為 NULL；stderr={:?}",
        String::from_utf8_lossy(&out.stderr)
    );
}
