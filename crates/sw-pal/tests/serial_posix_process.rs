//! Slice 2.1 Task 2（規格 §6.2、arch §4.2）：跨程序獨占、symlink 去重、程序退出與 handle 繼承的失敗測試（TDD 紅燈）。
//!
//! 子程序以 `std::env::current_exe()` 加 `--exact <測試名>` 重入本測試二進位：
//! `S21_TEST` 傳入測試名（只有相同名稱的測試才進入子程序分支），`S21_CHILD` 傳入角色，`S21_DEV` 傳入 slave 路徑。
//! 子程序 stdout 印出 `S21_READY` 代表已就緒；父程序關閉子程序 stdin（EOF）即要求其結束。
//! 所有測試皆需 PTY 資源，標記 `#[ignore = "cap:posix_pty"]`，以 `-- --ignored cap_posix_pty__` 執行。
// sw-pal 是唯一允許平台 cfg 的 crate
#![cfg(all(unix, feature = "test-fakes"))]
// 測試名沿用計畫的 `cap_posix_pty__*` 命名（雙底線即為計畫指定的分隔），不套用 snake_case 警告。
#![allow(non_snake_case)]

use std::fs::File;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::{symlink, OpenOptionsExt};
use std::os::unix::io::{AsRawFd, FromRawFd};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::{Duration, Instant};

use sw_pal::conformance::posix_pty_pair;
use sw_pal::conformance::NoHolders;
use sw_pal::serial::posix::PosixSerialBackend;
use sw_pal::*;

const READY: &str = "S21_READY";

fn cfg() -> SerialConfig {
    SerialConfig::from_uart_profile(115200, 8, "N", 1, "none", false)
}

/// `Result<Box<dyn SerialHandle>, _>` 無法直接 `{:?}`（handle 為 trait object），此處只用來產生斷言訊息。
fn show_open(r: &Result<Box<dyn SerialHandle>, OpenError>) -> String {
    match r {
        Ok(_) => "Ok(<SerialHandle>)".to_string(),
        Err(e) => format!("{e:?}"),
    }
}

/// 子程序角色：只有 `S21_TEST` 與本測試名相同時才回傳 `S21_CHILD`（父程序的同名環境變數不存在）。
fn child_role(test: &str) -> Option<String> {
    if std::env::var("S21_TEST").ok().as_deref() != Some(test) {
        return None;
    }
    std::env::var("S21_CHILD").ok()
}

fn child_dev() -> DevicePath {
    DevicePath(std::env::var("S21_DEV").expect("S21_DEV"))
}

/// 子程序就緒後印 READY，之後阻塞於 stdin，直到父程序關閉 stdin（EOF）或被 kill。
fn child_ready_and_wait() {
    println!("{READY}");
    std::io::stdout().flush().ok();
    let mut b = [0u8; 1];
    let _ = std::io::stdin().read(&mut b);
}

#[repr(C)]
#[derive(Clone, Copy)]
struct IndependentHolderReady {
    pid: libc::pid_t,
    session_id: libc::pid_t,
    opened: libc::c_int,
}

#[derive(Clone, Copy)]
struct ProcIdentity {
    state: char,
    parent: libc::pid_t,
    session_id: libc::pid_t,
    start_time: u64,
}

/// 讀取 `/proc/<pid>/stat` 的 parent、session 與啟動時刻；comm 可含右括號，故從最後一個 `)` 後解析。
fn proc_identity(pid: libc::pid_t) -> Option<ProcIdentity> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let fields = stat
        .rsplit_once(')')?
        .1
        .split_whitespace()
        .collect::<Vec<_>>();
    Some(ProcIdentity {
        state: fields.first()?.chars().next()?,
        parent: fields.get(1)?.parse().ok()?,
        session_id: fields.get(3)?.parse().ok()?,
        start_time: fields.get(19)?.parse().ok()?,
    })
}

/// 真正位於呼叫者行程樹之外、獨立 session 的 PTY holder。
///
/// `probe_holders_in` 依 Python parity 的內部子行程防護排除呼叫者的整棵行程樹；單純 `Command::spawn` 的測試
/// holder 會因此被排除，不代表第二個 daemon。此處 double-fork 後讓 holder 由系統 reaper 收養，並以 `setsid`
/// 建立另一個 session；控制 pipe EOF 使它關閉 PTY，Drop 會等待並在必要時終止這支自己建立的測試行程。
struct IndependentHolder {
    pid: libc::pid_t,
    start_time: u64,
    release: Option<File>,
}

impl IndependentHolder {
    fn spawn(dev: &DevicePath) -> Self {
        let path = std::ffi::CString::new(dev.0.as_bytes()).expect("PTY path 不含 NUL");
        let mut ready = [0; 2];
        let mut release = [0; 2];
        assert_eq!(unsafe { libc::pipe(ready.as_mut_ptr()) }, 0, "ready pipe");
        if unsafe { libc::pipe(release.as_mut_ptr()) } != 0 {
            unsafe {
                libc::close(ready[0]);
                libc::close(ready[1]);
            }
            panic!("release pipe: {}", std::io::Error::last_os_error());
        }

        // SAFETY: fork 後的兩個子行程只呼叫 async-signal-safe 的 fork／setsid／open／read／write／close／_exit。
        let reaper = unsafe { libc::fork() };
        if reaper < 0 {
            unsafe {
                libc::close(ready[0]);
                libc::close(ready[1]);
                libc::close(release[0]);
                libc::close(release[1]);
            }
            panic!("fork holder reaper: {}", std::io::Error::last_os_error());
        }
        if reaper == 0 {
            unsafe {
                libc::close(ready[0]);
                libc::close(release[1]);
                let pid = libc::fork();
                if pid < 0 {
                    let record = IndependentHolderReady {
                        pid: 0,
                        session_id: -1,
                        opened: 0,
                    };
                    let bytes = (&record as *const IndependentHolderReady).cast::<u8>();
                    let _ = libc::write(
                        ready[1],
                        bytes.cast(),
                        std::mem::size_of::<IndependentHolderReady>(),
                    );
                    libc::close(ready[1]);
                    libc::close(release[0]);
                    libc::_exit(1);
                }
                if pid > 0 {
                    libc::close(ready[1]);
                    libc::close(release[0]);
                    libc::_exit(0);
                }

                let session_id = libc::setsid();
                let tty_fd = if session_id >= 0 {
                    libc::open(
                        path.as_ptr(),
                        libc::O_RDWR | libc::O_NOCTTY | libc::O_CLOEXEC,
                    )
                } else {
                    -1
                };
                let record = IndependentHolderReady {
                    pid: libc::getpid(),
                    session_id,
                    opened: libc::c_int::from(tty_fd >= 0),
                };
                let bytes = (&record as *const IndependentHolderReady).cast::<u8>();
                let mut offset = 0;
                while offset < std::mem::size_of::<IndependentHolderReady>() {
                    let written = libc::write(
                        ready[1],
                        bytes.add(offset).cast(),
                        std::mem::size_of::<IndependentHolderReady>() - offset,
                    );
                    if written <= 0 {
                        libc::_exit(1);
                    }
                    offset += written as usize;
                }
                libc::close(ready[1]);
                if tty_fd < 0 {
                    libc::close(release[0]);
                    libc::_exit(1);
                }
                let mut byte = 0u8;
                while libc::read(release[0], (&mut byte as *mut u8).cast(), 1) > 0 {}
                libc::close(tty_fd);
                libc::close(release[0]);
                libc::_exit(0);
            }
        }

        unsafe {
            libc::close(ready[1]);
            libc::close(release[0]);
        }
        // SAFETY: parent 已關閉對應 pipe 端點，以下 File 接管各自唯一的 fd。
        let mut ready_reader = unsafe { File::from_raw_fd(ready[0]) };
        let release_writer = unsafe { File::from_raw_fd(release[1]) };
        let mut child_status = 0;
        loop {
            // SAFETY: `reaper` 是本測試剛建立且尚未回收的直接子行程。
            let waited = unsafe { libc::waitpid(reaper, &mut child_status, 0) };
            if waited == reaper {
                break;
            }
            assert_eq!(
                std::io::Error::last_os_error().kind(),
                std::io::ErrorKind::Interrupted,
                "waitpid holder reaper"
            );
        }
        assert!(
            libc::WIFEXITED(child_status) && libc::WEXITSTATUS(child_status) == 0,
            "holder reaper 應成功 double-fork，status={child_status}"
        );

        let mut record = std::mem::MaybeUninit::<IndependentHolderReady>::uninit();
        // SAFETY: child 以相同 repr(C) 結構寫入完整 record；pid_t／c_int 的所有位元組組合皆可讀。
        let bytes = unsafe {
            std::slice::from_raw_parts_mut(
                record.as_mut_ptr().cast::<u8>(),
                std::mem::size_of::<IndependentHolderReady>(),
            )
        };
        ready_reader
            .read_exact(bytes)
            .expect("讀取 detached holder 就緒狀態");
        // SAFETY: read_exact 已填滿 repr(C) record，所有欄位都是整數型別。
        let record = unsafe { record.assume_init() };
        assert_eq!(record.opened, 1, "detached holder 必須成功開啟 PTY");
        assert_eq!(
            record.session_id, record.pid,
            "holder 必須建立自己的 session"
        );
        let identity = proc_identity(record.pid).expect("detached holder 應仍存活");
        assert_ne!(
            identity.state, 'Z',
            "holder 應在 release pipe EOF 前保持存活"
        );
        assert_eq!(identity.session_id, record.session_id);
        assert_ne!(
            identity.parent, reaper,
            "holder 必須在中間 reaper 結束後重新收養"
        );
        assert_ne!(
            identity.parent,
            std::process::id() as libc::pid_t,
            "holder 不得仍是呼叫者的子行程"
        );
        assert_ne!(
            identity.session_id,
            unsafe { libc::getsid(0) },
            "holder 與測試必須位於不同 session"
        );

        Self {
            pid: record.pid,
            start_time: identity.start_time,
            release: Some(release_writer),
        }
    }
}

impl Drop for IndependentHolder {
    fn drop(&mut self) {
        self.release.take(); // EOF 讓 holder 關閉自己開啟的 PTY。
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let Some(identity) = proc_identity(self.pid) else {
                return;
            };
            if identity.state == 'Z' {
                return;
            }
            if Instant::now() >= deadline {
                // 僅在 PID、session leader 與啟動時刻仍相符時，終止本測試建立但未自行退出的 holder。
                if identity.session_id == self.pid && identity.start_time == self.start_time {
                    unsafe {
                        libc::kill(self.pid, libc::SIGKILL);
                    }
                }
                return;
            }
            thread::sleep(Duration::from_millis(5));
        }
    }
}

/// 以本測試二進位重入 `test`（子程序），角色與 slave 路徑經環境變數傳入。
fn test_command(test: &str, role: &str, dev: &DevicePath) -> Command {
    let exe = std::env::current_exe().expect("current_exe");
    let mut cmd = Command::new(exe);
    cmd.args([
        "--exact",
        test,
        "--include-ignored",
        "--nocapture",
        "--test-threads=1",
    ])
    .env("S21_TEST", test)
    .env("S21_CHILD", role)
    .env("S21_DEV", &dev.0);
    cmd
}

/// 啟動子程序並等待其 READY。子程序 stderr 沿用父程序，使子程序的失敗原因在測試輸出中可見。
/// 注意：測試框架會先印 `test <name> ... ` 而不換行，故 READY 行是 `test <name> ... S21_READY`，以子字串比對。
/// 10 秒內未就緒即由看門狗 kill 子程序，避免測試無限掛住。
fn spawn_child(test: &str, role: &str, dev: &DevicePath) -> Child {
    let mut child = test_command(test, role, dev)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .expect("spawn child");
    let done = Arc::new(AtomicBool::new(false));
    let watchdog_done = Arc::clone(&done);
    let pid = child.id();
    thread::spawn(move || {
        thread::sleep(Duration::from_secs(10));
        if !watchdog_done.load(Ordering::SeqCst) {
            // SAFETY: pid 為本測試剛 spawn 的子程序；未就緒即強制結束，讓父程序的讀取回 EOF。
            unsafe {
                libc::kill(pid as libc::pid_t, libc::SIGKILL);
            }
        }
    });
    {
        let stdout = child.stdout.as_mut().expect("child stdout");
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        loop {
            line.clear();
            let n = reader.read_line(&mut line).expect("read child stdout");
            assert!(n > 0, "子程序在 READY 前結束（見上方子程序 stderr）");
            if line.contains(READY) {
                break;
            }
        }
    }
    done.store(true, Ordering::SeqCst);
    child
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__second_process_open_is_held() {
    let (dev, _peer) = posix_pty_pair();
    // 必須是呼叫者行程樹外的另一個 session，否則內部子行程防護會正確排除 holder。
    let holder = IndependentHolder::spawn(&dev);
    match sw_pal::native().serial.open_exclusive(&dev, &cfg()) {
        Err(OpenError::Held(rep)) => {
            eprintln!(
                "外部 session holder：pid={} status={:?} holder_unknown={}",
                holder.pid, rep.status, rep.holder_unknown
            );
            assert!(
                rep.pids.contains(&(holder.pid as u32)),
                "pids {:?} 應含外部 session holder pid {}",
                rep.pids,
                holder.pid
            );
            assert!(!rep.holder_unknown, "已識別的外部 holder 不得降為未知");
        }
        other => panic!(
            "expected Held with independent holder pid, got {}",
            show_open(&other)
        ),
    }
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__two_symlinks_same_tty_is_held() {
    let (dev, _peer) = posix_pty_pair();
    let tmp = tempfile::tempdir().expect("tempdir");
    let by_id = tmp.path().join("by-id-a");
    let by_path = tmp.path().join("by-path-b");
    symlink(&dev.0, &by_id).expect("symlink a");
    symlink(&dev.0, &by_path).expect("symlink b");
    let pal = sw_pal::native();
    let _h = pal
        .serial
        .open_exclusive(
            &DevicePath(by_id.to_string_lossy().into_owned()),
            &cfg(),
        )
        .expect("經 by-id symlink 第一次開啟");
    match pal.serial.open_exclusive(
        &DevicePath(by_path.to_string_lossy().into_owned()),
        &cfg(),
    ) {
        Err(OpenError::Held(_)) => {}
        other => panic!(
            "同一 inode 經第二個 symlink 應為 Held，實際 {}",
            show_open(&other)
        ),
    }
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__holder_exit_releases_flock_excl_persists_while_master_open() {
    const NAME: &str = "cap_posix_pty__holder_exit_releases_flock_excl_persists_while_master_open";
    if let Some(role) = child_role(NAME) {
        assert_eq!(role, "sigkill_hold");
        // 父程序預持保活 fd，若子程序用真正的鎖後重掃，會把父程序識別為外部 holder 而 Held（R0-F2）；
        // 故子程序以 test-fakes 的 NoHolders 取代重掃，只驗證 flock／TIOCEXCL 的釋放語意。
        let be = PosixSerialBackend::with_uart_exclusive(Arc::new(NoHolders));
        let _h = be
            .open_exclusive(&child_dev(), &cfg())
            .expect("child open_exclusive");
        child_ready_and_wait();
        return;
    }
    if sw_pal::conformance::has_cap_sys_admin() {
        eprintln!("EVIDENCE_MISSING: TIOCEXCL bypassed under CAP_SYS_ADMIN");
        return;
    }
    let (dev, _peer) = posix_pty_pair();
    // 父程序以一般 open（不 flock）保活 fd：TIOCEXCL 是 tty 物件旗標，只在 tty 物件釋放時重置
    let keep = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NOCTTY)
        .open(&dev.0)
        .expect("keep fd");
    let mut child = spawn_child(NAME, "sigkill_hold", &dev);
    child.kill().expect("kill child");
    child.wait().expect("reap child");
    thread::sleep(Duration::from_millis(100));
    // flock 隨程序結束而釋放：父程序以另一個 open file description 取得應成功
    let rc = unsafe { libc::flock(keep.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    assert_eq!(
        rc,
        0,
        "SIGKILL 後 100 ms 內 flock 應已釋放（errno={:?}）",
        std::io::Error::last_os_error().raw_os_error()
    );
    unsafe { libc::flock(keep.as_raw_fd(), libc::LOCK_UN) };
    // TIOCEXCL 仍存活（tty 物件未釋放）：一般 open 得 EBUSY
    let err = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&dev.0)
        .expect_err("TIOCEXCL 應仍存活");
    assert_eq!(err.raw_os_error(), Some(libc::EBUSY));
    // open_exclusive 同樣得 Held，且無 pid 可歸因（holder_unknown:true）
    match sw_pal::native().serial.open_exclusive(&dev, &cfg()) {
        Err(OpenError::Held(rep)) => {
            assert!(rep.pids.is_empty(), "pids {:?}", rep.pids);
            assert!(rep.holder_unknown);
        }
        other => panic!(
            "expected Held{{pids:[], holder_unknown:true}}, got {}",
            show_open(&other)
        ),
    }
    // 父程序 TIOCNXCL 後，open_exclusive 成功
    let rc = unsafe { libc::ioctl(keep.as_raw_fd(), libc::TIOCNXCL) };
    assert_eq!(rc, 0, "TIOCNXCL 應成功");
    let _h = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("TIOCNXCL 後可取得");
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__release_then_external_plain_open_succeeds() {
    const NAME: &str = "cap_posix_pty__release_then_external_plain_open_succeeds";
    if let Some(role) = child_role(NAME) {
        assert_eq!(role, "plain_write");
        // 模擬外部燒錄工具：一般 open（不 flock、不 TIOCEXCL）寫入 b"X"
        let mut f = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_NOCTTY)
            .open(child_dev().0)
            .expect("外部一般 open 必須成功（release 後旗標已清）");
        f.write_all(b"X").expect("external write");
        return;
    }
    let (dev, mut peer) = posix_pty_pair();
    let h = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("第一次 attach");
    let (r, w, _waker, close) = h.split();
    let rc = close.close(r, w);
    assert!(
        rc.closed && rc.excl_cleared && rc.error.is_none(),
        "release 應完整解除獨占：{rc:?}"
    );
    let status = test_command(NAME, "plain_write", &dev)
        .status()
        .expect("spawn external writer");
    assert!(status.success(), "外部一般 open 子程序應 exit 0，實際 {status:?}");
    assert_eq!(
        peer.read_available(Duration::from_millis(500)),
        b"X".to_vec(),
        "peer 應讀到外部寫入的 X"
    );
    let _h2 = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("release→外部→attach 循環後仍可取得");
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__fd_not_inherited_by_child() {
    let (dev, _peer) = posix_pty_pair();
    let h = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("open");
    let out = Command::new("sh")
        .args(["-c", "ls -l /proc/self/fd"])
        .output()
        .expect("spawn sh");
    let text = String::from_utf8_lossy(&out.stdout).into_owned();
    let needle = format!("-> {}", dev.0);
    assert!(
        !text.lines().any(|l| l.ends_with(needle.as_str())),
        "子程序不得繼承 slave fd：\n{text}"
    );
    assert!(
        !text.contains("eventfd"),
        "子程序不得繼承 eventfd（EFD_CLOEXEC）：\n{text}"
    );
    drop(h);
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__concurrent_wake_reaches_reader_and_writer() {
    for iter in 0..200 {
        let (dev, mut peer) = posix_pty_pair();
        peer.stop_reading(); // 讓寫端阻塞於 POLLOUT
        let h = sw_pal::native()
            .serial
            .open_exclusive(&dev, &cfg())
            .unwrap_or_else(|e| panic!("iter {iter}: open: {e:?}"));
        let (mut r, mut w, waker, _c) = h.split();
        let tok = CancelToken::new(waker.clone());
        let t_w = tok.clone();
        let payload = vec![b'B'; 1 << 20];
        let (tx_r, rx_r) = mpsc::channel::<Result<ReadOutcome, ReadError>>();
        let (tx_w, rx_w) = mpsc::channel::<TxResult>();
        thread::spawn(move || {
            let mut b = [0u8; 64];
            let _ = tx_r.send(r.read(&mut b, None));
        });
        thread::spawn(move || {
            let _ = tx_w.send(w.write(&payload, &t_w));
        });
        thread::sleep(Duration::from_millis(20));
        tok.cancel();
        waker.wake();
        let got_r = rx_r
            .recv_timeout(Duration::from_millis(100))
            .unwrap_or_else(|e| panic!("iter {iter}: reader 未在 100 ms 內返回（lost wakeup？）: {e:?}"));
        assert_eq!(got_r.expect("reader io"), ReadOutcome::Woken, "iter {iter}");
        let _ = rx_w
            .recv_timeout(Duration::from_millis(100))
            .unwrap_or_else(|e| panic!("iter {iter}: writer 未在 100 ms 內返回（lost wakeup？）: {e:?}"));
        peer.resume_reading();
    }
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_sys_admin__tiocexcl_bypass_documented() {
    // §6.2 例外：只在 root／CAP_SYS_ADMIN 下記錄 TIOCEXCL 繞過行為；非 root 直接 skip（報告列為證據不足）。
    if !sw_pal::conformance::has_cap_sys_admin() {
        eprintln!("SKIP: 需 root／CAP_SYS_ADMIN 才能記錄 TIOCEXCL 繞過行為（證據不足）");
        return;
    }
    let (dev, _peer) = posix_pty_pair();
    let _h = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("open");
    // 報告標「§6.2 例外，需權限管控」：root 下外部普通 open 在 TIOCEXCL 後成功
    let f = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&dev.0);
    assert!(f.is_ok(), "root 下外部普通 open 應成功，實際 {:?}", f.err());
}
