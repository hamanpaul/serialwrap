//! `HandlerSpawner` conformance（P7 Task 14，SC-26）：POSIX 與 Windows 共用斷言；平台命令差異集中於本檔下方輔助函式。
//! Windows 原生執行列 LOCAL-WINDOWS-TODO（`scripts/rust/win-test.sh --package sw-pal --test handler_spawner`）。

use std::collections::BTreeSet;
use std::time::{Duration, Instant};

use crate::traits::{HandlerAbort, HandlerOutcome, HandlerSpawner, HandlerSpec};

fn is_windows(s: &dyn HandlerSpawner) -> bool {
    s.shell_argv("x")[0] == "cmd.exe"
}

/// 父行程環境中 `inherited_env_keys` 存在者（dispatcher `build_env` 的基底）。
pub fn minimal_env(s: &dyn HandlerSpawner) -> Vec<(String, String)> {
    s.inherited_env_keys()
        .iter()
        .filter_map(|k| std::env::var(k).ok().map(|v| (k.to_string(), v)))
        .collect()
}

pub fn unique_marker() -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or_default();
    format!("swp7m{}x{nanos}", std::process::id())
}

pub fn wait_until(mut f: impl FnMut() -> bool, limit: Duration) -> bool {
    let end = Instant::now() + limit;
    while Instant::now() < end {
        if f() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    f()
}

/// 測試期間設定父行程環境變數，結束時還原。
pub struct EnvSentinel {
    key: String,
    prev: Option<String>,
}

impl EnvSentinel {
    pub fn set(key: &str, value: &str) -> Self {
        let prev = std::env::var(key).ok();
        std::env::set_var(key, value);
        Self {
            key: key.into(),
            prev,
        }
    }
}

impl Drop for EnvSentinel {
    fn drop(&mut self) {
        match &self.prev {
            Some(v) => std::env::set_var(&self.key, v),
            None => std::env::remove_var(&self.key),
        }
    }
}

/// `env`／`set` 輸出的鍵集合。
pub fn parse_env_dump(out: &[u8]) -> BTreeSet<String> {
    String::from_utf8_lossy(out)
        .lines()
        .filter_map(|l| l.split_once('=').map(|(k, _)| k.trim().to_string()))
        .filter(|k| !k.is_empty())
        .collect()
}

/// shell 自行注入的環境鍵（不由 spec 決定）。
pub fn is_shell_injected(k: &str) -> bool {
    matches!(k, "PWD" | "SHLVL" | "_" | "PROMPT" | "OLDPWD")
}

fn echo_stdin_then_stderr_cmd(win: bool) -> &'static str {
    if win {
        r#"findstr /r "^" & echo ERR 1>&2 & exit /b 3"#
    } else {
        "cat; echo ERR 1>&2; exit 3"
    }
}

fn spawn_grandchild_sleep_cmd(win: bool, marker: &str) -> String {
    if win {
        format!("cmd.exe /d /c \"ping -n 60 127.0.0.1 >nul & rem {marker}\"")
    } else {
        format!("/bin/sh -c 'sleep 60; : {marker}' & wait")
    }
}

fn emit_1mib_cmd(win: bool) -> &'static str {
    if win {
        // 64 bytes／行（含 CRLF）× 16384 行 = 1 MiB。
        "for /l %i in (1,1,16384) do @echo 0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab"
    } else {
        "head -c 1048576 /dev/zero | tr '\\0' a"
    }
}

fn print_env_cmd(win: bool) -> &'static str {
    if win {
        "set"
    } else {
        "env"
    }
}

#[cfg(unix)]
pub fn process_with_marker_alive(marker: &str) -> bool {
    let Ok(dir) = std::fs::read_dir("/proc") else {
        return false;
    };
    dir.flatten().any(|e| {
        let name = e.file_name();
        let pid = name.to_string_lossy();
        if !pid.bytes().all(|b| b.is_ascii_digit()) {
            return false;
        }
        let cmd = std::fs::read(e.path().join("cmdline")).unwrap_or_default();
        let stat = std::fs::read_to_string(e.path().join("stat")).unwrap_or_default();
        // 殭屍（state Z）不算存活。
        let zombie = stat
            .rsplit_once(')')
            .is_some_and(|(_, rest)| rest.trim_start().starts_with('Z'));
        !zombie && String::from_utf8_lossy(&cmd).contains(marker)
    })
}

#[cfg(windows)]
pub fn process_with_marker_alive(marker: &str) -> bool {
    let script = format!(
        "if (Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{marker}*' -and $_.Name -ne 'powershell.exe' }}) {{ exit 0 }} else {{ exit 1 }}"
    );
    std::process::Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command", &script])
        .status()
        .is_ok_and(|s| s.success())
}

/// 可被繼承的探針：POSIX 為無 CLOEXEC 的 pipe 讀端 fd；Windows 為 `bInheritHandle=TRUE`、不共用寫入的暫存檔 handle。
pub struct ProbeHandle {
    #[cfg(unix)]
    fds: [libc::c_int; 2],
    #[cfg(windows)]
    handle: windows_sys::Win32::Foundation::HANDLE,
    #[cfg(windows)]
    path: std::path::PathBuf,
}

#[cfg(unix)]
pub fn open_inheritable_probe_handle() -> ProbeHandle {
    let mut fds = [0; 2];
    // SAFETY: fds 指向兩個可寫整數；不帶 O_CLOEXEC，刻意製造可被繼承的 fd。
    assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) }, 0);
    ProbeHandle { fds }
}

#[cfg(unix)]
impl Drop for ProbeHandle {
    fn drop(&mut self) {
        // SAFETY: 兩個 fd 由 pipe() 建立，只關閉一次。
        unsafe {
            libc::close(self.fds[0]);
            libc::close(self.fds[1]);
        }
    }
}

/// POSIX：子 shell 檢查 `/dev/fd/<n>` 不存在才 exit 0。
#[cfg(unix)]
pub fn probe_handle_argv(p: &ProbeHandle) -> Vec<String> {
    vec![
        "/bin/sh".into(),
        "-c".into(),
        format!(
            "if [ -e /dev/fd/{0} ]; then echo LEAKED {0}; exit 1; fi; exit 0",
            p.fds[0]
        ),
    ]
}

#[cfg(windows)]
pub fn open_inheritable_probe_handle() -> ProbeHandle {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Security::SECURITY_ATTRIBUTES;
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, FILE_SHARE_READ,
    };
    let path = std::env::temp_dir().join(format!("{}.probe", unique_marker()));
    let mut wide: Vec<u16> = path.as_os_str().encode_wide().collect();
    wide.push(0);
    let sa = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: std::ptr::null_mut(),
        bInheritHandle: 1,
    };
    // SAFETY: wide 為 NUL 結尾路徑；sa 在呼叫期間有效。
    let handle = unsafe {
        CreateFileW(
            wide.as_ptr(),
            0x4000_0000, // GENERIC_WRITE
            FILE_SHARE_READ,
            &sa,
            CREATE_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            std::ptr::null_mut(),
        )
    };
    ProbeHandle { handle, path }
}

#[cfg(windows)]
impl ProbeHandle {
    /// 父端關閉自己的 handle：若子行程未繼承，檔案即可被再次以寫入開啟。
    fn close_parent(&mut self) {
        if !self.handle.is_null() {
            // SAFETY: handle 由 CreateFileW 建立，只關閉一次。
            unsafe { windows_sys::Win32::Foundation::CloseHandle(self.handle) };
            self.handle = std::ptr::null_mut();
        }
    }
}

#[cfg(windows)]
impl Drop for ProbeHandle {
    fn drop(&mut self) {
        self.close_parent();
        let _ = std::fs::remove_file(&self.path);
    }
}

/// Windows：子 cmd 等 2 s（父端於 run 期間關閉 handle）後以附加寫入開啟探針檔；被繼承時共用衝突 → exit 1。
#[cfg(windows)]
pub fn probe_handle_argv(p: &ProbeHandle) -> Vec<String> {
    vec![
        "cmd.exe".into(),
        "/d".into(),
        "/s".into(),
        "/c".into(),
        format!(
            "ping -n 3 127.0.0.1 >nul & (echo x>>\"{}\") 2>nul && exit /b 0 || (echo LEAKED & exit /b 1)",
            p.path.display()
        ),
    ]
}

fn spec(
    argv: Vec<String>,
    env: Vec<(String, String)>,
    stdin: &[u8],
    timeout: Duration,
) -> HandlerSpec {
    HandlerSpec {
        argv,
        env,
        stdin: stdin.to_vec(),
        timeout,
        tail_bytes: 4096,
    }
}

pub fn completes_with_stdin_echo_and_tail(s: &dyn HandlerSpawner) {
    let win = is_windows(s);
    let sp = spec(
        s.shell_argv(echo_stdin_then_stderr_cmd(win)),
        minimal_env(s),
        b"{\"a\": 1}\n",
        Duration::from_secs(10),
    );
    match s.run(&sp, &HandlerAbort::new()) {
        HandlerOutcome::Completed {
            exit_code,
            stdout_tail,
            stderr_tail,
        } => {
            assert_eq!(exit_code, 3);
            assert!(String::from_utf8_lossy(&stdout_tail).contains("{\"a\": 1}"));
            assert!(String::from_utf8_lossy(&stderr_tail).contains("ERR"));
        }
        o => panic!("unexpected {o:?}"),
    }
}

pub fn timeout_kills_whole_tree(s: &dyn HandlerSpawner) {
    let marker = unique_marker();
    let win = is_windows(s);
    let sp = spec(
        s.shell_argv(&spawn_grandchild_sleep_cmd(win, &marker)),
        minimal_env(s),
        b"",
        Duration::from_millis(300),
    );
    let t0 = Instant::now();
    let out = s.run(&sp, &HandlerAbort::new());
    assert!(matches!(out, HandlerOutcome::TimedOut), "{out:?}");
    // 相對量測：逾時＋POSIX SIGTERM→SIGKILL 寬限 1.0 s＋排程餘裕 3 s（EDR 環境延遲由 B-08 實測，不在此收緊）。
    assert!(
        t0.elapsed() < Duration::from_millis(300) + Duration::from_secs(1) + Duration::from_secs(3)
    );
    assert!(
        wait_until(
            || !process_with_marker_alive(&marker),
            Duration::from_secs(5)
        ),
        "孫行程必須一併終止"
    );
}

pub fn abort_terminates_tree(s: &dyn HandlerSpawner) {
    let marker = unique_marker();
    let abort = HandlerAbort::new();
    let win = is_windows(s);
    let sp = spec(
        s.shell_argv(&spawn_grandchild_sleep_cmd(win, &marker)),
        minimal_env(s),
        b"",
        Duration::from_secs(60),
    );
    let a2 = abort.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(200));
        a2.trigger();
    });
    let t0 = Instant::now();
    let out = s.run(&sp, &abort);
    assert!(matches!(out, HandlerOutcome::TimedOut), "{out:?}");
    assert!(t0.elapsed() < Duration::from_secs(10), "abort 必須提前收尾");
    assert!(wait_until(
        || !process_with_marker_alive(&marker),
        Duration::from_secs(5)
    ));
}

pub fn stdout_tail_is_bounded(s: &dyn HandlerSpawner) {
    let win = is_windows(s);
    let sp = spec(
        s.shell_argv(emit_1mib_cmd(win)),
        minimal_env(s),
        b"",
        Duration::from_secs(20),
    );
    let out = s.run(&sp, &HandlerAbort::new());
    let HandlerOutcome::Completed { stdout_tail, .. } = out else {
        panic!("{out:?}")
    };
    assert_eq!(stdout_tail.len(), 4096);
}

pub fn spawn_failure_message_is_python_like(s: &dyn HandlerSpawner) {
    let sp = spec(
        vec!["sw-p7-nonexistent-binary".into()],
        minimal_env(s),
        b"",
        Duration::from_secs(1),
    );
    let out = s.run(&sp, &HandlerAbort::new());
    let HandlerOutcome::SpawnFailed(e) = out else {
        panic!("{out:?}")
    };
    if is_windows(s) {
        assert!(
            e.py_message.starts_with("[WinError 2] "),
            "{}",
            e.py_message
        );
    } else {
        // CPython 3.12：`[Errno 2] No such file or directory: 'sw-p7-nonexistent-binary'`。
        assert_eq!(
            e.py_message,
            "[Errno 2] No such file or directory: 'sw-p7-nonexistent-binary'"
        );
    }
}

pub fn env_is_cleared_except_spec(s: &dyn HandlerSpawner) {
    let win = is_windows(s);
    let _g = EnvSentinel::set("SW_P7_PARENT_SENTINEL", "leak");
    let mut env = minimal_env(s);
    env.push(("SW_P7_ALLOWED".into(), "1".into()));
    let mut sp = spec(
        s.shell_argv(print_env_cmd(win)),
        env.clone(),
        b"",
        Duration::from_secs(10),
    );
    sp.tail_bytes = 65536;
    let out = s.run(&sp, &HandlerAbort::new());
    let HandlerOutcome::Completed { stdout_tail, .. } = out else {
        panic!("{out:?}")
    };
    let keys = parse_env_dump(&stdout_tail);
    let has = |k: &str| {
        keys.iter().any(|x| {
            if win {
                x.eq_ignore_ascii_case(k)
            } else {
                x == k
            }
        })
    };
    assert!(!has("SW_P7_PARENT_SENTINEL"), "父環境不得滲入：{keys:?}");
    for (k, _) in &env {
        assert!(has(k) || is_shell_injected(k), "{k} 必須存在：{keys:?}");
    }
    // 陰性對照：把 sentinel 放進 spec.env 必須看得到（證明本測試能失敗）。
    let mut env2 = env;
    env2.push(("SW_P7_PARENT_SENTINEL".into(), "x".into()));
    let sp2 = HandlerSpec { env: env2, ..sp };
    let out = s.run(&sp2, &HandlerAbort::new());
    let HandlerOutcome::Completed { stdout_tail, .. } = out else {
        panic!("{out:?}")
    };
    assert!(parse_env_dump(&stdout_tail)
        .iter()
        .any(|k| k.eq_ignore_ascii_case("SW_P7_PARENT_SENTINEL")));
}

pub fn handler_inherits_only_stdio(s: &dyn HandlerSpawner) {
    #[allow(unused_mut)]
    let mut leaked = open_inheritable_probe_handle();
    let sp = spec(
        probe_handle_argv(&leaked),
        minimal_env(s),
        b"",
        Duration::from_secs(10),
    );
    #[cfg(windows)]
    let out = std::thread::scope(|scope| {
        let run = scope.spawn(|| s.run(&sp, &HandlerAbort::new()));
        std::thread::sleep(Duration::from_millis(500));
        leaked.close_parent();
        run.join().unwrap()
    });
    #[cfg(unix)]
    let out = s.run(&sp, &HandlerAbort::new());
    let HandlerOutcome::Completed {
        exit_code,
        stdout_tail,
        ..
    } = out
    else {
        panic!("{out:?}")
    };
    assert_eq!(exit_code, 0, "{}", String::from_utf8_lossy(&stdout_tail));
    drop(leaked);
}

/// 只在 `shell_argv[0] == "cmd.exe"` 時呼叫：`/s` 只剝最外層引號，內部引號原樣交給 cmd。
pub fn cmd_shell_preserves_inner_quotes(s: &dyn HandlerSpawner) {
    let sp = spec(
        s.shell_argv(r#"echo "a  b" & exit /b 3"#),
        minimal_env(s),
        b"",
        Duration::from_secs(10),
    );
    let out = s.run(&sp, &HandlerAbort::new());
    let HandlerOutcome::Completed {
        exit_code,
        stdout_tail,
        ..
    } = out
    else {
        panic!("{out:?}")
    };
    assert_eq!(exit_code, 3);
    assert!(String::from_utf8_lossy(&stdout_tail).contains("\"a  b\""));
}

/// POSIX：沒有 shebang 的 0755 檔案（審查 96b28dd_06c61d0 M1）。CPython `_posixsubprocess` 直接 `execve`，
/// 不做 glibc `execvp` 的 ENOEXEC→`/bin/sh` 後備：`[Errno 8] Exec format error: '<argv0>'`（PATH 查找時 argv0 原樣）。
#[cfg(unix)]
pub fn spawn_enoexec_is_python_like(s: &dyn HandlerSpawner) {
    use std::os::unix::fs::PermissionsExt;
    let dir = std::env::temp_dir().join(unique_marker());
    std::fs::create_dir_all(&dir).unwrap();
    let file = dir.join("noshebang");
    std::fs::write(&file, b"echo RAN-AS-SH\nexit 4\n").unwrap();
    std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o755)).unwrap();
    let abs = file.to_string_lossy().into_owned();
    let run = |argv: Vec<String>, env: Vec<(String, String)>| {
        s.run(
            &spec(argv, env, b"", Duration::from_secs(5)),
            &HandlerAbort::new(),
        )
    };
    let out = run(
        vec![abs.clone()],
        vec![("PATH".into(), "/usr/bin:/bin".into())],
    );
    let HandlerOutcome::SpawnFailed(e) = out else {
        panic!("{out:?}")
    };
    assert_eq!(
        e.py_message,
        format!("[Errno 8] Exec format error: '{abs}'")
    );
    let path = format!("/nonexist:{}", dir.display());
    let out = run(vec!["noshebang".into()], vec![("PATH".into(), path)]);
    let HandlerOutcome::SpawnFailed(e) = out else {
        panic!("{out:?}")
    };
    assert_eq!(e.py_message, "[Errno 8] Exec format error: 'noshebang'");
    // spec.env 無 PATH → `os.defpath`（`/bin:/usr/bin`）。
    let out = run(vec!["sw-p7-nonexistent-binary".into()], vec![]);
    let HandlerOutcome::SpawnFailed(e) = out else {
        panic!("{out:?}")
    };
    assert_eq!(
        e.py_message,
        "[Errno 2] No such file or directory: 'sw-p7-nonexistent-binary'"
    );
    let out = run(vec!["sh".into(), "-c".into(), "exit 5".into()], vec![]);
    assert!(
        matches!(out, HandlerOutcome::Completed { exit_code: 5, .. }),
        "defpath 找得到 /bin/sh：{out:?}"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

/// POSIX：子程序 environ 依 spec.env 的順序（Python 依 dict 插入序；審查 96b28dd_06c61d0 L2）。
#[cfg(unix)]
pub fn env_order_is_spec_order(s: &dyn HandlerSpawner) {
    let env: Vec<(String, String)> = vec![
        ("PATH".into(), "/usr/bin:/bin".into()),
        ("SERIALWRAP_EVENT_SCHEMA_VERSION".into(), "1".into()),
        ("SERIALWRAP_EVENT_A".into(), "a".into()),
        ("HOME".into(), "/h".into()),
    ];
    let out = s.run(
        &spec(
            vec!["/bin/sh".into(), "-c".into(), "cat /proc/$$/environ".into()],
            env.clone(),
            b"",
            Duration::from_secs(5),
        ),
        &HandlerAbort::new(),
    );
    let HandlerOutcome::Completed { stdout_tail, .. } = out else {
        panic!("{out:?}")
    };
    let want: Vec<u8> = env
        .iter()
        .flat_map(|(k, v)| format!("{k}={v}\0").into_bytes())
        .collect();
    assert_eq!(stdout_tail, want);
}

/// 期限溢位（審查 sol-backfill p7-20261028 #1）：Python `communicate(timeout=…)` 以 `OverflowError` 穿出——
/// 子行程已啟動且不被終止、`_run_handler` 不 emit。PAL 以非 OSError 的 `SpawnFailed(InvalidInput)` 回報，且不得
/// panic（修前 `start + Duration::MAX` 溢位 panic，worker 跳過 release 而死）。
pub fn huge_timeout_overflow_is_python_like(s: &dyn HandlerSpawner) {
    let marker = unique_marker();
    let win = is_windows(s);
    let cmd = if win {
        format!("ping -n 3 127.0.0.1 >nul & rem {marker}")
    } else {
        format!("sleep 2; : {marker}")
    };
    let sp = spec(s.shell_argv(&cmd), minimal_env(s), b"", Duration::MAX);
    let t0 = Instant::now();
    let out = s.run(&sp, &HandlerAbort::new());
    match &out {
        HandlerOutcome::SpawnFailed(e) => {
            assert_eq!(e.kind, crate::traits::PalErrorKind::InvalidInput, "{e:?}");
            assert!(e.os_code.is_none() && e.winerror.is_none(), "不得呈現為 OSError：{e:?}");
        }
        o => panic!("unexpected {o:?}"),
    }
    assert!(t0.elapsed() < Duration::from_millis(1500), "溢位須立即回報，不得等子行程");
    assert!(
        wait_until(|| process_with_marker_alive(&marker), Duration::from_secs(1)),
        "子行程已啟動且不被終止（Python Popen 成功後才溢位）"
    );
    assert!(
        wait_until(|| !process_with_marker_alive(&marker), Duration::from_secs(10)),
        "子行程自行結束"
    );
    // 未溢位的上限（POSIX INT_MAX ms）照常完成。
    let ok = spec(
        s.shell_argv("exit 0"),
        minimal_env(s),
        b"",
        Duration::from_millis(i32::MAX as u64),
    );
    assert!(
        matches!(s.run(&ok, &HandlerAbort::new()), HandlerOutcome::Completed { exit_code: 0, .. }),
        "INT_MAX ms 不溢位"
    );
}
