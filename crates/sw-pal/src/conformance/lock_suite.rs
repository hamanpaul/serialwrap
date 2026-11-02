//! InstanceLock 共用一致性測試（arch §4.4、計畫 Task 4）。

use std::path::Path;
use std::sync::Arc;

use crate::lock::InstanceLockFactory;

/// 實例鎖共用斷言：二次取得回 `AlreadyRunning`、失效 socket 可接管、路徑正規化（SC-10）（Task 4）。
pub fn lock_suite(factory: Arc<dyn InstanceLockFactory>, dir: &Path) {
    let lock = dir.join("lock-suite/serialwrapd.lock");
    let endpoint =
        crate::traits::EndpointSpec::Unix(dir.join("lock-suite/serialwrapd.sock").into_os_string());
    let guard = factory
        .acquire(&lock, &endpoint)
        .expect("[lock_acquire] 第一次取得實例鎖應成功");
    assert!(
        matches!(
            factory.acquire(&lock, &endpoint),
            Err(crate::lock::InstanceLockError::AlreadyRunning)
        ),
        "[second_acquire] 同一 lock 檔的第二次取得應回 AlreadyRunning"
    );
    guard.release();
    assert_eq!(
        std::fs::read_to_string(&lock).expect("[pid_written] 讀取 lock 檔"),
        format!("{}\n", std::process::id()),
        "[pid_written] lock 檔應只有目前 PID"
    );
    assert!(lock.exists(), "[release_keeps_file] release 不刪除 lock 檔");

    #[cfg(unix)]
    {
        // 失效 socket：只 bind、不 listen 即關閉（P5 第 7 輪）。以 UnixListener bind→drop 製造時，同程序他執行緒 spawn 的
        // 子行程在 exec 前持有 listener 複本，drop 後該複本仍在 listen，探測連線成功而得 AlreadyRunning（r6 gate 間歇失敗）；
        // 未 listen 的 socket 不論有無複本，連線一律 ECONNREFUSED，與 daemon 結束後殘留的 socket 檔相同。
        let socket = dir.join("lock-suite/stale.sock");
        bind_stale_unix_socket(&socket);
        let stale_guard = factory
            .acquire(
                &dir.join("lock-suite/stale.lock"),
                &crate::traits::EndpointSpec::Unix(socket.clone().into_os_string()),
            )
            .expect("[stale_socket_reclaimed] 失效 socket 不阻止取得鎖");
        assert!(
            !socket.exists(),
            "[stale_socket_unlinked] 接管前應移除失效 socket"
        );
        stale_guard.release();
    }

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
        crate::path_norm::normalize_for_compare("unix:///a/b/../c.sock", absolute_cwd, None),
        expected.0,
        "[normalize_absolute] 絕對路徑應先正規化"
    );
    assert_eq!(
        crate::path_norm::normalize_for_compare("rel/x.sock", relative_cwd, None),
        expected.1,
        "[normalize_relative] 相對路徑應以 cwd 為基準"
    );
    assert_eq!(
        crate::path_norm::normalize_for_compare("~/s.sock", relative_cwd, Some(home)),
        expected.2,
        "[normalize_home] 路徑應展開 home"
    );
}

/// 在 `path` 建立一個只 bind、從未 listen 的 Unix stream socket 檔後關閉 fd（失效 socket 情境）。
/// 測試以此取代 `UnixListener::bind`→drop：後者在同執行檔他測試 fork 後、exec 前會留下仍在 listen 的複本。
#[cfg(unix)]
pub fn bind_stale_unix_socket(path: &Path) {
    use std::os::unix::ffi::OsStrExt;

    let bytes = path.as_os_str().as_bytes();
    // SAFETY: sockaddr_un 為純資料結構，全零為合法初值。
    let mut addr: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    assert!(bytes.len() < addr.sun_path.len(), "[stale_socket_setup] socket 路徑過長：{}", path.display());
    addr.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (dst, src) in addr.sun_path.iter_mut().zip(bytes.iter().copied()) {
        *dst = src as libc::c_char;
    }
    let len = (std::mem::offset_of!(libc::sockaddr_un, sun_path) + bytes.len() + 1) as libc::socklen_t;
    // SAFETY: 參數皆為常數；回傳 fd 由下方 close 釋放。
    let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, 0) };
    assert!(fd >= 0, "[stale_socket_setup] socket: {}", std::io::Error::last_os_error());
    // SAFETY: addr 為已初始化的 sockaddr_un，len 不超過其大小；fd 為上方取得的有效 fd。
    let rc = unsafe { libc::bind(fd, (&addr as *const libc::sockaddr_un).cast::<libc::sockaddr>(), len) };
    let err = std::io::Error::last_os_error();
    // SAFETY: fd 有效且只關閉一次。
    unsafe { libc::close(fd) };
    assert_eq!(rc, 0, "[stale_socket_setup] bind: {err}");
}
