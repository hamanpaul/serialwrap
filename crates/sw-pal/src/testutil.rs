//! console 測試工具（P6 Task 8；feature `test-fakes`）。
//! 平台 cfg 只在 sw-pal 內，讓 sw-core／sw-daemon 測試不需自行分支。

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

#[cfg_attr(not(unix), allow(unused_imports))]
use crate::console_mux::MuxHandleInner;
use crate::console_pty::{ConsoleIo, ConsoleListener, PtyPair};
use crate::traits::{PalError, PalResult};
#[cfg(not(target_os = "linux"))]
use crate::traits::PalErrorKind;

/// `tcgetattr` 的可比較摘要（raw 模式斷言用）。
#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(non_snake_case)]
pub struct TermiosView {
    pub iflag: u64,
    pub oflag: u64,
    pub cflag: u64,
    pub lflag: u64,
    pub vmin: u8,
    pub vtime: u8,
    /// 平台 `CREAD`／`CLOCAL`／`CS8` 位元值（讓斷言不必直接碰 `libc`）。
    pub CREAD: u64,
    pub CLOCAL: u64,
    pub CS8: u64,
}

pub fn tcgetattr_path(p: &str) -> PalResult<TermiosView> {
    #[cfg(unix)]
    {
        use std::os::fd::AsRawFd;
        use std::os::unix::fs::OpenOptionsExt;
        let f = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_NOCTTY | libc::O_NONBLOCK)
            .open(p)
            .map_err(crate::traits::posix_io_error)?;
        // SAFETY: t 為 tcgetattr 輸出位置，fd 在 f 存活期間有效。
        let mut t: libc::termios = unsafe { std::mem::zeroed() };
        if unsafe { libc::tcgetattr(f.as_raw_fd(), &mut t) } != 0 {
            return Err(PalError::from_errno(crate::console_mux::posix::errno()));
        }
        Ok(TermiosView {
            iflag: t.c_iflag as u64,
            oflag: t.c_oflag as u64,
            cflag: t.c_cflag as u64,
            lflag: t.c_lflag as u64,
            vmin: t.c_cc[libc::VMIN],
            vtime: t.c_cc[libc::VTIME],
            CREAD: libc::CREAD as u64,
            CLOCAL: libc::CLOCAL as u64,
            CS8: libc::CS8 as u64,
        })
    }
    #[cfg(not(unix))]
    {
        Err(PalError::plain(PalErrorKind::Unsupported, format!("tcgetattr 在此平台不支援：{p}")))
    }
}

pub fn master_is_nonblocking(p: &PtyPair) -> bool {
    match p.master.mux_handle().0 {
        #[cfg(unix)]
        MuxHandleInner::Posix(fd) => {
            // SAFETY: fcntl 不保留指標，fd 由 p.master 保持開啟。
            let fl = unsafe { libc::fcntl(fd, libc::F_GETFL) };
            fl >= 0 && fl & libc::O_NONBLOCK != 0
        }
        _ => false,
    }
}

/// `dirs` 內（遞迴）沒有任何 symlink 指向 `target`（D-01：console PTY 不建 symlink）。
pub fn no_symlink_points_to(target: &str, dirs: &[&Path]) -> bool {
    fn walk(dir: &Path, target: &Path) -> bool {
        let Ok(entries) = std::fs::read_dir(dir) else {
            return true;
        };
        for e in entries.flatten() {
            let path = e.path();
            let Ok(meta) = std::fs::symlink_metadata(&path) else {
                continue;
            };
            if meta.file_type().is_symlink() {
                if let Ok(dest) = std::fs::read_link(&path) {
                    let dest = if dest.is_absolute() { dest } else { dir.join(dest) };
                    if dest == target || std::fs::canonicalize(&dest).is_ok_and(|c| c == target) {
                        return false;
                    }
                }
            } else if meta.is_dir() && !walk(&path, target) {
                return false;
            }
        }
        true
    }
    let target = Path::new(target);
    dirs.iter().all(|d| walk(d, target))
}

/// D-01 掃描的目錄：PAL 實際可能落 console symlink 之處（`SERIALWRAP_RUN_DIR`、`XDG_RUNTIME_DIR`、
/// 系統 temp、cwd、unix 的 `/dev`）。與 `symlink_snapshot`／`new_symlinks_to` 搭配做前後差分。
pub fn d01_scan_dirs() -> Vec<PathBuf> {
    let mut dirs: Vec<PathBuf> = Vec::new();
    for key in ["SERIALWRAP_RUN_DIR", "XDG_RUNTIME_DIR"] {
        if let Some(v) = std::env::var_os(key).filter(|v| !v.is_empty()) {
            dirs.push(PathBuf::from(v));
        }
    }
    dirs.push(std::env::temp_dir());
    if let Ok(cwd) = std::env::current_dir() {
        dirs.push(cwd);
    }
    if cfg!(unix) {
        dirs.push(PathBuf::from("/dev"));
    }
    let mut seen = std::collections::HashSet::new();
    dirs.retain(|d| seen.insert(d.clone()));
    dirs
}

/// D-01 掃描深度（目錄本身為 1）；temp／`/dev` 可能很大，只看 PAL 可能直接落檔的淺層。
const D01_SCAN_DEPTH: usize = 3;

fn walk_symlinks(dir: &Path, depth: usize, out: &mut Vec<(PathBuf, PathBuf)>) {
    if depth == 0 {
        return;
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for e in entries.flatten() {
        let path = e.path();
        let Ok(meta) = std::fs::symlink_metadata(&path) else {
            continue;
        };
        if meta.file_type().is_symlink() {
            if let Ok(dest) = std::fs::read_link(&path) {
                let dest = if dest.is_absolute() { dest } else { dir.join(dest) };
                let dest = std::fs::canonicalize(&dest).unwrap_or(dest);
                out.push((path, dest));
            }
        } else if meta.is_dir() {
            walk_symlinks(&path, depth - 1, out);
        }
    }
}

/// `dirs` 內（淺層遞迴、不跟隨目錄 symlink）所有 symlink 的 `(link, 解析後目的地)`。
pub fn symlink_snapshot(dirs: &[PathBuf]) -> std::collections::HashSet<(PathBuf, PathBuf)> {
    let mut out = Vec::new();
    for d in dirs {
        walk_symlinks(d, D01_SCAN_DEPTH, &mut out);
    }
    out.into_iter().collect()
}

/// 相對 `before` 快照新增、且指向 `target` 的 symlink（排序後回傳）。
pub fn new_symlinks_to(
    target: &str,
    dirs: &[PathBuf],
    before: &std::collections::HashSet<(PathBuf, PathBuf)>,
) -> Vec<PathBuf> {
    let target = Path::new(target);
    let canon = std::fs::canonicalize(target).ok();
    let mut added: Vec<PathBuf> = symlink_snapshot(dirs)
        .into_iter()
        .filter(|entry| !before.contains(entry))
        .filter(|(_, dest)| dest == target || canon.as_deref() == Some(dest.as_path()))
        .map(|(link, _)| link)
        .collect();
    added.sort();
    added
}

/// `^/dev/pts/\d+$` 手寫判斷（不引入 regex）。
pub fn is_dev_pts_path(p: &str) -> bool {
    p.strip_prefix("/dev/pts/")
        .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
}

pub fn wait_accept(l: &mut dyn ConsoleListener, timeout: Duration) -> (Box<dyn ConsoleIo>, String) {
    let start = Instant::now();
    loop {
        let remaining = timeout.saturating_sub(start.elapsed());
        if let Some(v) = l.accept(remaining).expect("accept") {
            return v;
        }
        assert!(!remaining.is_zero(), "wait_accept：{timeout:?} 內無連線");
    }
}

pub fn nodelay(io: &dyn ConsoleIo) -> bool {
    match io.mux_handle().0 {
        #[cfg(unix)]
        MuxHandleInner::Posix(fd) => {
            crate::console_tcp::posix::get_int_opt(fd, libc::IPPROTO_TCP, libc::TCP_NODELAY)
                .is_some_and(|v| v != 0)
        }
        #[cfg(windows)]
        MuxHandleInner::WinSocket(socket) => {
            use windows_sys::Win32::Networking::WinSock::{IPPROTO_TCP, TCP_NODELAY};
            crate::console_tcp::windows::get_int_opt(socket as _, IPPROTO_TCP, TCP_NODELAY)
                .is_some_and(|v| v != 0)
        }
        _ => false,
    }
}

pub fn reuseaddr(l: &dyn ConsoleListener) -> bool {
    match l.mux_handle().0 {
        #[cfg(unix)]
        MuxHandleInner::Posix(fd) => {
            crate::console_tcp::posix::get_int_opt(fd, libc::SOL_SOCKET, libc::SO_REUSEADDR)
                .is_some_and(|v| v != 0)
        }
        #[cfg(windows)]
        MuxHandleInner::WinSocket(socket) => {
            use windows_sys::Win32::Networking::WinSock::{SOL_SOCKET, SO_REUSEADDR};
            crate::console_tcp::windows::get_int_opt(socket as _, SOL_SOCKET, SO_REUSEADDR)
                .is_some_and(|v| v != 0)
        }
        _ => false,
    }
}

/// 連線到已關閉 loopback port 的錯誤（`winsock_error_message_format`）。
pub fn tcp_connect_closed_port_error() -> PalError {
    let port = {
        let l = std::net::TcpListener::bind("127.0.0.1:0").expect("bind 暫用 port");
        l.local_addr().expect("local_addr").port()
    };
    let err = std::net::TcpStream::connect(("127.0.0.1", port)).expect_err("已關閉 port 應拒絕連線");
    #[cfg(unix)]
    {
        crate::traits::posix_io_error(err)
    }
    #[cfg(windows)]
    {
        match err.raw_os_error() {
            Some(code) => crate::traits::windows_wsa_error(code),
            None => PalError::plain(PalErrorKind::Io, err.to_string()),
        }
    }
}

/// com0com console handle 上的設定 API 呼叫計數（SC-29 控制線隔離證據）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct CommApiCalls {
    pub set_comm_state: u32,
    pub escape_comm_function: u32,
}

/// LOCAL-WINDOWS-TODO（P6 Task 8）：讀 Windows com0com console io 的 API 計數器。
pub fn comm_api_calls(_io: &dyn ConsoleIo) -> CommApiCalls {
    todo!("P6 Task 8")
}

/// 對 peer 端（COMb）改 baud 並切換 DTR／RTS。LOCAL-WINDOWS-TODO（P6 Task 8）。
pub fn toggle_peer_modem_lines(_port: &str) {
    todo!("P6 Task 8")
}

/// P8 Task 13（編排者 T9 裁決）：以「一般 open」扮演 flasher 的 tty 用戶端——`O_RDWR|O_NOCTTY|O_NONBLOCK`，
/// 不設 `TIOCEXCL`（與 `ocp-mcu-upgrade` 等外部工具相同）。供 sw-core 的 `/dev/ttyMCU` 真 PTY 測試使用，
/// 使其不需自行分支平台。非 Linux 平台一律回 `Unsupported`。
pub struct PlainTty {
    #[cfg(unix)]
    file: std::fs::File,
}

impl PlainTty {
    /// 一般 open（不設 TIOCEXCL）；tty 已被設為 exclusive 時一般使用者會得到 `EBUSY`。
    pub fn open(path: &Path) -> PalResult<PlainTty> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            let file = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .custom_flags(libc::O_NOCTTY | libc::O_NONBLOCK)
                .open(path)
                .map_err(crate::traits::posix_io_error)?;
            Ok(PlainTty { file })
        }
        #[cfg(not(unix))]
        {
            Err(PalError::plain(PalErrorKind::Unsupported, format!("PlainTty 在此平台不支援：{}", path.display())))
        }
    }

    /// `ioctl(TIOCGEXCL)`：tty 目前是否為 exclusive 模式（Linux）。
    pub fn exclusive(&self) -> PalResult<bool> {
        #[cfg(target_os = "linux")]
        {
            use std::os::fd::AsRawFd;
            let mut value: libc::c_int = 0;
            // SAFETY: value 為 TIOCGEXCL 的輸出位置，fd 在 self.file 存活期間有效。
            if unsafe { libc::ioctl(self.file.as_raw_fd(), libc::TIOCGEXCL, &mut value) } != 0 {
                return Err(PalError::from_errno(crate::console_mux::posix::errno()));
            }
            Ok(value != 0)
        }
        #[cfg(not(target_os = "linux"))]
        {
            Err(PalError::plain(PalErrorKind::Unsupported, "TIOCGEXCL 只在 Linux 支援"))
        }
    }

    /// 寫完全部 bytes（非阻塞 fd：`EAGAIN` 時短暫等待，上限 5 s）。
    pub fn write_all(&mut self, data: &[u8]) -> PalResult<()> {
        #[cfg(unix)]
        {
            use std::io::Write;
            let deadline = Instant::now() + Duration::from_secs(5);
            let mut off = 0usize;
            while off < data.len() {
                match self.file.write(&data[off..]) {
                    Ok(n) => off += n,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock && Instant::now() < deadline => {
                        std::thread::sleep(Duration::from_millis(1));
                    }
                    Err(e) => return Err(crate::traits::posix_io_error(e)),
                }
            }
            Ok(())
        }
        #[cfg(not(unix))]
        {
            let _ = data;
            Err(PalError::plain(PalErrorKind::Unsupported, "PlainTty 在此平台不支援"))
        }
    }

    /// 讀到 `n` bytes 或 `timeout` 到期（實時）為止，回傳已讀到的 bytes（可能不足 `n`；讀到 EOF 亦提前返回）。
    pub fn read_within(&mut self, n: usize, timeout: Duration) -> PalResult<Vec<u8>> {
        #[cfg(unix)]
        {
            use std::io::Read;
            let deadline = Instant::now() + timeout;
            let mut out = Vec::with_capacity(n);
            let mut buf = vec![0u8; n.max(1)];
            while out.len() < n {
                match self.file.read(&mut buf[..n - out.len()]) {
                    Ok(0) => break,
                    Ok(k) => out.extend_from_slice(&buf[..k]),
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        if Instant::now() >= deadline {
                            break;
                        }
                        std::thread::sleep(Duration::from_millis(2));
                    }
                    Err(e) => return Err(crate::traits::posix_io_error(e)),
                }
            }
            Ok(out)
        }
        #[cfg(not(unix))]
        {
            let _ = (n, timeout);
            Err(PalError::plain(PalErrorKind::Unsupported, "PlainTty 在此平台不支援"))
        }
    }
}

/// 一般 open `path` 後立刻讀 `TIOCGEXCL` 再關閉（tty 為 exclusive 時一般使用者的 open 會先以 `EBUSY` 失敗）。
pub fn tty_exclusive(path: &Path) -> PalResult<bool> {
    PlainTty::open(path)?.exclusive()
}
