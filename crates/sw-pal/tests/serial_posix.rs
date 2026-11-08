//! Slice 2.1 Task 2（規格 §6.2、arch §4.2）：POSIX 序列埠獨占開啟與 PTY 行為的失敗測試（TDD 紅燈）。
//!
//! 需要 PTY 資源的測試標記 `#[ignore = "cap:posix_pty"]`，以 `-- --ignored cap_posix_pty__` 執行；
//! fakes conformance 不需資源，預設執行。實作者必須讓這裡的測試依規格通過，不得改寫斷言。
// sw-pal 是唯一允許平台 cfg 的 crate
#![cfg(all(unix, feature = "test-fakes"))]
// 測試名沿用計畫的 `cap_posix_pty__*` 命名（雙底線即為計畫指定的分隔），不套用 snake_case 警告。
#![allow(non_snake_case)]

use std::{
    io::{BufRead, BufReader, Write},
    os::fd::{AsRawFd, FromRawFd, OwnedFd},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{mpsc::{self, Receiver}, Mutex, MutexGuard},
    thread,
    thread::JoinHandle,
    time::{Duration, Instant},
};
use sw_pal::{conformance::posix_pty_pair, *};

// 同一 test-binary 的平行 PTY case 可能在 holder 掃描期間改變 `/proc` 行程樹；
// 逐 case 執行可保留每支回歸的原斷言，同時避免測試彼此成為外部 holder。
static PTY_CASE_LOCK: Mutex<()> = Mutex::new(());

fn pty_case_guard() -> MutexGuard<'static, ()> {
    PTY_CASE_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

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

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__second_open_in_process_is_held() {
    let _pty_case = pty_case_guard();
    let (dev, _peer) = posix_pty_pair();
    let pal = sw_pal::native();
    let _h = pal.serial.open_exclusive(&dev, &cfg()).expect("first open");
    match pal.serial.open_exclusive(&dev, &cfg()) {
        Err(OpenError::Held(rep)) => {
            assert!(
                rep.holder_unknown,
                "self pid excluded → pids empty → holder_unknown"
            )
        }
        other => panic!("expected Held, got {}", show_open(&other)),
    }
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__external_plain_open_after_tiocexcl_is_ebusy() {
    let _pty_case = pty_case_guard();
    if sw_pal::conformance::has_cap_sys_admin() {
        eprintln!("EVIDENCE_MISSING: TIOCEXCL bypassed under CAP_SYS_ADMIN");
        return;
    } // 報告計為證據不足
    let (dev, _peer) = posix_pty_pair();
    let _h = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("open_exclusive");
    let err = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&dev.0)
        .expect_err("TIOCEXCL 之後的一般 open 應失敗");
    assert_eq!(err.raw_os_error(), Some(libc::EBUSY));
}

/// 規格 §6.2「拒絕已識別的外部衝突」：外部程式先以普通 open（不 flock、不設 TIOCEXCL）持有，
/// 本端 open→flock→TIOCEXCL 都會成功，必須靠鎖後 holder 重掃拒絕；被拒時不得改 termios、不得送任何 byte。
#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__external_plain_open_before_daemon_is_held() {
    let _pty_case = pty_case_guard();
    if let Ok(role) = std::env::var("S21_ROLE") {
        match role.as_str() {
            "holder" => run_external_holder_role(),
            "opener" => run_external_opener_role(),
            other => panic!("未知 S21_ROLE：{other}"),
        }
        return;
    }

    let (dev, mut peer) = posix_pty_pair();
    let controller_pid = std::process::id();
    let mut holder = ReenteredTestProcess::spawn("holder", &dev.0, None, controller_pid);
    let holder_ready = holder.wait_for_marker("S21_HOLDER_READY");
    let (holder_pid, holder_parent_pid) = parse_pid_pair(&holder_ready, "S21_HOLDER_READY");
    assert_eq!(
        holder_pid,
        holder.id(),
        "holder READY PID 應與子程序 PID 相同"
    );
    assert_eq!(
        holder_parent_pid, controller_pid,
        "holder 應由控制程序直接啟動"
    );

    let before = sw_pal::conformance::tcgetattr_snapshot(&dev);
    let mut opener =
        ReenteredTestProcess::spawn("opener", &dev.0, Some(holder_pid), controller_pid);
    let first_open = opener.wait_for_marker("S21_OPENER_HELD");
    let (opener_pid, opener_parent_pid) = parse_pid_pair(&first_open, "S21_OPENER_HELD");
    assert_eq!(
        opener_pid,
        opener.id(),
        "回報 opener 必須是實際開埠的子程序"
    );
    assert_ne!(holder_pid, opener_pid, "holder 與 opener 必須是不同 PID");
    assert_eq!(
        holder_parent_pid, opener_parent_pid,
        "holder 與實際呼叫 open_exclusive() 的 opener 必須是同父程序兄弟行程"
    );
    assert_eq!(
        opener_parent_pid, controller_pid,
        "opener 應由控制程序直接啟動"
    );
    assert_eq!(
        sw_pal::conformance::tcgetattr_snapshot(&dev),
        before,
        "被拒時不得改寫外部工具的 termios"
    );
    assert_eq!(
        peer.read_available(Duration::from_millis(500)),
        Vec::<u8>::new(),
        "被拒時 UART 零 TX"
    );

    // peer（PTY master）持續開啟；同一 opener 重試可抓出未清除的 TIOCEXCL（R0-F3）。
    holder.send_line("STOP");
    holder.wait_for_marker("S21_HOLDER_STOPPED");
    holder.wait_success("holder");
    opener.send_line("RETRY");
    opener.wait_for_marker("S21_OPENER_REOPENED");
    opener.wait_success("opener");
}

const REENTERED_HOLDER_TEST: &str = "cap_posix_pty__external_plain_open_before_daemon_is_held";
const ROLE_IPC_TIMEOUT: Duration = Duration::from_secs(10);

struct ReenteredTestProcess {
    child: Child,
    stdin: Option<ChildStdin>,
    output: Receiver<String>,
    output_reader: Option<JoinHandle<()>>,
}

impl ReenteredTestProcess {
    fn spawn(role: &str, dev: &str, holder_pid: Option<u32>, controller_pid: u32) -> Self {
        let executable = std::env::current_exe().expect("current_exe");
        let mut command = Command::new(executable);
        command
            .args([
                "--exact",
                REENTERED_HOLDER_TEST,
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .env("S21_ROLE", role)
            .env("S21_DEV", dev)
            .env("S21_CONTROLLER_PID", controller_pid.to_string())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        if let Some(pid) = holder_pid {
            command.env("S21_HOLDER_PID", pid.to_string());
        }

        let mut child = command.spawn().expect("啟動重入測試程序");
        let stdin = child.stdin.take().expect("子程序 stdin");
        let stdout = child.stdout.take().expect("子程序 stdout");
        let (sender, output) = mpsc::channel();
        let mut process = Self {
            child,
            stdin: Some(stdin),
            output,
            output_reader: None,
        };
        process.output_reader = Some(thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                match line {
                    Ok(line) => {
                        if sender.send(line).is_err() {
                            break;
                        }
                    }
                    _ => break,
                }
            }
        }));
        process
    }

    fn id(&self) -> u32 {
        self.child.id()
    }

    fn wait_for_marker(&self, marker: &str) -> String {
        let deadline = Instant::now() + ROLE_IPC_TIMEOUT;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            assert!(!remaining.is_zero(), "等待子程序訊息 {marker} 逾時");
            match self.output.recv_timeout(remaining) {
                Ok(line) if line.contains(marker) => return line,
                Ok(_) => {}
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    panic!("等待子程序訊息 {marker} 逾時")
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    panic!("子程序提前結束，未收到訊息 {marker}")
                }
            }
        }
    }

    fn send_line(&mut self, message: &str) {
        let stdin = self.stdin.as_mut().expect("子程序 stdin 尚未關閉");
        writeln!(stdin, "{message}").expect("寫入子程序控制訊息");
        stdin.flush().expect("flush 子程序控制訊息");
    }

    fn wait_success(&mut self, role: &str) {
        let deadline = Instant::now() + ROLE_IPC_TIMEOUT;
        loop {
            match self.child.try_wait().expect("等待子程序狀態") {
                Some(status) => {
                    assert!(status.success(), "{role} 子程序失敗：{status}");
                    return;
                }
                None if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
                None => panic!("等待 {role} 子程序結束逾時"),
            }
        }
    }
}

impl Drop for ReenteredTestProcess {
    fn drop(&mut self) {
        self.stdin.take();
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
        }
        let _ = self.child.wait();
        if let Some(reader) = self.output_reader.take() {
            let _ = reader.join();
        }
    }
}

fn parse_pid_pair(message: &str, marker: &str) -> (u32, u32) {
    let payload = message
        .split_once(marker)
        .unwrap_or_else(|| panic!("訊息缺少 {marker}：{message}"))
        .1;
    let mut fields = payload.split_whitespace();
    let pid = fields
        .next()
        .and_then(|value| value.parse().ok())
        .unwrap_or_else(|| panic!("{marker} 缺少有效 PID：{message}"));
    let parent_pid = fields
        .next()
        .and_then(|value| value.parse().ok())
        .unwrap_or_else(|| panic!("{marker} 缺少有效 PPID：{message}"));
    (pid, parent_pid)
}

fn write_role_message(message: &str) {
    println!("{message}");
    std::io::stdout().flush().expect("flush 子程序 IPC 訊息");
}

fn run_external_holder_role() {
    use std::os::unix::fs::OpenOptionsExt;

    let dev = std::env::var("S21_DEV").expect("S21_DEV");
    let holder = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NOCTTY)
        .open(&dev)
        .expect("holder plain open");
    write_role_message(&format!(
        "S21_HOLDER_READY {} {}",
        std::process::id(),
        // SAFETY: getppid 不存取指標或呼叫者管理的資源。
        unsafe { libc::getppid() as u32 }
    ));

    let mut command = String::new();
    std::io::stdin()
        .lock()
        .read_line(&mut command)
        .expect("讀取 holder 控制訊息");
    assert_eq!(command.trim(), "STOP", "holder 預期 STOP 釋放握手");
    drop(holder);
    write_role_message("S21_HOLDER_STOPPED");
}

fn run_external_opener_role() {
    let holder_pid = std::env::var("S21_HOLDER_PID")
        .expect("S21_HOLDER_PID")
        .parse::<u32>()
        .expect("有效 S21_HOLDER_PID");
    let controller_pid = std::env::var("S21_CONTROLLER_PID")
        .expect("S21_CONTROLLER_PID")
        .parse::<u32>()
        .expect("有效 S21_CONTROLLER_PID");
    let opener_pid = std::process::id();
    // SAFETY: getppid 不存取指標或呼叫者管理的資源。
    let opener_parent_pid = unsafe { libc::getppid() as u32 };
    assert_ne!(holder_pid, opener_pid, "holder 與 opener 必須是不同 PID");
    assert_eq!(
        opener_parent_pid, controller_pid,
        "opener 必須是控制程序的直接子行程"
    );

    let dev = DevicePath(std::env::var("S21_DEV").expect("S21_DEV"));
    match sw_pal::native().serial.open_exclusive(&dev, &cfg()) {
        Err(OpenError::Held(rep)) => {
            assert_eq!(rep.pids, vec![holder_pid]);
            assert!(!rep.holder_unknown);
        }
        other => panic!(
            "expected Held for plain external holder, got {}",
            show_open(&other)
        ),
    }
    write_role_message(&format!("S21_OPENER_HELD {opener_pid} {opener_parent_pid}"));

    let mut command = String::new();
    std::io::stdin()
        .lock()
        .read_line(&mut command)
        .expect("讀取 opener 控制訊息");
    assert_eq!(command.trim(), "RETRY", "opener 預期 RETRY 釋放握手");
    let reopened = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("holder 結束後同一 opener 可重新取得");
    drop(reopened);
    write_role_message(&format!("S21_OPENER_REOPENED {opener_pid}"));
}

#[cfg(target_os = "linux")]
struct PausedSysenvSpawn<T> {
    call: sw_pal::conformance::SysenvSpawnPauseCall<T>,
    child_pid: u32,
    slave_identity: (u64, u64),
    _held_slave: std::fs::File,
}

#[cfg(target_os = "linux")]
impl<T: Send + 'static> PausedSysenvSpawn<T> {
    fn assert_child_still_holds_slave(&self) {
        use std::os::unix::fs::MetadataExt;
        use std::sync::mpsc::TryRecvError;

        let child_pid = self.child_pid;
        assert!(
            matches!(self.call.try_result(), Err(TryRecvError::Empty)),
            "pre-exec 放行前 native().env.run() 不得完成；暫停子行程 PID={child_pid}"
        );
        // SAFETY: pid 是剛由 SysEnv spawn 回報且仍應停在 pre-exec 的子行程。
        assert_eq!(
            unsafe { libc::kill(child_pid as libc::pid_t, 0) },
            0,
            "暫停子行程 PID={child_pid} 必須仍存活"
        );

        let fd_dir = format!("/proc/{child_pid}/fd");
        let inherited_fds: Vec<String> = std::fs::read_dir(&fd_dir)
            .unwrap_or_else(|error| panic!("讀取 {fd_dir} 失敗，子行程 PID={child_pid}: {error}"))
            .filter_map(Result::ok)
            .filter_map(|entry| {
                let metadata = std::fs::metadata(entry.path()).ok()?;
                (metadata.dev() == self.slave_identity.0 && metadata.ino() == self.slave_identity.1)
                    .then(|| entry.file_name().to_string_lossy().into_owned())
            })
            .collect();
        assert!(
            !inherited_fds.is_empty(),
            "暫停子行程 PID={child_pid} 的 /proc fd 中必須有與 PTY slave 相同 inode 的描述符"
        );
    }
}

/// 普通開啟 PTY slave 後，透過正式 SysEnv spawn 停在 fork→exec 窗口並確認子行程繼承相同 inode。
#[cfg(target_os = "linux")]
fn pause_sysenv_spawn_while_holding_slave<T, F>(dev: &DevicePath, run: F) -> PausedSysenvSpawn<T>
where
    T: Send + 'static,
    F: FnOnce() -> T + Send + 'static,
{
    use std::os::{
        fd::AsRawFd,
        unix::fs::{MetadataExt, OpenOptionsExt},
    };

    let held_slave = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NOCTTY | libc::O_CLOEXEC)
        .open(&dev.0)
        .expect("以普通 open 持有 PTY slave");
    let slave_metadata = held_slave.metadata().expect("取得 PTY slave inode");
    // SAFETY: held_slave 仍開啟且由本函式持有，是有效 fd。
    let fd_flags = unsafe { libc::fcntl(held_slave.as_raw_fd(), libc::F_GETFD) };
    assert!(
        fd_flags >= 0 && fd_flags & libc::FD_CLOEXEC != 0,
        "普通 open 的 slave fd 必須保留 CLOEXEC（flags={fd_flags}）"
    );

    let mut call = sw_pal::conformance::SysenvSpawnPauseCall::spawn(Duration::from_secs(5), run)
        .expect("建立 sysenv spawn 暫停呼叫");
    let child_pid = call
        .wait_for_pre_exec(Duration::from_secs(2))
        .expect("未收到子行程進入 pre-exec 的 PID 通知");
    let paused = PausedSysenvSpawn {
        call,
        child_pid,
        slave_identity: (slave_metadata.dev(), slave_metadata.ino()),
        _held_slave: held_slave,
    };
    paused.assert_child_still_holds_slave();
    paused
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__native_attach_spawn_sysenv() {
    let _pty_case = pty_case_guard();
    let (dev, mut peer) = posix_pty_pair();
    let mut paused = pause_sysenv_spawn_while_holding_slave(&dev, || {
        sw_pal::native()
            .env
            .run(&["/bin/true".to_string()], Some(Duration::from_secs(8)))
    });
    let child_pid = paused.child_pid;

    let serial_handle = match sw_pal::native().serial.open_exclusive(&dev, &cfg()) {
        Ok(handle) => handle,
        Err(OpenError::Held(report)) => panic!(
            "暫停的 sysenv 子行程 PID={child_pid} 不得被判為外部 holder；open_exclusive 回 Held(pids={:?}, holder_unknown={})",
            report.pids, report.holder_unknown
        ),
        Err(error) => panic!(
            "暫停的 sysenv 子行程 PID={child_pid} 期間 open_exclusive 應成功，實際錯誤：{error:?}"
        ),
    };
    paused.assert_child_still_holds_slave();
    assert_eq!(
        peer.read_available(Duration::from_millis(100)),
        Vec::<u8>::new(),
        "sysenv spawn 與 attach 開埠不得向 PTY 對端送出資料"
    );

    paused.call.release().expect("放行 pre-exec 子行程");
    let output = paused
        .call
        .recv_timeout(Duration::from_secs(5))
        .expect("release 後 native().env.run() 未完成")
        .expect("native().env.run() 失敗");
    assert_eq!(output.rc, 0, "子行程 PID={child_pid} 應以 rc=0 結束");
    paused.call.join().expect("回收 sysenv 呼叫執行緒");
    drop(serial_handle);
    drop(paused);

    assert_eq!(
        peer.read_available(Duration::from_millis(100)),
        Vec::<u8>::new(),
        "關閉 serial handle 後 PTY 對端仍不得有資料"
    );
}

#[test]
#[cfg(target_os = "linux")]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__native_attach_spawn_process_tunnel() {
    let _pty_case = pty_case_guard();
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};

    let (dev, mut peer) = posix_pty_pair();
    let inherited_slave = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NOCTTY)
        .open(&dev.0)
        .expect("以普通 open 持有非 CLOEXEC PTY slave");
    let slave_metadata = inherited_slave.metadata().expect("取得 PTY slave inode");
    let inherited_fd = inherited_slave.as_raw_fd();
    // SAFETY: inherited_slave 仍開啟且由本測試持有，是有效 fd。
    let fd_flags = unsafe { libc::fcntl(inherited_fd, libc::F_GETFD) };
    assert!(
        fd_flags >= 0,
        "讀取 inherited slave fd flags 失敗：{fd_flags}"
    );
    // SAFETY: inherited_slave 仍開啟；只清除此 fd 的 close-on-exec 旗標。
    assert_eq!(
        unsafe { libc::fcntl(inherited_fd, libc::F_SETFD, fd_flags & !libc::FD_CLOEXEC) },
        0,
        "清除 inherited slave fd 的 FD_CLOEXEC 失敗"
    );
    // SAFETY: inherited_slave 仍開啟且由本測試持有，是有效 fd。
    let inherited_flags = unsafe { libc::fcntl(inherited_fd, libc::F_GETFD) };
    assert!(
        inherited_flags >= 0 && inherited_flags & libc::FD_CLOEXEC == 0,
        "spawn 前 PTY slave fd 必須非 CLOEXEC（flags={inherited_flags}）"
    );

    let log_dir = tempfile::tempdir().expect("建立 process_tunnel log 暫存目錄");
    let log = log_dir.path().join("process-tunnel.log");
    let argv = vec!["/bin/sleep".to_string(), "30".to_string()];
    let tunnel = sw_pal::native().tunnel.clone();
    let spawn_tunnel = tunnel.clone();
    let (cleanup_sender, cleanup_result) = mpsc::channel();
    let fixture_result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        pause_sysenv_spawn_while_holding_slave(&dev, move || {
            let result = spawn_tunnel.spawn_detached(&argv, &log);
            let cleanup_copy = result
                .as_ref()
                .map(Clone::clone)
                .map_err(|error| format!("{error:?}"));
            let _ = cleanup_sender.send(cleanup_copy);
            result
        })
    }));

    let mut paused = match fixture_result {
        Ok(paused) => paused,
        Err(payload) => {
            let reason = payload
                .downcast_ref::<String>()
                .map(String::as_str)
                .or_else(|| payload.downcast_ref::<&'static str>().copied())
                .unwrap_or("暫停夾具發生非字串 panic");
            let spawn_result = cleanup_result.recv_timeout(Duration::from_secs(2));
            let cleanup_result = match spawn_result {
                Ok(Ok(process)) => tunnel
                    .terminate(&process, Duration::from_millis(100))
                    .err()
                    .map(|error| format!("terminate 失敗：{error:?}")),
                Ok(Err(error)) => Some(format!("spawn_detached 失敗：{error}")),
                Err(error) => Some(format!("收取 spawn 結果失敗：{error}")),
            };
            drop(inherited_slave);
            panic!(
                "未取得 process_tunnel pre-exec 通知；暫停接線缺漏紅燈（{reason}）；清理結果：{cleanup_result:?}"
            );
        }
    };

    let child_pid = paused.child_pid;
    let inherited_fd_path = format!("/proc/{child_pid}/fd/{inherited_fd}");
    let inherited_fd_check = std::fs::metadata(&inherited_fd_path).map(|metadata| {
        metadata.dev() == slave_metadata.dev() && metadata.ino() == slave_metadata.ino()
    });
    let attach_result = sw_pal::native().serial.open_exclusive(&dev, &cfg());
    let peer_bytes = peer.read_available(Duration::from_millis(100));

    let release_result = paused.call.release();
    let spawn_result = paused.call.recv_timeout(Duration::from_secs(8));
    let join_result = paused.call.join();
    drop(paused);
    let cleanup_spawn_result = cleanup_result.recv_timeout(Duration::from_secs(2));
    let terminate_result = match &cleanup_spawn_result {
        Ok(Ok(process)) => tunnel.terminate(process, Duration::from_millis(100)),
        Ok(Err(_)) | Err(_) => Ok(()),
    };
    drop(inherited_slave);

    assert!(
        matches!(&spawn_result, Ok(Ok(_))),
        "release 後 native().tunnel.spawn_detached() 應成功，實際結果：{spawn_result:?}"
    );
    assert!(join_result.is_ok(), "回收 process_tunnel 呼叫執行緒失敗");
    assert!(
        release_result.is_ok(),
        "放行 process_tunnel pre-exec 失敗：{release_result:?}"
    );
    assert!(
        matches!(&inherited_fd_check, Ok(true)),
        "pre-exec 子行程 PID={child_pid} 必須仍持有同編號、同 inode 的非 CLOEXEC slave fd，實際：{inherited_fd_check:?}"
    );
    assert!(
        matches!(&cleanup_spawn_result, Ok(Ok(_))),
        "清理通道應收到 process_tunnel spawn 結果：{cleanup_spawn_result:?}"
    );
    assert!(
        terminate_result.is_ok(),
        "回收 process_tunnel 建立的行程群組失敗：{terminate_result:?}"
    );
    assert!(
        attach_result.is_ok(),
        "暫停的 process_tunnel 子行程 PID={child_pid} 不得被判為外部 holder；open_exclusive 結果：{}",
        show_open(&attach_result)
    );
    assert!(
        peer_bytes.is_empty(),
        "process_tunnel spawn 與 attach 開埠不得向 PTY 對端送出資料：{peer_bytes:?}"
    );
}

/// R0-F3：TIOCEXCL 是 tty 物件旗標，不隨 fd 關閉清除；PTY master 持續開啟時，CloseToken 必須先 TIOCNXCL 才關 fd，
/// 否則 PROBE→close→正式 open 與 release→外部工具 open 都會 EBUSY。本測試在修正前必定 FAIL（第一次 reopen 即 EBUSY）。
#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__probe_close_reopen_with_master_open() {
    let _pty_case = pty_case_guard();
    if sw_pal::conformance::has_cap_sys_admin() {
        eprintln!("EVIDENCE_MISSING: TIOCEXCL bypassed under CAP_SYS_ADMIN");
        return;
    }
    let (dev, mut peer) = posix_pty_pair(); // peer 持有 master 直到測試結束
    for round in 0..3 {
        // 模擬 PROBE bridge → 正式 bridge → 再一次 recover
        let h = sw_pal::native()
            .serial
            .open_exclusive(&dev, &cfg())
            .unwrap_or_else(|e| panic!("round {round}: reopen failed {e:?}"));
        let (r, w, _waker, close) = h.split();
        let rc = close.close(r, w);
        assert!(
            rc.closed && rc.excl_cleared && rc.error.is_none(),
            "round {round}: {rc:?}"
        );
        // 外部一般 open（不經 serialwrap）也必須成功：證明旗標已解除，而不只是同 backend 的路徑碰巧可用
        let ext = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&dev.0)
            .unwrap_or_else(|e| {
                panic!(
                    "round {round}: plain open after close must not be EBUSY, got {:?}",
                    e.raw_os_error()
                )
            });
        drop(ext);
    }
    assert_eq!(
        peer.read_available(Duration::from_millis(100)),
        Vec::<u8>::new(),
        "open／close 循環不得寫出任何 byte"
    );
}

/// R0-F3：open_exclusive 在 TIOCEXCL 成功之後失敗（termios／eventfd），失敗返回前必須 TIOCNXCL。
#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__open_failure_after_tiocexcl_clears_excl() {
    let _pty_case = pty_case_guard();
    if sw_pal::conformance::has_cap_sys_admin() {
        eprintln!("EVIDENCE_MISSING: TIOCEXCL bypassed under CAP_SYS_ADMIN");
        return;
    }
    let (dev, _peer) = posix_pty_pair();
    // test-fakes only：TIOCEXCL 成功後立即回注入的錯誤（模擬 termios 失敗）
    let be = sw_pal::serial::posix::PosixSerialBackend::with_post_excl_fault(PalError::io(
        5,
        "injected tcsetattr failure",
    ));
    match be.open_exclusive(&dev, &cfg()) {
        Err(OpenError::Other(e)) => assert_eq!(e.kind, PalErrorKind::Io),
        other => panic!("expected Other(Io), got {}", show_open(&other)),
    }
    match std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&dev.0)
    {
        Ok(f) => drop(f),
        Err(e) => panic!("失敗路徑殘留 TIOCEXCL：errno={:?}", e.raw_os_error()),
    }
    let _h = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("失敗後可重新取得");
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__blocked_write_cancel_returns_partial_completed() {
    let _pty_case = pty_case_guard();
    let (dev, mut peer) = posix_pty_pair();
    peer.stop_reading(); // 讓 PTY buffer 塞滿
    let h = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("open");
    let (_r, mut w, waker, _c) = h.split();
    let tok = CancelToken::new(waker);
    let t2 = tok.clone();
    let payload = vec![b'A'; 1 << 20];
    let j = thread::spawn(move || w.write(&payload, &t2));
    thread::sleep(Duration::from_millis(200));
    let t0 = Instant::now();
    tok.cancel();
    let res = j.join().expect("writer thread");
    assert!(
        t0.elapsed() < Duration::from_millis(100),
        "cancel must wake poll(POLLOUT)"
    );
    match res.outcome {
        TxOutcome::Completed(n) => assert!(n > 0 && n < (1 << 20)),
        o => panic!("{o:?}"),
    }
    assert_eq!(
        res.error.expect("cancel error").kind,
        PalErrorKind::Cancelled
    );
    peer.resume_reading();
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__reader_woken_within_50ms() {
    let _pty_case = pty_case_guard();
    let (dev, _peer) = posix_pty_pair();
    let h = sw_pal::native()
        .serial
        .open_exclusive(&dev, &cfg())
        .expect("open");
    let (mut r, _w, waker, _c) = h.split();
    let j = thread::spawn(move || {
        let mut b = [0u8; 64];
        r.read(&mut b, None)
    });
    thread::sleep(Duration::from_millis(50));
    let t0 = Instant::now();
    waker.wake();
    assert_eq!(
        j.join().expect("reader thread").expect("read"),
        ReadOutcome::Woken
    );
    assert!(t0.elapsed() < Duration::from_millis(50));
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__conformance_suite() {
    let _pty_case = pty_case_guard();
    sw_pal::conformance::serial_suite(
        sw_pal::native().serial.clone(),
        &|| {
            let (d, p) = posix_pty_pair();
            (d, Box::new(p) as Box<dyn PeerEnd>)
        },
        sw_pal::conformance::SerialPairCaps {
            eof_on_peer_close: true,
        },
    );
}

/// 鎖後重掃 status 非 ok、pids 空（`PostLockVerdict::AcceptUnverified`）的 holder 診斷替身。
struct UnknownStatusHolders;

impl UartExclusive for UnknownStatusHolders {
    fn probe_holders(&self, _real_path: &str) -> HolderReport {
        HolderReport {
            pids: Vec::new(),
            holder_unknown: true,
            status: HolderScanStatus::Unknown,
        }
    }
}

/// P5-T18 parity-amendments-2026-10-26 #11：開檔回 AcceptUnverified 時登記為 holder_unknown 裝置，
/// close 後移除；鎖後重掃 ok（Accept）不登記（Python `serial_port._set_holder_unknown`）。
#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__accept_unverified_registers_unknown_holder_device() {
    let _pty_case = pty_case_guard();
    use std::sync::Arc;
    let (dev, _peer) = posix_pty_pair();
    let key = std::fs::canonicalize(&dev.0)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| dev.0.clone());
    let be = sw_pal::serial::posix::PosixSerialBackend::with_uart_exclusive(Arc::new(
        UnknownStatusHolders,
    ));
    let h = be.open_exclusive(&dev, &cfg()).expect("AcceptUnverified 仍開檔成功");
    assert!(
        sw_pal::serial::unknown_holder_devices().contains(&key),
        "AcceptUnverified 應登記 {key}"
    );
    let (r, w, _waker, token) = h.split();
    let _ = token.close(r, w);
    assert!(
        !sw_pal::serial::unknown_holder_devices().contains(&key),
        "close 後應移除 {key}"
    );

    let be = sw_pal::serial::posix::PosixSerialBackend::with_uart_exclusive(Arc::new(
        sw_pal::conformance::NoHolders,
    ));
    let h = be.open_exclusive(&dev, &cfg()).expect("Accept 開檔");
    assert!(
        !sw_pal::serial::unknown_holder_devices().contains(&key),
        "鎖後重掃 ok 不登記"
    );
    drop(h);
}

/// 鎖後掃描只因他 uid 的 fd 目錄不可讀而不完整。
struct PartialOtherUidHolders;

impl sw_pal::traits::UartExclusive for PartialOtherUidHolders {
    fn probe_holders(&self, _real_path: &str) -> HolderReport {
        HolderReport {
            pids: Vec::new(),
            holder_unknown: false,
            status: HolderScanStatus::PartialOtherUid,
        }
    }
}

/// S1X-a-6：真實 POSIX backend 對 PartialOtherUid 正常開埠且不登記 unknown。
#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__wp59_partial_scan_not_registered_unknown() {
    let _pty_case = pty_case_guard();
    let (dev, _peer) = posix_pty_pair();
    let be = sw_pal::serial::posix::PosixSerialBackend::with_uart_exclusive(std::sync::Arc::new(
        PartialOtherUidHolders,
    ));

    let handle = be
        .open_exclusive(&dev, &cfg())
        .expect("PartialOtherUid 應接受開埠");
    assert!(
        !sw_pal::serial::unknown_holder_devices().contains(&dev.0),
        "開埠期間不應登記 {} 為 holder_unknown",
        dev.0
    );

    let (reader, writer, _waker, close) = handle.split();
    let receipt = close.close(reader, writer);
    assert!(receipt.closed, "PTY handle 應成功關閉：{receipt:?}");
    assert!(
        !sw_pal::serial::unknown_holder_devices().contains(&dev.0),
        "關閉後不得殘留 {} 的 holder_unknown 登記",
        dev.0
    );
}

/// 模擬 daemon 在鎖後 holder 重掃期間 spawn 子行程：子行程短暫繼承 daemon 已開啟的 tty fd，
/// 不應被當成另一個外部 holder。子行程停在 fork 後、exec 前的窗口，確保重掃能穩定觀察到它。
struct ForkWindowHolders;

impl UartExclusive for ForkWindowHolders {
    fn probe_holders(&self, real_path: &str) -> HolderReport {
        let mut gate = [0; 2];
        // SAFETY: gate 指向兩個可寫 fd 槽位；pipe 成功後由本函式關閉兩端。
        assert_eq!(unsafe { libc::pipe(gate.as_mut_ptr()) }, 0, "pipe");
        // SAFETY: fork 後子行程只呼叫 async-signal-safe 的 close/read/_exit。
        let child = unsafe { libc::fork() };
        assert!(child >= 0, "fork: {}", std::io::Error::last_os_error());
        if child == 0 {
            // SAFETY: 子行程只讀取 gate，並以 _exit 結束，不回到 Rust runtime。
            unsafe {
                libc::close(gate[1]);
                let mut byte = 0u8;
                let _ = libc::read(gate[0], (&mut byte as *mut u8).cast(), 1);
                libc::_exit(0);
            }
        }
        // SAFETY: 父行程不讀 gate，掃描後寫一 byte 讓子行程退出。
        unsafe {
            libc::close(gate[0]);
        }
        let report = sw_pal::serial::posix::PosixUartExclusive.probe_holders(real_path);
        let byte = 1u8;
        // SAFETY: gate[1] 仍由本函式持有，子行程正阻塞於對應 read。
        assert_eq!(
            unsafe { libc::write(gate[1], (&byte as *const u8).cast(), 1) },
            1
        );
        // SAFETY: 父行程關閉 gate 寫端並回收剛建立的子行程。
        unsafe {
            libc::close(gate[1]);
            let mut status = 0;
            assert_eq!(libc::waitpid(child, &mut status, 0), child);
            assert!(libc::WIFEXITED(status) && libc::WEXITSTATUS(status) == 0);
        }
        report
    }
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__own_child_inherited_tty_is_not_foreign_holder() {
    let _pty_case = pty_case_guard();
    use std::os::unix::fs::OpenOptionsExt;

    let (dev, _peer) = posix_pty_pair();
    // 在 TIOCEXCL 前先開啟 tty，並以 dup 建立非 CLOEXEC fd；模擬 daemon-owned
    // process_tunnel/sysenv 子行程在 fork→exec 間暫時繼承的 descriptor。
    let parent_fd = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NOCTTY)
        .open(&dev.0)
        .expect("預先開啟 tty");
    // SAFETY: parent_fd 是有效 fd；F_DUPFD 建立由下方 OwnedFd 管理且不帶 CLOEXEC 的副本。
    let inherited_fd = unsafe { libc::fcntl(parent_fd.as_raw_fd(), libc::F_DUPFD, 3) };
    assert!(
        inherited_fd >= 0,
        "dup: {}",
        std::io::Error::last_os_error()
    );
    // SAFETY: inherited_fd 為 fcntl 成功回傳的新 fd，交由 OwnedFd 單獨關閉。
    let _inherited_fd = unsafe { OwnedFd::from_raw_fd(inherited_fd) };

    let be = sw_pal::serial::posix::PosixSerialBackend::with_uart_exclusive(std::sync::Arc::new(
        ForkWindowHolders,
    ));
    let opened = be.open_exclusive(&dev, &cfg());
    assert!(
        opened.is_ok(),
        "daemon 自身尚未 exec 的子行程不應造成外部 holder 拒絕：{}",
        show_open(&opened)
    );
}

#[test]
fn fakes_conformance_suite() {
    let be = sw_pal::fakes::serial::FakeSerialBackend::new();
    sw_pal::conformance::serial_suite(
        be.clone(),
        &sw_pal::conformance::fake_pair_factory(be.clone()),
        sw_pal::conformance::SerialPairCaps {
            eof_on_peer_close: true,
        },
    );
}

/// `spawn_plain_holder` 的子程序入口（僅 `S21_CHILD=plain_holder` 時作用）：普通 open（不 flock、不 TIOCEXCL），
/// 回報 READY 後阻塞於 stdin，直到父程序關閉 stdin 或 kill。預設（未 `--include-ignored`）不執行。
#[test]
#[ignore = "helper:plain_holder_child"]
fn plain_holder_child() {
    use std::io::{Read, Write};
    use std::os::unix::fs::OpenOptionsExt;
    if std::env::var("S21_CHILD").as_deref() != Ok("plain_holder") {
        return;
    }
    let dev = std::env::var("S21_DEV").expect("S21_DEV");
    let _f = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NOCTTY)
        .open(&dev)
        .expect("plain open");
    println!("S21_READY");
    std::io::stdout().flush().ok();
    let mut b = [0u8; 1];
    let _ = std::io::stdin().read(&mut b);
}

// ---- parity-amendments-2026-10-26 #62／#63／#65：開啟序 errno 映射逐字照 Python `serial_port.py:373-387,429-435` ----

use sw_pal::serial::posix::{open_sequence, OpenOp, TtyOpenOps};

#[derive(Default)]
struct FakeOps {
    ops: Vec<OpenOp>,
    open_err: Option<i32>,
    flock_err: Option<i32>,
    probe: Option<HolderReport>,
}

impl TtyOpenOps for FakeOps {
    fn open(&mut self) -> Result<(), i32> {
        self.ops.push(OpenOp::Open);
        self.open_err.map_or(Ok(()), Err)
    }
    fn flock(&mut self) -> Result<(), i32> {
        self.ops.push(OpenOp::Flock);
        self.flock_err.map_or(Ok(()), Err)
    }
    fn excl(&mut self) -> Result<(), i32> {
        self.ops.push(OpenOp::Excl);
        Ok(())
    }
    fn probe_holders(&mut self) -> HolderReport {
        self.ops.push(OpenOp::ProbeHolders);
        self.probe.clone().unwrap_or(HolderReport {
            pids: vec![],
            holder_unknown: false,
            status: HolderScanStatus::Ok,
        })
    }
    fn termios(&mut self) -> Result<(), i32> {
        self.ops.push(OpenOp::Termios(Ok(())));
        Ok(())
    }
    fn nxcl(&mut self) -> Result<(), i32> {
        self.ops.push(OpenOp::Nxcl);
        Ok(())
    }
    fn close_fd(&mut self) -> Result<(), i32> {
        self.ops.push(OpenOp::CloseFd);
        Ok(())
    }
    fn warn(&mut self, _line: &str) {}
}

fn open_with(open_err: Option<i32>, flock_err: Option<i32>) -> Result<(), OpenError> {
    let mut ops = FakeOps {
        open_err,
        flock_err,
        ..Default::default()
    };
    open_sequence(&mut ops, "/dev/ttyUSB0")
}

/// #62：只有 `ENOENT`→`NotFound`、`EBUSY`→`Held`；`EACCES`／`EPERM`／`ENODEV`／`ENXIO` 原樣 → `Other`，
/// 且 `Other` 的 Python 例外類別名與 Python `raise` 的 OSError 子類別相同（session `ATTACH_FAILED:<名>`）。
#[test]
fn open_errno_maps_like_python() {
    assert!(matches!(
        open_with(Some(libc::ENOENT), None),
        Err(OpenError::NotFound(_))
    ));
    match open_with(Some(libc::EBUSY), None) {
        Err(OpenError::Held(rep)) => assert!(rep.holder_unknown, "pids 空時 holder_unknown"),
        other => panic!("EBUSY 應為 Held，實際 {other:?}"),
    }
    for (code, py_name) in [
        (libc::EACCES, "PermissionError"),
        (libc::EPERM, "PermissionError"),
        (libc::ENODEV, "OSError"),
        (libc::ENXIO, "OSError"),
    ] {
        match open_with(Some(code), None) {
            Err(OpenError::Other(e)) => {
                assert_eq!(e.os_code, Some(code), "errno {code}");
                assert_eq!(e.py_exc_name(), py_name, "errno {code}");
            }
            other => panic!("errno {code} 應為 Other，實際 {other:?}"),
        }
    }
}

/// #63：flock 失敗 errno ∈ {EACCES, EAGAIN, EWOULDBLOCK} → `Held`；其他 → `Other`；兩者皆先 close(fd)、不 TIOCNXCL。
#[test]
fn flock_eacces_is_held() {
    for code in [libc::EACCES, libc::EAGAIN, libc::EWOULDBLOCK] {
        let mut ops = FakeOps {
            flock_err: Some(code),
            ..Default::default()
        };
        match open_sequence(&mut ops, "/dev/ttyUSB0") {
            Err(OpenError::Held(_)) => {}
            other => panic!("flock errno {code} 應為 Held，實際 {other:?}"),
        }
        assert!(ops.ops.contains(&OpenOp::CloseFd));
        assert!(!ops.ops.contains(&OpenOp::Nxcl));
    }
    assert!(matches!(
        open_with(None, Some(libc::EBADF)),
        Err(OpenError::Other(_))
    ));
}

/// #65：衝突路徑（EBUSY／flock 失敗）在掃描無 pids 時 `holder_unknown:true`（`_conflict_holder_report`），
/// 掃描到 pids 時 `holder_unknown:false`。
#[test]
fn conflict_holder_unknown_when_no_pids() {
    for (open_err, flock_err) in [(Some(libc::EBUSY), None), (None, Some(libc::EWOULDBLOCK))] {
        let mut ops = FakeOps {
            open_err,
            flock_err,
            ..Default::default()
        };
        match open_sequence(&mut ops, "/dev/ttyUSB0") {
            Err(OpenError::Held(rep)) => {
                assert!(rep.pids.is_empty());
                assert!(
                    rep.holder_unknown,
                    "status ok 但 pids 空 → 仍須 holder_unknown"
                );
            }
            other => panic!("應為 Held，實際 {other:?}"),
        }
        let mut ops = FakeOps {
            open_err,
            flock_err,
            probe: Some(HolderReport {
                pids: vec![4242],
                holder_unknown: false,
                status: HolderScanStatus::Permission,
            }),
            ..Default::default()
        };
        match open_sequence(&mut ops, "/dev/ttyUSB0") {
            Err(OpenError::Held(rep)) => {
                assert_eq!(rep.pids, vec![4242]);
                assert!(!rep.holder_unknown);
            }
            other => panic!("應為 Held，實際 {other:?}"),
        }
    }
}
