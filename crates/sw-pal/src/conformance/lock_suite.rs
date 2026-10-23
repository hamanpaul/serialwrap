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
        use std::os::unix::net::UnixListener;

        let socket = dir.join("lock-suite/stale.sock");
        drop(UnixListener::bind(&socket).expect("[stale_socket_setup] 建立 socket"));
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
