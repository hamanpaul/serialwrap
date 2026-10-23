#[cfg(unix)]
use std::ffi::OsString;
#[cfg(windows)]
use std::io::{BufRead, BufReader};
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
#[cfg(unix)]
use std::os::unix::net::UnixListener;
use std::{
    path::{Path, PathBuf},
    process::{Child, Command, ExitStatus, Stdio},
    sync::Arc,
    thread,
    time::{Duration, Instant},
};

mod common;

use sw_pal::{native, path_norm::normalize_for_compare, traits::EndpointSpec, *};

#[test]
#[cfg(feature = "test-fakes")]
fn lock_conformance_suite() {
    let d = tempfile::tempdir().unwrap();
    conformance::lock_suite(native().instance_lock, d.path());
}

#[cfg(unix)]
#[test]
fn second_acquire_same_lock_is_already_running_and_pid_written() {
    let d = tempfile::tempdir().unwrap();
    let lock = d.path().join("run/serialwrapd.lock");
    let ep = EndpointSpec::Unix(d.path().join("run/serialwrapd.sock").into_os_string());
    let f = native().instance_lock;
    let g = f.acquire(&lock, &ep).expect("first acquire");
    assert_eq!(
        std::fs::read_to_string(&lock).unwrap(),
        format!("{}\n", std::process::id())
    );
    assert!(matches!(
        f.acquire(&lock, &ep),
        Err(InstanceLockError::AlreadyRunning)
    ));
    g.release();
    assert!(lock.exists(), "lock file is never deleted");
}

#[cfg(unix)]
#[test]
fn live_socket_under_other_lock_path_is_already_running_even_via_unix_url() {
    let d = tempfile::tempdir().unwrap();
    std::fs::create_dir(d.path().join("x")).unwrap();
    let sock = d.path().join("s.sock");
    let _listener = UnixListener::bind(&sock).unwrap();
    let ep = EndpointSpec::Unix(OsString::from(format!(
        "unix://{}/x/../s.sock",
        d.path().display()
    )));
    let r = native()
        .instance_lock
        .acquire(&d.path().join("other.lock"), &ep);
    assert!(matches!(r, Err(InstanceLockError::AlreadyRunning)));
}

#[cfg(unix)]
#[test]
fn socket_probe_preserves_symlink_parent_resolution_order() {
    use std::os::unix::fs::symlink;

    let d = tempfile::tempdir().unwrap();
    let target_parent = d.path().join("other/nested");
    std::fs::create_dir_all(&target_parent).unwrap();
    symlink(&target_parent, d.path().join("link")).unwrap();
    let sock = d.path().join("other/s.sock");
    let _listener = UnixListener::bind(&sock).unwrap();
    let endpoint =
        EndpointSpec::Unix(format!("unix://{}/link/../s.sock", d.path().display()).into());

    assert!(matches!(
        native()
            .instance_lock
            .acquire(&d.path().join("other.lock"), &endpoint),
        Err(InstanceLockError::AlreadyRunning)
    ));
}

#[cfg(unix)]
#[test]
fn stale_socket_is_unlinked_then_acquired() {
    let d = tempfile::tempdir().unwrap();
    let sock = d.path().join("s.sock");
    drop(UnixListener::bind(&sock).unwrap());
    let g = native()
        .instance_lock
        .acquire(
            &d.path().join("l.lock"),
            &EndpointSpec::Unix(sock.clone().into_os_string()),
        )
        .unwrap();
    assert!(!sock.exists());
    g.release();
}

#[cfg(unix)]
#[test]
fn dangling_socket_symlink_is_not_unlinked() {
    use std::os::unix::fs::symlink;

    let d = tempfile::tempdir().unwrap();
    let sock = d.path().join("s.sock");
    symlink(d.path().join("missing-target"), &sock).unwrap();
    let guard = native()
        .instance_lock
        .acquire(
            &d.path().join("l.lock"),
            &EndpointSpec::Unix(sock.clone().into_os_string()),
        )
        .unwrap();
    assert!(std::fs::symlink_metadata(&sock)
        .unwrap()
        .file_type()
        .is_symlink());
    guard.release();
}

#[cfg(unix)]
#[test]
fn lock_path_alias_matches_python_home_expansion_and_unix_url() {
    const CHILD_ENV: &str = "S21_LOCK_PATH_CHILD";
    if std::env::var_os(CHILD_ENV).is_some() {
        let home = PathBuf::from(std::env::var_os("HOME").unwrap());
        let lock = home.join("run/serialwrapd.lock");
        let endpoint = EndpointSpec::Unix(home.join("run/serialwrapd.sock").into_os_string());
        let guard = native()
            .instance_lock
            .acquire(Path::new("unix://~/run/serialwrapd.lock"), &endpoint)
            .expect("鎖路徑應先去除 unix:// 並展開 ~");
        assert!(lock.exists(), "Rust 應建立 Python 基線的鎖檔路徑");

        let script = r#"
import sys
from sw_core.lock_posix import SingletonLock
try:
    SingletonLock(sys.argv[1], sys.argv[2]).acquire()
except RuntimeError as exc:
    if str(exc) == "DAEMON_ALREADY_RUNNING":
        raise SystemExit(0)
    raise
raise SystemExit(1)
"#;
        let repo = common::data_root().canonicalize().unwrap();
        let status = Command::new("python3")
            .args(["-c", script])
            .arg(&lock)
            .arg(home.join("run/serialwrapd.sock"))
            .env("PYTHONPATH", &repo)
            .current_dir(&repo)
            .status()
            .expect("啟動 Python 鎖基線");
        assert!(status.success(), "Python 應與 Rust 互斥：{status}");
        guard.release();
        return;
    }

    let d = tempfile::tempdir().unwrap();
    std::fs::set_permissions(d.path(), std::fs::Permissions::from_mode(0o777)).unwrap();
    let mut child = ChildGuard::spawn(
        Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "lock_path_alias_matches_python_home_expansion_and_unix_url",
                "--test-threads=1",
            ])
            .env(CHILD_ENV, "1")
            .env("HOME", d.path())
            .current_dir(d.path())
            .stdout(Stdio::null())
            .stderr(Stdio::null()),
    );
    let status = wait_child_with_timeout(&mut child.child, Duration::from_secs(10))
        .expect("路徑互斥子行程應於期限內完成");
    assert!(status.success(), "Rust／Python 路徑別名測試失敗：{status}");
}

#[cfg(unix)]
#[test]
fn socket_probe_eacces_is_socket_unavailable() {
    if std::env::var_os("S21_EACCES_CHILD").is_some() {
        let base = PathBuf::from(std::env::var_os("S21_EACCES_ROOT").unwrap());
        assert_socket_probe_eacces(&base);
        return;
    }

    let d = tempfile::tempdir().unwrap();
    if unsafe { libc::geteuid() } == 0 {
        std::fs::set_permissions(d.path(), std::fs::Permissions::from_mode(0o777)).unwrap();
        use std::os::unix::process::CommandExt;
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "socket_probe_eacces_is_socket_unavailable",
                "--test-threads=1",
            ])
            .env("S21_EACCES_CHILD", "1")
            .env("S21_EACCES_ROOT", d.path())
            .stdout(Stdio::null())
            .stderr(Stdio::inherit());
        // SAFETY: 僅在 root 測試程序的子行程中降權，讓 chmod 000 的目錄確實不可讀。
        unsafe {
            command.pre_exec(|| {
                if libc::setgid(65534) != 0 || libc::setuid(65534) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = ChildGuard::spawn(&mut command);
        let status = wait_child_with_timeout(&mut child.child, Duration::from_secs(5))
            .expect("權限子行程應於期限內完成");
        assert!(status.success(), "非 root 權限斷言失敗：{status}");
        return;
    }
    assert_socket_probe_eacces(d.path());
}

#[cfg(unix)]
fn assert_socket_probe_eacces(base: &Path) {
    let private = base.join("private");
    std::fs::create_dir(&private).unwrap();
    let sock = private.join("s.sock");
    let _listener = UnixListener::bind(&sock).unwrap();
    std::fs::set_permissions(&private, std::fs::Permissions::from_mode(0o000)).unwrap();
    let lock = base.join("l.lock");
    let endpoint = EndpointSpec::Unix(sock.into_os_string());
    let factory = native().instance_lock;
    let first = matches!(
        factory.acquire(&lock, &endpoint),
        Err(InstanceLockError::SocketUnavailable)
    );
    // 第二次仍能走到 socket 探測，證明第一個錯誤路徑已關閉 lock fd。
    let second = matches!(
        factory.acquire(&lock, &endpoint),
        Err(InstanceLockError::SocketUnavailable)
    );
    std::fs::set_permissions(&private, std::fs::Permissions::from_mode(0o700)).unwrap();
    assert!(first);
    assert!(second);
}

#[cfg(unix)]
#[test]
fn lock_file_mode_0600() {
    let d = tempfile::tempdir().unwrap();
    let lock = d.path().join("run/serialwrapd.lock");
    let guard = native()
        .instance_lock
        .acquire(
            &lock,
            &EndpointSpec::Unix(d.path().join("run/serialwrapd.sock").into_os_string()),
        )
        .unwrap();
    assert_eq!(
        std::fs::metadata(&lock).unwrap().permissions().mode() & 0o777,
        0o600
    );
    guard.release();
}

#[test]
fn normalize_for_compare_cases() {
    #[cfg(not(windows))]
    let (expected, absolute_cwd, relative_cwd, home) = (
        ("/a/c.sock", "/w/rel/x.sock", "/h/s.sock"),
        Path::new("/"),
        Path::new("/w"),
        Path::new("/h"),
    );
    #[cfg(windows)]
    let (expected, absolute_cwd, relative_cwd, home) = (
        (r"c:\a\c.sock", r"c:\work\rel\x.sock", r"c:\home\s.sock"),
        Path::new(r"C:\work"),
        Path::new(r"C:\work"),
        Path::new(r"C:\home"),
    );

    assert_eq!(
        normalize_for_compare("unix:///a/b/../c.sock", absolute_cwd, None),
        expected.0
    );
    assert_eq!(
        normalize_for_compare("rel/x.sock", relative_cwd, None),
        expected.1
    );
    assert_eq!(
        normalize_for_compare("~/s.sock", relative_cwd, Some(home)),
        expected.2
    );

    #[cfg(windows)]
    assert_eq!(
        normalize_for_compare(r"\run\s.sock", Path::new(r"C:\work"), None),
        normalize_for_compare(r"C:\run\s.sock", Path::new(r"D:\other"), None),
        "root-relative 路徑應沿用 cwd 的磁碟機"
    );

    #[cfg(windows)]
    let username = std::env::var("USERNAME").ok();
    #[cfg(windows)]
    if let Some(username) = username {
        let named_home = format!("~{username}\\s.sock");
        assert_eq!(
            normalize_for_compare(&named_home, relative_cwd, None),
            normalize_for_compare("~/s.sock", relative_cwd, None),
            "expanduser 應支援目前帳戶名稱"
        );
    }

    #[cfg(unix)]
    if let Some((username, passwd_home)) = current_passwd_identity() {
        let named_home = format!("~{username}/s.sock");
        let passwd_path = passwd_home.join("s.sock");
        assert_eq!(
            normalize_for_compare(&named_home, relative_cwd, None),
            normalize_for_compare(&passwd_path.to_string_lossy(), relative_cwd, None),
            "~user 應依 passwd home 展開，與 HOME 覆寫無關"
        );
    }

    let missing_user_name = "serialwrap_user_that_does_not_exist_73219";
    #[cfg(not(windows))]
    let missing_user = format!("~{missing_user_name}/s.sock");
    #[cfg(windows)]
    let missing_user = format!("~{missing_user_name}\\s.sock");
    #[cfg(not(windows))]
    let missing_user_expected = format!("/w/~{missing_user_name}/s.sock");
    #[cfg(windows)]
    let missing_user_expected = {
        let profile = std::path::PathBuf::from(std::env::var_os("USERPROFILE").unwrap());
        let current_user = std::env::var("USERNAME").unwrap();
        if profile.file_name().and_then(|name| name.to_str()) == Some(current_user.as_str()) {
            let guessed_home = profile
                .parent()
                .unwrap()
                .join(missing_user_name)
                .join("s.sock");
            normalize_for_compare(&guessed_home.to_string_lossy(), relative_cwd, None)
        } else {
            r"c:\work\~serialwrap_user_that_does_not_exist_73219\s.sock".to_string()
        }
    };
    assert_eq!(
        normalize_for_compare(&missing_user, relative_cwd, None),
        missing_user_expected,
        "未列名使用者路徑應符合平台 expanduser 基線"
    );

    #[cfg(unix)]
    for (input, expected) in [("//a/b", "//a/b"), ("//", "//"), ("///a", "/a")] {
        assert_eq!(
            normalize_for_compare(input, Path::new("/"), None),
            expected,
            "雙斜線僅在恰有兩個前導斜線時保留"
        );
    }

    #[cfg(windows)]
    assert_eq!(
        normalize_for_compare(r"C:s.sock", Path::new(r"C:\work"), None),
        r"c:\work\s.sock",
        "同磁碟的 drive-relative 路徑應以傳入 cwd 為基準"
    );

    #[cfg(windows)]
    {
        let drive = std::env::var("SystemDrive").unwrap_or_else(|_| "C:".to_string());
        let other_drive = if drive.eq_ignore_ascii_case("C:") {
            "D:"
        } else {
            "C:"
        };
        let drive_relative = format!("{drive}s.sock");
        let actual = normalize_for_compare(
            &drive_relative,
            Path::new(&format!(r"{other_drive}\work")),
            None,
        );
        let python = Command::new("py")
            .args([
                "-3.12",
                "-c",
                "import os,sys; print(os.path.abspath(sys.argv[1]))",
            ])
            .arg(&drive_relative)
            .output()
            .expect("執行 Windows Python 路徑基線");
        assert!(python.status.success(), "Windows Python 路徑基線失敗");
        let expected = String::from_utf8(python.stdout)
            .unwrap()
            .trim()
            .replace('/', "\\")
            .to_lowercase();
        assert_eq!(
            actual, expected,
            "其他磁碟的 drive-relative 路徑應符合 Python abspath"
        );
    }
}

#[cfg(unix)]
fn current_passwd_identity() -> Option<(String, PathBuf)> {
    use std::{ffi::CStr, mem::MaybeUninit, os::unix::ffi::OsStringExt};

    let mut buffer = vec![0_u8; 16 * 1024];
    let mut entry = MaybeUninit::<libc::passwd>::uninit();
    let mut result = std::ptr::null_mut();
    let rc = unsafe {
        libc::getpwuid_r(
            libc::getuid(),
            entry.as_mut_ptr(),
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            &mut result,
        )
    };
    if rc != 0 || result.is_null() {
        return None;
    }
    let entry = unsafe { entry.assume_init() };
    if entry.pw_name.is_null() || entry.pw_dir.is_null() {
        return None;
    }
    let username = unsafe { CStr::from_ptr(entry.pw_name) }
        .to_string_lossy()
        .into_owned();
    let home = unsafe { CStr::from_ptr(entry.pw_dir) }.to_bytes().to_vec();
    Some((username, PathBuf::from(std::ffi::OsString::from_vec(home))))
}

#[cfg(windows)]
#[test]
fn windows_live_tcp_endpoint_checked_before_lock() {
    use std::net::TcpListener;

    let d = tempfile::tempdir().unwrap();
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    let lock = d.path().join("run/serialwrapd.lock");
    let endpoint = EndpointSpec::Tcp {
        host: "127.0.0.1".to_string(),
        port,
    };
    assert!(matches!(
        native().instance_lock.acquire(&lock, &endpoint),
        Err(InstanceLockError::AlreadyRunning)
    ));
    assert!(
        !lock.exists(),
        "live endpoint is checked before lock creation"
    );
    drop(listener);
}

#[cfg(windows)]
#[test]
fn windows_lock_path_strips_unix_url_prefix() {
    let d = tempfile::tempdir().unwrap();
    let lock = d.path().join("serialwrapd.lock");
    let alias = PathBuf::from(format!("unix://{}", lock.display()));
    let factory = native().instance_lock;
    let guard = factory
        .acquire(&alias, &EndpointSpec::Unix("unused.sock".into()))
        .expect("Windows lock path 應先去除 unix://");
    assert!(lock.exists(), "Windows 應在指定的絕對路徑建立鎖檔");
    assert!(matches!(
        factory.acquire(&lock, &EndpointSpec::Unix("unused.sock".into())),
        Err(InstanceLockError::AlreadyRunning)
    ));
    guard.release();
}

#[cfg(windows)]
#[test]
fn windows_lock_path_alias_matches_python_home_expansion_and_unix_url() {
    const CHILD_ENV: &str = "S21_WIN_LOCK_PATH_CHILD";
    if std::env::var_os(CHILD_ENV).is_some() {
        let home = PathBuf::from(std::env::var_os("USERPROFILE").unwrap());
        let lock = home.join("run/serialwrapd.lock");
        let guard = native()
            .instance_lock
            .acquire(
                Path::new("unix://~/run/serialwrapd.lock"),
                &EndpointSpec::Unix("unused.sock".into()),
            )
            .expect("Windows 鎖路徑應先去除 unix:// 並展開 ~");
        assert!(lock.exists(), "Rust 應建立 Python 基線的鎖檔路徑");

        let script = r#"
import msvcrt, os, sys
path = sys.argv[1]
if path.startswith("unix://"):
    path = path[len("unix://"):]
path = os.path.abspath(os.path.expanduser(path))
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
try:
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
except OSError:
    raise SystemExit(0)
msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
raise SystemExit(1)
"#;
        let output = Command::new("py")
            .args(["-3.12", "-c", script])
            .arg("unix://~/run/serialwrapd.lock")
            .current_dir(common::data_root())
            .output()
            .expect("啟動 Python Windows 鎖基線");
        assert!(
            output.status.success(),
            "Python 應與 Rust 的路徑別名互斥：{}",
            String::from_utf8_lossy(&output.stderr)
        );
        guard.release();
        return;
    }

    let d = tempfile::tempdir().unwrap();
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "windows_lock_path_alias_matches_python_home_expansion_and_unix_url",
            "--test-threads=1",
        ])
        .env(CHILD_ENV, "1")
        .env("USERPROFILE", d.path())
        .current_dir(d.path())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("啟動隔離的 Windows 鎖路徑子行程");
    let status = wait_child_with_timeout(&mut child, Duration::from_secs(15))
        .expect("Windows 鎖路徑子行程應於期限內完成");
    assert!(
        status.success(),
        "Rust／Python Windows 路徑別名測試失敗：{status}"
    );
}

#[cfg(windows)]
#[test]
fn windows_lockfileex_conflicts_with_python_msvcrt_holder() {
    let d = tempfile::tempdir().unwrap();
    let lock = d.path().join("run/serialwrapd.lock");
    std::fs::create_dir_all(lock.parent().unwrap()).unwrap();
    let script = "import msvcrt,os,sys,time; fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600); msvcrt.locking(fd,msvcrt.LK_NBLCK,1); print('held',flush=True); time.sleep(30)";
    let mut child = ChildGuard::spawn(
        Command::new("py")
            .args(["-3.12", "-c", script])
            .arg(&lock)
            .stdout(Stdio::piped())
            .stderr(Stdio::null()),
    );
    let mut line = String::new();
    BufReader::new(child.child.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    assert_eq!(line.trim(), "held", "Windows Python msvcrt holder started");
    assert!(matches!(
        native()
            .instance_lock
            .acquire(&lock, &EndpointSpec::Unix("unused.sock".into())),
        Err(InstanceLockError::AlreadyRunning)
    ));
}

#[cfg(windows)]
#[test]
fn windows_second_acquire_in_process() {
    let d = tempfile::tempdir().unwrap();
    let lock = d.path().join("run/serialwrapd.lock");
    let endpoint = EndpointSpec::Unix("unused.sock".into());
    let factory = native().instance_lock;
    let guard = factory.acquire(&lock, &endpoint).unwrap();
    assert!(matches!(
        factory.acquire(&lock, &endpoint),
        Err(InstanceLockError::AlreadyRunning)
    ));
    guard.release();
    assert!(factory.acquire(&lock, &endpoint).is_ok());
}

#[cfg(windows)]
#[cfg(feature = "test-fakes")]
#[test]
fn windows_signal_install_is_idempotent_in_child() {
    let current_exe = std::env::current_exe().unwrap();
    let staged_helper = current_exe
        .parent()
        .unwrap()
        .join("signal_wait_harness.exe");
    let helper = if staged_helper.is_file() {
        staged_helper
    } else {
        PathBuf::from(env!("CARGO_BIN_EXE_signal_wait_harness"))
    };
    let status = Command::new(helper)
        .status()
        .expect("啟動獨立訊號測試子行程");
    assert!(status.success(), "子行程重複 install() 應成功：{status}");
}

#[test]
fn signal_extra_channel_stops_wait() {
    let signals = Arc::new(signal::NativeSignalWait);
    let (tx, rx) = crossbeam_channel::unbounded();
    let waiter = Arc::clone(&signals);
    let (done_tx, done_rx) = crossbeam_channel::bounded(1);
    thread::spawn(move || {
        waiter.wait_stop(rx);
        let _ = done_tx.send(());
    });
    tx.send(()).unwrap();
    done_rx
        .recv_timeout(Duration::from_millis(100))
        .expect("extra stop channel must wake wait_stop within 100 ms");
}

fn wait_child_with_timeout(child: &mut Child, timeout: Duration) -> std::io::Result<ExitStatus> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(status);
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "子行程逾時，已終止並回收",
            ));
        }
        thread::sleep(Duration::from_millis(10));
    }
}

#[cfg(unix)]
struct ChildGuard {
    child: Child,
}

#[cfg(unix)]
impl ChildGuard {
    fn spawn(command: &mut Command) -> Self {
        Self {
            child: command.spawn().unwrap(),
        }
    }
}

#[cfg(unix)]
impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(unix)]
#[test]
#[cfg(feature = "test-fakes")]
fn posix_sigterm_stops_wait_in_child() {
    let mut child = ChildGuard::spawn(
        Command::new(env!("CARGO_BIN_EXE_signal_wait_harness")).stderr(Stdio::inherit()),
    );
    let status = wait_child_with_timeout(&mut child.child, Duration::from_secs(5))
        .expect("訊號等待 helper 應於期限內完成");
    assert!(
        status.success(),
        "install() 後存活的 worker 應能隨 SIGTERM 有序結束：{status}"
    );
}

#[cfg(unix)]
#[test]
fn posix_threads_spawned_after_install_inherit_mask() {
    if std::env::var_os("S21_CHILD").is_some() {
        let signals = signal::NativeSignalWait;
        signals.install().unwrap();
        let worker = thread::spawn(|| {
            let mut current = unsafe { std::mem::zeroed::<libc::sigset_t>() };
            let rc =
                unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, std::ptr::null(), &mut current) };
            assert_eq!(rc, 0);
            unsafe {
                libc::sigismember(&current, libc::SIGTERM) == 1
                    && libc::sigismember(&current, libc::SIGINT) == 1
            }
        });
        assert!(
            worker.join().unwrap(),
            "new threads inherit SIGINT/SIGTERM mask"
        );
        return;
    }

    let mut child = ChildGuard::spawn(
        Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "posix_threads_spawned_after_install_inherit_mask",
                "--test-threads=1",
            ])
            .env("S21_CHILD", "inherit_mask")
            .stdout(Stdio::null())
            .stderr(Stdio::inherit()),
    );
    let status = wait_child_with_timeout(&mut child.child, Duration::from_secs(5))
        .expect("遮罩繼承子行程應於期限內結束");
    assert!(
        status.success(),
        "threads created after install inherit the blocked signals"
    );
}

#[cfg(windows)]
struct ChildGuard {
    child: Child,
}

#[cfg(windows)]
impl ChildGuard {
    fn spawn(command: &mut Command) -> Self {
        Self {
            child: command.spawn().unwrap(),
        }
    }
}

#[cfg(windows)]
impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(windows)]
#[test]
#[ignore = "需要互動主控台工作階段；由 cap_interactive_desktop gate 執行"]
#[allow(non_snake_case)]
fn cap_interactive_desktop__ctrl_break_stops_wait() {
    use std::os::windows::process::CommandExt;
    use windows_sys::Win32::System::Console::{GenerateConsoleCtrlEvent, CTRL_BREAK_EVENT};

    if std::env::var_os("S21_CHILD").is_some() {
        let signals = signal::NativeSignalWait;
        signals.install().unwrap();
        println!("READY");
        let (_tx, rx) = crossbeam_channel::unbounded();
        signals.wait_stop(rx);
        return;
    }

    const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
    let mut child = ChildGuard::spawn(
        Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "cap_interactive_desktop__ctrl_break_stops_wait",
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .env("S21_CHILD", "ctrl_break")
            .creation_flags(CREATE_NEW_PROCESS_GROUP)
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit()),
    );
    let stdout = child.child.stdout.take().unwrap();
    let mut reader = BufReader::new(stdout);
    let mut line = String::new();
    while reader.read_line(&mut line).unwrap() != 0 && !line.contains("READY") {
        line.clear();
    }
    assert!(
        line.contains("READY"),
        "child installed signal wait: {line}"
    );
    let ok = unsafe { GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, child.child.id()) };
    assert_ne!(ok, 0, "send CTRL_BREAK to child process group");
    let status = child.child.wait().unwrap();
    assert!(
        status.success(),
        "child handled CTRL_BREAK and exited normally: {status}"
    );
}
